"""Value codec (plan item 1): nothing flattened, nothing lost without a warning."""

from __future__ import annotations

from pathlib import Path

import pytest
from okto_grafx import connect
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

from okto_grafx_server.engine.values import (
    EncodeWarnings,
    ValueCodec,
    decode_parameters,
    extract_entities,
)


def encode(value: object, **kwargs: object) -> tuple[object, EncodeWarnings]:
    warnings = EncodeWarnings()
    codec = ValueCodec({1: "Doc", 2: "CITES"}, **kwargs)  # type: ignore[arg-type]
    return codec.encode(value, warnings), warnings


# -- scalars ---------------------------------------------------------------------------


def test_primitives_pass_through() -> None:
    for value in (None, True, False, 0, -7, 2**62, "text", ""):
        encoded, warnings = encode(value)
        assert encoded == value
        assert not warnings


def test_nan_and_infinity_are_refused_as_values_and_flagged() -> None:
    for value in (float("nan"), float("inf"), float("-inf")):
        encoded, warnings = encode(value)
        assert encoded is None
        assert warnings.items[0]["kind"] == "non_finite_float"


def test_parameters_reject_non_finite_floats() -> None:
    with pytest.raises(Exception) as refused:
        decode_parameters({"x": float("nan")})
    assert getattr(refused.value, "code", None) == "invalid_parameter_value"


def test_long_strings_are_truncated_with_a_warning() -> None:
    encoded, warnings = encode("x" * 500, max_string_characters=64)
    assert encoded == "x" * 64
    assert warnings.items[0]["kind"] == "string_truncated"
    assert warnings.items[0]["characters"] == 500


def test_bytes_become_hex() -> None:
    encoded, _ = encode(b"\x00\xff")
    assert encoded == {"kind": "bytes", "hex": "00ff"}


# -- exact numerics and temporals ------------------------------------------------------


def test_decimal_keeps_its_exact_value_and_metadata() -> None:
    encoded, warnings = encode(DecimalValue(-123450, precision=9, scale=4))
    assert not warnings
    assert encoded["kind"] == "decimal"
    assert encoded["coefficient"] == -123450
    assert encoded["precision"] == 9
    assert encoded["scale"] == 4
    assert encoded["value"] == "-12.3450"


def test_vector_is_not_a_plain_list() -> None:
    encoded, _ = encode(VectorValue((1.0, 0.5), space_ref=3, dtype="float32"))
    assert encoded == {
        "kind": "vector",
        "values": [1.0, 0.5],
        "dtype": "float32",
        "space_ref": 3,
    }


def test_temporal_families_encode_exactly() -> None:
    cases = {
        Timestamp(1_700_000_000_000_000): ("timestamp", {"micros": 1_700_000_000_000_000}),
        DateValue(2024, 1, 31): ("date", {"value": "2024-01-31"}),
        LocalTimeValue(3_600_000_000_000): ("local_time", {"nanoseconds": 3_600_000_000_000}),
        TimeValue(LocalTimeValue(60_000_000_000), offset_seconds=-10800): (
            "time",
            {"nanoseconds": 60_000_000_000, "offset_seconds": -10800},
        ),
        LocalDateTimeValue(DateValue(2024, 12, 31), LocalTimeValue(43_200_000_000_000)): (
            "local_datetime",
            {"date": "2024-12-31", "nanoseconds": 43_200_000_000_000},
        ),
        DateTimeValue(
            LocalDateTimeValue(DateValue(2024, 6, 1), LocalTimeValue(0)),
            offset_seconds=3600,
            zone="Europe/Lisbon",
        ): ("datetime", {"date": "2024-06-01", "offset_seconds": 3600, "zone": "Europe/Lisbon"}),
        DurationValue(months=1, days=2, seconds=3, nanoseconds=4): (
            "duration",
            {"months": 1, "days": 2, "seconds": 3, "nanoseconds": 4},
        ),
    }
    for value, (kind, expected) in cases.items():
        encoded, warnings = encode(value)
        assert not warnings
        assert encoded["kind"] == kind
        for key, item in expected.items():
            assert encoded[key] == item


# -- collections -----------------------------------------------------------------------


def test_nested_lists_and_maps_survive() -> None:
    encoded, warnings = encode({"a": [1, {"b": [DecimalValue(150, 3, 2)]}], "c": None})
    assert not warnings
    assert encoded["a"][1]["b"][0]["value"] == "1.50"
    assert encoded["c"] is None


def test_non_string_map_keys_are_stringified_once_and_flagged() -> None:
    from okto_grafx_server.engine.values import EncodeWarnings as _W

    warnings = _W()
    codec = ValueCodec({})
    encoded = codec.encode({1: "a", 2: "b", "1": "collision"}, warnings)
    assert encoded["1"] in {"a", "collision"}
    kinds = [item["kind"] for item in warnings.items]
    assert "map_key_collision" in kinds


def test_unknown_objects_are_reported_not_dropped() -> None:
    class Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    encoded, warnings = encode(Opaque())
    assert encoded["kind"] == "unknown"
    assert warnings.items[0]["kind"] == "unencodable_value"


# -- entities --------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path):
    from conftest import seed_store

    path = seed_store(tmp_path / "store")
    db = connect(str(path))
    yield db
    db.close()


