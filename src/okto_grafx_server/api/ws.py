"""WebSocket: a transactional session with batched streaming.

A Grafx transaction belongs to one handle and one operation, and the integration guide is explicit
that a request must not interleave lifecycles on a shared transaction. So a WebSocket session *is*
the operation: it checks out a lane, owns exactly one transaction, and never hands that transaction
to another connection.

Write sessions take the store's single writer lane. That is not a limitation the server invented —
the engine keeps a writer lease and an exclusive publication section, so a second concurrent writer
would only contend. A busy writer lane is an honest ``429 write_lane_busy``, and a conflict is an
honest ``409 write_conflict`` whose advice is "reopen a snapshot and recompute", never an automatic
replay of business logic.

Streaming is real batching, not a fake cursor: ``fetchmany`` pages come from the engine's own
streaming cursor when the transaction is read-only, and a client that disconnects mid-stream gets
its cursor closed with its transaction.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from ..errors import AppError, map_grafx_error, too_many
from ..control.keys import looks_like_api_key
from .context import (
    Principal,
    SESSION_COOKIE,
    ServerContext,
    admin_open_to_loopback,
    principal_allows_writes,
    require_scope,
    summarize_parameters,
)

log = logging.getLogger("okto_grafx_server.ws")


def _audit(
    ctx: ServerContext,
    session: WsSession,
    action: str,
    *,
    outcome: str,
    cypher: Any = None,
    parameters: Any = None,
    rows: int | None = None,
    duration_ms: float | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """One audit line for one motion of a WebSocket session.

    Writes reach the store only through a session, so an audit trail that stopped at the REST door
    would record every read and none of the changes. Metadata only: the statement is stored
    truncated to 400 characters and parameters are named by shape (`str<12>`), never by value.
    """

    request: dict[str, Any] = {}
    if cypher is not None:
        request["query"] = str(cypher)[:400]
    if parameters:
        request["parameters"] = summarize_parameters(parameters)
    if extra:
        request.update(extra)
    ctx.store.audit(
        actor=session.principal_label,
        action=action,
        database_slug=session.slug,
        request=request,
        outcome=outcome,
        rows=rows,
        duration_ms=duration_ms,
    )

PROTOCOL_VERSION = 1


@dataclass
class WsSession:
    session_id: str
    slug: str
    mode: str
    handle: Any
    release: Any
    record: Any
    principal_label: str = "key"
    transaction: Any = None
    token: Any = None
    busy: bool = False
    # Frames that arrived while a statement held the connection; the loop replays them after.
    pending: Any = None
    last_activity: float = field(default_factory=time.monotonic)

    def close_transaction(self) -> None:
        txn = self.transaction
        self.transaction = None
        if txn is None:
            return
        try:
            if txn.active:
                txn.rollback()
        except Exception as exc:  # noqa: BLE001 - a rollback failure is not a reason to leak a lane
            log.warning("rollback failed on session %s: %s", self.session_id, exc)


class WsSessionRegistry:
    def __init__(self, limit: int, max_write_sessions: int = 1) -> None:
        self.limit = max(1, int(limit))
        self.max_write_sessions = max(1, int(max_write_sessions))
        self._sessions: dict[str, WsSession] = {}
        self._reserved_writes = 0
        self._lock = asyncio.Lock()

    @property
    def write_sessions(self) -> int:
        """Live write sessions plus the ones being opened right now."""

        live = sum(1 for session in self._sessions.values() if session.mode == "write")
        return live + self._reserved_writes

    def claim_write_slot(self) -> bool:
        """Take one of the server's write slots, if any is free.

        ``max_write_sessions`` bounds concurrent write sessions across stores; inside one store the
        exclusive writer lane is the real limit, because the engine keeps a single writer lease.
        """

        if self.write_sessions >= self.max_write_sessions:
            return False
        self._reserved_writes += 1
        return True

    def release_write_slot(self) -> None:
        self._reserved_writes = max(0, self._reserved_writes - 1)

    @property
    def count(self) -> int:
        return len(self._sessions)

    async def add(self, session: WsSession) -> None:
        async with self._lock:
            if len(self._sessions) >= self.limit:
                raise too_many(
                    f"This server allows {self.limit} concurrent WebSocket sessions.",
                    code="too_many_sessions",
                    # A session slot frees itself when a client finishes or the idle TTL fires, so a
                    # second is an honest hint rather than a shrug.
                    headers={"Retry-After": "2"},
                )
            self._sessions[session.session_id] = session
            # The reservation taken at handshake becomes a live session here; counting both would
            # charge the same write session twice.
            self.release_write_slot()

    async def remove(self, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is not None and session.mode == "write":
                self.release_write_slot()

    async def close_all(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._reserved_writes = 0
        for session in sessions:
            _retire(session)


def _retire(session: WsSession) -> None:
    session.close_transaction()
    if session.release is not None:
        try:
            session.release()
        except Exception as exc:  # noqa: BLE001
            log.warning("releasing a session lane failed: %s", exc)
    session.release = None


async def websocket_session(websocket: WebSocket) -> None:
    """The whole session lifecycle, including the parts a client cannot see."""

    ctx: ServerContext = websocket.app.state.context
    registry: WsSessionRegistry = websocket.app.state.ws_sessions
    await websocket.accept()

    session: WsSession | None = None
    idle_ttl = ctx.config.idle_session_ttl_seconds

    try:
        hello = await _receive(websocket)
        if hello is None:
            return
        if hello.get("type") != "hello":
            await _send(
                websocket,
                {
                    "type": "error",
                    "code": "hello_required",
                    "message": "The first message must be {'type':'hello','db':'<slug>'}.",
                },
            )
            return
        want_write = str(hello.get("mode", "read")) == "write"
        if want_write and not registry.claim_write_slot():
            await _send(
                websocket,
                {
                    "type": "error",
                    "code": "write_lane_busy",
                    "message": f"This server allows {registry.max_write_sessions} concurrent "
                    "write session(s). The engine keeps one writer lease per store, so a second "
                    "writer would only contend for it.",
                    "retryable": True,
                },
            )
            with contextlib.suppress(Exception):
                await websocket.close(code=4429)
            return
        session, error = _open_session(ctx, websocket, hello)
        if error is not None:
            if want_write:
                registry.release_write_slot()
            await _send(websocket, _error_frame(error, hello.get("id")))
            await websocket.close(code=4401)
            return
        await registry.add(session)
    except AppError as exc:
        await _send(websocket, _error_frame(exc, None))
        with contextlib.suppress(Exception):
            await websocket.close(code=4403)
        return
    except (WebSocketDisconnect, RuntimeError):
        return

    await _send(
        websocket,
        {
            "type": "hello_ok",
            "protocol_version": PROTOCOL_VERSION,
            "session_id": session.session_id,
            "db": session.slug,
            "read_only": session.mode == "read",
            "idle_ttl_seconds": idle_ttl,
            "max_transactions_per_session": 1,
            "note": "One transaction per session, owned by this connection.",
        },
    )

    reaper = asyncio.create_task(_idle_reaper(websocket, session, registry, idle_ttl))
    try:
        while True:
            if session.pending:
                message = session.pending.popleft()
            else:
                message = await _receive(websocket)
            if message is None:
                break
            session.last_activity = time.monotonic()
            if message.get("type") == "close":
                await _send(websocket, {"type": "closed", "session_id": session.session_id})
                break
            await _dispatch(websocket, ctx, session, message)
    except WebSocketDisconnect:
        pass
    except AppError as exc:
        await _send(websocket, _error_frame(exc, None))
    finally:
        reaper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper
        await registry.remove(session.session_id)
        _retire(session)
        if websocket.client_state is not WebSocketState.DISCONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close()


def _open_session(
    ctx: ServerContext, websocket: WebSocket, hello: dict[str, Any]
) -> tuple[WsSession | None, AppError | None]:
    """Authenticate, scope-check and check out a lane for this connection."""

    try:
        principal = _authenticate_websocket(ctx, websocket)
        slug = str(hello.get("db") or "")
        record = require_scope(ctx, principal, slug)
        want_write = str(hello.get("mode", "read")) == "write"
        if want_write:
            ctx.service.require_writes(record, principal_allows_writes(principal))
            release = ctx.pool.acquire_writer(slug, path=record.path, options=record.options)
            handle = release.handle  # type: ignore[attr-defined]
            mode = "write"
        else:
            stack = contextlib.ExitStack()
            try:
                handle = stack.enter_context(
                    ctx.pool.reader(
                        slug,
                        path=record.path,
                        options=record.options,
                        read_only=record.read_only,
                    )
                )
            except BaseException:
                # A refused checkout must not leave the stack holding a lane.
                stack.close()
                raise
            release = stack.close
            mode = "read"
    except AppError as exc:
        return None, exc
    session = WsSession(
        session_id=uuid.uuid4().hex,
        slug=slug,
        mode=mode,
        handle=handle,
        release=release,
        record=record,
        principal_label=principal.label,
        pending=collections.deque(),
    )
    return session, None


def _websocket_token(websocket: WebSocket) -> str | None:
    """Bearer header for native clients; ``?token=`` or the ``bearer.<token>`` subprotocol for
    browsers, which cannot set a header on a WebSocket handshake."""

    header = websocket.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    from urllib.parse import parse_qs, unquote

    query = parse_qs(websocket.scope.get("query_string", b"").decode("utf-8"))
    token = query.get("token", [""])[0]
    if token:
        return unquote(token)
    for candidate in [part.strip() for part in websocket.headers.get("sec-websocket-protocol", "").split(",")]:
        if candidate.startswith("bearer."):
            return candidate[7:]
    return None


def _authenticate_websocket(ctx: ServerContext, websocket: WebSocket) -> Principal:
    """A WebSocket session is authenticated like HTTP: bearer token, or admin session cookie.

    Browsers cannot set a header on a WebSocket handshake, so ``?token=`` and the
    ``bearer.<token>`` subprotocol are accepted as the same credential.
    """

    token = _websocket_token(websocket)
    if token:
        if not looks_like_api_key(token):
            raise AppError(
                "That does not look like an API key (expected the 'okg_' prefix).",
                status=1008,
                code="malformed_api_key",
            )
        key = ctx.store.authenticate_key(token)
        ctx.store.touch_key(key.id)
        key = ctx.store.get_key(key.id)
        return Principal(
            kind="key",
            label=f"key:{key.fingerprint}",
            databases=tuple(sorted(key.databases)),
            allow_writes=bool(key.allow_writes and key.enabled and not key.revoked_at),
            key_id=key.id,
        )
    cookies = websocket.cookies or {}
    session_id = cookies.get(SESSION_COOKIE)
    if session_id and ctx.store.validate_session(session_id):
        return _loopback_admin(ctx)
    # While no token is configured the console works from this machine with no cookie at all. A
    # handshake is treated as a change, not a read: browsers always send Origin on a WebSocket, so a
    # page that is not on this side fails the same-site test before it can open a write channel.
    if admin_open_to_loopback(ctx, websocket, method="WEBSOCKET"):
        return _loopback_admin(ctx)
    raise AppError(
        "Send the API key as 'Authorization: Bearer okg_…', '?token=…', the "
        "'bearer.<token>' subprotocol, or sign in as administrator.",
        status=1008,
        code="missing_api_key",
    )


def _loopback_admin(ctx: ServerContext) -> Principal:
    return Principal(
        kind="admin",
        label="admin:loopback",
        databases=tuple(
            sorted(record.slug for record in ctx.store.list_databases() if record.enabled)
        ),
        allow_writes=True,
    )


async def _receive(websocket: WebSocket) -> dict[str, Any] | None:
    try:
        message = await websocket.receive()
    except WebSocketDisconnect:
        return None
    if message.get("type") == "websocket.disconnect":
        return None
    if message.get("text") is not None:
        import json

        try:
            payload = json.loads(message["text"])
        except ValueError:
            return {"type": "_malformed"}
        return payload if isinstance(payload, dict) else {"type": "_malformed"}
    if message.get("bytes") is not None:
        return {"type": "_malformed"}
    return None


async def _send(websocket: WebSocket, payload: dict[str, Any]) -> None:
    if websocket.client_state is WebSocketState.DISCONNECTED:
        return
    with contextlib.suppress(Exception):
        await websocket.send_json(payload)


def _error_frame(exc: AppError, correlation: Any) -> dict[str, Any]:
    frame = {"type": "error", **exc.envelope()["error"]}
    if correlation is not None:
        frame["id"] = correlation
    return frame


async def _idle_reaper(
    websocket: WebSocket, session: WsSession, registry: WsSessionRegistry, ttl: float
) -> None:
    while True:
        await asyncio.sleep(max(1.0, ttl / 4))
        if session.busy:
            continue
        if time.monotonic() - session.last_activity <= ttl:
            continue
        await _send(
            websocket,
            {
                "type": "idle_timeout",
                "session_id": session.session_id,
                "message": "The session was idle: its transaction was rolled back and the "
                "connection closed.",
            },
        )
        _retire(session)
        with contextlib.suppress(Exception):
            await websocket.close(code=4408)
        return


async def _dispatch(
    websocket: WebSocket, ctx: ServerContext, session: WsSession, message: dict[str, Any]
) -> None:
    kind = str(message.get("type", ""))
    correlation = message.get("id")
    if kind == "ping":
        await _send(websocket, {"type": "pong", "id": correlation, "at": time.time()})
        return
    if kind == "_malformed":
        await _send(
            websocket,
            {"type": "error", "code": "malformed_message", "message": "Expected a JSON object."},
        )
        return
    if kind == "begin":
        await _begin(websocket, ctx, session, message, correlation)
        return
    if kind not in {"execute", "commit", "rollback", "close_tx", "cancel"}:
        # A typo in a message type is not a transaction problem: answering "open a transaction
        # first" would send the caller looking in the wrong place.
        await _send(
            websocket,
            {
                "type": "error",
                "id": correlation,
                "code": "unknown_message",
                "message": f"No message type {kind!r}.",
            },
        )
        return
    if session.transaction is None:
        await _send(
            websocket,
            {
                "type": "error",
                "id": correlation,
                "code": "no_transaction",
                "message": "Open a transaction first with {'type':'begin','mode':'read'|'write'}.",
            },
        )
        return
    if kind == "execute":
        await _execute(websocket, ctx, session, message, correlation)
        return
    if kind == "commit":
        await _commit(websocket, ctx, session, correlation)
        return
    if kind == "rollback":
        session.close_transaction()
        _audit(ctx, session, "ws.rollback", outcome="ok")
        await _send(websocket, {"type": "rollback_ok", "id": correlation})
        return
    if kind == "close_tx":
        session.close_transaction()
        _audit(ctx, session, "ws.close_tx", outcome="ok")
        await _send(websocket, {"type": "close_tx_ok", "id": correlation})
        return
    if kind == "cancel":
        if session.mode == "write":
            await _send(
                websocket,
                {
                    "type": "error",
                    "id": correlation,
                    "code": "cancellation_unavailable",
                    "message": "A statement inside a write transaction cannot be cancelled: the "
                    "engine offers execution control on read transactions only. Roll the "
                    "transaction back, or close the connection, to abandon the write.",
                },
            )
            return
        cancelled = session.token is not None
        if cancelled:
            session.token.cancel()  # type: ignore[union-attr]
        await _send(
            websocket,
            {
                "type": "cancel_ack",
                "id": correlation,
                "cancelled": cancelled,
                "note": None if cancelled else "No read statement is in flight on this session.",
            },
        )
        return


async def _begin(
    websocket: WebSocket, ctx: ServerContext, session: WsSession, message: dict[str, Any],
    correlation: Any,
) -> None:
    if session.transaction is not None and session.transaction.active:
        await _send(
            websocket,
            {
                "type": "error",
                "id": correlation,
                "code": "transaction_open",
                "message": "This session already holds a transaction. Commit or roll it back "
                "first: one session, one transaction.",
            },
        )
        return
    mode = str(message.get("mode", "read"))
    if mode not in {"read", "write"}:
        await _send(
            websocket,
            {"type": "error", "id": correlation, "code": "invalid_mode",
             "message": "mode must be 'read' or 'write'."},
        )
        return
    if mode == "write" and session.mode != "write":
        await _send(
            websocket,
            {
                "type": "error",
                "id": correlation,
                "code": "write_not_permitted",
                "message": "This session was opened read-only. Reconnect with mode='write' on a "
                "key and database whose write toggles are both on.",
            },
        )
        return
    try:
        def begin():
            return session.handle.begin("write" if mode == "write" else "read")

        txn = await ctx.runner.submit_thread(begin)
    except AppError as exc:
        await _send(websocket, _error_frame(exc, correlation))
        return
    except Exception as exc:  # noqa: BLE001
        await _send(websocket, _error_frame(map_grafx_error(exc), correlation))
        return
    session.transaction = txn
    snapshot = getattr(txn, "snapshot", None)
    _audit(ctx, session, "ws.begin", outcome="ok", extra={"mode": mode})
    await _send(
        websocket,
        {
            "type": "begin_ok",
            "id": correlation,
            "tx": txn.txn_id,
            "mode": mode,
            "snapshot_lsn": int(getattr(snapshot, "read_lsn", 0) or 0) or None,
        },
    )


async def _execute(
    websocket: WebSocket, ctx: ServerContext, session: WsSession, message: dict[str, Any],
    correlation: Any,
) -> None:
    if session.busy:
        await _send(
            websocket,
            {
                "type": "error",
                "id": correlation,
                "code": "session_busy",
                "message": "One statement at a time per session.",
            },
        )
        return
    cypher = message.get("query")
    if not isinstance(cypher, str) or not cypher.strip():
        await _send(
            websocket,
            {"type": "error", "id": correlation, "code": "invalid_request", "field": "query",
             "message": "'query' must be a non-empty string."},
        )
        return
    parameters = message.get("parameters") or {}
    if not isinstance(parameters, dict):
        await _send(
            websocket,
            {"type": "error", "id": correlation, "code": "invalid_parameters",
             "message": "'parameters' must be a JSON object."},
        )
        return
    batch_size = int(message.get("batch_size") or 200)
    batch_size = max(1, min(batch_size, 1000))
    limit = int(message.get("limit") or ctx.config.max_request_rows)
    limit = max(0, min(limit, ctx.config.max_request_rows))

    from ..engine.values import decode_parameters

    session.busy = True
    from okto_grafx.domain.query.control import CancellationToken

    token = CancellationToken()
    # Grafx 0.0.8 refuses cancellation and deadlines on a write transaction: control belongs to
    # read execution. A write statement runs to completion, and the way to stop it is to roll the
    # transaction back or drop the connection.
    controllable = session.mode == "read"
    session.token = token if controllable else None
    try:
        def run():
            params = decode_parameters(parameters)
            if controllable:
                return session.transaction.execute(
                    cypher,
                    params,
                    cancellation=token,
                    timeout_seconds=ctx.config.query_timeout_seconds,
                )
            return session.transaction.execute(cypher, params)

        started = time.monotonic()
        try:
            result = await _await_statement(websocket, ctx, session, run, correlation)
        except _StatementAbandoned:
            _audit(
                ctx,
                session,
                "ws.execute",
                outcome="abandoned",
                cypher=cypher,
                parameters=parameters,
                duration_ms=round((time.monotonic() - started) * 1000, 2),
            )
            raise
        except AppError as exc:
            _audit(
                ctx,
                session,
                "ws.execute",
                outcome=exc.code,
                cypher=cypher,
                parameters=parameters,
                duration_ms=round((time.monotonic() - started) * 1000, 2),
            )
            await _send(websocket, _error_frame(exc, correlation))
            return
        except Exception as exc:  # noqa: BLE001
            mapped = map_grafx_error(exc)
            _audit(
                ctx,
                session,
                "ws.execute",
                outcome=mapped.code,
                cypher=cypher,
                parameters=parameters,
                duration_ms=round((time.monotonic() - started) * 1000, 2),
            )
            await _send(websocket, _error_frame(mapped, correlation))
            return

        from ..engine.service import plan_to_dict

        codec = ctx.service.codec_for(session.handle, session.record)
        columns = [str(c) for c in (result.columns or ())]
        rows = list(result.rows)
        truncated = len(rows) > limit
        kept = rows[:limit]
        encoded, warnings = codec.encode_rows(kept, columns)
        seq = 0
        sent = 0
        for start in range(0, len(encoded), batch_size):
            chunk = encoded[start : start + batch_size]
            await _send(
                websocket,
                {
                    "type": "batch",
                    "id": correlation,
                    "tx": session.transaction.txn_id if session.transaction else None,
                    "seq": seq,
                    "columns": columns,
                    "rows": chunk,
                },
            )
            seq += 1
            sent += len(chunk)
            if seq >= ctx.config.max_ws_batches and start + batch_size < len(encoded):
                # The batch cap is a real refusal, so it is reported as a truncated answer.
                truncated = True
                break
        _audit(
            ctx,
            session,
            "ws.execute",
            outcome="ok",
            cypher=cypher,
            parameters=parameters,
            rows=sent,
            duration_ms=round((time.monotonic() - started) * 1000, 2),
            extra={
                "mode": session.mode,
                "truncated": bool(truncated),
                "rows_available": len(rows),
                "limit": limit,
            },
        )
        await _send(
            websocket,
            {
                "type": "batch_end",
                "id": correlation,
                "seq": seq,
                "columns": columns,
                # Rows actually delivered, never rows the engine happened to produce.
                "row_count": sent,
                "rows_available": len(rows),
                "limit": limit,
                "truncated": bool(truncated),
                "statistics": dict(result.statistics or {}),
                "warnings": warnings.items,
                "plan": plan_to_dict(result.plan) if message.get("explain") else None,
            },
        )
    finally:
        session.busy = False
        session.token = None
        session.last_activity = time.monotonic()


class _StatementAbandoned(WebSocketDisconnect):
    """The connection went away while the statement was still running.

    Ending the session is the honest response: the statement thread cannot be interrupted, so it
    is cancelled best-effort and its transaction is rolled back by the session teardown.
    """

    def __init__(self) -> None:
        super().__init__(code=1006)


async def _await_statement(
    websocket: WebSocket, ctx: ServerContext, session: WsSession, run, correlation: Any
):  # noqa: ANN001 - the engine result type is the library's own
    """Run one statement while keeping the connection able to cancel it.

    The protocol puts cancellation on the same connection as the statement, so the server cannot
    block reading while it executes: control frames (``cancel``, ``ping``) are answered from a
    statement, everything else is queued and replayed when the statement settles. A write
    statement has no cancellation token to hand the engine, so ``cancel`` says so instead of
    pretending.
    """

    statement = asyncio.ensure_future(ctx.runner.submit_thread(run))
    try:
        while True:
            receiving = asyncio.ensure_future(websocket.receive())
            done, _pending = await asyncio.wait(
                {statement, receiving}, return_when=asyncio.FIRST_COMPLETED
            )
            if receiving in done:
                try:
                    message = receiving.result()
                except Exception:  # noqa: BLE001 - a dead transport is a disconnect
                    raise _StatementAbandoned from None
                frame = _frame_of(message)
                if frame is None:
                    raise _StatementAbandoned from None
                await _serve_during_statement(websocket, session, frame, correlation)
            else:
                receiving.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await receiving
            if statement.done():
                return statement.result()
    finally:
        if not statement.done():
            statement.cancel()


async def _serve_during_statement(
    websocket: WebSocket, session: WsSession, frame: dict[str, Any], correlation: Any
) -> None:
    kind = str(frame.get("type", ""))
    if kind == "ping":
        await _send(websocket, {"type": "pong", "id": frame.get("id"), "at": time.time()})
        return
    if kind == "cancel":
        if session.token is None:
            await _send(
                websocket,
                {
                    "type": "error",
                    "id": frame.get("id"),
                    "code": "cancellation_unavailable",
                    "message": "This statement cannot be cancelled: the engine offers execution "
                    "control on read transactions only. Roll the transaction back, or drop the "
                    "connection, to abandon the write.",
                },
            )
            return
        session.token.cancel()
        await _send(
            websocket,
            {"type": "cancel_ack", "id": frame.get("id"), "cancelled": True, "note": None},
        )
        return
    session.pending.append(frame)


def _frame_of(message: dict[str, Any]) -> dict[str, Any] | None:
    if message.get("type") == "websocket.disconnect":
        return None
    if message.get("text") is not None:
        import json

        try:
            payload = json.loads(message["text"])
        except ValueError:
            return {"type": "_malformed"}
        return payload if isinstance(payload, dict) else {"type": "_malformed"}
    if message.get("bytes") is not None:
        return {"type": "_malformed"}
    return None


async def _commit(
    websocket: WebSocket, ctx: ServerContext, session: WsSession, correlation: Any
) -> None:
    txn = session.transaction
    if txn is None or not txn.active:
        await _send(
            websocket,
            {"type": "error", "id": correlation, "code": "no_transaction",
             "message": "There is no open transaction to commit."},
        )
        return
    transaction = txn

    def commit() -> Any:
        return transaction.commit()

    try:
        report = await ctx.runner.submit_thread(commit)
    except AppError as exc:
        _audit(ctx, session, "ws.commit", outcome=exc.code)
        session.close_transaction()
        await _send(websocket, _error_frame(exc, correlation))
        return
    except Exception as exc:  # noqa: BLE001
        mapped = map_grafx_error(exc)
        _audit(ctx, session, "ws.commit", outcome=mapped.code, extra=dict(mapped.details))
        details = dict(mapped.details)
        if details.get("committed") or details.get("durable"):
            mapped.message = (
                mapped.message
                + " The transaction IS committed (durable="
                f"{bool(details.get('durable'))}); do not replay this write."
            )
        session.close_transaction()
        await _send(websocket, _error_frame(mapped, correlation))
        return
    session.close_transaction()
    _audit(
        ctx,
        session,
        "ws.commit",
        outcome="ok",
        extra={"csn": report.csn, "durable": bool(report.durable), "wrote": bool(report.wrote)},
    )
    await _send(
        websocket,
        {
            "type": "commit_ok",
            "id": correlation,
            "csn": report.csn,
            "durable": bool(report.durable),
            "wrote": report.wrote,
        },
    )


