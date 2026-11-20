"""Handle-pool policy: what gets closed, and what is refused even while it stays warm.

Two rules the plan states and the pool must actually enforce:

* the LRU is **budget-aware** — `buffer_budget_bytes` is per handle, so the cost of warming a store
  is `buffer_budget_bytes x handles` and a store configured with a large budget must push idle
  stores out rather than silently multiply the daemon's memory;
* a handle reporting `recovery_required` is **quarantined** — the server refuses to read or write
  through it until the lane is closed, which is what the explicit recovery action does. Refusing is
  the safe answer; serving whatever the pages happen to hold is not.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest
from conftest import Harness, seed_store
from okto_grafx.engine.database import Database

from okto_grafx_server.engine.handles import DEFAULT_BUFFER_BUDGET_BYTES, HandlePool
from okto_grafx_server.errors import AppError

MIB = 1024 * 1024


@pytest.fixture
def pool() -> Iterator[HandlePool]:
    """A pool the tests own, with no reaper in the way."""

    owned = HandlePool(max_open_handles=2, readers_per_database=1, idle_ttl_seconds=3600.0)
    try:
        yield owned
    finally:
        owned.shutdown(drain_seconds=5.0)


def _store(root: Path, name: str) -> str:
    return str(seed_store(root / name))


def test_the_count_bound_closes_the_least_recently_used_idle_store(pool: HandlePool, tmp_path):
    pool.buffer_budget_ceiling_bytes = 100 * DEFAULT_BUFFER_BUDGET_BYTES  # a count-only regime
    paths = {name: _store(tmp_path, name) for name in ("abc", "bcd", "cde")}

    pool.open(pool.spec_for("abc", paths["abc"], {}, False))
    pool.open(pool.spec_for("bcd", paths["bcd"], {}, False))
    assert pool.open_slugs() == ["abc", "bcd"]

    pool.open(pool.spec_for("cde", paths["cde"], {}, False))

    assert pool.open_slugs() == ["bcd", "cde"], "the oldest idle store pays for the new one"
    assert not pool.is_open("abc")


def test_a_large_page_budget_closes_an_idle_store_below_the_count_bound(tmp_path: Path):
    paths = {name: _store(tmp_path, name) for name in ("small", "big", "extra")}
    ceiling = 3 * DEFAULT_BUFFER_BUDGET_BYTES
    sized = HandlePool(
        max_open_handles=8,
        readers_per_database=1,
        idle_ttl_seconds=3600.0,
        buffer_budget_ceiling_bytes=ceiling,
    )
    try:
        # Two handles at the library default plus one with double the budget exactly fill it.
        sized.open(sized.spec_for("small", paths["small"], {}, False))
        sized.open(
            sized.spec_for("big", paths["big"], {"buffer_budget_bytes": 2 * DEFAULT_BUFFER_BUDGET_BYTES}, False)
        )
        assert sorted(sized.open_slugs()) == ["big", "small"]
        budgets = {row["slug"]: row["buffer_budget_bytes"] for row in sized.status()}
        assert budgets == {
            "small": DEFAULT_BUFFER_BUDGET_BYTES,
            "big": 2 * DEFAULT_BUFFER_BUDGET_BYTES,
        }

        # A third default-budget handle would overshoot: the idle one is closed, not the big one.
        sized.open(sized.spec_for("extra", paths["extra"], {}, False))

        assert sorted(sized.open_slugs()) == ["big", "extra"]
        assert not sized.is_open("small")
        assert sum(row["buffer_budget_bytes"] for row in sized.status()) <= ceiling
    finally:
        sized.shutdown(drain_seconds=5.0)


def test_a_store_with_work_in_flight_is_never_the_victim(tmp_path: Path):
    paths = {name: _store(tmp_path, name) for name in ("held", "next", "third")}
    tight = HandlePool(
        max_open_handles=2,
        readers_per_database=1,
        idle_ttl_seconds=3600.0,
        buffer_budget_ceiling_bytes=2 * DEFAULT_BUFFER_BUDGET_BYTES,
    )
    try:
        with tight.reader("held", path=paths["held"]):
            assert tight.active_operations("held") == 1
            tight.open(tight.spec_for("next", paths["next"], {}, False))
            tight.open(tight.spec_for("third", paths["third"], {}, False))

            assert tight.is_open("held"), "a lane with work in flight is not an eviction candidate"
            assert tight.open_slugs() == ["held", "third"]
        assert tight.active_operations("held") == 0
    finally:
        tight.shutdown(drain_seconds=5.0)


class _NeedsRecovery:
    """A transaction view that reports the store as needing recovery, delegating everything else."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def recovery_required(self) -> bool:
        return True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@contextmanager
