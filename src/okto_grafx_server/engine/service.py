"""The engine-facing operations every surface shares.

REST, WebSocket, MCP and the admin console all call into this service. There is one reason: the
access rules ("a key sees only the databases scoped to it", "writes need both toggles", "a
read-only registration really is read-only") must not be reimplemented per surface, and no surface
may hold a second, parallel authority over a store. Every call here is synchronous and blocking;
the API layer runs it through :class:`~okto_grafx_server.engine.runner.EngineRunner`.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from okto_grafx import connect
from okto_grafx.domain.vector.filter import RecordIdFilter
from okto_grafx.domain.query.hybrid import HybridSearchOptions
from okto_grafx.domain.query.control import CancellationToken
from okto_grafx.engine.database import Database
from okto_grafx.projections import ProjectionLimits, project_graph

from ..config import describe_path, resolve_existing_store
from ..control.store import ControlStore, DatabaseRecord
from ..errors import AppError, bad_request, conflict, forbidden, map_grafx_error, not_found
from .handles import HandlePool
from .values import (
    ValueCodec,
    decode_parameters,
    entity_id,
)

log = logging.getLogger("okto_grafx_server.service")

MAX_SELECT_NAMES = 256


@dataclass
class QueryOutcome:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    snapshot_lsn: int | None
    statistics: dict[str, Any]
    warnings: list[dict[str, Any]] = field(default_factory=list)
    plan: dict[str, Any] | None = None
    duration_ms: float = 0.0

    def public(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "statistics": self.statistics,
            "warnings": self.warnings,
            "duration_ms": self.duration_ms,
        }
        if self.snapshot_lsn is not None:
            body["snapshot_lsn"] = self.snapshot_lsn
        if self.plan is not None:
            body["plan"] = self.plan
        return body


@dataclass
class GraphOutcome:
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    shown_nodes: int
    total_nodes: int
    shown_edges: int
    total_edges: int
    truncated: bool
    snapshot_lsn: int | None
    database_uuid: str
    mode: str
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "nodes": self.nodes,
            "edges": self.edges,
            "shown_nodes": self.shown_nodes,
            "total_nodes": self.total_nodes,
            "shown_edges": self.shown_edges,
            "total_edges": self.total_edges,
            "truncated": self.truncated,
            "snapshot_lsn": self.snapshot_lsn,
            "database_uuid": self.database_uuid,
            "mode": self.mode,
            "warnings": self.warnings,
        }


class DatabaseService:
    """Store operations with the server's access rules applied once, here."""

    def __init__(
        self,
        *,
        store: ControlStore,
        pool: HandlePool,
        allowed_roots: Sequence[str],
        max_request_rows: int = 1000,
        max_graph_nodes: int = 2000,
        max_graph_edges: int = 5000,
        query_timeout_seconds: float = 30.0,
        max_query_value_characters: int = 65536,
    ) -> None:
        self.store = store
        self.pool = pool
        self.allowed_roots = [__import__("pathlib").Path(r) for r in allowed_roots]
        self.max_request_rows = max_request_rows
        self.max_graph_nodes = max_graph_nodes
        self.max_graph_edges = max_graph_edges
        self.query_timeout_seconds = query_timeout_seconds
        self.max_query_value_characters = max_query_value_characters

    # -- admission ---------------------------------------------------------------------

    def record(self, slug: str) -> DatabaseRecord:
        record = self.store.get_database(slug)
        if not record.enabled:
            raise forbidden(
                f"Database {slug!r} is registered but disabled.",
                code="database_disabled",
                details={"slug": slug},
            )
        return record

    def require_writes(self, record: DatabaseRecord, key_allows_writes: bool) -> None:
        if record.read_only:
            raise forbidden(
                f"Database {record.slug!r} is registered read-only. Clear read_only in its "
                "configuration to allow writes.",
                code="write_not_permitted",
                details={"slug": record.slug, "reason": "database_read_only"},
            )
        if not record.allow_writes:
            raise forbidden(
                f"Database {record.slug!r} does not allow writes. Its write toggle is off.",
                code="write_not_permitted",
                details={"slug": record.slug, "reason": "database_write_toggle_off"},
            )
        if not key_allows_writes:
            raise forbidden(
                "This API key is read-only. Create or update the key with allow_writes to write.",
                code="write_not_permitted",
                details={"reason": "key_read_only"},
            )

    def open_handle(
        self, record: DatabaseRecord, *, write: bool = False
    ):  # pragma: no cover - convenience wrapper
        if write:
            return self.pool.acquire_writer(record.slug, path=record.path, options=record.options)
        return self.pool.reader(record.slug, path=record.path, options=record.options)

    # -- registry ----------------------------------------------------------------------

    def inspect(self, raw_path: str) -> dict[str, Any]:
        """Facts about a candidate store. This opens and closes a handle briefly; it keeps none."""

        import pathlib

        candidate = pathlib.Path(raw_path)
        path = resolve_existing_store(candidate, tuple(self.allowed_roots))
        already = self.store.get_database_by_path(str(path))
        facts = describe_path(path)
        facts["already_registered_as"] = already.slug if already else None
        try:
            handle = connect(str(path), read_only=False)
        except Exception as exc:  # noqa: BLE001 - a typed refusal is the answer here
            mapped = map_grafx_error(exc)
            mapped.details = {**mapped.details, "path": str(path)}
            raise mapped from exc
        try:
            catalog = handle.catalog.catalog
            facts.update(
                {
                    "database_uuid": handle.identity.database_uuid.hex(),
                    "page_size": handle.identity.page_size,
                    "format_version": handle.identity.format_version,
                    "node_tables": sorted(
                        t.name for t in catalog.table_definitions if t.kind == "node"
                    ),
                    "relationship_tables": sorted(
                        t.name for t in catalog.table_definitions if t.kind == "rel"
                    ),
                    "vector_spaces": sorted(s.name for s in handle.vectors.spaces()),
                    "recovery_required": bool(handle.transactions.recovery_required),
                }
            )
            existing = self.store.get_database_by_uuid(handle.identity.database_uuid.hex())
            facts["uuid_registered_elsewhere"] = existing.slug if existing else None
        finally:
            handle.close()
        return facts

    def attach(
        self,
        *,
        raw_path: str,
        slug: str,
        name: str | None,
        read_only: bool,
        allow_writes: bool,
        options: dict[str, Any] | None,
        open_policy: str,
        note: str,
    ) -> DatabaseRecord:
        import pathlib

        if read_only and allow_writes:
            raise conflict(
                "A read-only registration cannot allow writes.", code="read_only_conflict"
            )
        path = pathlib.Path(raw_path)
        if str(path).startswith("~"):
            from ..config import PathPolicyError

            raise PathPolicyError("The server does not expand '~'; pass an absolute path.", value=str(path))
        path = resolve_existing_store(path, tuple(self.allowed_roots))
        facts = self.inspect(str(path))
        if facts["uuid_registered_elsewhere"]:
            raise conflict(
                f"A database with physical identity {facts['database_uuid']} is already "
                f"registered as {facts['uuid_registered_elsewhere']!r}.",
                code="database_uuid_taken",
            )
        return self.store.add_database(
            slug=slug,
            name=name or slug,
            path=str(path),
            database_uuid=facts["database_uuid"],
            format_version=facts.get("format_version"),
            read_only=read_only,
            allow_writes=allow_writes,
            options=options or {},
            open_policy=open_policy,
            note=note,
        )

    def candidate_roots(self) -> list[dict[str, Any]]:
        """The declared roots and their immediate Grafx-looking children."""


        out: list[dict[str, Any]] = []
        for root in self.allowed_roots:
            entry: dict[str, Any] = {"path": str(root), "exists": root.is_dir(), "stores": []}
            if entry["exists"]:
                try:
                    for child in sorted(root.iterdir()):
                        if child.is_dir() and (child / "grafx.meta").exists():
                            entry["stores"].append(
                                {"path": str(child), "name": child.name}
                            )
                except OSError as exc:  # pragma: no cover - permissions
                    entry["error"] = str(exc)[:200]
            out.append(entry)
        return out

    def open_database(self, record: DatabaseRecord) -> dict[str, Any]:
        spec = self.pool.spec_for(record.slug, record.path, record.options, record.read_only)
        info = self.pool.open(spec)
        if not record.database_uuid and info.get("database_uuid"):
            self.store.set_database_uuid(record.slug, info["database_uuid"])
        observed = info.get("format_version")
        if observed is not None and observed != record.format_version:
            self.store.update_database(record.slug, {"format_version": observed})
        return info

    def close_database(self, record: DatabaseRecord, *, force: bool = False) -> dict[str, Any]:
        self.pool.close(record.slug, force=force)
        return {"slug": record.slug, "open": False}

    # -- schema ------------------------------------------------------------------------

    def codec_for(self, db: Database, record: DatabaseRecord | None = None) -> ValueCodec:
        """A codec for a handle the caller already holds (the WebSocket session owns one)."""

        return self._codec(db, record)

    def _codec(self, db: Database, record: DatabaseRecord | None = None) -> ValueCodec:
        """A codec whose string cap is the store's own effective option.

        ``DatabaseConfig.max_query_value_characters`` is what the engine enforces internally; the
        JSON envelope must cut the same values the same way and say so, instead of shipping a
        longer string than the configured store budget.
        """

        catalog = db.catalog.catalog
        names = {t.table_id: t.name for t in catalog.table_definitions}
        # Whichever cap is tighter wins: the server's own ceiling, or the store's configured one.
        cap = self.max_query_value_characters
        if record is not None:
            configured = record.options.get("max_query_value_characters")
            if isinstance(configured, int) and configured > 0:
                cap = min(cap, configured)
        return ValueCodec(names, max_string_characters=cap)

    def schema(self, record: DatabaseRecord, *, diagnostics: bool = False) -> dict[str, Any]:
        with self.pool.reader(
            record.slug,
            path=record.path,
            options=record.options,
            read_only=record.read_only,
            diagnostics=diagnostics,
        ) as db:
            catalog = db.catalog.catalog
            tables = [_table_public(t) for t in catalog.table_definitions if t.kind == "node"]
            relations = [
                _relation_public(t) for t in catalog.table_definitions if t.kind == "rel"
            ]
            spaces = [_space_public(s) for s in db.vectors.spaces()]
            return {
                "slug": record.slug,
                "database_uuid": db.identity.database_uuid.hex(),
                "read_only": bool(db.read_only),
                "tables": tables,
                "relationships": relations,
                "vector_spaces": spaces,
            }

    def index_inventory(self, record: DatabaseRecord, *, diagnostics: bool = False) -> dict[str, Any]:
        """What the store currently reports about itself.

        `diagnostics` lets the administrator's view (and the maintenance job's own report) read a
        store that the pool has quarantined — that view is how the operator finds the recovery
        action, so it may not be the thing that fails.
        """

        with self.pool.reader(
            record.slug,
            path=record.path,
            options=record.options,
            read_only=record.read_only,
            diagnostics=diagnostics,
        ) as db:
            return {
                "slug": record.slug,
                "indexes": [_index_public(i) for i in db.indexes.indexes()],
                "vector_indexes": [_vector_index_public(i) for i in db.vectors.indexes()],
                "stale": [_index_public(i) for i in db.stale_indexes]
                if isinstance(db.stale_indexes, (list, tuple))
                else [],
                "maintenance": _plain(db.maintenance.status()),
            }

    # -- query -------------------------------------------------------------------------

    def query(
        self,
        record: DatabaseRecord,
        *,
        cypher: str,
        parameters: Mapping[str, Any] | None = None,
        limit: int | None = None,
        timeout_seconds: float | None = None,
        explain: bool = False,
    ) -> QueryOutcome:
        statement = _require_text(cypher, "query")
        bound = max(0, min(int(limit or self.max_request_rows), self.max_request_rows))
        params = decode_parameters(parameters)
        timeout = timeout_seconds or self.query_timeout_seconds
        started = time.monotonic()
        with self.pool.reader(
            record.slug, path=record.path, options=record.options, read_only=record.read_only
        ) as db:
            codec = self._codec(db, record)
            with db.begin("read") as txn:
                try:
                    result = txn.execute(statement, params, timeout_seconds=timeout)
                except Exception as exc:  # noqa: BLE001 - classify before mapping
                    raise self._refuse_write_on_read_door(record.slug, exc) from exc
                snapshot = getattr(txn, "snapshot", None)
                lsn = int(getattr(snapshot, "read_lsn", 0) or 0) or None
                rows = list(result.rows)
                truncated = len(rows) > bound
                kept = rows[:bound]
                encoded, warnings = codec.encode_rows(kept, result.columns)
                statistics = dict(result.statistics or {})
                plan = plan_to_dict(result.plan) if (explain and result.plan is not None) else None
        return QueryOutcome(
            columns=[str(c) for c in (result.columns or ())],
            rows=encoded,
            row_count=len(encoded),
            truncated=truncated,
            snapshot_lsn=lsn,
            statistics=statistics,
            warnings=warnings.items,
            plan=plan,
            duration_ms=round((time.monotonic() - started) * 1000.0, 2),
        )

    def _refuse_write_on_read_door(self, slug: str, exc: BaseException) -> AppError:
        """``execute`` on a read transaction refuses a write plan; that is a 403, not a fault.

        Classified by exception class, never by message text: the engine documents that a read
        transaction rejects write plans before execution, so this door can name the door the client
        actually needs.
        """

        from okto_grafx.errors import GrafxTransactionStateError

        if isinstance(exc, GrafxTransactionStateError):
            return AppError(
                f"Database {slug!r}: this is the read door. A write statement must go through a "
                "WebSocket session opened with mode='write' on a key and database whose write "
                "toggles are both on.",
                status=403,
                code="write_not_permitted",
                details={"reason": "read_only_door", "slug": slug},
            )
        return map_grafx_error(exc)

    def explain(self, record: DatabaseRecord, cypher: str) -> dict[str, Any]:
        statement = _require_text(cypher, "query")
        with self.pool.reader(
            record.slug, path=record.path, options=record.options, read_only=record.read_only
        ) as db:
            return plan_to_dict(db.explain(statement))

    # -- search ------------------------------------------------------------------------

    def search_text(
        self,
        record: DatabaseRecord,
        *,
        index: str,
        query: str,
        k: int = 20,
        prefix: bool = False,
        phrase: bool = False,
        return_positions: bool = False,
        record_ids: Sequence[int] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        with self.pool.reader(
            record.slug, path=record.path, options=record.options, read_only=record.read_only
        ) as db:
            table = self._text_index_table(db, index)
            with db.begin("read") as reader:
                result = db.search_text(
                    reader,
                    index=_require_text(index, "index"),
                    query=_require_text(query, "query"),
                    k=_bounded_k(k),
                    prefix=bool(prefix),
                    phrase=bool(phrase),
                    return_positions=bool(return_positions),
                    filter=_record_filter(record_ids),
                    timeout_seconds=timeout_seconds or self.query_timeout_seconds,
                )
            hits = [
                {
                    "table": table,
                    "record_id": hit.record_id,
                    "id": entity_id(table, hit.record_id),
                    "score": hit.score,
                    "matched_fields": list(hit.matched_fields),
                    "matched_terms": list(hit.matched_terms),
                    "positions": [
                        {
                            "field": p.field,
                            "term": p.term,
                            "positions": list(p.positions),
                        }
                        for p in hit.positions
                    ],
                }
                for hit in result.hits
            ]
            return {
                "hits": hits,
                "regime": result.regime,
                "candidates": result.candidates,
                "postings_visited": result.postings_visited,
                "corpus_documents": result.corpus_documents,
                "index_built_through_commit": result.index_built_through_commit,
                "snapshot_commit": result.snapshot_commit,
                "statistics_regime": result.statistics_regime,
                "table": table,
            }

    def search_vector(
        self,
        record: DatabaseRecord,
        *,
        space: str,
        vector: Sequence[float],
        k: int = 20,
        table: str | None = None,
        table_kind: str | None = None,
        record_ids: Sequence[int] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        values = _require_vector(vector)
        selector = _selector(table, table_kind)
        with self.pool.reader(
            record.slug, path=record.path, options=record.options, read_only=record.read_only
        ) as db:
            owner = self._vector_owner(db, space, selector)
            with db.begin("read") as reader:
                result = db.search_vectors(
                    reader,
                    space=_require_text(space, "space"),
                    query=values,
                    k=_bounded_k(k),
                    candidate_filter=_record_filter(record_ids),
                    table=selector,
                    timeout_seconds=timeout_seconds or self.query_timeout_seconds,
                )
            hits = [
                {
                    "table": owner,
                    "record_id": hit.record_id,
                    "id": entity_id(owner, hit.record_id),
                    "score": hit.score,
                    "retired": bool(hit.retired),
                }
                for hit in result.hits
            ]
            return {
                "hits": hits,
                "regime": result.regime,
                "space": result.space,
                "table": owner,
                "requested_k": result.requested_k,
                "achieved_k": result.achieved_k,
            }

    def search_hybrid(
        self,
        record: DatabaseRecord,
        *,
        table: str,
        index: str | None,
        query: str,
        space: str | None,
        vector: Sequence[float],
        k: int = 20,
        lexical_weight: float = 1.0,
        vector_weight: float = 1.0,
        candidate_k: int = 20,
        allow_partial: bool = False,
        graph_relations: Sequence[str] = (),
        graph_weight: float = 0.0,
        graph_filter: bool = False,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        if not index and not space:
            raise bad_request(
                "Hybrid search needs at least one of index or space.",
                code="invalid_hybrid_request",
            )
        options = HybridSearchOptions(
            lexical_weight=float(lexical_weight),
            vector_weight=float(vector_weight),
            candidate_k=_bounded_k(candidate_k),
            allow_partial=bool(allow_partial),
            graph_relations=tuple(graph_relations),
            graph_weight=float(graph_weight),
            graph_filter=bool(graph_filter),
        )
        with self.pool.reader(
            record.slug, path=record.path, options=record.options, read_only=record.read_only
        ) as db:
            with db.begin("read") as reader:
                result = db.search_hybrid(
                    reader,
                    table=_require_text(table, "table"),
                    index=index,
                    query=_require_text(query, "query"),
                    space=space,
                    vector=_require_vector(vector, allow_empty=index is not None),
                    k=_bounded_k(k),
                    options=options,
                    timeout_seconds=timeout_seconds or self.query_timeout_seconds,
                )
        hits = [
            {
                "table": hit.table,
                "record_id": hit.record_id,
                "id": entity_id(hit.table, hit.record_id),
                "score": hit.score,
                "lexical_rank": hit.lexical_rank,
                "lexical_score": hit.lexical_score,
                "vector_rank": hit.vector_rank,
                "vector_score": hit.vector_score,
                "graph_distance": hit.graph_distance,
            }
            for hit in result.hits
        ]
        return {
            "hits": hits,
            "regime": result.regime,
            "fusion": result.fusion,
            "lexical_regime": result.lexical_regime,
            "vector_regime": result.vector_regime,
            "snapshot_commit": result.snapshot_commit,
            "source_errors": list(result.source_errors),
        }

    # -- graph -------------------------------------------------------------------------

    def graph(
        self,
        record: DatabaseRecord,
        *,
        mode: str = "projection",
        node_tables: Sequence[str] = (),
        relationship_tables: Sequence[str] = (),
        weight_columns: Mapping[str, str] | None = None,
        cypher: str | None = None,
        parameters: Mapping[str, Any] | None = None,
        max_nodes: int | None = None,
        max_edges: int | None = None,
    ) -> GraphOutcome:
        cap_nodes = max(1, min(int(max_nodes or self.max_graph_nodes), self.max_graph_nodes))
        cap_edges = max(1, min(int(max_edges or self.max_graph_edges), self.max_graph_edges))
        if mode == "query":
            outcome = self.query(
                record,
                cypher=cypher or "",
                parameters=parameters,
                limit=self.max_request_rows,
            )
            return self._graph_from_rows(record, outcome, cap_nodes, cap_edges)
        if mode != "projection":
            raise bad_request(
                "graph mode must be 'projection' or 'query'.",
                code="invalid_graph_mode",
                field="mode",
            )
        return self._graph_projection(
            record,
            node_tables=node_tables,
            relationship_tables=relationship_tables,
            weight_columns=dict(weight_columns or {}),
            cap_nodes=cap_nodes,
            cap_edges=cap_edges,
        )

    def _graph_projection(
        self,
        record: DatabaseRecord,
        *,
        node_tables: Sequence[str],
        relationship_tables: Sequence[str],
        weight_columns: dict[str, str],
        cap_nodes: int,
        cap_edges: int,
    ) -> GraphOutcome:
        if not node_tables:
            raise bad_request(
                "Projection mode needs at least one node table.",
                code="graph_selection_required",
                field="node_tables",
            )
        _check_selection(node_tables, "node_tables")
        _check_selection(relationship_tables, "relationship_tables")
        with self.pool.reader(
            record.slug, path=record.path, options=record.options, read_only=record.read_only
        ) as db:
            self._check_endpoints(db, tuple(node_tables), tuple(relationship_tables))
            try:
                projection = project_graph(
                    db,
                    node_tables=tuple(node_tables),
                    relationship_tables=tuple(relationship_tables),
                    limits=ProjectionLimits(max_nodes=cap_nodes, max_edges=cap_edges),
                    weight_columns=weight_columns or None,
                    timeout_seconds=self.query_timeout_seconds,
                )
            except AppError:
                raise
            except Exception as exc:  # noqa: BLE001 - typed budget refusal
                mapped = map_grafx_error(exc)
                if mapped.status == 413:
                    mapped.message = (
                        f"{mapped.message} The projection refused to silently truncate: narrow "
                        "the selected tables, lower the caps, or use mode='query' with an "
                        "explicit LIMIT."
                    )
                    mapped.details = {
                        **mapped.details,
                        "max_graph_nodes": self.max_graph_nodes,
                        "max_graph_edges": self.max_graph_edges,
                    }
                raise mapped from exc
            node_ids = [entity_id(node.table, node.record_id) for node in projection.nodes]
            nodes = [
                {
                    "id": node_ids[index],
                    # The physical table is the label; ``id`` stays the composite identity.
                    "label": node.table,
                    "table": node.table,
                    "record_id": node.record_id,
                    "category": node.table,
                    # A ProjectionNode carries identity only: topology mode has no properties.
                    "properties": {},
                }
                for index, node in enumerate(projection.nodes)
            ]
            weights = tuple(projection.weights or ())
            edges = []
            for index, edge in enumerate(projection.edges):
                edges.append(
                    {
                        "id": entity_id(edge.table, edge.record_id),
                        "source": node_ids[edge.source],
                        "target": node_ids[edge.target],
                        "label": edge.table,
                        "table": edge.table,
                        "record_id": edge.record_id,
                        "properties": (
                            {"weight": weights[index]} if index < len(weights) else {}
                        ),
                    }
                )
            return GraphOutcome(
                nodes=nodes,
                edges=edges,
                shown_nodes=len(nodes),
                total_nodes=len(nodes),
                shown_edges=len(edges),
                total_edges=len(edges),
                truncated=False,
                snapshot_lsn=projection.snapshot_lsn,
                database_uuid=projection.database_uuid.hex(),
                mode="projection",
            )

    def _graph_from_rows(
        self,
        record: DatabaseRecord,
        outcome: QueryOutcome,
        cap_nodes: int,
        cap_edges: int,
    ) -> GraphOutcome:
        with self.pool.reader(
            record.slug, path=record.path, options=record.options, read_only=record.read_only
        ) as db:
            uuid_hex = db.identity.database_uuid.hex()
        # Re-extract from the encoded rows: entities survive as tagged objects.
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        seen_nodes: set[str] = set()
        seen_edges: set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                kind = value.get("kind")
                if kind == "node" and value["id"] not in seen_nodes:
                    seen_nodes.add(value["id"])
                    nodes.append(
                        {
                            "id": value["id"],
                            "label": value.get("label") or value["id"],
                            "table": value.get("table"),
                            "record_id": value.get("record_id"),
                            "category": value.get("table") or "node",
                            "properties": value.get("properties") or {},
                        }
                    )
                elif kind == "relationship" and value["id"] not in seen_edges:
                    seen_edges.add(value["id"])
                    edges.append(
                        {
                            "id": value["id"],
                            "source": value.get("source"),
                            "target": value.get("target"),
                            "label": value.get("label") or value["id"],
                            "table": value.get("table"),
                            "record_id": value.get("record_id"),
                            "properties": value.get("properties") or {},
                        }
                    )
                for item in value.values():
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        for row in outcome.rows:
            for cell in row:
                visit(cell)
        truncated = len(nodes) > cap_nodes or len(edges) > cap_edges or outcome.truncated
        shown_nodes = nodes[:cap_nodes]
        shown_edges = edges[:cap_edges]

        # A statement may return a relationship without returning its endpoints
        # (`MATCH ()-[r]->() RETURN r`). The edge is still true, so it is kept and its endpoints
        # are added as bare nodes — with a warning saying their properties were never returned,
        # rather than dropping the edge or pretending the node has no properties.
        known = {node["id"] for node in shown_nodes}
        stubbed: list[dict[str, Any]] = []
        for edge in shown_edges:
            for endpoint in (edge["source"], edge["target"]):
                if endpoint in known or endpoint is None:
                    continue
                if len(shown_nodes) >= cap_nodes:
                    # The caller's node cap wins: an edge that does not fit is dropped below.
                    continue
                table, _, record_id = str(endpoint).rpartition(":")
                shown_nodes.append(
                    {
                        "id": endpoint,
                        "label": table,
                        "table": table or None,
                        "record_id": int(record_id) if record_id.isdigit() else None,
                        "category": table or "node",
                        "properties": {},
                    }
                )
                known.add(endpoint)
                stubbed.append({"kind": "endpoint_not_returned", "id": endpoint})

        # An endpoint that was cut by the node cap drops its edge: a dangling edge would be a lie.
        shown_edges = [
            edge
            for edge in shown_edges
            if edge["source"] in known and edge["target"] in known
        ]
        warnings = [*outcome.warnings, *stubbed]
        return GraphOutcome(
            nodes=shown_nodes,
            edges=shown_edges,
            shown_nodes=len(shown_nodes),
            total_nodes=len(nodes),
            shown_edges=len(shown_edges),
            total_edges=len(edges),
            truncated=bool(truncated),
            snapshot_lsn=outcome.snapshot_lsn,
            database_uuid=uuid_hex,
            mode="query",
            warnings=warnings,
        )

    def neighbors(
        self, record: DatabaseRecord, *, table: str, record_id: int, limit: int = 100
    ) -> GraphOutcome:
        """One hop around a node, built from a bounded projection (there is no ``id()`` in the
        query language, so physical identity is resolved through the scan cursor, not Cypher)."""

        cap = max(1, min(int(limit), self.max_graph_nodes))
        with self.pool.reader(
            record.slug, path=record.path, options=record.options, read_only=record.read_only
        ) as db:
            codec = self._codec(db, record)
            tables = db.catalog.catalog.table_definitions
            node_names = {t.name for t in tables if t.kind == "node"}
            if table not in node_names:
                raise not_found(
                    f"No node table {table!r} in {record.slug!r}.", field="table"
                )
            rel_tables = [t.name for t in tables if t.kind == "rel" and table in {t.from_table, t.to_table}]
            other_nodes = {
                name
                for rel in tables
                if rel.name in rel_tables
                for name in (rel.from_table, rel.to_table)
                if name
            }
            selection = sorted(other_nodes | {table})
            projection = project_graph(
                db,
                node_tables=tuple(selection),
                relationship_tables=tuple(rel_tables),
                limits=ProjectionLimits(
                    max_nodes=self.max_graph_nodes, max_edges=self.max_graph_edges
                ),
                timeout_seconds=self.query_timeout_seconds,
            )
            wanted = (table, int(record_id))
            try:
                anchor = next(
                    index
                    for index, node in enumerate(projection.nodes)
                    if (node.table, node.record_id) == wanted
                )
            except StopIteration:
                raise not_found(
                    f"No node {table}:{record_id} in the captured snapshot.",
                    code="node_not_found",
                ) from None
            # ``total_*`` describes the one-hop neighbourhood the caller asked for, not the whole
            # store: a UI paging through edges needs to know how many edges this node actually has.
            incident_all = [
                edge
                for edge in projection.edges
                if edge.source == anchor or edge.target == anchor
            ]
            incident = incident_all[:cap]
            wanted_nodes = {anchor}
            for edge in incident:
                wanted_nodes.update({edge.source, edge.target})
            neighbourhood = {anchor}
            for edge in incident_all:
                neighbourhood.update({edge.source, edge.target})
            properties, property_warnings = self._properties_for(
                db, projection, wanted_nodes, codec, scan_cap=max(cap, self.max_graph_nodes)
            )
            nodes = []
            id_of_index: dict[int, str] = {}
            for index in sorted(wanted_nodes):
                captured = projection.nodes[index]
                node_id = entity_id(captured.table, captured.record_id)
                id_of_index[index] = node_id
                nodes.append(
                    {
                        "id": node_id,
                        "label": captured.table,
                        "table": captured.table,
                        "record_id": captured.record_id,
                        "category": captured.table,
                        # An incremental expansion is only useful if the panel has something to
                        # show, so the few returned nodes are read back through a capped scan.
                        "properties": properties.get(index, {}),
                    }
                )
            edges = [
                {
                    "id": entity_id(edge.table, edge.record_id),
                    "source": id_of_index[edge.source],
                    "target": id_of_index[edge.target],
                    "label": edge.table,
                    "table": edge.table,
                    "record_id": edge.record_id,
                    "properties": {},
                }
                for edge in incident
            ]
            return GraphOutcome(
                nodes=nodes,
                edges=edges,
                shown_nodes=len(nodes),
                total_nodes=len(neighbourhood),
                shown_edges=len(edges),
                total_edges=len(incident_all),
                truncated=len(incident_all) > len(incident),
                snapshot_lsn=projection.snapshot_lsn,
                database_uuid=projection.database_uuid.hex(),
                mode="neighbors",
                warnings=property_warnings,
            )

    def _properties_for(
        self,
        db: Database,
        projection: Any,
        wanted: set[int],
        codec: ValueCodec,
        *,
        scan_cap: int,
    ) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
        """Read back the properties of a handful of nodes by scanning their tables once, capped.

        The projection carries identity only, and the query language has no ``id()`` to look a node
        up by physical identity — so the only honest way to enrich a neighborhood is a bounded scan
        per involved table. Anything not reached inside the cap is reported as a warning rather than
        returned as a node that looks like it has no properties.
        """

        by_table: dict[str, set[int]] = {}
        index_of: dict[tuple[str, int], int] = {}
        for index in wanted:
            node = projection.nodes[index]
            by_table.setdefault(node.table, set()).add(node.record_id)
            index_of[(node.table, node.record_id)] = index

        found: dict[int, dict[str, Any]] = {}
        warnings: list[dict[str, Any]] = []
        for table, record_ids in sorted(by_table.items()):
            if "`" in table:
                warnings.append(
                    {"kind": "properties_not_read", "table": table, "reason": "unquotable_name"}
                )
                continue
            remaining = set(record_ids)
            statement = f"MATCH (n:`{table}`) RETURN n LIMIT {int(scan_cap)}"
            try:
                with db.begin("read") as txn:
                    result = txn.execute(statement, {}, timeout_seconds=self.query_timeout_seconds)
                    encoded, _row_warnings = codec.encode_rows(result.rows, result.columns)
                for values in encoded:
                    value = values[0] if values else None
                    if not isinstance(value, dict) or value.get("kind") != "node":
                        continue
                    key = (str(value.get("table")), int(value.get("record_id", -1)))
                    index = index_of.get(key)
                    if index is not None and index not in found:
                        found[index] = value.get("properties") or {}
                        remaining.discard(key[1])
                        if not remaining:
                            break
            except Exception:  # noqa: BLE001 - a preview that cannot read is still a preview
                remaining = set(record_ids)
            for record_id in sorted(remaining):
                warnings.append(
                    {
                        "kind": "properties_not_read",
                        "table": table,
                        "record_id": record_id,
                        "scanned_rows": int(scan_cap),
                        "hint": "the capped scan did not reach this node; ask for the node "
                        "through /query with an explicit WHERE on its primary key",
                    }
                )
        return found, warnings

    # -- DDL (admin only) --------------------------------------------------------------

    def ddl(
        self, record: DatabaseRecord, statements: Sequence[str], *, key_allows_writes: bool
    ) -> dict[str, Any]:
        self.require_writes(record, key_allows_writes)
        scripts = [s.strip() for s in statements if s and s.strip()]
        if not scripts:
            raise bad_request("At least one DDL statement is required.", code="empty_ddl")
        if len(scripts) > 64:
            raise bad_request("A DDL call accepts at most 64 statements.", code="too_many_statements")
        release = self.pool.acquire_writer(
            record.slug, path=record.path, options=record.options
        )
        handle: Database = release.handle  # type: ignore[attr-defined]
        try:
            with handle.begin("write") as txn:
                executed = []
                for statement in scripts:
                    result = txn.execute(statement)
                    executed.append(
                        {
                            "statement": statement,
                            "rows": len(result.rows),
                            "statistics": dict(result.statistics or {}),
                        }
                    )
                commit = txn.commit()
            return {
                "statements": executed,
                "commit": {"csn": commit.csn, "durable": bool(commit.durable), "wrote": commit.wrote},
                "schema": self.schema(record),
            }
        finally:
            release()

    def ensure_identity_indexes(
        self, record: DatabaseRecord, *, key_allows_writes: bool
    ) -> dict[str, Any]:
        """Open the one-way catalog door the library offers: activate identity access paths.

        ``ensure_identity_indexes`` is the library's explicit v1→v2 catalog door. It is worth
        exposing because it is safe to repeat — once every required generation is active it performs
        no durable write — and because it is refused by a read-only handle, so it has to travel on
        the same write rules as DDL rather than quietly on a GET.
        """

        self.require_writes(record, key_allows_writes)
        release = self.pool.acquire_writer(record.slug, path=record.path, options=record.options)
        handle: Database = release.handle  # type: ignore[attr-defined]
        before = {index.name for index in handle.indexes.indexes()}
        try:
            handle.ensure_identity_indexes()
        finally:
            release()
        after = self.index_inventory(record, diagnostics=True)
        names = {entry["name"] for entry in after["indexes"]} | {
            entry["name"] for entry in after["vector_indexes"]
        }
        return {
            "activated": sorted(names - before),
            "already_present": sorted(before & names),
            "indexes": after,
        }

    def write_one(
        self,
        record: DatabaseRecord,
        *,
        cypher: str,
        parameters: Mapping[str, Any] | None = None,
        key_allows_writes: bool,
    ) -> dict[str, Any]:
        """One statement, one write transaction, one durable answer.

        There is no automatic retry here. A conflict is returned as a conflict so the caller
        recomputes from a fresh snapshot, and a failure after the durability barrier is reported as
        an unknown outcome rather than replayed.
        """

        self.require_writes(record, key_allows_writes)
        statement = _require_text(cypher, "query")
        params = decode_parameters(parameters)
        release = self.pool.acquire_writer(record.slug, path=record.path, options=record.options)
        handle: Database = release.handle  # type: ignore[attr-defined]
        try:
            with handle.begin("write") as txn:
                # No deadline here: Grafx 0.0.8 accepts execution control on read
                # transactions only, and refusing the call would be worse than not claiming it.
                result = txn.execute(statement, params)
                report = txn.commit()
            return {
                "columns": [str(c) for c in (result.columns or ())],
                "row_count": len(result.rows),
                "statistics": dict(result.statistics or {}),
                "commit": {
                    "csn": report.csn,
                    "durable": bool(report.durable),
                    "wrote": report.wrote,
                },
            }
        finally:
            release()

    # -- helpers -----------------------------------------------------------------------

    def _text_index_table(self, db: Database, index: str) -> str | None:
        try:
            view = db.indexes.index(index)
        except Exception as exc:  # noqa: BLE001 - typed refusal
            raise map_grafx_error(exc) from exc
        return view.definition.table_name

    def _vector_owner(self, db: Database, space: str, selector) -> str | None:
        if selector is not None:
            name, kind = selector if isinstance(selector, tuple) else (selector, "node")
            return name
        indexes = [i for i in db.vectors.indexes() if i.space_name == space]
        if not indexes:
            raise not_found(
                f"No vector index is bound to space {space!r}.", code="unknown_vector_space"
            )
        names = {t.table_id: t.name for t in db.catalog.catalog.table_definitions}
        if len(indexes) > 1:
            owners = sorted(
                str(names.get(index.table_id, f"table {index.table_id}")) for index in indexes
            )
            raise conflict(
                f"The embedding space {space!r} has {len(indexes)} physical owners "
                f"({', '.join(owners)}). Say which one you mean with 'table' (and 'table_kind' "
                "when a node and a relationship share the spelling) — this server will not "
                "search the index that happens to sort first.",
                code="ambiguous_vector_owner",
                details={"space": space, "owners": owners},
            )
        return names.get(indexes[0].table_id)

    def _check_endpoints(
        self, db: Database, node_tables: tuple[str, ...], relationship_tables: tuple[str, ...]
    ) -> None:
        tables = db.catalog.catalog.table_definitions
        nodes = {t.name for t in tables if t.kind == "node"}
        rels = {t.name: t for t in tables if t.kind == "rel"}
        missing_nodes = [t for t in node_tables if t not in nodes]
        if missing_nodes:
            raise not_found(
                f"Unknown node table(s): {', '.join(missing_nodes)}.", field="node_tables"
            )
        for name in relationship_tables:
            rel = rels.get(name)
            if rel is None:
                raise not_found(f"Unknown relationship table {name!r}.", field="relationship_tables")
            endpoints = {rel.from_table, rel.to_table} - {None}
            missing = sorted(endpoints - set(node_tables))
            if missing:
                raise bad_request(
                    f"Relationship {name!r} needs its endpoint table(s) {', '.join(missing)} in "
                    "node_tables; an unselected endpoint is not followed implicitly.",
                    code="graph_endpoints_missing",
                    details={"relationship": name, "missing": missing},
                )


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise bad_request(
            f"{field!r} must be a non-empty string.", code="invalid_request", field=field
        )
    return value


def _require_vector(values: Any, *, allow_empty: bool = False) -> list[float]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise bad_request(
            "A search vector must be a JSON array of numbers.",
            code="invalid_vector",
            field="vector",
        )
    if not values and not allow_empty:
        raise bad_request(
            "A search vector must not be empty. The server does not generate embeddings.",
            code="invalid_vector",
            field="vector",
        )
    out: list[float] = []
    for item in values:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise bad_request(
                "A search vector must contain only numbers.", code="invalid_vector", field="vector"
            )
        number = float(item)
        if math.isnan(number) or math.isinf(number):
            raise bad_request(
                "A search vector must contain only finite numbers.",
                code="invalid_vector",
                field="vector",
            )
        out.append(number)
    return out


def _bounded_k(k: int) -> int:
    try:
        value = int(k)
    except (TypeError, ValueError):
        raise bad_request("'k' must be an integer.", code="invalid_request", field="k") from None
    if not 1 <= value <= 1000:
        raise bad_request(
            "'k' must be between 1 and 1000.", code="invalid_request", field="k"
        )
    return value


def _selector(table: Any, kind: Any):
    if table is None:
        if kind is not None:
            raise bad_request(
                "'table_kind' requires 'table'.", code="invalid_request", field="table_kind"
            )
        return None
    name = _require_text(table, "table")
    if kind is None:
        return name
    if kind not in {"node", "rel"}:
        raise bad_request(
            "'table_kind' must be 'node' or 'rel'.", code="invalid_request", field="table_kind"
        )
    return (kind, name)


def _record_filter(record_ids: Sequence[int] | None) -> RecordIdFilter | None:
    if record_ids is None:
        return None
    ids = [int(v) for v in record_ids]
    return RecordIdFilter.of(ids)


def _check_selection(names: Sequence[str], field: str) -> None:
    if len(names) > MAX_SELECT_NAMES:
        raise bad_request(
            f"{field} accepts at most {MAX_SELECT_NAMES} distinct names.",
            code="invalid_request",
            field=field,
        )
    for name in names:
        if not isinstance(name, str) or not name or len(name) > 256:
            raise bad_request(
                f"{field} must contain table names of at most 256 characters.",
                code="invalid_request",
                field=field,
            )


def _table_public(table: Any) -> dict[str, Any]:
    return {
        "table_id": table.table_id,
        "name": table.name,
        "kind": table.kind,
        "primary_key": table.primary_key,
        "schema_version": table.schema_version,
        "flexible_properties": bool(table.flexible_properties),
        "unlabeled": bool(table.unlabeled),
        "columns": [
            {
                "name": c.name,
                "type": _type_name(c.type),
                "nullable": bool(c.nullable),
                "vector_space": c.vector_space,
                "decimal_precision": c.decimal_precision,
                "decimal_scale": c.decimal_scale,
                "stored_type": str(c.stored_type) if c.stored_type is not None else None,
            }
            for c in table.columns
        ],
    }


def _relation_public(rel: Any) -> dict[str, Any]:
    return {
        "table_id": rel.table_id,
        "name": rel.name,
        "kind": rel.kind,
        "from_table": rel.from_table,
        "to_table": rel.to_table,
        "schema_version": rel.schema_version,
        "columns": [
            {
                "name": c.name,
                "type": _type_name(c.type),
                "nullable": bool(c.nullable),
                "vector_space": c.vector_space,
            }
            for c in rel.columns
            if c.name not in {"_from", "_to"}
        ],
    }


def _space_public(space: Any) -> dict[str, Any]:
    return {
        "space_id": space.space_id,
        "name": space.name,
        "dimension": space.dimension,
        "metric": _type_name(space.metric),
        "normalized": bool(space.normalized),
        "storage_dtype": space.storage_dtype,
        "state": space.state,
    }


def _index_public(view: Any) -> dict[str, Any]:
    return {
        "name": view.name,
        "table": view.definition.table_name,
        "columns": list(view.columns),
        "kind": _index_kind(view),
        "layout": _type_name(view.definition.layout),
        "visibility": _type_name(view.visibility),
        "bucket_count": view.definition.bucket_count,
        "stale": bool(view.stale),
        "stale_reason": view.stale_reason,
        "built_through_lsn": view.built_through_lsn,
        "missing_targets": view.missing_targets,
        "automatic": view.automatic,
        "expected_cardinality": view.expected_cardinality,
    }


def _index_kind(view: Any) -> str:
    """Say what an index *is*, from what the engine recorded rather than from its name.

    The search panel has to offer the full-text indexes of a store, and the index inventory is also
    where an operator decides what is worth rebuilding. A name prefix would work until the library
    renamed something; the key derivation and the visibility are the facts the index was built with.
    """

    derivation = str(getattr(view.definition, "key_derivation", "") or "")
    if derivation.startswith("fulltext"):
        return "text"
    if derivation.startswith("vector_digest") or _type_name(view.visibility) == "PROXIMITY":
        return "vector"
    return "exact"


def _vector_index_public(view: Any) -> dict[str, Any]:
    return {
        "name": view.name,
        "space": view.space_name,
        "table_id": view.table_id,
        "dimension": view.dimension,
        "metric": _type_name(view.metric_of_space),
        "storage_dtype": view.storage_dtype,
        "ef_search": view.ef_search,
        "stale": bool(view.stale),
        "stale_reason": view.stale_reason,
        "built_through_lsn": view.built_through_lsn,
    }


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    return str(value)


def _plain(value: Any) -> Any:
    from .jobs import describe_report

    return describe_report(value) if value is not None else None


def plan_to_dict(plan: Any) -> dict[str, Any]:
    """A readable, bounded view of a plan snapshot (the UI shows shape, not engine internals)."""

    if plan is None:
        return {}
    import dataclasses

    def walk(node: Any, depth: int) -> dict[str, Any] | str:
        if depth > 12:
            return "…"
        if node is None:
            return "null"
        if isinstance(node, (str, int, float, bool)):
            return node
        if isinstance(node, (list, tuple)):
            return {"_list": [walk(item, depth + 1) for item in list(node)[:16]]}  # type: ignore[return-value]
        if dataclasses.is_dataclass(node) and not isinstance(node, type):
            out: dict[str, Any] = {"operator": type(node).__name__}
            for f in dataclasses.fields(node):
                value = getattr(node, f.name, None)
                if value is None or isinstance(value, (str, int, float, bool)):
                    out[f.name] = value
                elif f.name in {"table", "from_table", "to_table"} and hasattr(value, "name"):
                    out[f.name] = getattr(value, "name")
                elif isinstance(value, (list, tuple)):
                    out[f.name] = [walk(item, depth + 1) for item in list(value)[:16]]
                elif dataclasses.is_dataclass(value) and not isinstance(value, type):
                    if depth >= 8:
                        out[f.name] = type(value).__name__
                    else:
                        child = walk(value, depth + 1)
                        out[f.name] = child
                else:
                    out[f.name] = str(value)[:120]
            return out
        if hasattr(node, "name") and not dataclasses.is_dataclass(node):
            return {"operator": str(getattr(node, "name"))}
        return str(node)[:200]

    walked = walk(plan, 0)
    return walked if isinstance(walked, dict) else {"operator": str(walked)}


def make_cancellation_token() -> CancellationToken:
    return CancellationToken()
