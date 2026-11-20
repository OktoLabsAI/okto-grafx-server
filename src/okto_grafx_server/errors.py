"""One error envelope for REST, WebSocket and MCP.

The envelope is derived from the *class and code* of ``okto_grafx.errors`` members, never from
message text: the library documents ``code``/``retryable``/``details`` as the machine-readable
contract, and a message that changes wording must not change an HTTP status. No traceback ever
reaches a client, which is the same policy the library's own CLI holds to.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any

from okto_grafx.errors import GrafxError

log = logging.getLogger("okto_grafx_server")

_request_id: ContextVar[str] = ContextVar("request_id", default="-")


def set_request_id(value: str) -> None:
    _request_id.set(value)


def get_request_id() -> str:
    return _request_id.get()


class AppError(Exception):
    """A refusal the server states in full: status, code, retryability and safe details."""

    status = 500
    code = "internal_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        retryable: bool | None = None,
        field: str | None = None,
        value: Any = None,
        details: dict[str, Any] | None = None,
        grafx_code: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.field = field
        self.value = value
        self.details = details or {}
        self.grafx_code = grafx_code
        # Response headers the refusal must carry, e.g. WWW-Authenticate and Retry-After.
        self.headers = dict(headers or {})

    def envelope(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "details": self.details,
                "request_id": get_request_id(),
            }
        }
        if self.field is not None:
            body["error"]["field"] = self.field
        if self.value is not None and isinstance(self.value, (str, int, float, bool)):
            body["error"]["value"] = self.value
        if self.grafx_code:
            body["error"]["grafx_code"] = self.grafx_code
        return body


def bad_request(message: str, **kw: Any) -> AppError:
    # A caller may name a more specific code; the helper only supplies the default.
    kw.setdefault("code", "bad_request")
    return AppError(message, status=400, **kw)


def unauthorized(message: str = "A valid API key is required.", **kw: Any) -> AppError:
    # A caller may name a more specific code; the helper only supplies the default.
    kw.setdefault("code", "unauthorized")
    return AppError(message, status=401, **kw)


def forbidden(message: str, **kw: Any) -> AppError:
    # A caller may name a more specific code; the helper only supplies the default.
    kw.setdefault("code", "forbidden")
    return AppError(message, status=403, **kw)


def not_found(message: str, **kw: Any) -> AppError:
    # A caller may name a more specific code; the helper only supplies the default.
    kw.setdefault("code", "not_found")
    return AppError(message, status=404, **kw)


def conflict(message: str, **kw: Any) -> AppError:
    # A caller may name a more specific code; the helper only supplies the default.
    kw.setdefault("code", "conflict")
    return AppError(message, status=409, **kw)


def too_large(message: str, **kw: Any) -> AppError:
    # A caller may name a more specific code; the helper only supplies the default.
    kw.setdefault("code", "budget_exceeded")
    return AppError(message, status=413, **kw)


def too_many(message: str, **kw: Any) -> AppError:
    kw.setdefault("code", "too_many_requests")
    kw.setdefault("retryable", True)
    # A refusal that says “come back” has to say roughly when, or the client can only guess and hammer.
    kw.setdefault("headers", {"Retry-After": "1"})
    return AppError(message, status=429, **kw)


# --------------------------------------------------------------------------------------
# Grafx -> HTTP mapping
# --------------------------------------------------------------------------------------

_BAD_REQUEST = {
    "parse_error",
    "plan_error",
    "query_error",
    "configuration_error",
    "vector_validation_error",
    "vector_validation",
    "embedding_space_mismatch",
    "index_error",
    "unsupported_operation",
    "port_not_configured",
    "transaction_state_error",
}
_BUDGET = {
    "query_budget_exceeded",
    "transaction_budget_exceeded",
    "buffer_budget_exceeded",
}
_CONFLICT = {
    "write_conflict",
    "snapshot_reclaimed",
    "space_retired",
    "history_expired",
    "history_unavailable",
    "schema_version_mismatch",
}
_STORAGE = {"storage_error", "device_full", "ledger_error", "quarantine_error"}
_SERVER = {"corruption_detected", "recovery_refused", "durability_barrier_failed"}
_UNAVAILABLE = {"lease_timeout", "lease_stolen", "stale_epoch"}
_TIMEOUT = {"query_cancelled", "query_deadline_exceeded"}


def grafx_code_of(exc: GrafxError) -> str:
    code = getattr(exc, "code", None)
    return str(code) if code else "error"


def map_grafx_error(exc: BaseException) -> AppError:
    """Turn any exception into the server envelope without leaking a traceback."""

    if isinstance(exc, AppError):
        return exc
    if not isinstance(exc, GrafxError):
        log.exception("unhandled server error", exc_info=exc)
        return AppError(
            "The server failed to complete this request.",
            status=500,
            code="internal_error",
        )

    code = grafx_code_of(exc)
    details = {k: _safe(v) for k, v in (getattr(exc, "details", None) or {}).items()}
    # The engine names the offending field inside its details rather than on an attribute.
    field = getattr(exc, "field", None) or details.get("field")
    value = getattr(exc, "value", None)
    if value is None:
        value = details.get("value")
    common = {
        "field": field if isinstance(field, str) else None,
        "value": value if isinstance(value, (str, int, float, bool)) else None,
        "details": details,
        "grafx_code": code,
    }
    retryable = bool(getattr(exc, "retryable", False))

    if code in _TIMEOUT:
        return AppError(str(exc), status=408, code=code, retryable=True, **common)
    if code in _BUDGET:
        return AppError(str(exc), status=413, code="budget_exceeded", retryable=False, **common)
    if code == "write_conflict":
        return AppError(
            str(exc) + " Reopen a fresh snapshot and recompute the operation; the server never "
            "replays a write automatically.",
            status=409,
            code="write_conflict",
            retryable=True,
            **common,
        )
    if code in _CONFLICT:
        return AppError(str(exc), status=409, code=code, retryable=retryable, **common)
    if code in _SERVER:
        body = {
            "corruption_detected": "The store refused verification. Use the maintenance panel.",
            "recovery_refused": "Recovery refused to truncate unproved history. Preserve the "
            "directory and inspect the recovery report.",
            "durability_barrier_failed": "The durability barrier failed: the outcome of that write "
            "is unknown. Do not replay it; reconcile against durable state.",
        }.get(code, str(exc))
        return AppError(
            body,
            status=500,
            code=code,
            retryable=False,
            details={**details, "unknown_outcome": code == "durability_barrier_failed"},
            field=common["field"],
            value=common["value"],
            grafx_code=code,
        )
    if code in _UNAVAILABLE:
        return AppError(str(exc), status=503, code=code, retryable=True, **common)
    if code in _STORAGE:
        status = 507 if code == "device_full" else 422
        return AppError(str(exc), status=status, code=code, retryable=retryable, **common)
    if code == "recovery_required":
        return AppError(str(exc), status=503, code=code, retryable=True, **common)
    if code in _BAD_REQUEST:
        return AppError(str(exc), status=400, code=code, retryable=retryable, **common)
    return AppError(str(exc), status=500, code=code, retryable=retryable, **common)


def _safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value][:32]
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in list(value.items())[:32]}
    if isinstance(value, bytes):
        return value.hex()
    return str(value)[:512]
