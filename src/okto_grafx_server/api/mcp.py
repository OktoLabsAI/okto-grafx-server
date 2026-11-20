"""MCP over Streamable HTTP, on the same daemon, key and pool as REST.

The protocol surface is a JSON-RPC 2.0 exchange over ``POST /mcp``: ``initialize``, ``ping``,
``tools/list`` and ``tools/call``. A single JSON response is a conforming Streamable HTTP reply;
no separate process, no second authority, no extra capability.

The important property is that MCP tools call **the same service** as REST, under the same API key
and scope. An agent cannot reach a database its key cannot see, and there is no administrative
tool here at all — registering stores, minting keys and running maintenance stay behind the admin
session where they belong.

Write tools do not exist unless the caller's key carries ``allow_writes``; when it does, ``query``
additionally requires ``confirm_write: true``, because a tool call from a model is exactly the place
where an unconfirmed mutation is expensive.
"""

from __future__ import annotations

import json
import time
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ..errors import AppError, map_grafx_error
from .context import (
    Principal,
    ServerContext,
    authenticate_principal,
    context_of,
    principal_allows_writes,
    require_scope,
    summarize_parameters,
)

router = APIRouter(tags=["mcp"])

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "okto-grafx-server", "version": "0.1.0"}

ToolHandler = Callable[[ServerContext, Any, dict[str, Any]], Awaitable[dict[str, Any]]]


def _tool(
    name: str, description: str, schema: dict[str, Any], *, write: bool = False, database: bool = True
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": schema,
        "annotations": {"readOnlyHint": not write, "destructiveHint": write},
        "_write": write,
        "database": database,
    }


_DB = {"type": "string", "description": "Database id (slug) scoped to this API key."}