def forcing_recovery() -> Iterator[None]:
    """Make every handle report `recovery_required` for the duration of the block."""

    real = Database.transactions
    Database.transactions = property(lambda self: _NeedsRecovery(real.fget(self)))  # type: ignore[method-assign]
    try:
        yield
    finally:
        Database.transactions = real  # type: ignore[method-assign]


def test_a_handle_needing_recovery_is_refused_and_stays_refused(pool: HandlePool, tmp_path):
    path = _store(tmp_path, "docs")
    pool.open(pool.spec_for("docs", path, {}, False))

    with forcing_recovery(), pytest.raises(AppError) as refused:
        with pool.reader("docs", path=path):
            pass  # pragma: no cover - the checkout must not hand out a handle

    error = refused.value
    assert error.status == 503
    assert error.code == "recovery_required"
    assert error.retryable, "recovery is a state the operator can fix, not a permanent failure"
    assert "recover" in error.message
    assert error.details["hint"] == "run_recover_action"

    # The lane is marked and holds no handle, so the refusal does not reopen the store each time.
    [lane] = pool.status()
    assert lane["quarantined"] is True
    assert lane["readers"] == 0

    with forcing_recovery(), pytest.raises(AppError) as again:
        with pool.reader("docs", path=path):
            pass  # pragma: no cover
    assert again.value.code == "recovery_required"

    with forcing_recovery():
        # Diagnostics is the way *into* the recovery panel, so it keeps answering.
        with pool.reader("docs", path=path, diagnostics=True) as db:
            assert db.catalog.catalog is not None

    # Closing the lane — which is what the recovery action does — ends the quarantine.
    pool.close("docs")
    assert pool.open_slugs() == []
    with pool.reader("docs", path=path) as db:
        assert db.catalog.catalog is not None


def test_a_quarantined_store_still_leads_the_operator_to_recovery(daemon: Harness):
    with forcing_recovery():
        refused = daemon.query(daemon.secret, "MATCH (d:Doc) RETURN d.id")  # type: ignore[attr-defined]
        assert refused.status_code == 503
        error = refused.json()["error"]
        assert error["code"] == "recovery_required"
        assert error["retryable"] is True
        assert error["details"]["hint"] == "run_recover_action"
        assert "maintenance panel" in error["message"]
        assert "Traceback" not in error["message"]

        # The data plane is shut: a store that cannot prove consistency serves no rows.
        schema = daemon.client.get(
            "/api/v1/databases/docs/schema",
            headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
        )
        assert schema.status_code == 503
        assert schema.json()["error"]["code"] == "recovery_required"

        # The administrator's view answers anyway, and says what is wrong — that panel is the remedy.
        inventory = daemon.client.get("/admin/api/databases/docs/inventory")
        assert inventory.status_code == 200
        assert inventory.json()["maintenance"]["recovery_required"] is True
        listed = daemon.client.get("/admin/api/databases").json()
        assert [row["slug"] for row in listed["databases"]] == ["docs"]

    # Closing the lane (the recovery action closes handles the same way) clears the refusal.
    daemon.client.post("/admin/api/databases/docs/close?force=true")

    reopened = daemon.query(daemon.secret, "MATCH (d:Doc) RETURN d.id")  # type: ignore[attr-defined]
    assert reopened.status_code == 200, reopened.text
    assert len(reopened.json()["rows"]) == 4


def test_the_pool_reports_what_it_is_holding(daemon: Harness) -> None:
    daemon.query(daemon.secret, "MATCH (d:Doc) RETURN d.id")  # type: ignore[attr-defined]
    handles: list[dict[str, Any]] = daemon.context.pool.status()
    assert [row["slug"] for row in handles] == ["docs"]
    row = handles[0]
    assert row["readers"] >= 1
    assert row["buffer_budget_bytes"] == DEFAULT_BUFFER_BUDGET_BYTES
    assert row["quarantined"] is False
    assert row["idle_seconds"] >= 0.0
    assert row["path"].endswith("docs")
    # The pool's ceiling is the plan's arithmetic: handles x the engine's own default budget.
    assert daemon.context.pool.buffer_budget_ceiling_bytes == (
        daemon.context.pool.max_open_databases * DEFAULT_BUFFER_BUDGET_BYTES
    )
    assert DEFAULT_BUFFER_BUDGET_BYTES == 64 * MIB