def test_node_relationship_and_path_encode_with_physical_ids(store) -> None:
    codec = ValueCodec({1: "Doc", 2: "Tag", 3: "TAGGED", 4: "CITES"})
    warnings = EncodeWarnings()
    result = store.execute(
        "MATCH (d:Doc {id:1})-[c:CITES]->(e:Doc {id:2}) RETURN d, c, e"
    )
    node, rel, other = (codec.encode(value, warnings) for value in result.rows[0])
    assert node["kind"] == "node" and node["id"] == "Doc:1"
    assert node["table"] == "Doc" and node["label"] == "Doc"
    assert node["properties"]["title"] == "durable wal"
    assert rel["kind"] == "relationship" and rel["source"] == "Doc:1"
    assert rel["target"] == "Doc:2" and rel["properties"]["weight"] == 1.0
    assert other["id"] == "Doc:2"


def test_path_value_encodes_nodes_and_relationships(store) -> None:
    codec = ValueCodec({1: "Doc", 2: "Tag", 3: "TAGGED", 4: "CITES"})
    warnings = EncodeWarnings()
    result = store.execute(
        "MATCH p = (d:Doc {id:1})-[c:CITES]->(e:Doc {id:2}) RETURN p"
    )
    encoded = codec.encode(result.rows[0][0], warnings)
    assert encoded["kind"] == "path"
    assert [node["id"] for node in encoded["nodes"]] == ["Doc:1", "Doc:2"]
    assert [rel["id"].split(":")[0] for rel in encoded["relationships"]] == ["CITES"]


def test_unlabeled_node_reports_null_label(store) -> None:
    with store.begin("write") as tx:
        try:
            tx.execute("CREATE ({kind: 'untyped'})")
        except Exception:  # noqa: BLE001 - flexible graph may be unavailable in this store
            pytest.skip("this store does not allow unlabeled nodes")
    result = store.execute("MATCH (n) WHERE n.kind = 'untyped' RETURN n")
    codec = ValueCodec({1: "Doc", 2: "Tag", 3: "TAGGED", 4: "CITES"})
    encoded = codec.encode(result.rows[0][0], EncodeWarnings())
    assert encoded["label"] is None or encoded["label"] == ""


def test_extract_entities_walks_paths_and_maps(tmp_path: Path) -> None:
    from okto_grafx.domain.query.entity_identity import EntityProvenance
    from okto_grafx.domain.query.entity_values import (
        EntityIdentity,
        NodeValue,
        PathValue,
        RelationshipValue,
    )

    uuid = b"0" * 16

    def node(table_id: int, record_id: int) -> NodeValue:
        return NodeValue(
            identity=EntityIdentity(
                database_uuid=uuid, table_id=table_id, kind="node", record_id=record_id
            ),
            label="Doc",
            properties={},
            provenance=EntityProvenance(
                read_lsn=1, schema_version=1, version_lsn=1, pending=False
            ),
            node_labels=("Doc",),
        )

    first, second = node(1, 1), node(1, 2)
    relationship = RelationshipValue(
        identity=EntityIdentity(
            database_uuid=uuid, table_id=4, kind="relationship", record_id=9
        ),
        label="CITES",
        source=first.identity,
        target=second.identity,
        properties={},
        provenance=EntityProvenance(
            read_lsn=1, schema_version=1, version_lsn=1, pending=False
        ),
    )
    path = PathValue(nodes=(first, second), relationships=(relationship,))
    nodes, rels = extract_entities([({"nested": [path]}, first), (relationship,)])
    assert len(nodes) == 2 and len(rels) == 1


# -- parameters ------------------------------------------------------------------------


def test_tagged_parameters_decode_to_engine_values() -> None:
    decoded = decode_parameters(
        {
            "amount": {"$decimal": {"coefficient": 1250, "precision": 6, "scale": 2}},
            "day": {"$date": "2024-02-29"},
            "when": {"$timestamp_micros": 1700000000000000},
            "embedding": {"$vector": [1.0, 0.0]},
            "plain": ["a", 1, {"k": None}],
        }
    )
    assert decoded["amount"] == DecimalValue(1250, 6, 2)
    assert decoded["day"] == DateValue(2024, 2, 29)
    assert decoded["when"] == Timestamp(1700000000000000)
    assert decoded["plain"] == ["a", 1, {"k": None}]
    # A tagged vector is a sequence, not a VectorValue: the target column supplies the space
    # authority, and inventing a space reference could write an embedding into the wrong space.
    assert decoded["embedding"] == (1.0, 0.0)


def test_a_vector_parameter_binds_with_the_columns_space(tmp_path: Path) -> None:
    from conftest import seed_store

    path = seed_store(tmp_path / "store")
    db = connect(str(path))
    try:
        with db.begin("write") as txn:
            txn.execute(
                "CREATE (:Doc {id: 77, title: 'tagged', body: 'x', v: $vector})",
                {"vector": decode_parameters({"v": {"$vector": [0.25, 0.75]}})["v"]},
            )
        with db.begin("read") as txn:
            row = txn.execute("MATCH (d:Doc {id:77}) RETURN d.v").rows[0][0]
        assert row.values == (0.25, 0.75)
        assert row.space_ref  # the column's space, not one the server guessed
    finally:
        db.close()
