"""Application assembly: one daemon, three surfaces, one shutdown order.

Mount order matters: the API routers and the WebSocket endpoint are installed before the SPA
fallback, so a client typo gets a JSON ``404`` from the API rather than the HTML of the console.

Shutdown is deliberately ordered — refuse new work, stop the reaper and the job runner, drain
worker threads, close handles, then close the control database. Cancelling a request does not
cancel native disk I/O, so closing handles before the queue drains would pull the ground out from
under work still in flight.
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

from . import __version__
from .api import admin as admin_api
from .api import mcp as mcp_api
from .api import rest as rest_api
from .api import ws as ws_api
from .api.context import ServerContext
from .config import ServerConfig
from .control.store import ControlStore
from .engine.handles import HandlePool
from .engine.jobs import JobRunner
from .engine.runner import EngineRunner
from .engine.service import DatabaseService
from .errors import AppError, get_request_id, map_grafx_error, set_request_id

log = logging.getLogger("okto_grafx_server")

# Polling endpoints: they are the console's heartbeat, so they log at debug and keep the rotating
# file readable.
_QUIET_PATHS = frozenset(
    {"/api/v1/health", "/admin/api/session", "/metrics", "/admin/api/jobs"}
)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'self'"
    ),
}

WEB_DIRECTORY = Path(__file__).with_name("web")


def build_context(config: ServerConfig) -> ServerContext:
    """Compose the server's own components (no I/O beyond opening the control store)."""

    store = ControlStore(config.server_db_path)
    pool = HandlePool(
        max_open_handles=config.max_open_handles,
        readers_per_database=config.readers_per_database,
        idle_ttl_seconds=config.handle_idle_ttl_seconds,
        checkout_timeout_seconds=config.reader_lane_timeout_seconds,
    )
    runner = EngineRunner(
        max_workers=max(4, config.readers_per_database * 4),
        queue_capacity=max(16, config.max_ws_sessions * 8),
    )
    service = DatabaseService(
        store=store,
        pool=pool,
        allowed_roots=config.allowed_roots,
        max_request_rows=config.max_request_rows,
        max_graph_nodes=config.max_graph_nodes,
        max_graph_edges=config.max_graph_edges,
        query_timeout_seconds=config.query_timeout_seconds,
        max_query_value_characters=config.max_query_value_characters,
    )
    jobs = JobRunner(store=store, pool=pool)
    return ServerContext(
        config=config, store=store, pool=pool, runner=runner, service=service, jobs=jobs
    )


