"""Grafx values ⇄ JSON.

The library hands back detached DTOs with native types that JSON cannot hold: exact decimals,
microsecond timestamps, vectors with a space reference, graph entities with *physical* identity,
and ``mappingproxy`` property maps. This module is the single place where that becomes a document.

Two rules govern it:

* nothing is silently flattened. A ``DecimalValue`` is never emitted as a float, a vector is never
  emitted as a plain list without its dtype, and a non-string map key is flagged rather than
  stringified into a collision;
* nothing is silently lost. Anything the encoder had to give up (a non-finite float that JSON has
  no symbol for, a string above the character cap, a key that had to be stringified) is reported in
  ``warnings`` with its row/column position, so ``truncated`` is a fact and not a hope.

Identity is ``"<table>:<record_id>"`` — physical and table-local, exactly as the engine defines it.
It is never an application primary key and the UI says so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from okto_grafx.domain.model import Timestamp, VectorValue
from okto_grafx.domain.model.decimal_values import DecimalValue
from okto_grafx.domain.model.temporal_values import (
    DateValue,
    DateTimeValue,
    DurationValue,
    LocalDateTimeValue,
    LocalTimeValue,
    TimeValue,
)
from okto_grafx.domain.query.entity_values import (
    NodeValue,
    PathValue,
    RelationshipValue,
)

MAX_STRING_CHARACTERS = 65536


@dataclass
class EncodeWarnings:
    """Positions where the encoder had to give something up."""

    items: list[dict[str, Any]] = field(default_factory=list)

    def add(self, kind: str, **detail: Any) -> None:
        self.items.append({"kind": kind, **detail})

    def __bool__(self) -> bool:
        return bool(self.items)

    def extend(self, other: "EncodeWarnings") -> None:
        self.items.extend(other.items)


def entity_id(table: str | None, record_id: int) -> str:
    return f"{table}:{record_id}"


class ValueCodec:
    """Encodes engine values to JSON-safe data, resolving ``table_id`` → name from the catalog."""

    def __init__(
        self,
        table_names: Mapping[int, str] | None = None,
        *,
        max_string_characters: int = MAX_STRING_CHARACTERS,
    ) -> None:
        self._table_names = dict(table_names or {})
        self.max_string_characters = max(16, int(max_string_characters))

    def table_name(self, table_id: int) -> str | None:
        return self._table_names.get(table_id)

    # -- encoding ----------------------------------------------------------------------

    def encode(self, value: Any, warnings: EncodeWarnings, *, where: str = "value") -> Any:
        # bool first: bool is an int subclass and must not become 1/0.
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                warnings.add("non_finite_float", where=where, value=str(value))
                return None
            return value
        if isinstance(value, str):
            if len(value) > self.max_string_characters:
                warnings.add(
                    "string_truncated",
                    where=where,
                    characters=len(value),
                    kept=self.max_string_characters,
                )
                return value[: self.max_string_characters]
            return value
        if isinstance(value, (bytes, bytearray)):
            return {"kind": "bytes", "hex": bytes(value).hex()}

        if isinstance(value, DecimalValue):
            return {
                "kind": "decimal",
                "coefficient": value.coefficient,
                "precision": value.precision,
                "scale": value.scale,
                "value": format(Decimal(value.coefficient).scaleb(-value.scale), "f"),
            }
        if isinstance(value, VectorValue):
            return {
                "kind": "vector",
                "values": [float(v) for v in value.values],
                "dtype": value.dtype,
                "space_ref": value.space_ref,
            }
        if isinstance(value, Timestamp):
            return {"kind": "timestamp", "micros": value.micros}
        if isinstance(value, DateValue):
            return {"kind": "date", "value": f"{value.year:04d}-{value.month:02d}-{value.day:02d}"}
        if isinstance(value, LocalTimeValue):
            return {"kind": "local_time", "nanoseconds": value.nanoseconds}
        if isinstance(value, TimeValue):
            return {
                "kind": "time",
                "nanoseconds": value.time.nanoseconds,
                "offset_seconds": value.offset_seconds,
            }
        if isinstance(value, LocalDateTimeValue):
            return {
                "kind": "local_datetime",
                "date": f"{value.date.year:04d}-{value.date.month:02d}-{value.date.day:02d}",
                "nanoseconds": value.time.nanoseconds,
            }
        if isinstance(value, DateTimeValue):
            return {
                "kind": "datetime",
                "date": f"{value.local.date.year:04d}-{value.local.date.month:02d}-"
                f"{value.local.date.day:02d}",
                "nanoseconds": value.local.time.nanoseconds,
                "offset_seconds": value.offset_seconds,
                "zone": value.zone,
            }
        if isinstance(value, DurationValue):
            return {
                "kind": "duration",
                "months": value.months,
                "days": value.days,
                "seconds": value.seconds,
                "nanoseconds": value.nanoseconds,
            }
        if isinstance(value, NodeValue):
            return self._encode_node(value, warnings, where)
        if isinstance(value, RelationshipValue):
            return self._encode_relationship(value, warnings, where)
        if isinstance(value, PathValue):
            return {
                "kind": "path",
                "nodes": [
                    self.encode(n, warnings, where=f"{where}.nodes") for n in value.nodes
                ],
                "relationships": [
                    self.encode(r, warnings, where=f"{where}.relationships")
                    for r in value.relationships
                ],
            }

        if isinstance(value, Mapping):
            return self._encode_mapping(value, warnings, where=where)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [
                self.encode(item, warnings, where=f"{where}[{index}]")
                for index, item in enumerate(value)
            ]

        enum_value = getattr(value, "value", None)
        if enum_value is not None and isinstance(enum_value, (str, int)):
            return {"kind": "enum", "type": type(value).__name__, "value": enum_value}
        warnings.add("unencodable_value", where=where, type=type(value).__name__)
        return {"kind": "unknown", "type": type(value).__name__, "repr": str(value)[:256]}

    def _encode_mapping(
        self, value: Mapping[Any, Any], warnings: EncodeWarnings, *, where: str
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str):
                mapped = key
                stringified = False
            else:
                encoded = self.encode(key, warnings, where=f"{where}.key")
                mapped = encoded if isinstance(encoded, str) else _stringify_key(encoded)
                stringified = True
            if mapped_is_ambiguous(out, mapped):
                # JSON objects cannot carry two keys with the same spelling. Saying so beats
                # letting one value quietly replace another.
                warnings.add(
                    "map_key_collision",
                    where=where,
                    key=mapped,
                    original_key_type=type(key).__name__,
                    stringified=stringified,
                )
            out[mapped] = self.encode(item, warnings, where=f"{where}.{mapped}")
        return out

    def _encode_node(self, value: NodeValue, warnings: EncodeWarnings, where: str) -> dict[str, Any]:
        table = self.table_name(value.identity.table_id) or value.label
        labels = list(value.node_labels or ())
        if value.label and value.label not in labels:
            labels.insert(0, value.label)
        return {
            "kind": "node",
            "id": entity_id(table, value.identity.record_id),
            "table": table,
            "table_id": value.identity.table_id,
            "record_id": value.identity.record_id,
            "label": value.label,
            "labels": labels,
            "properties": self._encode_mapping(
                value.properties, warnings, where=f"{where}.properties"
            ),
        }

    def _encode_relationship(
        self, value: RelationshipValue, warnings: EncodeWarnings, where: str
    ) -> dict[str, Any]:
        table = self.table_name(value.identity.table_id) or value.label
        return {
            "kind": "relationship",
            "id": entity_id(table, value.identity.record_id),
            "table": table,
            "table_id": value.identity.table_id,
            "record_id": value.identity.record_id,
            "label": value.label,
            "source": entity_id(
                self.table_name(_table_of(value.source)), _record_of(value.source)
            ),
            "target": entity_id(
                self.table_name(_table_of(value.target)), _record_of(value.target)
            ),
            "properties": self._encode_mapping(
                value.properties, warnings, where=f"{where}.properties"
            ),
        }

    # -- results -----------------------------------------------------------------------

    def encode_rows(
        self, rows: Iterable[Sequence[Any]], columns: Sequence[str] | None = None
    ) -> tuple[list[list[Any]], EncodeWarnings]:
        warnings = EncodeWarnings()
        out: list[list[Any]] = []
        for index, row in enumerate(rows):
            out.append(
                [
                    self.encode(cell, warnings, where=f"row {index} column {name}")
                    for name, cell in _labelled(row, columns)
                ]
            )
        return out, warnings

    def encode_row(self, row: Sequence[Any], columns: Sequence[str] | None) -> tuple[dict[str, Any], EncodeWarnings]:
        encoded, warnings = self.encode_rows([row], columns)
        return dict(zip(columns or [], encoded[0])), warnings


def _labelled(row: Sequence[Any], columns: Sequence[str] | None) -> Iterable[tuple[str, Any]]:
    for index, cell in enumerate(row):
        name = columns[index] if columns and index < len(columns) else f"_{index}"
        yield str(name), cell


def _table_of(identity: Any) -> int | None:
    return getattr(identity, "table_id", None)


def _record_of(identity: Any) -> int:
    return int(getattr(identity, "record_id", -1))


def _stringify_key(encoded: Any) -> str:
    if isinstance(encoded, dict):
        if "value" in encoded and isinstance(encoded["value"], (str, int)):
            return str(encoded["value"])
        if "hex" in encoded:
            return f"bytes:{encoded['hex']}"
        if "coefficient" in encoded:
            return f"decimal:{encoded['value']}"
    return str(encoded)


def mapped_is_ambiguous(existing: dict[str, Any], candidate: str) -> bool:
    return candidate in existing


# --------------------------------------------------------------------------------------
# Parameters (JSON -> engine values)
# --------------------------------------------------------------------------------------


def decode_parameters(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Turn a JSON parameter document into engine values.

    Plain JSON scalars, lists and objects pass through. Tagged objects make the types JSON has no
    literal for explicit, so a decimal does not arrive as a rounded float:
    ``{"$decimal": {"coefficient": 1234, "precision": 9, "scale": 4}}``,
    ``{"$date": "2024-01-31"}``, ``{"$timestamp_micros": 1700000000000000}``,
    ``{"$vector": [1.0, 0.0]}`` (which binds with the space the target column declares, because a
    space reference is physical and this server will not guess one).
    """

    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        from ..errors import AppError

        raise AppError(
            "parameters must be a JSON object.", status=400, code="invalid_parameters", field="parameters"
        )
    return {str(name): decode_value(value) for name, value in raw.items()}


