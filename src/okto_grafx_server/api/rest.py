"""HTTP surface for applications: query, schema, indexes, search, graph.

Everything here is scoped by API key. A key never learns whether a database it cannot see exists —
an out-of-scope slug is a ``404``, identical to a database that was never registered. Reads run as
one owned read transaction per request; there is no cursor continuation token, because a
``ScanCursor`` cannot be serialised and a page that quietly mixes snapshots would be worse than a
page that documents itself as a fresh snapshot.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..errors import AppError
from ..engine.service import QueryOutcome
from .context import (
    ServerContext,
    authenticate_principal,
    context_of,
    principal_allows_writes,
    require_scope,
    summarize_parameters,
)

router = APIRouter(prefix="/api/v1", tags=["data"])


def ctx_of(request: Request) -> ServerContext:
    return context_of(request)


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200_000)
    parameters: dict[str, Any] | None = None
    limit: int | None = Field(default=None, ge=0)
    timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    explain: bool = False


class TextSearchRequest(BaseModel):
    index: str = Field(min_length=1, max_length=256)
    query: str = Field(min_length=1, max_length=8192)
    k: int = Field(default=20, ge=1, le=1000)
    prefix: bool = False
    phrase: bool = False
    return_positions: bool = False
    record_ids: list[int] | None = None
    timeout_seconds: float | None = Field(default=None, gt=0, le=600)


class VectorSearchRequest(BaseModel):
    space: str = Field(min_length=1, max_length=256)
    vector: list[float]
    k: int = Field(default=20, ge=1, le=1000)
    table: str | None = None
    table_kind: Literal["node", "rel"] | None = None
    record_ids: list[int] | None = None
    timeout_seconds: float | None = Field(default=None, gt=0, le=600)


class HybridSearchRequest(BaseModel):
    table: str = Field(min_length=1, max_length=256)
    index: str | None = None
    query: str = Field(min_length=1, max_length=8192)
    space: str | None = None
    vector: list[float] = Field(default_factory=list)
    k: int = Field(default=20, ge=1, le=1000)
    lexical_weight: float = Field(default=1.0, ge=0.0, le=10.0)
    vector_weight: float = Field(default=1.0, ge=0.0, le=10.0)
    candidate_k: int = Field(default=20, ge=1, le=1000)
    allow_partial: bool = False
    graph_relations: list[str] = Field(default_factory=list)
    graph_weight: float = Field(default=0.0, ge=0.0, le=10.0)
    graph_filter: bool = False
    timeout_seconds: float | None = Field(default=None, gt=0, le=600)


class GraphRequest(BaseModel):
    mode: Literal["projection", "query"] = "projection"
    node_tables: list[str] = Field(default_factory=list)
    relationship_tables: list[str] = Field(default_factory=list)
    weight_columns: dict[str, str] | None = None
    query: str | None = None
    parameters: dict[str, Any] | None = None
    max_nodes: int | None = Field(default=None, ge=1)
    max_edges: int | None = Field(default=None, ge=1)


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    from .. import __version__

    ctx = context_of(request)
    return {
        "status": "ok",
        "server": __version__,
        "grafx": _grafx_version(),
        "databases_registered": len(ctx.store.list_databases()),
        "databases_open": len(ctx.pool.open_slugs()),
        "uptime_seconds": round(time.monotonic() - ctx.started_at, 1),
        "runner": vars(ctx.runner.stats),
    }


def _grafx_version() -> str:
    import okto_grafx

    return okto_grafx.__version__


@router.get("/databases")
def list_databases(request: Request, principal=Depends(authenticate_principal)) -> dict[str, Any]:
    ctx = context_of(request)
    out = []
    for slug in sorted(principal.databases):
        # One lookup, and it is the guarded one: a registration that was disabled is refused here
        # rather than listed as if it were queryable.
        status = ctx.service.record(slug)
        out.append(
            {
                "slug": status.slug,
                "name": status.name,
                "read_only": status.read_only,
                "allow_writes": status.allow_writes and principal_allows_writes(principal),
                "open": ctx.pool.is_open(slug),
                "open_transactions": ctx.pool.open_transactions(slug),
                "enabled": status.enabled,
            }
        )
    return {"databases": out, "actor": {"kind": principal.kind, "label": principal.label}}


async def _run(request: Request, call, *, admission: str | None = None):
    ctx = context_of(request)
    gate = ctx.admission_for(admission) if admission is not None else None
    return await ctx.runner.submit(call, admission=gate, label=admission or "engine")


@router.get("/databases/{slug}/schema")
async def schema(slug: str, request: Request, principal=Depends(authenticate_principal)) -> dict[str, Any]:
    ctx = context_of(request)
    record = require_scope(ctx, principal, slug)
    started = time.monotonic()
    result = await _run(request, lambda: ctx.service.schema(record), admission=slug)
    ctx.store.audit(
        actor=principal.label,
        action="schema",
        database_slug=slug,
        outcome="ok",
        duration_ms=round((time.monotonic() - started) * 1000, 2),
    )
    return result


@router.get("/databases/{slug}/indexes")
async def indexes(slug: str, request: Request, principal=Depends(authenticate_principal)) -> dict[str, Any]:
    ctx = context_of(request)
    record = require_scope(ctx, principal, slug)
    return await _run(request, lambda: ctx.service.index_inventory(record), admission=slug)


@router.post("/databases/{slug}/query")
async def query(
    slug: str, body: QueryRequest, request: Request, principal=Depends(authenticate_principal)
) -> dict[str, Any]:
    ctx = context_of(request)
    record = require_scope(ctx, principal, slug)
    limit = body.limit
    if limit is not None and limit > ctx.config.max_request_rows:
        limit = ctx.config.max_request_rows

    def call() -> QueryOutcome:
        return ctx.service.query(
            record,
            cypher=body.query,
            parameters=body.parameters,
            limit=limit,
            timeout_seconds=body.timeout_seconds,
            explain=body.explain,
        )

    started = time.monotonic()
    try:
        outcome = await _run(request, call, admission=slug)
    except AppError as exc:
        ctx.store.audit(
            actor=principal.label,
            action="query",
            database_slug=slug,
            request={"query": body.query[:400], "parameters": summarize_parameters(body.parameters)},
            outcome=exc.code,
            duration_ms=round((time.monotonic() - started) * 1000, 2),
        )
        raise
    ctx.store.audit(
        actor=principal.label,
        action="query",
        database_slug=slug,
        request={"query": body.query[:400], "parameters": summarize_parameters(body.parameters)},
        outcome="ok",
        rows=outcome.row_count,
        duration_ms=round((time.monotonic() - started) * 1000, 2),
    )
    return outcome.public()


@router.get("/databases/{slug}/explain")
async def explain(
    slug: str,
    request: Request,
    query_text: str = Query(min_length=1, alias="query"),
    principal=Depends(authenticate_principal),
) -> dict[str, Any]:
    ctx = context_of(request)
    record = require_scope(ctx, principal, slug)
    return await _run(request, lambda: ctx.service.explain(record, query_text), admission=slug)


@router.post("/databases/{slug}/search/text")
async def search_text(
    slug: str, body: TextSearchRequest, request: Request, principal=Depends(authenticate_principal)
) -> dict[str, Any]:
    ctx = context_of(request)
    record = require_scope(ctx, principal, slug)
    started = time.monotonic()
    result = await _run(
        request,
        lambda: ctx.service.search_text(
            record,
            index=body.index,
            query=body.query,
            k=body.k,
            prefix=body.prefix,
            phrase=body.phrase,
            return_positions=body.return_positions,
            record_ids=body.record_ids,
            timeout_seconds=body.timeout_seconds,
        ),
        admission=slug,
    )
    ctx.store.audit(
        actor=principal.label,
        action="search_text",
        database_slug=slug,
        request={"index": body.index, "query": body.query[:200], "k": body.k},
        outcome="ok",
        rows=len(result["hits"]),
        duration_ms=round((time.monotonic() - started) * 1000, 2),
    )
    return result


@router.post("/databases/{slug}/search/vector")
async def search_vector(
    slug: str, body: VectorSearchRequest, request: Request, principal=Depends(authenticate_principal)
) -> dict[str, Any]:
    ctx = context_of(request)
    record = require_scope(ctx, principal, slug)
    started = time.monotonic()
    result = await _run(
        request,
        lambda: ctx.service.search_vector(
            record,
            space=body.space,
            vector=body.vector,
            k=body.k,
            table=body.table,
            table_kind=body.table_kind,
            record_ids=body.record_ids,
            timeout_seconds=body.timeout_seconds,
        ),
        admission=slug,
    )
    ctx.store.audit(
        actor=principal.label,
        action="search_vector",
        database_slug=slug,
        request={"space": body.space, "dimension": len(body.vector), "k": body.k},
        outcome="ok",
        rows=len(result["hits"]),
        duration_ms=round((time.monotonic() - started) * 1000, 2),
    )
    return result


@router.post("/databases/{slug}/search/hybrid")
async def search_hybrid(
    slug: str, body: HybridSearchRequest, request: Request, principal=Depends(authenticate_principal)
) -> dict[str, Any]:
    ctx = context_of(request)
    record = require_scope(ctx, principal, slug)
    result = await _run(
        request,
        lambda: ctx.service.search_hybrid(
            record,
            table=body.table,
            index=body.index,
            query=body.query,
            space=body.space,
            vector=body.vector,
            k=body.k,
            lexical_weight=body.lexical_weight,
            vector_weight=body.vector_weight,
            candidate_k=body.candidate_k,
            allow_partial=body.allow_partial,
            graph_relations=body.graph_relations,
            graph_weight=body.graph_weight,
            graph_filter=body.graph_filter,
            timeout_seconds=body.timeout_seconds,
        ),
        admission=slug,
    )
    ctx.store.audit(
        actor=principal.label,
        action="search_hybrid",
        database_slug=slug,
        request={"table": body.table, "index": body.index, "space": body.space},
        outcome="ok",
        rows=len(result["hits"]),
    )
    return result


@router.post("/databases/{slug}/graph")
async def graph(
    slug: str, body: GraphRequest, request: Request, principal=Depends(authenticate_principal)
) -> dict[str, Any]:
    ctx = context_of(request)
    record = require_scope(ctx, principal, slug)
    result = await _run(
        request,
        lambda: ctx.service.graph(
            record,
            mode=body.mode,
            node_tables=body.node_tables,
            relationship_tables=body.relationship_tables,
            weight_columns=body.weight_columns,
            cypher=body.query,
            parameters=body.parameters,
            max_nodes=body.max_nodes,
            max_edges=body.max_edges,
        ),
        admission=slug,
    )
    return result.public()


@router.get("/databases/{slug}/neighbors")
async def neighbors(
    slug: str,
    request: Request,
    table: str = Query(min_length=1, max_length=256),
    record_id: int = Query(ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    principal=Depends(authenticate_principal),
) -> dict[str, Any]:
    ctx = context_of(request)
    record = require_scope(ctx, principal, slug)
    result = await _run(
        request,
        lambda: ctx.service.neighbors(record, table=table, record_id=record_id, limit=limit),
        admission=slug,
    )
    return result.public()