def create_app(context: ServerContext | None = None, *, config: ServerConfig | None = None) -> FastAPI:
    """Build the ASGI app. ``context`` is what tests inject to control the state directory."""

    if context is None:
        if config is None:
            raise ValueError("create_app needs a context or a config")
        context = build_context(config)

    app = FastAPI(
        title="Okto Grafx Server",
        version=__version__,
        summary="Daemon and console for Okto Grafx embedded graph stores.",
        description=(
            "Three surfaces on one daemon: a REST/JSON data API for applications, a WebSocket "
            "session for multi-statement transactions, and MCP over Streamable HTTP — all scoped "
            "by API key — plus an administrative console for the stores this server manages.\n\n"
            "There is no TLS here: bind loopback and put a reverse proxy in front otherwise. And a "
            "local process with filesystem access can open these stores directly: this server is "
            "logical access control, not an operating-system sandbox."
        ),
        docs_url="/api-docs",
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
        lifespan=_lifespan(context),
    )
    app.state.context = context
    registry = ws_api.WsSessionRegistry(
        context.config.max_ws_sessions, context.config.max_write_sessions
    )
    app.state.ws_sessions = registry
    context.ws_registry = registry

    if context.config.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(context.config.cors_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        set_request_id(request.headers.get("x-request-id") or uuid.uuid4().hex[:12])
        started = time.monotonic()

        def logged(response: JSONResponse) -> JSONResponse:
            """One structured line per turn: who, what, with what outcome, in how long.

            Never the query string — the WebSocket handshake carries `?token=` — and never a body.
            Health checks and the session probe are demoted to debug so the file keeps incidents
            rather than the console's heartbeat.
            """

            status = int(response.status_code)
            path = request.url.path
            quiet = path in _QUIET_PATHS or not path.startswith(("/api", "/admin", "/mcp", "/ws"))
            if status >= 500:
                level = logging.ERROR
            elif status >= 400:
                level = logging.WARNING
            elif request.method in ("GET", "HEAD", "OPTIONS") and quiet:
                level = logging.DEBUG
            else:
                level = logging.INFO
            log.log(
                level,
                "%s %s -> %s",
                request.method,
                path,
                status,
                extra={
                    "request_id": get_request_id(),
                    "method": request.method,
                    "path": path,
                    "status": status,
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                    "principal": getattr(request.state, "principal_label", None),
                    "code": getattr(request.state, "error_code", None),
                },
            )
            return response

        length = request.headers.get("content-length")
        if length is not None and length.isdigit() and int(length) > context.config.max_request_bytes:
            return logged(
                JSONResponse(
                    AppError(
                        f"A request body above {context.config.max_request_bytes} bytes is refused.",
                        status=413,
                        code="request_too_large",
                    ).envelope(),
                    status_code=413,
                )
            )
        try:
            response = await call_next(request)
        except AppError as exc:
            mapped = exc
            if mapped.status >= 500:
                log.error(
                    "%s %s refused: %s", request.method, request.url.path, mapped.code,
                    exc_info=True,
                )
            response = JSONResponse(
                mapped.envelope(), status_code=mapped.status, headers=mapped.headers or None
            )
            request.state.error_code = mapped.code
        except Exception as exc:  # noqa: BLE001 - the last line before a traceback escapes
            mapped = map_grafx_error(exc)
            if mapped.status >= 500:
                log.exception("unhandled error on %s %s", request.method, request.url.path)
            response = JSONResponse(
                mapped.envelope(), status_code=mapped.status, headers=mapped.headers or None
            )
            request.state.error_code = mapped.code
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        response.headers.setdefault("X-Request-Id", get_request_id())
        response.headers.setdefault("X-Server-Time-Ms", f"{(time.monotonic() - started) * 1000:.1f}")
        return logged(response)

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:  # type: ignore[no-untyped-def]
        request.state.error_code = exc.code
        return JSONResponse(exc.envelope(), status_code=exc.status, headers=exc.headers or None)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):  # type: ignore[no-untyped-def]
        if request.url.path.startswith(("/api", "/admin/api", "/mcp")):
            request.state.error_code = "not_found" if exc.status_code == 404 else "http_error"
            return JSONResponse(
                {
                    "error": {
                        "code": "not_found" if exc.status_code == 404 else "http_error",
                        "message": str(exc.detail),
                        "retryable": False,
                        "details": {},
                        "request_id": get_request_id(),
                    }
                },
                status_code=exc.status_code,
                headers=getattr(exc, "headers", None) or {},
            )
        return _spa_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):  # type: ignore[no-untyped-def]
        request.state.error_code = "invalid_request"
        problems = []
        for item in exc.errors()[:12]:
            location = [str(part) for part in item.get("loc", []) if part != "body"]
            problems.append(
                {
                    "field": ".".join(location),
                    "message": str(item.get("msg", ""))[:200],
                    "type": str(item.get("type", "")),
                }
            )
        return JSONResponse(
            {
                "error": {
                    "code": "invalid_request",
                    "message": "The request body did not match the documented schema.",
                    "retryable": False,
                    "details": {"problems": problems},
                    "request_id": get_request_id(),
                }
            },
            # 422 for a request that does not match the published schema; 400 stays reserved for
            # a statement the engine itself refuses, so a client can tell the two apart.
            status_code=422,
        )

    app.include_router(rest_api.router)
    app.include_router(admin_api.router)
    app.include_router(mcp_api.router)
    app.websocket("/api/v1/ws")(ws_api.websocket_session)
    _publish_contract(app)

    @app.get("/metrics", include_in_schema=False)
    def metrics(request: Request) -> PlainTextResponse:
        if not context.config.metrics_enabled:
            raise AppError(
                "Metrics are disabled. Set metrics_enabled = true in server.toml.",
                status=404,
                code="metrics_disabled",
            )
        # Prometheus scrapes without asking and OpenMetrics clients say so; both are one page.
        wants_openmetrics = "application/openmetrics-text" in request.headers.get("accept", "")
        return PlainTextResponse(
            _render_metrics(context, openmetrics=wants_openmetrics),
            media_type=(
                "application/openmetrics-text; version=1.0.0; charset=utf-8"
                if wants_openmetrics
                else "text/plain; version=0.0.4"
            ),
        )

    _mount_console(app)
    return app


