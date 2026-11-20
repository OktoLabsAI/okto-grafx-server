"""Shared request context, authentication and the access rules both surfaces share."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import Request

from ..config import ServerConfig, host_is_loopback
from ..control.keys import looks_like_api_key
from ..control.store import ControlStore, DatabaseRecord, KeyRecord
from ..engine.handles import HandlePool
from ..engine.jobs import JobRunner
from ..engine.runner import EngineRunner
from ..engine.service import DatabaseService
from ..errors import AppError, not_found, unauthorized

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
        if session_id and ctx.store.validate_session(session_id):
            label = "admin"
        elif admin_open_to_loopback(ctx, request):
            label = "admin:loopback"
        else:
            raise unauthorized(
                "Send an API key as 'Authorization: Bearer okg_…' or sign in as administrator."
            )
        principal = Principal(
            kind="admin",
            label=label,
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


# ----------------------------------------------------------------------------------- the admin door
#
# The administrator surface — the console, and everything under /admin/api — is open to this machine
# by default and closed to everything else. "This machine" is made a real boundary rather than a
# guess by three checks, because none of them alone is enough:
#
#   * ``bind_host`` is validated loopback (config.py refuses anything else), so the socket itself
#     cannot be reached from another host;
#   * the ASGI peer must be a loopback address, in case something is forwarding to us;
#   * a request that changes state must look like it came from this side of the browser.
#
# The last one is the one people leave out. A page on the internet cannot *read* the response to a
# cross-origin POST against 127.0.0.1, but it can send one, and DNS rebinding makes the browser
# believe evil.com is the local host — so a peer check on its own would be an open door with a
# welcome mat. Turning a token ON closes all of this again: a configured token is always demanded.

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# One reading of "this machine", shared with the bind check in config.py. The rule that decides who
# may act as administrator without a credential must not disagree with the rule that decides what we
# are allowed to bind to.
_host_is_loopback = host_is_loopback


def peer_is_loopback(request: Request) -> bool:
    client = getattr(request, "client", None)
    return bool(client) and _host_is_loopback(getattr(client, "host", "") or "")


def bind_is_loopback(ctx: ServerContext) -> bool:
    return _host_is_loopback(ctx.config.bind_host)


def admin_token_required(ctx: ServerContext) -> bool:
    """A token is demanded when one exists, and off-loopback always.

    The second clause cannot fire for a config that passed validation, and that is not the point:
    relaxing the bind check is one edit away, and an unauthenticated administrator surface is the
    thing that must not survive it.
    """

    return ctx.store.admin_token_configured() or not bind_is_loopback(ctx)


def _browser_came_from_this_side(request: Request) -> bool:
    """Same-site evidence for a state-changing request.

    A browser says which side a request came from: ``Sec-Fetch-Site`` where the browser has it, and
    ``Origin`` on anything but a same-origin form POST. A command-line client sends neither, and is
    not the thing this protects against — a curl on this machine is already the operator.
    """

    sec_fetch = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if sec_fetch:
        return sec_fetch in {"same-origin", "none"}
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if value:
            # Absent origin ("null") comes from a sandboxed or opaque origin: not this side.
            return _host_is_loopback(urlsplit(value).hostname or "")
    return True


def open_loopback_refusal(
    ctx: ServerContext, request: Any, *, method: str | None = None
) -> str | None:
    """``None`` when this request may act as administrator with no credential, otherwise why not.

    ``method`` overrides the verb for callers that are not HTTP requests — a WebSocket handshake has
    no method but is not a safe read either.
    """

    if admin_token_required(ctx):
        return "token_required"
    if not peer_is_loopback(request):
        return "peer"
    if not _host_is_loopback(request.headers.get("host", "")):
        # DNS rebinding: the peer is genuinely loopback, but the browser thinks it is talking to the
        # attacker's host, and that is how a remote page reaches a "local only" API.
        return "host"
    verb = (method or getattr(request, "method", "GET") or "GET").upper()
    if verb not in SAFE_METHODS and not _browser_came_from_this_side(request):
        return "origin"
    return None


def admin_open_to_loopback(
    ctx: ServerContext, request: Any, *, method: str | None = None
) -> bool:
    """Whether this request may act as administrator with no credential."""

    return open_loopback_refusal(ctx, request, method=method) is None


def _admin_refusal(ctx: ServerContext) -> str:
    if not bind_is_loopback(ctx):
        return (
            f"This server is bound to {ctx.config.bind_host!r}, which is not loopback, so an "
            "administrator token is required and none is configured. Mint one with: "
            "oktografx-server admin-token rotate"
        )
    if not ctx.store.admin_token_configured():
        return (
            "The administrator surface is open to this machine only, and this did not arrive as a "
            "request from this machine: it must come from a loopback address, name a loopback host, "
            "and — when it changes something — come from this side of the browser. To require a "
            "token instead: Settings, or oktografx-server admin-token rotate"
        )
    return (
        "An administrator session is required. Sign in from this machine, or send the token as "
        "'X-Admin-Token'."
    )


def require_admin(request: Request) -> str:
    ctx = context_of(request)
    session_id = admin_session_id(request)
    if session_id and ctx.store.validate_session(session_id):
        return "admin"
    token = request.headers.get("x-admin-token")
    if token and ctx.store.check_admin_token(token):
        return "admin:token"
    refusal = open_loopback_refusal(ctx, request)
    if refusal is None:
        # A cookie that no longer names a session is not a refusal while the door is open. The browser
        # holding it would be bounced to a sign-in screen that has nothing to accept — which is exactly
        # what happens to every tab that was open when the token was rotated and then removed.
        return "admin:loopback"
    if refusal == "origin":
        # Worth its own answer: this is not an anonymous stranger, it is a page somewhere else
        # reaching into a local API, and the operator should be told that is what was stopped.
        raise AppError(
            "A page that is not on this machine may not change anything here. Administrator "
            "access from elsewhere needs a token: turn one on in Settings, or run "
            "oktografx-server admin-token rotate.",
            status=403,
            code="admin_origin_not_same_side",
        )
    if session_id:
        raise AppError(
            "That administrator session has expired. Sign in again.",
            status=401,
            code="admin_session_expired",
        )
    raise AppError(_admin_refusal(ctx), status=401, code="admin_session_required")


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
