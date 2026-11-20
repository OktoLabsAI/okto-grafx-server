"""Handle pool: lanes of ``Database`` handles per store, owned by the server process.

The library's integration contract is explicit: a transaction belongs to one handle, a long commit
on a shared handle queues reads behind it, and every extra handle multiplies page budgets and file
descriptors (``max_open_files`` defaults to 256 against a 512-entry Windows stdio limit). So the
pool is not a connection cache of convenience — it is the shape of the concurrency contract:

* **reader lanes**: a bounded number of independently opened handles per store, checked out one at
  a time, so a slow commit on one handle cannot queue an unrelated read on another;
* **one writer lane per store**, exclusive, because the engine keeps a writer lease and an
  exclusive publication section. Two concurrent write sessions would contend on the same lease, so
  the pool says "busy" instead of pretending to scale;
* **handles belong to this process**. They are never forked, pickled or handed to another process;
* **closing is a lifecycle, not a ``del``**: an idle reaper and an LRU bound release page budgets,
  and shutdown drains in-flight work before closing anything, because cancelling a request does not
  cancel native disk I/O.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from okto_grafx import connect
from okto_grafx.engine.database import Database

from ..errors import AppError, conflict, map_grafx_error, too_many

log = logging.getLogger("okto_grafx_server.handles")

# The engine's own default page-cache budget per handle (`DatabaseConfig.buffer_budget_bytes`).
# Every handle gets its own budget, so the pool bounds the product, not just the count.
DEFAULT_BUFFER_BUDGET_BYTES = 64 * 1024 * 1024


class HandleUnavailable(AppError):
    pass


@dataclass
class LaneSpec:
    slug: str
    path: str
    options: dict[str, Any] = field(default_factory=dict)
    read_only: bool = False
    readers: int = 2


class _Lanes:
    """Reader/writer lanes for one store."""

    def __init__(self, spec: LaneSpec) -> None:
        self.spec = spec
        self.opened_at = time.monotonic()
        self.last_used = time.monotonic()
        self.active = 0
        self._readers: queue.LifoQueue[Database] = queue.LifoQueue()
        self._created_readers = 0
        self._writer_lock = threading.Lock()
        self._writer_handle: Database | None = None
        self._state_lock = threading.Lock()
        self._closed = False
        # Set once a handle reports that the store needs recovery: from then on every checkout is
        # refused until the lane is closed, which is what "quarantined until explicit recovery"
        # means in practice — the recover action closes handles, so it gets a fresh lane.
        self._quarantined = False

    # -- recovery quarantine -----------------------------------------------------------

    def _refuse_recovery(self) -> AppError:
        return AppError(
            f"Database {self.spec.slug!r} needs recovery before this server will read or write it "
            "again. The handle is quarantined: run the 'recover' maintenance action from the "
            "maintenance panel (the store must be writable and quiescent), then retry.",
            status=503,
            code="recovery_required",
            retryable=True,
            details={"slug": self.spec.slug, "hint": "run_recover_action"},
        )

    def _accept(self, handle: Database) -> Database:
        """Hand back only a handle that can still prove a consistent read.

        A store that has slipped into ``recovery_required`` would answer reads with whatever its
        pages happen to hold. Serving that quietly is the failure mode the library warns about, so
        the handle is closed, the lane is marked, and the caller is sent to the recovery action.
        """

        if self._quarantined:
            _quietly_close(handle)
            raise self._refuse_recovery()
        try:
            needed = bool(handle.transactions.recovery_required)
        except Exception:  # noqa: BLE001 - a handle that cannot answer is not one to serve from
            needed = True
        if not needed:
            return handle
        self._quarantined = True
        _quietly_close(handle)
        with self._state_lock:
            self._created_readers = max(0, self._created_readers - 1)
        raise self._refuse_recovery()

    def _require_not_quarantined(self) -> None:
        if self._quarantined:
            raise self._refuse_recovery()

    # -- handles -----------------------------------------------------------------------

    def _connect(self, *, read_only: bool) -> Database:
        options = dict(self.spec.options)
        options["read_only"] = bool(read_only)
        try:
            return connect(self.spec.path, **options)
        except Exception as exc:  # noqa: BLE001 - typed refusal surface
            mapped = map_grafx_error(exc)
            mapped.details = {**mapped.details, "slug": self.spec.slug, "path": self.spec.path}
            if read_only and _read_only_consistency(exc):
                # This is the read-only door's one real precondition, and the engine's sentence
                # does not tell the caller what to do about it.
                mapped.message = (
                    f"{self.spec.slug!r} is registered read-only, and this store cannot be opened "
                    "read-only: a read-only open can only prove consistency of a database whose WAL "
                    "is already checkpoint-complete. Clear read_only, run the 'recover' and "
                    "'checkpoint' maintenance actions once, then register it read-only again. "
                    f"(engine: {mapped.message})"
                )
                mapped.details = {**mapped.details, "hint": "recover_then_checkpoint"}
            raise mapped from exc

    def checkout_reader(self, timeout: float, *, diagnostics: bool = False) -> Database:
        with self._state_lock:
            if self._closed:
                raise conflict(
                    f"Database {self.spec.slug!r} is closed.", code="database_closed"
                )
            if self._quarantined and not diagnostics:
                # No new handle is opened for a quarantined store either: refusal is the point.
                raise self._refuse_recovery()
            try:
                handle = self._readers.get_nowait()
            except queue.Empty:
                if self._created_readers < max(1, self.spec.readers):
                    self._created_readers += 1
                    try:
                        handle = self._connect(read_only=self.spec.read_only)
                    except Exception:
                        self._created_readers -= 1
                        raise
                else:
                    handle = None
            if handle is None:
                try:
                    handle = self._readers.get(timeout=timeout)
                except queue.Empty as exc:
                    raise too_many(
                        f"All reader lanes of {self.spec.slug!r} are busy.",
                        code="reader_lanes_busy",
                        details={"slug": self.spec.slug},
                    ) from exc
            elif handle.closed:
                handle = self._connect(read_only=self.spec.read_only)
        self._touch(1)
        try:
            # A diagnostics read is the way *into* the recovery panel, so it is allowed to look at a
            # store this pool refuses to serve — and it must not re-quarantine the lane.
            return handle if diagnostics else self._accept(handle)
        except BaseException:
            self._touch(-1)
            raise

    def return_reader(self, handle: Database) -> None:
        self._touch(-1)
        with self._state_lock:
            if self._closed or handle.closed:
                self._created_readers = max(0, self._created_readers - 1)
                _quietly_close(handle)
                return
            self._readers.put(handle)

    def acquire_writer(self) -> Database:
        if self.spec.read_only:
            raise AppError(
                f"Database {self.spec.slug!r} is registered read-only.",
                status=403,
                code="write_not_permitted",
                details={"slug": self.spec.slug},
            )
        if not self._writer_lock.acquire(blocking=False):
            raise too_many(
                f"The writer lane of {self.spec.slug!r} is busy. The engine keeps one writer "
                "lease per store, so write sessions are serialised by design.",
                code="write_lane_busy",
                details={"slug": self.spec.slug},
            )
        with self._state_lock:
            if self._closed:  # pragma: no cover - race with shutdown
                self._writer_lock.release()
                raise conflict(f"Database {self.spec.slug!r} is closed.", code="database_closed")
            self._require_not_quarantined()
            handle = self._writer_handle
            if handle is None or handle.closed:
                handle = self._connect(read_only=False)
                self._writer_handle = handle
        try:
            checked = self._accept(handle)
        except BaseException:
            self.drop_writer_handle()
            self._writer_lock.release()
            raise
        self._touch(1)
        return checked

    def release_writer(self) -> None:
        self._touch(-1)
        self._writer_lock.release()

    def drop_writer_handle(self) -> None:
        with self._state_lock:
            handle, self._writer_handle = self._writer_handle, None
        if handle is not None and not handle.closed:
            _quietly_close(handle)

    # -- accounting --------------------------------------------------------------------

    def _touch(self, delta: int) -> None:
        with self._state_lock:
            self.active = max(0, self.active + delta)
            self.last_used = time.monotonic()

    def open_transactions(self) -> int:
        total = 0
        with self._state_lock:
            handles = list(self._readers.queue)
            if self._writer_handle is not None:
                handles.append(self._writer_handle)
        for handle in handles:
            try:
                total += int(handle.transactions.open_transactions)
            except Exception:  # noqa: BLE001 - diagnostics must never fail a request
                continue
        return total

    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_used

    def close(self, *, force: bool = False) -> None:
        with self._state_lock:
            if self.active and not force:
                raise conflict(
                    f"Database {self.spec.slug!r} still has {self.active} operation(s) in flight.",
                    code="database_busy",
                    details={"slug": self.spec.slug, "active": self.active},
                )
            self._closed = True
            handles = list(self._readers.queue)
            self._readers = queue.LifoQueue()
            self._created_readers = 0
            writer, self._writer_handle = self._writer_handle, None
        for handle in handles + ([writer] if writer else []):
            _quietly_close(handle)

    def status(self) -> dict[str, Any]:
        return {
            "slug": self.spec.slug,
            "path": self.spec.path,
            "readers": self._created_readers,
            "idle_readers": self._readers.qsize(),
            "writer_open": self._writer_handle is not None and not self._writer_handle.closed,
            "active_operations": self.active,
            "open_transactions": self.open_transactions(),
            "idle_seconds": round(self.idle_seconds(), 1),
            "buffer_budget_bytes": self.budget_bytes,
            "quarantined": self._quarantined,
        }

    @property
    def budget_bytes(self) -> int:
        """Page-cache budget this store's handles are allowed to consume.

        The engine gives *every handle* its own budget, so the cost of warming a store is
        ``buffer_budget_bytes x handles``. The pool uses that number when it decides what to close.
        """

        raw = self.spec.options.get("buffer_budget_bytes", DEFAULT_BUFFER_BUDGET_BYTES)
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):  # pragma: no cover - validated upstream
            return DEFAULT_BUFFER_BUDGET_BYTES


def _quietly_close(handle: Database) -> None:
    try:
        if not handle.closed:
            handle.close()
    except Exception as exc:  # noqa: BLE001 - a close failure must not destroy the rest
        log.warning("closing a handle failed: %s: %s", type(exc).__name__, exc)


class HandlePool:
    """Open, warm, bounded and honest about what is holding a store."""

    def __init__(
        self,
        *,
        max_open_handles: int = 4,
        readers_per_database: int = 2,
        idle_ttl_seconds: float = 120.0,
        checkout_timeout_seconds: float = 30.0,
        reaper_interval_seconds: float = 30.0,
        buffer_budget_ceiling_bytes: int | None = None,
    ) -> None:
        self.max_open_databases = max(1, int(max_open_handles))
        self.readers_per_database = max(1, int(readers_per_database))
        self.idle_ttl_seconds = float(idle_ttl_seconds)
        self.checkout_timeout_seconds = float(checkout_timeout_seconds)
        # Page-cache ceiling for the whole pool: the plan's `buffer_budget_bytes x handles`.
        # Defaults to what `max_open_handles` handles at the library's own default budget cost,
        # so a store configured with an unusual budget cannot silently multiply the daemon RSS.
        self.buffer_budget_ceiling_bytes = int(
            buffer_budget_ceiling_bytes
            if buffer_budget_ceiling_bytes is not None
            else self.max_open_databases * DEFAULT_BUFFER_BUDGET_BYTES
        )
        self._lock = threading.RLock()
        self._lanes: dict[str, _Lanes] = {}
        self._closed = False
        self._reaper_interval = reaper_interval_seconds
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------------------

    def start(self) -> None:
        if self._reaper is None:
            self._reaper = threading.Thread(
                target=self._reap_loop, name="grafx-handle-reaper", daemon=True
            )
            self._reaper.start()

    def _reap_loop(self) -> None:
        while not self._stop.wait(self._reaper_interval):
            try:
                self.reap_idle()
            except Exception as exc:  # noqa: BLE001 - the reaper must survive its own errors
                log.warning("idle reaper failed: %s", exc)

    def stop_reaper(self) -> None:
        self._stop.set()
        if self._reaper is not None:
            self._reaper.join(timeout=5.0)
            self._reaper = None

    def spec_for(self, slug: str, path: str, options: dict[str, Any], read_only: bool) -> LaneSpec:
        return LaneSpec(
            slug=slug,
            path=path,
            options=dict(options),
            read_only=read_only,
            readers=self.readers_per_database,
        )

    def _lanes_for(
        self,
        slug: str,
        *,
        path: str | None = None,
        options: dict[str, Any] | None = None,
        read_only: bool = False,
    ) -> _Lanes:
        if self._closed:
            raise AppError(
                "The server is shutting down.", status=503, code="shutting_down", retryable=True
            )
        with self._lock:
            lanes = self._lanes.get(slug)
            if lanes is None:
                if path is None:
                    raise AppError(
                        f"Database {slug!r} is not open.", status=409, code="database_closed"
                    )
                lanes = _Lanes(self.spec_for(slug, path, options or {}, read_only))
                self._lanes[slug] = lanes
                self._enforce_bound(keep=slug)
            return lanes

    def _enforce_bound(self, keep: str) -> None:
        """Close least-recently-used idle stores until the pool fits its two bounds.

        Two bounds, both real: the number of open handles (`max_open_handles`) and the aggregate
        page-cache budget of those handles (`buffer_budget_bytes x handles`). A store configured
        with a large budget therefore pushes idle stores out early — that is the point, more
        handles multiply the budget rather than share it.
        """

        if len(self._lanes) <= self.max_open_databases and self._budget_total() <= self.buffer_budget_ceiling_bytes:
            return
        candidates = sorted(
            (
                lanes
                for slug, lanes in self._lanes.items()
                if slug != keep and lanes.active == 0
            ),
            key=lambda lanes: lanes.last_used,
        )
        for lanes in candidates:
            if not self._over_bounds(keep=keep):
                break
            log.info(
                "closing idle store %r (%d budget bytes) to stay within the pool bounds",
                lanes.spec.slug,
                lanes.budget_bytes,
            )
            lanes.close(force=True)
            self._lanes.pop(lanes.spec.slug, None)

    def _budget_total(self) -> int:
        return sum(lanes.budget_bytes for lanes in self._lanes.values())

    def _over_bounds(self, keep: str) -> bool:
        """Is the pool over budget or over count, ignoring a lone unavoidable store?

        A single store is always allowed to stay open: refusing to serve the only registered
        database because its own budget exceeds a pool-wide ceiling would be a worse failure than
        the memory it costs.
        """

        remaining = [slug for slug in self._lanes if slug != keep]
        if len(self._lanes) > self.max_open_databases:
            return bool(remaining)
        if self._budget_total() > self.buffer_budget_ceiling_bytes:
            return bool(remaining)
        return False

    def open(self, spec: LaneSpec) -> dict[str, Any]:
        """Open (or warm) a store now and report what it took."""

        with self._lock:
            lanes = self._lanes_for(
                spec.slug, path=spec.path, options=spec.options, read_only=spec.read_only
            )
            handle = lanes.checkout_reader(self.checkout_timeout_seconds)
            identity = handle.identity
            catalog = handle.catalog.catalog
            tables = len(catalog.table_definitions) + len(catalog.relationship_type_definitions)
            lanes.return_reader(handle)
            self._enforce_bound(keep=spec.slug)
            return {
                "slug": spec.slug,
                "opened": True,
                "database_uuid": identity.database_uuid.hex(),
                "page_size": identity.page_size,
                "format_version": identity.format_version,
                "tables": tables,
                "recovery_required": bool(handle.transactions.recovery_required),
            }

    def close(self, slug: str, *, force: bool = False) -> None:
        with self._lock:
            lanes = self._lanes.get(slug)
            if lanes is None:
                return
            lanes.close(force=force)
            self._lanes.pop(slug, None)

    def reap_idle(self) -> list[str]:
        closed: list[str] = []
        with self._lock:
            for slug, lanes in list(self._lanes.items()):
                if lanes.active == 0 and lanes.idle_seconds() > self.idle_ttl_seconds:
                    lanes.close()
                    self._lanes.pop(slug, None)
                    closed.append(slug)
        if closed:
            log.info("closed idle handles: %s", ", ".join(closed))
        return closed

    def shutdown(self, *, drain_seconds: float = 30.0) -> None:
        self._closed = True
        self.stop_reaper()
        deadline = time.monotonic() + drain_seconds
        while time.monotonic() < deadline:
            with self._lock:
                if all(lanes.active == 0 for lanes in self._lanes.values()):
                    break
            time.sleep(0.05)
        with self._lock:
            for lanes in list(self._lanes.values()):
                lanes.close(force=True)
            self._lanes.clear()

    # -- access ------------------------------------------------------------------------

    @contextmanager
    def reader(
        self,
        slug: str,
        *,
        path: str | None = None,
        options: dict[str, Any] | None = None,
        read_only: bool = False,
        diagnostics: bool = False,
    ) -> Iterator[Database]:
        """Serve from a warm reader lane.

        `diagnostics=True` is the one exception to the recovery quarantine: the inventory call that
        tells the operator *why* a store needs recovery must keep working while the store is
        refused for data. It gets a handle, and it serves nothing else.
        """

        lanes = self._lanes_for(slug, path=path, options=options, read_only=read_only)
        handle = lanes.checkout_reader(self.checkout_timeout_seconds, diagnostics=diagnostics)
        try:
            yield handle
        finally:
            lanes.return_reader(handle)

    def acquire_writer(self, slug: str, *, path: str, options: dict[str, Any] | None = None):
        """Take the exclusive writer lane; the caller must release it."""

        lanes = self._lanes_for(slug, path=path, options=options, read_only=False)
        handle = lanes.acquire_writer()

        def release() -> None:
            lanes.release_writer()

        release.handle = handle  # type: ignore[attr-defined]
        return release

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            return [lanes.status() for lanes in self._lanes.values()]

    def open_slugs(self) -> list[str]:
        with self._lock:
            return sorted(self._lanes)

    def is_open(self, slug: str) -> bool:
        with self._lock:
            return slug in self._lanes

    def active_operations(self, slug: str) -> int:
        with self._lock:
            lanes = self._lanes.get(slug)
        return lanes.active if lanes else 0

    def open_transactions(self, slug: str) -> int:
        with self._lock:
            lanes = self._lanes.get(slug)
        return lanes.open_transactions() if lanes else 0


def _read_only_consistency(exc: BaseException) -> bool:
    """True for the engine's refusal to open a non-checkpoint-complete store read-only."""

    text = str(exc)
    return "read-only open cannot prove" in text or "checkpoint-complete" in text