def _lifespan(context: ServerContext):  # type: ignore[no-untyped-def]
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        context.pool.start()
        context.jobs.start()
        pinned = [record for record in context.store.list_databases() if record.enabled]
        for record in pinned:
            if record.open_policy == "pinned":
                try:
                    context.service.open_database(record)
                    log.info("opened pinned database %s", record.slug)
                except Exception as exc:  # noqa: BLE001 - a bad store must not stop the daemon
                    log.error("could not open pinned database %s: %s", record.slug, exc)
        context.store.prune_sessions()
        context.store.prune_audit(context.config.audit_retention_days)
        try:
            yield
        finally:
            await context.ashutdown()

    return lifespan


def _render_metrics(context: ServerContext, *, openmetrics: bool = False) -> str:
    """The exposition, in whichever dialect the scraper asked for.

    Every family carries its own ``# TYPE``: a labelled sample without one is not a legal
    exposition, and a scraper that silently drops the family is worse than one that refuses the
    page. ``# UNIT`` and the closing ``# EOF`` are OpenMetrics-only, so they appear only there.
    """

    stats = context.runner.stats
    lines = [
        "# HELP oktografx_up 1 when the daemon answers",
        "# TYPE oktografx_up gauge",
        "oktografx_up 1",
        "# HELP oktografx_databases_registered Registered stores",
        "# TYPE oktografx_databases_registered gauge",
        f"oktografx_databases_registered {len(context.store.list_databases())}",
        "# HELP oktografx_databases_open Stores with a live handle",
        "# TYPE oktografx_databases_open gauge",
        f"oktografx_databases_open {len(context.pool.open_slugs())}",
        "# HELP oktografx_worker_in_flight Engine threads currently busy",
        "# TYPE oktografx_worker_in_flight gauge",
        f"oktografx_worker_in_flight {stats.in_flight}",
        "# HELP oktografx_worker_submitted_total Operations submitted to the engine pool",
        "# TYPE oktografx_worker_submitted_total counter",
        f"oktografx_worker_submitted_total {stats.submitted}",
        "# HELP oktografx_worker_failed_total Operations that raised",
        "# TYPE oktografx_worker_failed_total counter",
        f"oktografx_worker_failed_total {stats.failed}",
        "# HELP oktografx_worker_cancelled_waiters_total Waiters cancelled while I/O continued",
        "# TYPE oktografx_worker_cancelled_waiters_total counter",
        f"oktografx_worker_cancelled_waiters_total {stats.cancelled_waiters}",
        "# HELP oktografx_store_open_transactions Write transactions open on this store's handle",
        "# TYPE oktografx_store_open_transactions gauge",
        "# HELP oktografx_store_idle_seconds How long this store's handle has been idle",
        "# TYPE oktografx_store_idle_seconds gauge",
    ]
    if openmetrics:
        lines.extend(
            [
                "# UNIT oktografx_store_idle_seconds seconds",
                "# UNIT oktografx_worker_in_flight operations",
            ]
        )
    for entry in context.pool.status():
        slug = entry["slug"].replace('"', "")
        lines.append(f'oktografx_store_open_transactions{{db="{slug}"}} {entry["open_transactions"]}')
        lines.append(f'oktografx_store_idle_seconds{{db="{slug}"}} {entry["idle_seconds"]}')
    if openmetrics:
        lines.append("# EOF")
    return "\n".join(lines) + "\n"


def _mount_console(app: FastAPI) -> None:
    """Serve the built console, with an honest answer when it was never built."""

    assets = WEB_DIRECTORY / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/", include_in_schema=False)
    @app.get("/favicon.ico", include_in_schema=False)
    async def console_root(request: Request):  # type: ignore[no-untyped-def]
        return _spa_response(request, None)


def _spa_response(request: Request, exc: StarletteHTTPException | None):  # type: ignore[no-untyped-def]
    """An asset from the bundle, or the shell for any path the router did not claim.

    A browser route is not a file, so most paths here end at `index.html`. The ones that *do* name a
    file are checked against the console directory rather than trusted: a request carrying `..`
    segments — raw or percent-encoded — gets the shell, never a file from outside the bundle.
    """

    index = WEB_DIRECTORY / "index.html"
    path = request.url.path
    if index.exists():
        root = WEB_DIRECTORY.resolve()
        candidate = (WEB_DIRECTORY / path.lstrip("/")).resolve()
        if (
            path != "/"
            and candidate.is_file()
            and candidate != root
            and candidate.is_relative_to(root)
        ):
            return FileResponse(str(candidate))
        return FileResponse(str(index), media_type="text/html")
    return JSONResponse(
        {
            "error": {
                "code": "console_not_built",
                "message": "The web console has not been built into this installation. Run "
                "'npm --prefix web run build', or use the JSON API at /api/v1 and the "
                "documentation at /api-docs.",
                "retryable": False,
                "details": {"expected": str(index)},
                "request_id": get_request_id(),
            }
        },
        status_code=404 if exc is not None else 200,
    )