TOOLS: tuple[dict[str, Any], ...] = (
    _tool(
        "list_databases",
        "List the databases this API key can reach, with read/write capability.",
        {"type": "object", "properties": {}},
        database=False,
    ),
    _tool(
        "describe_schema",
        "Tables, columns, primary keys, relationship endpoints and vector spaces of one database.",
        {"type": "object", "properties": {"db": _DB}, "required": ["db"]},
    ),
    _tool(
        "query",
        "Run one Cypher statement. Reads are the default; a write needs allow_writes and "
        "confirm_write=true.",
        {
            "type": "object",
            "properties": {
                "db": _DB,
                "cypher": {"type": "string"},
                "parameters": {"type": "object"},
                "limit": {"type": "integer", "minimum": 0},
                "confirm_write": {"type": "boolean", "default": False},
            },
            "required": ["db", "cypher"],
        },
    ),
    _tool(
        "explain",
        "The plan for a statement, without executing it.",
        {
            "type": "object",
            "properties": {"db": _DB, "cypher": {"type": "string"}},
            "required": ["db", "cypher"],
        },
    ),
    _tool(
        "search_text",
        "Native full-text search (BM25) over an existing text index. No analyzer runs implicitly.",
        {
            "type": "object",
            "properties": {
                "db": _DB,
                "index": {"type": "string"},
                "query": {"type": "string"},
                "k": {"type": "integer", "minimum": 1, "maximum": 1000},
                "prefix": {"type": "boolean"},
                "phrase": {"type": "boolean"},
            },
            "required": ["db", "index", "query"],
        },
    ),
    _tool(
        "search_vector",
        "Vector search in a declared space. The caller supplies the embedding: the server never "
        "generates one.",
        {
            "type": "object",
            "properties": {
                "db": _DB,
                "space": {"type": "string"},
                "vector": {"type": "array", "items": {"type": "number"}},
                "k": {"type": "integer", "minimum": 1, "maximum": 1000},
                "table": {"type": "string"},
                "table_kind": {"type": "string", "enum": ["node", "rel"]},
            },
            "required": ["db", "space", "vector"],
        },
    ),
    _tool(
        "search_hybrid",
        "Lexical + vector fusion (RRF) on one snapshot, with optional bounded graph evidence.",
        {
            "type": "object",
            "properties": {
                "db": _DB,
                "table": {"type": "string"},
                "index": {"type": "string"},
                "query": {"type": "string"},
                "space": {"type": "string"},
                "vector": {"type": "array", "items": {"type": "number"}},
                "k": {"type": "integer", "minimum": 1, "maximum": 1000},
                "graph_relations": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["db", "table", "query"],
        },
    ),
    _tool(
        "graph_neighborhood",
        "One hop around a node by physical (table, record_id) identity — a Sigma-ready subgraph.",
        {
            "type": "object",
            "properties": {
                "db": _DB,
                "table": {"type": "string"},
                "record_id": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "required": ["db", "table", "record_id"],
        },
    ),
    _tool(
        "graph_projection",
        "A bounded graph capture of selected tables for visualisation or topology algorithms.",
        {
            "type": "object",
            "properties": {
                "db": _DB,
                "node_tables": {"type": "array", "items": {"type": "string"}},
                "relationship_tables": {"type": "array", "items": {"type": "string"}},
                "max_nodes": {"type": "integer", "minimum": 1},
                "max_edges": {"type": "integer", "minimum": 1},
            },
            "required": ["db", "node_tables"],
        },
    ),
)

_WRITE_TOOL = _tool(
    "write",
    "Run ONE write statement in its own transaction and wait for durability. Requires a key with "
    "allow_writes and confirm_write=true.",
    {
        "type": "object",
        "properties": {
            "db": _DB,
            "cypher": {"type": "string"},
            "parameters": {"type": "object"},
            "confirm_write": {"type": "boolean", "default": False},
        },
        "required": ["db", "cypher", "confirm_write"],
    },
    write=True,
)


def available_tools(principal: Principal) -> list[dict[str, Any]]:
    tools = [dict(tool) for tool in TOOLS]
    if principal_allows_writes(principal):
        tools.append(dict(_WRITE_TOOL))
    for tool in tools:
        # Internal routing flags never go over the wire.
        tool.pop("_write", None)
        tool.pop("database", None)
    return tools


# --------------------------------------------------------------------------------------
# handlers
# --------------------------------------------------------------------------------------


async def _handle_tools_list(
    ctx: ServerContext, principal: Principal, arguments: dict[str, Any]
) -> dict[str, Any]:
    return {"tools": available_tools(principal)}


async def _handle_tools_call(
    ctx: ServerContext, principal: Principal, arguments: dict[str, Any]
) -> dict[str, Any]:
    name = str(arguments.get("name", ""))
    payload = arguments.get("arguments") or {}
    if not isinstance(payload, dict):
        raise bad_request_tool("'arguments' must be an object.")
    tool = _find_tool(name, principal)
    if tool is None:
        known = [tool["name"] for tool in available_tools(principal)]
        hint = ""
        if name == _WRITE_TOOL["name"] and not principal_allows_writes(principal):
            hint = " The write tool exists only for a key whose allow_writes is on."
        raise bad_request_tool(
            f"Unknown tool {name!r}.{hint} This server offers: {', '.join(known)}.",
            field="name",
        )
    db_slug = str(payload.get("db", ""))
    if tool.get("database") is False:
        # list_databases is the one tool that is not about a single store.
        record = None
    else:
        record = require_scope(ctx, principal, db_slug)
    handler = _HANDLERS.get(name)
    if handler is None:  # pragma: no cover - registry and tools stay in step
        raise bad_request_tool(f"Tool {name!r} has no handler.")
    started = time.monotonic()
    try:
        result = await handler(ctx, record, payload, principal)
    except AppError as exc:
        _audit(ctx, principal, name, record, payload, exc, started)
        raise
    except Exception as exc:  # noqa: BLE001 - the audit line is written either way
        mapped = map_grafx_error(exc)
        _audit(ctx, principal, name, record, payload, mapped, started)
        raise mapped
    _audit(ctx, principal, name, record, payload, None, started, result)
    return {
        "content": [
            {"type": "text", "text": json.dumps(result, default=str, ensure_ascii=False)}
        ],
        "structuredContent": result,
        "isError": False,
    }


def _audit(
    ctx: ServerContext,
    principal: Principal,
    name: str,
    record: Any,
    payload: dict[str, Any],
    error: AppError | None,
    started: float,
    result: Any = None,
) -> None:
    """One audit line per tool call, including the refusal.

    An agent writing through MCP must not be invisible in the log: the statement text is kept,
    parameter values are not.
    """

    metadata: dict[str, Any] = {"tool": name}
    if "cypher" in payload:
        metadata["query"] = str(payload["cypher"])[:400]
        metadata["parameters"] = summarize_parameters(payload.get("parameters"))
        metadata["confirm_write"] = bool(payload.get("confirm_write"))
    for key in ("index", "query", "space", "table", "record_id", "k", "node_tables"):
        if key in payload:
            value = payload[key]
            metadata[key] = value if not isinstance(value, list) else list(value)[:16]
    rows = None
    if isinstance(result, dict):
        rows = result.get("row_count") or result.get("achieved_k")
    ctx.store.audit(
        actor=principal.label,
        action=f"mcp.{name}",
        database_slug=getattr(record, "slug", None),
        request=metadata,
        outcome="ok" if error is None else error.code,
        rows=rows,
        duration_ms=round((time.monotonic() - started) * 1000.0, 2),
    )


def _find_tool(name: str, principal: Principal) -> dict[str, Any] | None:
    for tool in TOOLS:
        if tool["name"] == name:
            return tool
    if name == _WRITE_TOOL["name"] and principal_allows_writes(principal):
        return _WRITE_TOOL
    return None


def bad_request_tool(message: str, **kw: Any) -> AppError:
    return AppError(message, status=400, code="invalid_tool_request", **kw)


async def _tool_list_databases(ctx, principal, payload, record=None):  # type: ignore[no-untyped-def]
    out = []
    for slug in sorted(principal.databases):
        entry = ctx.store.get_database(slug)
        out.append(
            {
                "slug": entry.slug,
                "name": entry.name,
                "read_only": entry.read_only,
                "writable": entry.allow_writes and principal_allows_writes(principal),
            }
        )
    return {"databases": out}


async def _tool_describe_schema(ctx, record, payload, principal):  # type: ignore[no-untyped-def]
    return await ctx.runner.submit(
        lambda: ctx.service.schema(record), admission=ctx.admission_for(record.slug)
    )


async def _tool_query(ctx, record, payload, principal):  # type: ignore[no-untyped-def]
    confirm = bool(payload.get("confirm_write"))
    cypher = str(payload.get("cypher", ""))
    if confirm:
        if not principal_allows_writes(principal):
            raise AppError(
                "This API key is read-only, so it has no write tool.",
                status=403,
                code="write_not_permitted",
            )
        return await ctx.runner.submit(
            lambda: ctx.service.write_one(
                record,
                cypher=cypher,
                parameters=payload.get("parameters"),
                key_allows_writes=principal_allows_writes(principal),
            ),
            admission=ctx.admission_for(record.slug),
        )
    outcome = await ctx.runner.submit(
        lambda: ctx.service.query(
            record,
            cypher=cypher,
            parameters=payload.get("parameters"),
            limit=payload.get("limit"),
        ),
        admission=ctx.admission_for(record.slug),
    )
    return outcome.public()


async def _tool_write(ctx, record, payload, principal):  # type: ignore[no-untyped-def]
    if not bool(payload.get("confirm_write")):
        raise bad_request_tool(
            "A write through MCP must set confirm_write=true: an agent must state the mutation it "
            "is making."
        )
    return await ctx.runner.submit(
        lambda: ctx.service.write_one(
            record,
            cypher=str(payload.get("cypher", "")),
            parameters=payload.get("parameters"),
            key_allows_writes=principal_allows_writes(principal),
        ),
        admission=ctx.admission_for(record.slug),
    )


async def _tool_explain(ctx, record, payload, principal):  # type: ignore[no-untyped-def]
    return await ctx.runner.submit(
        lambda: ctx.service.explain(record, str(payload.get("cypher", ""))),
        admission=ctx.admission_for(record.slug),
    )


async def _tool_search_text(ctx, record, payload, principal):  # type: ignore[no-untyped-def]
    return await ctx.runner.submit(
        lambda: ctx.service.search_text(
            record,
            index=str(payload.get("index", "")),
            query=str(payload.get("query", "")),
            k=int(payload.get("k") or 20),
            prefix=bool(payload.get("prefix")),
            phrase=bool(payload.get("phrase")),
        ),
        admission=ctx.admission_for(record.slug),
    )


async def _tool_search_vector(ctx, record, payload, principal):  # type: ignore[no-untyped-def]
    return await ctx.runner.submit(
        lambda: ctx.service.search_vector(
            record,
            space=str(payload.get("space", "")),
            vector=list(payload.get("vector") or []),
            k=int(payload.get("k") or 20),
            table=payload.get("table"),
            table_kind=payload.get("table_kind"),
        ),
        admission=ctx.admission_for(record.slug),
    )


async def _tool_search_hybrid(ctx, record, payload, principal):  # type: ignore[no-untyped-def]
    return await ctx.runner.submit(
        lambda: ctx.service.search_hybrid(
            record,
            table=str(payload.get("table", "")),
            index=payload.get("index"),
            query=str(payload.get("query", "")),
            space=payload.get("space"),
            vector=list(payload.get("vector") or []),
            k=int(payload.get("k") or 20),
            graph_relations=list(payload.get("graph_relations") or []),
        ),
        admission=ctx.admission_for(record.slug),
    )


async def _tool_graph_neighborhood(ctx, record, payload, principal):  # type: ignore[no-untyped-def]
    outcome = await ctx.runner.submit(
        lambda: ctx.service.neighbors(
            record,
            table=str(payload.get("table", "")),
            record_id=int(payload.get("record_id", -1)),
            limit=int(payload.get("limit") or 100),
        ),
        admission=ctx.admission_for(record.slug),
    )
    return outcome.public()


async def _tool_graph_projection(ctx, record, payload, principal):  # type: ignore[no-untyped-def]
    outcome = await ctx.runner.submit(
        lambda: ctx.service.graph(
            record,
            mode="projection",
            node_tables=list(payload.get("node_tables") or []),
            relationship_tables=list(payload.get("relationship_tables") or []),
            max_nodes=payload.get("max_nodes"),
            max_edges=payload.get("max_edges"),
        ),
        admission=ctx.admission_for(record.slug),
    )
    return outcome.public()


_HANDLERS: dict[str, Any] = {
    "list_databases": lambda ctx, record, payload, principal: _tool_list_databases(ctx, principal, payload),
    "describe_schema": _tool_describe_schema,
    "query": _tool_query,
    "write": _tool_write,
    "explain": _tool_explain,
    "search_text": _tool_search_text,
    "search_vector": _tool_search_vector,
    "search_hybrid": _tool_search_hybrid,
    "graph_neighborhood": _tool_graph_neighborhood,
    "graph_projection": _tool_graph_projection,
}


# --------------------------------------------------------------------------------------
# JSON-RPC
# --------------------------------------------------------------------------------------


def _error(code: int, message: str, *, request_id: Any = None, data: Any = None) -> JSONResponse:
    body: dict[str, Any] = {"jsonrpc": "2.0", "error": {"code": code, "message": message}}
    if data is not None:
        body["error"]["data"] = data
    if request_id is not None:
        body["id"] = request_id
    return JSONResponse(body, status_code=200)


def _result(request_id: Any, result: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


@router.post("/mcp")
async def mcp(request: Request) -> Response:
    ctx = context_of(request)
    try:
        principal = authenticate_principal(request)
    except AppError as exc:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "error": {"code": -32001, "message": exc.message, "data": exc.envelope()["error"]},
            },
            status_code=exc.status,
        )
    try:
        envelope = await request.json()
    except Exception:  # noqa: BLE001
        return _error(-32700, "Parse error: expected a JSON-RPC document.")
    if isinstance(envelope, list):
        return _error(-32600, "Batch requests are not supported by this server.")
    if not isinstance(envelope, dict):
        return _error(-32600, "Invalid Request: expected a JSON-RPC object.")

    method = str(envelope.get("method", ""))
    request_id = envelope.get("id")
    params = envelope.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": (
                    "Every tool is scoped to the calling API key. Node and edge ids are physical "
                    "'<table>:<record_id>' identities, not application keys. Vector search takes "
                    "an embedding from the caller: nothing here generates one."
                ),
            },
        )
    if method in {"notifications/initialized", "notifications/cancelled"}:
        return Response(status_code=202)
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, await _handle_tools_list(ctx, principal, params))
    if method == "tools/call":
        try:
            return _result(request_id, await _handle_tools_call(ctx, principal, params))
        except AppError as exc:
            return _result(
                request_id,
                {
                    "content": [{"type": "text", "text": exc.message}],
                    "structuredContent": exc.envelope()["error"],
                    "isError": True,
                },
            )
        except Exception as exc:  # noqa: BLE001
            mapped = map_grafx_error(exc)
            return _result(
                request_id,
                {
                    "content": [{"type": "text", "text": mapped.message}],
                    "structuredContent": mapped.envelope()["error"],
                    "isError": True,
                },
            )
    return _error(-32601, f"Method not found: {method}.", request_id=request_id)


@router.get("/mcp")
def mcp_get() -> Response:
    return JSONResponse(
        {
            "error": "This MCP endpoint is Streamable HTTP: send JSON-RPC POSTs to /mcp.",
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": SERVER_INFO,
        },
        status_code=405,
    )
