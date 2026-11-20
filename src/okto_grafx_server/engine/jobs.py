"""Maintenance jobs: serial per database, quiesced, and never a surprise to a live reader.

``recover()``, ``vacuum()`` and index rebuilds have stronger lifecycle requirements than a normal
read: the library requires local quiescence and coordinates with writers. A job therefore
*refuses* while the server still has operations or open transactions on that store, closes the
server's handles, works on a dedicated handle, and leaves the store closed so the next request
reopens it through the normal path. One active job per database; further requests queue behind it.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
import queue
import threading
import time
from typing import Any, Callable

from okto_grafx import connect

from ..control.store import ControlStore
from ..errors import AppError, conflict, map_grafx_error
from .handles import HandlePool

log = logging.getLogger("okto_grafx_server.jobs")

@dataclass(frozen=True)
class _Action:
    """What one maintenance action costs before it is allowed to run.

    Three independent gates, because the library has three different requirements and the operator
    deserves to be told which one refused:

    ``read_only_ok``
        the action can be performed on a read-only open. Anything that proves durability or
        reclaims space cannot: a read-only open cannot prove a checkpoint-complete database.
    ``quiescing``
        the server must have no live handles, operations or transactions on the store.
    ``confirm_quiescent``
        the *operator* must assert that no other Grafx process holds this store. The server can
        only see its own handles, so it never infers this on the caller's behalf.
    """

    read_only_ok: bool = False
    quiescing: bool = False
    confirm_quiescent: bool = False
    requires: tuple[str, ...] = ()
    description: str = ""


ACTIONS: dict[str, _Action] = {
    "verify": _Action(
        read_only_ok=True,
        quiescing=True,
        description="Scan pages, records and index entries for corruption (read-only).",
    ),
    "bloat": _Action(
        read_only_ok=True,
        description="Census of recyclable space per table (read-only, no quiescence needed).",
    ),
    "status": _Action(
        read_only_ok=True,
        description="WAL size, checkpoint lag and recovery need as this handle last observed them.",
    ),
    "checkpoint": _Action(
        description="Recycle the WAL up to the horizon; needs a writable open.",
    ),
    "recover": _Action(
        quiescing=True, description="Replay the WAL and reconcile the catalog."
    ),
    "vacuum": _Action(
        quiescing=True,
        confirm_quiescent=True,
        description="Foreground MVCC reclamation. Requires confirm_quiescent.",
    ),
    "cleanup_indexes": _Action(
        quiescing=True,
        confirm_quiescent=True,
        description="Inventory or reclaim unreferenced index generations. Requires "
        "confirm_quiescent.",
    ),
    "rebuild_index": _Action(
        quiescing=True, requires=("name",), description="Rebuild one exact index."
    ),
    "rebuild_vector_index": _Action(
        quiescing=True,
        requires=("space",),
        description="Rebuild the ANN index of one vector space (optionally one table).",
    ),
    "rehash_index": _Action(
        quiescing=True,
        requires=("name",),
        description="Grow one exact index (bucket count / expected cardinality).",
    ),
}

MAINTENANCE_ACTIONS = tuple(ACTIONS)


def describe_actions() -> list[dict[str, Any]]:
    """The maintenance panel is generated from this, not hard-coded in the frontend."""

    return [
        {
            "action": name,
            "description": action.description,
            "read_only_ok": action.read_only_ok,
            "quiescing": action.quiescing,
            "confirm_quiescent": action.confirm_quiescent,
            "requires": list(action.requires),
        }
        for name, action in ACTIONS.items()
    ]


def describe_report(report: Any) -> dict[str, Any]:
    """A JSON-safe view of a library report dataclass, for the job panel."""

    if report is None:
        return {}
    if dataclasses.is_dataclass(report) and not isinstance(report, type):
        return {f.name: _plain(getattr(report, f.name, None)) for f in dataclasses.fields(report)}
    return {"value": _plain(report)}


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name, None)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value][:200]
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, (str, int)):
        return enum_value
    return str(value)[:512]


class JobRunner:
    """A single worker thread with a per-database serialised queue."""

    def __init__(
        self,
        *,
        store: ControlStore,
        pool: HandlePool,
        on_finished: Callable[[str, str], None] | None = None,
    ) -> None:
        self._store = store
        self._pool = pool
        self._on_finished = on_finished
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._database_busy: set[int] = set()
        self._lock = threading.Lock()

    def start(self) -> None:
        if not self._threads:
            thread = threading.Thread(target=self._loop, name="grafx-job-runner", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)
        for thread in self._threads:
            thread.join(timeout=5.0)
        self._threads = []

    def submit(
        self,
        *,
        database_id: int,
        slug: str,
        path: str,
        kind: str,
        options: dict[str, Any],
        arguments: dict[str, Any] | None = None,
        read_only: bool = False,
    ) -> str:
        self._check(kind, slug, options, arguments or {}, read_only=read_only)
        with self._lock:
            if self._store.active_job(database_id) is not None:
                raise conflict(
                    f"Database {slug!r} already has a maintenance job running.",
                    code="job_active",
                    details={"slug": slug},
                )
            job = self._store.create_job(database_id=database_id, kind=kind)
            self._store.set_job_state(
                job.id,
                "queued",
                {
                    "slug": slug,
                    "path": path,
                    "options": options,
                    "action": kind,
                    "arguments": arguments or {},
                    "read_only": read_only,
                },
            )
            self._queue.put(job.id)
        return job.id

    # -- execution ---------------------------------------------------------------------

    def _check(
        self,
        kind: str,
        slug: str,
        options: dict[str, Any],
        arguments: dict[str, Any],
        *,
        read_only: bool,
    ) -> None:
        """Refuse at the door what the engine would refuse later, with a better sentence."""

        action = ACTIONS.get(kind)
        if action is None:
            raise AppError(
                f"Unknown maintenance action {kind!r}. Known actions: "
                f"{', '.join(sorted(ACTIONS))}.",
                status=422,
                code="unknown_maintenance_action",
                field="action",
            )
        missing = [name for name in action.requires if not arguments.get(name)]
        if missing:
            raise AppError(
                f"Maintenance action {kind!r} needs {', '.join(missing)} in 'arguments'.",
                status=422,
                code="maintenance_argument_required",
                field="arguments",
                details={"action": kind, "missing": missing, "requires": list(action.requires)},
            )
        if read_only and not action.read_only_ok:
            raise AppError(
                f"Maintenance action {kind!r} cannot run on {slug!r} while it is registered "
                "read-only: a read-only open cannot prove a checkpoint-complete database, and it "
                "cannot reclaim anything. Clear read_only to run it.",
                status=409,
                code="maintenance_requires_writable",
                details={"slug": slug, "action": kind},
            )
        if action.confirm_quiescent and not arguments.get("confirm_quiescent"):
            raise AppError(
                f"Maintenance action {kind!r} needs arguments.confirm_quiescent = true. This "
                f"server can only quiesce its own handles on {slug!r}: another Grafx process may "
                "have the same store open, and only the operator knows that it does not.",
                status=409,
                code="maintenance_confirmation_required",
                details={"slug": slug, "action": kind, "confirm_quiescent": True},
            )

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if job_id is None:
                break
            try:
                self._run(job_id)
            except Exception as exc:  # noqa: BLE001 - a job must always settle
                log.exception("job %s crashed", job_id)
                try:
                    self._store.set_job_state(job_id, "failed", {"error": str(exc)[:500]})
                except Exception:  # pragma: no cover - control db is failing too
                    pass

    def _run(self, job_id: str) -> None:
        job = self._store.get_job(job_id)
        slug = str(job.detail.get("slug") or "")
        path = str(job.detail.get("path") or "")
        if not path:
            databases = {d.slug: d for d in self._store.list_databases()}
            record = databases.get(slug)
            if record is None:
                self._store.set_job_state(job_id, "failed", {"error": "database was removed"})
                return
            path = record.path
        options = dict(job.detail.get("options") or {})
        arguments = dict(job.detail.get("arguments") or {})
        read_only = bool(job.detail.get("read_only"))
        self._store.set_job_state(job_id, "running", {**job.detail, "started_by": "job-runner"})
        started = time.monotonic()
        try:
            detail = self._execute(
                job.kind, path, options, arguments, slug, job.database_id, read_only=read_only
            )
            detail["duration_ms"] = round((time.monotonic() - started) * 1000.0, 1)
            self._store.set_job_state(job_id, "succeeded", detail)
        except AppError as exc:
            self._store.set_job_state(
                job_id,
                "failed",
                {"code": exc.code, "message": exc.message, "details": exc.details},
            )
        except Exception as exc:  # noqa: BLE001 - report the typed refusal
            mapped = map_grafx_error(exc)
            message = mapped.message
            if read_only and "read-only open cannot prove" in message:
                message = (
                    message
                    + f" Clear read_only on {slug!r} and run 'recover' then 'checkpoint' once; a "
                    "read-only open can only prove consistency of a checkpoint-complete store."
                )
            self._store.set_job_state(
                job_id,
                "failed",
                {"code": mapped.code, "message": message, "details": mapped.details},
            )
        finally:
            if self._on_finished is not None:
                try:
                    self._on_finished(job_id, slug)
                except Exception:  # noqa: BLE001
                    log.exception("job completion hook failed for %s", job_id)

    def _execute(
        self,
        kind: str,
        path: str,
        options: dict[str, Any],
        arguments: dict[str, Any],
        slug: str,
        database_id: int,
        *,
        read_only: bool = False,
    ) -> dict[str, Any]:
        action = ACTIONS[kind]
        if action.quiescing:
            self._pool.close(slug)
            if self._pool.active_operations(slug) or self._pool.open_transactions(slug):
                raise conflict(
                    f"Database {slug!r} still has in-flight operations or open transactions.",
                    code="maintenance_not_quiesced",
                    details={
                        "active_operations": self._pool.active_operations(slug),
                        "open_transactions": self._pool.open_transactions(slug),
                    },
                )
        # A read-only registration can only run the read-only actions; everything else was
        # refused at the door. A writable registration is opened writable, because a read-only
        # open cannot prove durability.
        handle = connect(path, **{**options, "read_only": read_only})
        try:
            if kind == "verify":
                report = handle.verify(str(arguments.get("scope", "all")))
            elif kind == "bloat":
                report = handle.maintenance.bloat(
                    arguments.get("table") if arguments.get("table") else None
                )
            elif kind == "recover":
                report = handle.maintenance.recover()
            elif kind == "checkpoint":
                report = handle.maintenance.checkpoint()
            elif kind == "vacuum":
                report = handle.maintenance.vacuum(
                    arguments.get("table") if arguments.get("table") else None,
                    confirm_quiescent=True,
                    max_versions=arguments.get("max_versions"),
                    index_free_pages=bool(arguments.get("index_free_pages", False)),
                )
            elif kind == "rebuild_index":
                report = handle.maintenance.rebuild_index(str(arguments["name"]))
            elif kind == "rebuild_vector_index":
                report = handle.maintenance.rebuild_vector_index(
                    str(arguments["space"]),
                    table=arguments.get("table"),
                )
            elif kind == "rehash_index":
                report = handle.maintenance.rehash_index(
                    str(arguments["name"]),
                    bucket_count=arguments.get("bucket_count"),
                    expected_cardinality=arguments.get("expected_cardinality"),
                )
            elif kind == "cleanup_indexes":
                report = handle.maintenance.cleanup_indexes(
                    dry_run=bool(arguments.get("dry_run", True)),
                    confirm_quiescent=True,
                    max_files=int(arguments.get("max_files", 10_000)),
                    max_wal_records=int(arguments.get("max_wal_records", 100_000)),
                )
            elif kind == "status":
                report = handle.maintenance.status()
            else:  # pragma: no cover - guarded by submit()
                raise AppError(
                    f"Unknown action {kind!r}.", status=422, code="unknown_maintenance_action"
                )
            return {"action": kind, "slug": slug, "report": describe_report(report)}
        finally:
            try:
                handle.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("closing maintenance handle failed: %s", exc)