def decode_value(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            from ..errors import AppError

            raise AppError(
                "NaN and Infinity are not valid query parameters.",
                status=400,
                code="invalid_parameter_value",
            )
        return value
    if isinstance(value, list):
        return [decode_value(item) for item in value]
    if isinstance(value, Mapping):
        if set(value) == {"$decimal"}:
            spec = value["$decimal"]
            return DecimalValue(
                int(spec["coefficient"]), int(spec["precision"]), int(spec["scale"])
            )
        if set(value) == {"$date"}:
            parts = str(value["$date"]).split("-")
            return DateValue(int(parts[0]), int(parts[1]), int(parts[2]))
        if set(value) == {"$timestamp_micros"}:
            return Timestamp(int(value["$timestamp_micros"]))
        if set(value) == {"$vector"}:
            # A vector *value* carries a space reference, and guessing one would write an
            # embedding into whatever space happened to share the number. The engine binds a
            # plain sequence with the declared column's space authority, so that is what a
            # tagged vector becomes on the way in.
            return tuple(float(component) for component in value["$vector"])
        return {str(k): decode_value(v) for k, v in value.items()}
    from ..errors import AppError

    raise AppError(
        f"Unsupported query parameter of type {type(value).__name__}.",
        status=400,
        code="invalid_parameter_value",
    )


# --------------------------------------------------------------------------------------
# Graph extraction
# --------------------------------------------------------------------------------------


def extract_entities(rows: Iterable[Sequence[Any]]) -> tuple[list[NodeValue], list[RelationshipValue]]:
    """Collect every graph entity reachable in a result, including inside paths."""

    nodes: dict[str, NodeValue] = {}
    rels: dict[str, RelationshipValue] = {}

    def visit(value: Any) -> None:
        if isinstance(value, NodeValue):
            nodes[f"{value.identity.table_id}:{value.identity.record_id}"] = value
        elif isinstance(value, RelationshipValue):
            rels[f"{value.identity.table_id}:{value.identity.record_id}"] = value
        elif isinstance(value, PathValue):
            for item in value.nodes:
                visit(item)
            for item in value.relationships:
                visit(item)
        elif isinstance(value, Mapping):
            for item in value.values():
                visit(item)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                visit(item)

    for row in rows:
        for cell in row:
            visit(cell)
    return list(nodes.values()), list(rels.values())
