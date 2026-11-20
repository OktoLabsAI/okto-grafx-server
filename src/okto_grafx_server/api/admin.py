"""Administrative surface: the control plane, not the data plane.

Signed in with the bootstrap admin token, which is exchanged for an ``httpOnly; SameSite=Strict``
session cookie. API keys never authenticate here — a data-plane credential must not be able to
register stores, mint more credentials or run maintenance.

Two rules shape the doors:

* **attach is a reference, never a copy.** The server records a path inside an allowed root; it
  does not move, copy, create or delete a store directory. Removing a registration does not touch
  the files.
* **destructive-ish operations go through jobs.** ``verify``/``recover``/``checkpoint``/``vacuum``
  and index rebuilds need local quiescence, so they run as a job that first closes the server's
  handles and refuses while a write session is live.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from ..config import database_option_schema, validate_store_options
from ..control.keys import generate_admin_token
from ..control.store import valid_slug
from ..engine.jobs import describe_actions
from ..errors import AppError, bad_request
from .. import __version__
from .context import (
    SESSION_COOKIE,
    ServerContext,
    admin_session_id,
    context_of,
    require_admin,
)

router = APIRouter(prefix="/admin/api", tags=["admin"])

ADMIN_COOKIE_TTL_HOURS = 12.0


# --------------------------------------------------------------------------------------
# bodies
# --------------------------------------------------------------------------------------


class LoginRequest(BaseModel):
    token: str = Field(min_length=1, max_length=256)


class AttachRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    slug: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=120)
    read_only: bool = True
    allow_writes: bool = False
    options: dict[str, Any] | None = None
    open_policy: Literal["lazy", "pinned"] = "lazy"
    note: str = Field(default="", max_length=500)


class DatabasePatch(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    read_only: bool | None = None
    allow_writes: bool | None = None
    enabled: bool | None = None
    open_policy: Literal["lazy", "pinned"] | None = None
    note: str | None = Field(default=None, max_length=500)
    options: dict[str, Any] | None = None


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    databases: list[str] = Field(default_factory=list)
    allow_writes: bool = False


class KeyPatch(BaseModel):
    databases: list[str] | None = None
    allow_writes: bool | None = None


class ActionRequest(BaseModel):
    arguments: dict[str, Any] | None = None


class DdlRequest(BaseModel):
    statements: list[str] = Field(min_length=1, max_length=64)


# --------------------------------------------------------------------------------------
# session
# --------------------------------------------------------------------------------------


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    ctx = context_of(request)
    if not ctx.store.check_admin_token(body.token):
        raise AppError(
            "That admin token is not valid for this server.",
            status=401,
            code="invalid_admin_token",
        )
    session_id = ctx.store.create_session(ttl_hours=ADMIN_COOKIE_TTL_HOURS)
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        httponly=True,
        samesite="strict",
        secure=False,  # loopback HTTP in v1; TLS is a reverse-proxy concern
        max_age=int(ADMIN_COOKIE_TTL_HOURS * 3600),
        path="/",
    )
    ctx.store.audit(actor="admin", action="login", outcome="ok")
    return {"signed_in": True, "expires_in_seconds": int(ADMIN_COOKIE_TTL_HOURS * 3600)}


@router.post("/logout")
def logout(request: Request, response: Response, _=Depends(require_admin)) -> dict[str, Any]:
    ctx = context_of(request)
    ctx.store.revoke_session(admin_session_id(request))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"signed_in": False}


@router.get("/session")
def session(request: Request) -> dict[str, Any]:
    ctx = context_of(request)
    signed_in = ctx.store.validate_session(admin_session_id(request))
    return {
        "signed_in": signed_in,
        "token_configured": ctx.store.admin_token_configured(),
        "server_version": __version__,
    }


# --------------------------------------------------------------------------------------
# server facts
# --------------------------------------------------------------------------------------


@router.get("/settings")
def settings(request: Request, _=Depends(require_admin)) -> dict[str, Any]:
    ctx = context_of(request)
    config = ctx.config
    return {
        "server_version": __version__,
        "bind": {"host": config.bind_host, "port": config.bind_port},
        "state_dir": str(config.state_dir),
        "allowed_roots": list(config.allowed_roots),
        "limits": {
            "max_open_handles": config.max_open_handles,
            "readers_per_database": config.readers_per_database,
            "max_write_sessions": config.max_write_sessions,
            "max_ws_sessions": config.max_ws_sessions,
            "max_request_rows": config.max_request_rows,
            "max_graph_nodes": config.max_graph_nodes,
            "max_graph_edges": config.max_graph_edges,
            "query_timeout_seconds": config.query_timeout_seconds,
            "reader_lane_timeout_seconds": config.reader_lane_timeout_seconds,
            "idle_session_ttl_seconds": config.idle_session_ttl_seconds,
            "handle_idle_ttl_seconds": config.handle_idle_ttl_seconds,
            "audit_retention_days": config.audit_retention_days,
            "max_query_value_characters": config.max_query_value_characters,
            "max_request_bytes": config.max_request_bytes,
            "max_ws_batches": config.max_ws_batches,
        },
        "security": {
            "tls": False,
            "cors_origins": list(config.cors_origins),
            "metrics_enabled": config.metrics_enabled,
            "allow_remote_store_metrics": config.allow_remote_store_metrics,
            "note": "The server binds loopback and has no TLS. Put a reverse proxy in front for "
            "anything else. A local process with filesystem access can open these stores "
            "directly: this server is logical access control, not an OS sandbox.",
        },
        "database_options": database_option_schema(),
        "maintenance_actions": describe_actions(),
    }


@router.get("/status")
def status(request: Request, _=Depends(require_admin)) -> dict[str, Any]:
    ctx = context_of(request)
    return {
        "handles": ctx.pool.status(),
        "open_slugs": ctx.pool.open_slugs(),
        "runner": vars(ctx.runner.stats),
        "ws_sessions": request.app.state.ws_sessions.count,
        "uptime_seconds": round(time.monotonic() - ctx.started_at, 1),
    }


@router.get("/roots")
def roots(request: Request, _=Depends(require_admin)) -> dict[str, Any]:
    ctx = context_of(request)
    return {"roots": ctx.service.candidate_roots()}


@router.post("/admin-token/rotate")
def rotate_token(request: Request, _=Depends(require_admin)) -> dict[str, Any]:
    ctx = context_of(request)
    token = generate_admin_token()
    ctx.store.set_admin_token(token)
    session_id = admin_session_id(request)
    ctx.store.revoke_session(session_id)
    ctx.store.audit(actor="admin", action="admin_token.rotate", outcome="ok")
    return {
        "admin_token": token,
        "shown_once": True,
        "note": "The previous token and this browser session stop working immediately.",
    }


# --------------------------------------------------------------------------------------
# databases
# --------------------------------------------------------------------------------------


@router.get("/databases")
def list_databases(request: Request, _=Depends(require_admin)) -> dict[str, Any]:
    ctx = context_of(request)
    out = []
    for record in ctx.store.list_databases():
        body = record.public()
        body["open"] = ctx.pool.is_open(record.slug)
        body["open_transactions"] = ctx.pool.open_transactions(record.slug)
        body["active_operations"] = ctx.pool.active_operations(record.slug)
        body["keys"] = [k.fingerprint for k in ctx.store.keys_for_database(record.id)]
        job = ctx.store.active_job(record.id)
        body["active_job"] = job.public() if job else None
        out.append(body)
    return {"databases": out}


@router.get("/databases/{slug}")
def get_database(slug: str, request: Request, _=Depends(require_admin)) -> dict[str, Any]:
    ctx = context_of(request)
    record = ctx.store.get_database(slug)
    body = record.public()
    body["open"] = ctx.pool.is_open(slug)
    body["open_transactions"] = ctx.pool.open_transactions(slug)
    body["keys"] = [k.public() for k in ctx.store.keys_for_database(record.id)]
    body["jobs"] = [job.public() for job in ctx.store.list_jobs(record.id, limit=20)]
    return body


@router.post("/databases/inspect")
def inspect(body: dict[str, Any], request: Request, _=Depends(require_admin)) -> dict[str, Any]:
    ctx = context_of(request)
    raw = body.get("path")
    if not isinstance(raw, str) or not raw.strip():
        raise bad_request("'path' is required.", field="path")
    return ctx.service.inspect(raw)


@router.post("/databases", status_code=201)
def attach(body: AttachRequest, request: Request, _=Depends(require_admin)) -> dict[str, Any]:
    ctx = context_of(request)
    valid_slug(body.slug)
    options = validate_store_options(
        body.options or {}, allow_remote_metrics=ctx.config.allow_remote_store_metrics
    )
    record = ctx.service.attach(
        raw_path=body.path,
        slug=body.slug,
        name=body.name,
        read_only=body.read_only,
        allow_writes=body.allow_writes,
        options=options,
        open_policy=body.open_policy,
        note=body.note,
    )
    ctx.store.prune_audit(ctx.config.audit_retention_days)
    ctx.store.audit(
        actor="admin",
        action="database.attach",
        database_slug=record.slug,
        request={"path": record.path, "read_only": record.read_only},
    )
    return record.public()


@router.patch("/databases/{slug}")
def patch_database(
    slug: str, body: DatabasePatch, request: Request, _=Depends(require_admin)
) -> dict[str, Any]:
    ctx = context_of(request)
    changes = body.model_dump(exclude_none=True)
    if "options" in changes:
        changes["options"] = validate_store_options(
            changes["options"], allow_remote_metrics=ctx.config.allow_remote_store_metrics
        )
    if not changes:
        raise bad_request("Nothing to change.", code="empty_patch")
    if "options" in changes or "read_only" in changes:
        # These are open-time settings: the live handle has to go before they mean anything.
        ctx.pool.close(slug)
    record = ctx.store.update_database(slug, changes)
    ctx.store.audit(
        actor="admin", action="database.update", database_slug=slug, request=changes
    )
    return record.public()


@router.delete("/databases/{slug}")
def delete_database(
    slug: str, request: Request, _=Depends(require_admin)
) -> dict[str, Any]:
    """Remove the registration. The store directory is never deleted by this door."""

    ctx = context_of(request)
    ctx.pool.close(slug)
    record = ctx.store.remove_database(slug)
    ctx.store.audit(
        actor="admin",
        action="database.remove",
        request={"slug": slug, "path": record.path},
    )
    return {"removed": slug, "path_untouched": record.path}


@router.post("/databases/{slug}/open")
def open_database(slug: str, request: Request, _=Depends(require_admin)) -> dict[str, Any]:
    ctx = context_of(request)
    record = ctx.store.get_database(slug)
    result = ctx.service.open_database(record)
    ctx.store.audit(
        actor="admin", action="database.open", database_slug=slug, outcome="ok"
    )
    return result


@router.post("/databases/{slug}/eject")
def eject_database(slug: str, request: Request, _=Depends(require_admin)) -> dict[str, Any]:
    """Drop the server's handles for a store without touching the registration.

    In v1 there is no on-disk cache to invalidate, so ejecting is the same close the LRU performs
    when a store goes idle; the door exists so the console can say "release this store".
    """

    ctx = context_of(request)
    record = ctx.store.get_database(slug)
    ctx.service.close_database(record, force=False)
    ctx.store.audit(actor="admin", action="database.eject", database_slug=slug, outcome="ok")
    return {"slug": slug, "open": False, "ejected": True}


@router.post("/databases/{slug}/close")
def close_database(
    slug: str, request: Request, force: bool = Query(default=False), _=Depends(require_admin)
) -> dict[str, Any]:
    ctx = context_of(request)
    record = ctx.store.get_database(slug)
    result = ctx.service.close_database(record, force=force)
    ctx.store.audit(
        actor="admin", action="database.close", database_slug=slug, request={"force": force}
    )
    return result


@router.get("/databases/{slug}/inventory")
def inventory(slug: str, request: Request, _=Depends(require_admin)) -> dict[str, Any]:
    """Schema, indexes, vector spaces and maintenance status for the detail screen."""

    ctx = context_of(request)
    record = ctx.service.record(slug)
    schema = ctx.service.schema(record, diagnostics=True)
    indexes = ctx.service.index_inventory(record, diagnostics=True)
    return {"schema": schema, **indexes}


@router.post("/databases/{slug}/actions/{action}", status_code=202)
def queue_action(
    slug: str, action: str, body: ActionRequest, request: Request, _=Depends(require_admin)
) -> dict[str, Any]:
    ctx = context_of(request)
    record = ctx.store.get_database(slug)
    if ctx.pool.open_transactions(slug) or ctx.pool.active_operations(slug):
        raise AppError(
            f"Database {slug!r} still has open transactions or in-flight operations. Close the "
            "sessions that hold them; maintenance needs local quiescence.",
            status=409,
            code="maintenance_not_quiesced",
            details={
                "open_transactions": ctx.pool.open_transactions(slug),
                "active_operations": ctx.pool.active_operations(slug),
            },
        )
    job_id = ctx.jobs.submit(
        database_id=record.id,
        slug=slug,
        path=record.path,
        kind=action,
        options=record.options,
        arguments=(body.arguments or {}) if isinstance(body.arguments, dict) else {},
        read_only=record.read_only,
    )
    ctx.store.audit(
        actor="admin", action=f"maintenance.{action}", database_slug=slug, request={"job": job_id}
    )
    return {"job_id": job_id, "state": "queued"}


@router.post("/databases/{slug}/ddl")
def ddl(slug: str, body: DdlRequest, request: Request, _=Depends(require_admin)) -> dict[str, Any]:
    """Schema and index DDL from the console, gated on the same write rules as the data plane.

    A data-plane key also needs ``allow_writes``; the admin console acts as the operator, so it
    only requires the database's own toggles.
    """

    ctx = context_of(request)
    record = ctx.store.get_database(slug)
    result = ctx.service.ddl(record, body.statements, key_allows_writes=True)
    ctx.store.audit(
        actor="admin",
        action="ddl",
        database_slug=slug,
        request={"statements": [s[:200] for s in body.statements]},
        outcome="ok",
        rows=len(result["statements"]),
    )
    return result


# --------------------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------------------


@router.get("/schema/{slug}")
def schema(slug: str, request: Request, _=Depends(require_admin)) -> dict[str, Any]:
    """Catalog for the console, including the full ``stored_type`` of every column."""

    ctx = context_of(request)
    # The administrator's schema view is diagnostics: it has to answer even for a store the pool
    # has quarantined, because it is where the operator reads what went wrong.
    return ctx.service.schema(ctx.service.record(slug), diagnostics=True)


@router.post("/schema/{slug}/ddl")
def schema_ddl(
    slug: str, body: DdlRequest, request: Request, _=Depends(require_admin)
) -> dict[str, Any]:
    """The DDL door under its schema-console name; identical rules to ``databases/{db}/ddl``."""

    return ddl(slug, body, request)


@router.post("/schema/{slug}/identity-indexes")
def schema_identity_indexes(slug: str, request: Request, _=Depends(require_admin)) -> dict[str, Any]:
    """Activate the library's identity access paths (its one-way catalog v1→v2 door).

    This is not a read: the library refuses it on a read-only handle, so it goes through the same
    write admission as DDL and is audited as such. Repeating it once the generations are active
    performs no durable write, which is why the answer distinguishes what appeared from what was
    already there.
    """

    ctx = context_of(request)
    record = ctx.service.record(slug)
    result = ctx.service.ensure_identity_indexes(record, key_allows_writes=True)
    ctx.store.audit(
        actor="admin",
        action="schema.identity_indexes",
        database_slug=slug,
        request={
            "action": "ensure_identity_indexes",
            "activated": result["activated"][:20],
            "already_present": len(result["already_present"]),
        },
        outcome="ok",
        rows=len(result["activated"]),
    )
    return result


@router.get("/jobs")
def list_jobs(
    request: Request,
    database: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    _=Depends(require_admin),
) -> dict[str, Any]:
    ctx = context_of(request)
    database_id = None
    if database:
        database_id = ctx.store.get_database(database).id
    return {"jobs": [job.public() for job in ctx.store.list_jobs(database_id, limit=limit)]}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request, _=Depends(require_admin)) -> dict[str, Any]:
    return context_of(request).store.get_job(job_id).public()


# --------------------------------------------------------------------------------------
# keys
# --------------------------------------------------------------------------------------


@router.get("/keys")
def list_keys(request: Request, _=Depends(require_admin)) -> dict[str, Any]:
    return {"keys": [key.public() for key in context_of(request).store.list_keys()]}


@router.post("/keys", status_code=201)
def create_key(body: KeyCreate, request: Request, _=Depends(require_admin)) -> dict[str, Any]:
    ctx = context_of(request)
    record, secret = ctx.store.create_key(
        name=body.name, databases=body.databases, allow_writes=body.allow_writes
    )
    ctx.store.audit(
        actor="admin",
        action="key.create",
        request={"name": body.name, "databases": record.databases, "allow_writes": body.allow_writes},
    )
    return {**record.public(), "key": secret, "shown_once": True}


@router.patch("/keys/{key_id}")
def patch_key(
    key_id: int, body: KeyPatch, request: Request, _=Depends(require_admin)
) -> dict[str, Any]:
    ctx = context_of(request)
    current = ctx.store.get_key(key_id)
    databases = (
        body.databases if body.databases is not None else [s for s in current.databases]
    )
    allow_writes = (
        body.allow_writes if body.allow_writes is not None else current.allow_writes
    )
    record = ctx.store.set_key_scopes(key_id, databases=databases, allow_writes=allow_writes)
    ctx.store.audit(
        actor="admin",
        action="key.update",
        request={"key_id": key_id, "databases": record.databases, "allow_writes": allow_writes},
    )
    return record.public()


@router.post("/keys/{key_id}/revoke")
def revoke_key(key_id: int, request: Request, _=Depends(require_admin)) -> dict[str, Any]:
    ctx = context_of(request)
    record = ctx.store.revoke_key(key_id)
    ctx.store.audit(actor="admin", action="key.revoke", request={"key_id": key_id})
    return record.public()


@router.delete("/keys/{key_id}")
def delete_key(key_id: int, request: Request, _=Depends(require_admin)) -> dict[str, Any]:
    ctx = context_of(request)
    ctx.store.delete_key(key_id)
    ctx.store.audit(actor="admin", action="key.delete", request={"key_id": key_id})
    return {"removed": key_id}


# --------------------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------------------


@router.get("/audit")
def audit(
    request: Request,
    actor: str | None = Query(default=None, max_length=64),
    database: str | None = Query(default=None, max_length=64),
    action: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=1000),
    _=Depends(require_admin),
) -> dict[str, Any]:
    ctx = context_of(request)
    return {
        "entries": ctx.store.list_audit(
            actor=actor, database_slug=database, action=action, limit=limit
        )
    }
