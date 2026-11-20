"""Shared request context, authentication and the access rules both surfaces share."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import Request

from ..config import ServerConfig
from ..control.keys import looks_like_api_key
from ..control.store import ControlStore, DatabaseRecord, KeyRecord
from ..engine.handles import HandlePool
from ..engine.jobs import JobRunner
from ..engine.runner import EngineRunner
from ..engine.service import DatabaseService
from ..errors import AppError, forbidden, not_found, unauthorized

SESSION_COOKIE = "okg_session"


@dataclass
class ServerContext:
    config: ServerConfig
    store: ControlStore
    pool: HandlePool
    runner: EngineRunner
    service: DatabaseService
    jobs: JobRunner
    started_at: float = field(default_factory=time.monotonic)
    ws_registry: Any = None
    _admission: dict[str, Any] = field(default_factory=dict)

    def admission_for(self, slug: str):
        """One bounded admission gate per database, created on first use.

        The bound is deliberate: more concurrent work than the store's reader lanes only moves the
        queue, and a queue inside the engine would hide the refusal from the client.
        """

        gate = self._admission.get(slug)
        if gate is None:
            import asyncio

            gate = asyncio.Semaphore(max(1, self.config.readers_per_database + 1))
            self._admission[slug] = gate
        return gate

    def audit(self, *, actor: str, action: str, database: str | None = None,
              request: dict[str, Any] | None = None, outcome: str = "ok",
              rows: int | None = None, started: float | None = None) -> None:
        duration = None
        if started is not None:
            duration = round((time.monotonic() - started) * 1000.0, 2)
        try:
            self.store.audit(
                actor=actor,
                action=action,
                database_slug=database,
                request=request,
                outcome=outcome,
                rows=rows,
                duration_ms=duration,
            )
        except Exception:  # noqa: BLE001 - auditing must never fail the operation it records
            pass

    async def ashutdown(self) -> None:
        """Refuse work, retire sessions, drain workers, close handles, then the control store.

        The order is the point: a WebSocket session holds a lane and its transaction holds a
        snapshot, so closing handles first would pull the ground out from under live work.
        """

        self.runner.refuse_new_work()
        if self.ws_registry is not None:
            await self.ws_registry.close_all()
        self.jobs.stop()
        self.pool.shutdown()
        self.runner.shutdown(wait=True)
        self.store.close()

    def close(self) -> None:
        self.jobs.stop()
        self.pool.shutdown()
        self.runner.shutdown(wait=False)
        self.store.close()


def context_of(request: Request) -> ServerContext:
    ctx: ServerContext = request.app.state.context
    return ctx


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if not header:
        return None
    if not header.lower().startswith("bearer "):
        raise AppError(
            "This server authenticates with 'Authorization: Bearer okg_…' only.",
            status=401,
            code="bearer_scheme_required",
            headers={"WWW-Authenticate": "Bearer"},
            details={"scheme": header.split(" ", 1)[0][:32]},
        )
    return header[7:].strip() or None


def authenticate_api_key(request: Request) -> KeyRecord:
    token = bearer_token(request)
    if not token:
        token = request.query_params.get("token")
    if not token:
        raise AppError(
            "Send the API key as 'Authorization: Bearer okg_…'.",
            status=401,
            code="missing_api_key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not looks_like_api_key(token):
        raise AppError(
            "That does not look like an API key (expected the 'okg_' prefix).",
            status=401,
            code="malformed_api_key",
        )
    ctx = context_of(request)
    key = ctx.store.authenticate_key(token)
    ctx.store.touch_key(key.id)
    key.databases = ctx.store.get_key(key.id).databases
    return key


@dataclass(frozen=True)
class Principal:
    """Whoever is asking: a scoped API key, or the operator behind an admin session.

    The administrator is the operator, so it sees every enabled database and may write wherever the
    database's own toggles allow. An API key sees exactly the databases scoped to it — and an
    out-of-scope id is a ``404``, identical to one that was never registered.
    """

    kind: str            # "key" | "admin"
    label: str           # actor label for the audit log
    databases: tuple[str, ...]
    allow_writes: bool
    key_id: int | None = None

    def can_see(self, slug: str) -> bool:
        return slug in self.databases


def authenticate_principal(request: Request) -> Principal:
    """Accept an API key (the normal door) or an admin session (the console)."""

    ctx = context_of(request)
    if bearer_token(request) or request.query_params.get("token"):
        key = authenticate_api_key(request)
        principal = Principal(
            kind="key",
            label=actor_of(key),
            databases=tuple(sorted(key.databases)),
            allow_writes=key_allows_writes(key),
            key_id=key.id,
        )
    else:
        session_id = admin_session_id(request)
        if not (session_id and ctx.store.validate_session(session_id)):
            raise unauthorized(
                "Send an API key as 'Authorization: Bearer okg_…' or sign in as administrator."
            )
        principal = Principal(
            kind="admin",
            label="admin",
            databases=tuple(
                sorted(record.slug for record in ctx.store.list_databases() if record.enabled)
            ),
            allow_writes=True,
        )
    # The request middleware logs refusals; without the actor in that line, a log full of 403s
    # cannot be attributed. The label is the same vocabulary the audit trail uses.
    request.state.principal_label = principal.label
    return principal


def require_scope(ctx: ServerContext, principal: Principal, slug: str) -> DatabaseRecord:
    """A principal never learns whether a database it cannot see exists."""

    if not principal.can_see(slug):
        raise not_found(f"No database registered with id {slug!r}.", field="slug")
    record = ctx.store.get_database(slug)
    if not record.enabled:
        # The key already names this database in its own scope, so being told it is switched off
        # leaks nothing new — and "disabled" is the true reason the request stopped. A database the
        # principal cannot see at all stays a 404 above, which is where existence would leak.
        raise AppError(
            f"Database {slug!r} is registered but disabled.",
            status=403,
            code="database_disabled",
            details={"slug": slug},
        )
    return record


def key_allows_writes(key: KeyRecord) -> bool:
    return bool(key.allow_writes and key.enabled and not key.revoked_at)


def principal_allows_writes(principal: Principal) -> bool:
    return bool(principal.allow_writes)


def actor_of(key: KeyRecord) -> str:
    return f"key:{key.fingerprint}"


def admin_session_id(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


def require_admin(request: Request) -> str:
    ctx = context_of(request)
    session_id = admin_session_id(request)
    if not session_id:
        token = request.headers.get("x-admin-token")
        if token and ctx.store.check_admin_token(token):
            return "admin:token"
        raise AppError(
            "An administrator session is required.", status=401, code="admin_session_required"
        )
    if not ctx.store.validate_session(session_id):
        raise AppError(
            "That administrator session has expired. Sign in again.",
            status=401,
            code="admin_session_expired",
        )
    return "admin"


def summarize_parameters(parameters: Any) -> dict[str, Any]:
    """Audit metadata: names and shapes, never the values."""

    if not isinstance(parameters, dict):
        return {}
    summary: dict[str, Any] = {}
    for name, value in list(parameters.items())[:32]:
        if isinstance(value, (str,)):
            summary[str(name)] = f"str<{len(value)}>"
        elif isinstance(value, (list, tuple)):
            summary[str(name)] = f"list<{len(value)}>"
        elif isinstance(value, dict):
            summary[str(name)] = f"map<{len(value)}>"
        else:
            summary[str(name)] = type(value).__name__
    return summary


def timing() -> Callable[[], float]:
    return time.monotonic