# --------------------------------------------------------------------------------------
# The published contract
# --------------------------------------------------------------------------------------

_ERROR_ENVELOPE_SCHEMA = {
    "type": "object",
    "required": ["error"],
    "properties": {
        "error": {
            "type": "object",
            "required": ["code", "message", "retryable", "details", "request_id"],
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Stable machine code derived from the engine's own class and "
                    "code, e.g. write_conflict, budget_exceeded, corruption_detected.",
                },
                "message": {"type": "string", "description": "A sentence a human can act on. "
                "Never a traceback."},
                "retryable": {
                    "type": "boolean",
                    "description": "Whether the same request may be sent again. A write conflict "
                    "is retryable only with a recomputed operation; an unknown durability outcome "
                    "never is.",
                },
                "details": {"type": "object"},
                "request_id": {"type": "string"},
                "field": {"type": ["string", "null"]},
                "value": {"description": "The offending value, when it is safe to echo."},
                "http_status": {"type": "integer"},
                "grafx_code": {"type": ["string", "null"]},
            },
        }
    },
}

_ERROR_RESPONSES = {
    "400": "The statement or index was refused by the engine (parse_error, plan_error, "
    "index_error).",
    "401": "Missing, malformed or unknown credential.",
    "403": "The credential is valid but not allowed here (out of scope, read-only door, "
    "write_not_permitted).",
    "404": "No such database for this credential — identical whether or not it exists.",
    "409": "Write conflict or a state conflict (write sessions are one per store).",
    "413": "A budget refused: row, graph, projection or request-size cap.",
    "422": "The request did not match the documented schema, or a path was refused by policy.",
    "429": "Too many concurrent sessions or in-flight operations for this store.",
    "500": "Server or store fault (corruption_detected, recovery_refused, "
    "durability_barrier_failed).",
    "503": "The store is leased/busy or needs recovery; retry later.",
}


def _publish_contract(app: FastAPI) -> None:
    """Put the honest contract in the document: credentials, envelope, refusal statuses.

    The README's curl and JavaScript examples stand in for a client SDK in v1, so the published
    OpenAPI has to say what a refusal looks like, not only what success looks like.
    """

    original = app.openapi

    def openapi() -> dict:
        schema = original()
        if not schema:  # pragma: no cover - FastAPI always builds a document here
            return schema
        components = schema.setdefault("components", {})
        schemes = components.setdefault("securitySchemes", {})
        schemes["bearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "okg_",
            "description": "An API key ('okg_…'). Reads are the default; writes need the key's "
            "allow_writes, the database's allow_writes and read_only = false. Keys never reach "
            "the administrative surface, which uses a session cookie instead.",
        }
        schemes["adminSession"] = {
            "type": "apiKey",
            "in": "cookie",
            "name": "okg_session",
            "description": "Set by POST /admin/api/login from the bootstrap admin token; "
            "httpOnly, SameSite=Strict, 12 hours.",
        }
        schemas = components.setdefault("schemas", {})
        schemas.setdefault("ErrorEnvelope", _ERROR_ENVELOPE_SCHEMA)

        for path, operations in schema.get("paths", {}).items():
            for method, operation in operations.items():
                if not isinstance(operation, dict):
                    continue
                if path.startswith("/admin/api") and path != "/admin/api/login":
                    operation.setdefault("security", [{"adminSession": []}])
                elif path.startswith("/api/v1") and path != "/api/v1/health":
                    operation.setdefault("security", [{"bearerAuth": []}])
                responses = operation.setdefault("responses", {})
                for status, description in _ERROR_RESPONSES.items():
                    if status in responses:
                        continue
                    if status == "401" and path == "/api/v1/health":
                        continue
                    responses[status] = {
                        "description": description,
                        "content": {
                            "application/json": {"$ref": "#/components/schemas/ErrorEnvelope"}
                        },
                    }
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
