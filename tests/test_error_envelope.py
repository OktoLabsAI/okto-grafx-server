"""The error envelope (plan item 2): derived from the engine's class and code, never its prose."""

from __future__ import annotations

import pytest
from okto_grafx.errors import (
    GrafxBufferBudgetExceeded,
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxDeviceFull,
    GrafxDurabilityBarrierFailed,
    GrafxIndexError,
    GrafxLeaseTimeout,
    GrafxParseError,
    GrafxPlanError,
    GrafxQueryBudgetExceeded,
    GrafxQueryCancelled,
    GrafxSnapshotReclaimed,
    GrafxStorageError,
    GrafxTransactionStateError,
    GrafxWriteConflict,
)

from okto_grafx_server.errors import AppError, grafx_code_of, map_grafx_error


@pytest.mark.parametrize(
    "exception,status,code,retryable",
    [
        (GrafxParseError("bad syntax"), 400, "parse_error", False),
        (GrafxPlanError("bad plan"), 400, "plan_error", False),
        (GrafxConfigurationError("bad option"), 400, "configuration_error", False),
        (GrafxIndexError("bad index"), 400, "index_error", False),
        (GrafxQueryBudgetExceeded("too much"), 413, "budget_exceeded", False),
        (GrafxBufferBudgetExceeded("too small"), 413, "budget_exceeded", False),
        (GrafxWriteConflict("conflict"), 409, "write_conflict", True),
        (GrafxSnapshotReclaimed("old"), 409, "snapshot_reclaimed", False),
        (GrafxLeaseTimeout("busy"), 503, "lease_timeout", True),
        (GrafxStorageError("disk"), 422, "storage_error", False),
        (GrafxDeviceFull("full"), 507, "device_full", False),
        (GrafxCorruptionDetected("damage"), 500, "corruption_detected", False),
        (GrafxQueryCancelled("cancelled"), 408, "query_cancelled", True),
    ],
)
def test_grafx_errors_map_to_documented_statuses(exception, status, code, retryable) -> None:
    mapped = map_grafx_error(exception)
    assert mapped.status == status, (exception, mapped.status, mapped.code)
    assert mapped.code == code
    assert mapped.grafx_code == grafx_code_of(exception)
    # Retryability comes from the engine's own flag unless this server documents an override:
    # a conflict is always retryable-with-recompute, a budget refusal never is.
    overrides = {
        "write_conflict": True,
        "budget_exceeded": False,
        "query_cancelled": True,
        "query_deadline_exceeded": True,
        "durability_barrier_failed": False,
    }
    expected = overrides[code] if code in overrides else bool(exception.retryable)
    assert mapped.retryable is expected


def test_a_write_conflict_tells_the_client_to_recompute_not_replay() -> None:
    mapped = map_grafx_error(GrafxWriteConflict("snapshot moved"))
    assert "recompute" in mapped.message.lower()
    assert mapped.status == 409 and mapped.retryable


def test_durability_barrier_failure_reports_an_unknown_outcome() -> None:
    mapped = map_grafx_error(GrafxDurabilityBarrierFailed("barrier failed"))
    assert mapped.status == 500
    assert mapped.details.get("unknown_outcome") is True
    assert mapped.retryable is False, "an unknown outcome must never be offered for replay"
    assert "do not replay" in mapped.message.lower()


def test_transaction_state_on_the_read_door_is_a_403_not_a_fault() -> None:
    from okto_grafx_server.engine.service import DatabaseService

    refusal = DatabaseService._refuse_write_on_read_door(
        DatabaseService, "docs", GrafxTransactionStateError("read transaction cannot write")
    )
    assert refusal.status == 403
    assert refusal.code == "write_not_permitted"
    assert refusal.details["reason"] == "read_only_door"


def test_details_and_field_survive_the_mapping() -> None:
    error = GrafxConfigurationError("bad", field="page_size", value=1234, reason="pow2")
    mapped = map_grafx_error(error)
    assert mapped.field == "page_size"
    assert mapped.value == 1234
    assert mapped.details["reason"] == "pow2"


def test_non_grafx_failures_never_leak_a_traceback() -> None:
    mapped = map_grafx_error(RuntimeError("internal detail with a path /home/secret"))
    assert mapped.status == 500
    assert mapped.code == "internal_error"
    assert "RuntimeError" not in mapped.message
    assert "/home/secret" not in mapped.message


def test_bytes_and_nested_details_are_serialisable() -> None:
    class Error(Exception):
        pass

    from okto_grafx.errors import GrafxError

    error = GrafxError("opaque", code="opaque", blob=b"\x00\x01", nested={"k": [1, 2]})
    mapped = map_grafx_error(error)
    assert mapped.details["blob"] == "0001"
    assert mapped.details["nested"] == {"k": [1, 2]}
    assert mapped.status == 500


def test_envelope_shape_is_stable() -> None:
    error = AppError(
        "nope", status=400, code="bad_request", field="query", value="x", details={"a": 1}
    )
    body = error.envelope()["error"]
    assert set(body) >= {
        "code",
        "message",
        "retryable",
        "details",
        "request_id",
        "field",
        "value",
    }


def test_app_error_passes_through_unchanged() -> None:
    original = AppError("mine", status=418, code="teapot")
    assert map_grafx_error(original) is original
