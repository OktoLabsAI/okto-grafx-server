"""The server's own control plane, in local SQLite (stdlib ``sqlite3``, WAL).

This is deliberately separate from the stores it manages: registering a database must never
require writing inside that database. Migrations are numbered and additive; ``init`` and ``run``
apply them idempotently.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from ..errors import AppError, conflict, not_found
from .keys import fingerprint, generate_api_key, hash_secret

SLUG_RE = re.compile(r"^[a-z0-9_-]{1,64}$")

MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE databases (
        id INTEGER PRIMARY KEY,
        slug TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        path TEXT NOT NULL UNIQUE,
        database_uuid TEXT,
        read_only INTEGER NOT NULL DEFAULT 0,
        allow_writes INTEGER NOT NULL DEFAULT 0,
        options_json TEXT NOT NULL DEFAULT '{}',
        enabled INTEGER NOT NULL DEFAULT 1,
        open_policy TEXT NOT NULL DEFAULT 'lazy',
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE api_keys (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        key_hash BLOB NOT NULL UNIQUE,
        key_prefix TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        allow_writes INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        last_used_at TEXT,
        revoked_at TEXT
    );
    CREATE TABLE api_key_database (
        api_key_id INTEGER NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
        database_id INTEGER NOT NULL REFERENCES databases(id) ON DELETE CASCADE,
        UNIQUE (api_key_id, database_id)
    );
    CREATE TABLE admin_auth (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        token_hash BLOB NOT NULL,
        rotated_at TEXT NOT NULL
    );
    CREATE TABLE admin_session (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    CREATE TABLE jobs (
        id TEXT PRIMARY KEY,
        database_id INTEGER NOT NULL REFERENCES databases(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        state TEXT NOT NULL,
        detail_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT
    );
    CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        database_id INTEGER,
        request_json TEXT NOT NULL DEFAULT '{}',
        outcome TEXT NOT NULL,
        rows INTEGER,
        duration_ms REAL
    );
    CREATE INDEX idx_audit_at ON audit_log (at DESC);
    CREATE INDEX idx_jobs_db ON jobs (database_id, created_at DESC);
    """,
    # The format version is recorded at registration so the UI can show what a store is, and so a
    # store that changed underneath us is visible without opening it.
    """
    ALTER TABLE databases ADD COLUMN format_version INTEGER;
    """,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def valid_slug(slug: str) -> str:
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise AppError(
            "A database id must be 1-64 characters of [a-z0-9_-].",
            status=422,
            code="invalid_slug",
            field="slug",
            value=slug,
        )
    return slug


@dataclass
class DatabaseRecord:
    id: int
    slug: str
    name: str
    path: str
    database_uuid: str | None
    format_version: int | None
    read_only: bool
    allow_writes: bool
    options: dict[str, Any]
    enabled: bool
    open_policy: str
    note: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "DatabaseRecord":
        return cls(
            id=row["id"],
            slug=row["slug"],
            name=row["name"],
            path=row["path"],
            database_uuid=row["database_uuid"],
            format_version=row["format_version"],
            read_only=bool(row["read_only"]),
            allow_writes=bool(row["allow_writes"]),
            options=json.loads(row["options_json"] or "{}"),
            enabled=bool(row["enabled"]),
            open_policy=row["open_policy"],
            note=row["note"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def public(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "path": self.path,
            "database_uuid": self.database_uuid,
            "format_version": self.format_version,
            "read_only": self.read_only,
            "allow_writes": self.allow_writes,
            "options": self.options,
            "enabled": self.enabled,
            "open_policy": self.open_policy,
            "note": self.note,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class KeyRecord:
    id: int
    name: str
    key_prefix: str
    fingerprint: str
    allow_writes: bool
    enabled: bool
    created_at: str
    last_used_at: str | None
    revoked_at: str | None
    databases: list[str] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: sqlite3.Row, databases: Iterable[str] = ()) -> "KeyRecord":
        return cls(
            id=row["id"],
            name=row["name"],
            key_prefix=row["key_prefix"],
            fingerprint=row["fingerprint"],
            allow_writes=bool(row["allow_writes"]),
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            revoked_at=row["revoked_at"],
            databases=sorted(databases),
        )

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "prefix": self.key_prefix,
            "fingerprint": self.fingerprint,
            "allow_writes": self.allow_writes,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "revoked_at": self.revoked_at,
            "databases": self.databases,
        }


@dataclass
class JobRecord:
    id: str
    database_id: int
    kind: str
    state: str
    detail: dict[str, Any]
    created_at: str
    started_at: str | None
    finished_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "JobRecord":
        return cls(
            id=row["id"],
            database_id=row["database_id"],
            kind=row["kind"],
            state=row["state"],
            detail=json.loads(row["detail_json"] or "{}"),
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "database_id": self.database_id,
            "kind": self.kind,
            "state": self.state,
            "detail": self.detail,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class ControlStore:
    """Serialized access to ``server.db``. One process, one writer, WAL for readers."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=30.0
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=30000")
        self.migrate()

    # -- lifecycle ---------------------------------------------------------------------

    def migrate(self) -> None:
        with self._lock:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, "
                "applied_at TEXT NOT NULL)"
            )
            applied = {
                row["version"]
                for row in self._connection.execute("SELECT version FROM schema_version")
            }
            for number, script in enumerate(MIGRATIONS, start=1):
                if number in applied:
                    continue
                self._connection.executescript(script)
                self._connection.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (number, now_iso()),
                )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _write(self, sql: str, values: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._connection.execute(sql, values)
            self._connection.commit()
            return cursor

    def _read(self, sql: str, values: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._connection.execute(sql, values))

    # -- databases ---------------------------------------------------------------------

    def list_databases(self) -> list[DatabaseRecord]:
        return [
            DatabaseRecord.from_row(row)
            for row in self._read("SELECT * FROM databases ORDER BY slug")
        ]

    def get_database(self, slug: str) -> DatabaseRecord:
        rows = self._read("SELECT * FROM databases WHERE slug = ?", (slug,))
        if not rows:
            raise not_found(f"No database registered with id {slug!r}.", field="slug")
        return DatabaseRecord.from_row(rows[0])

    def get_database_by_path(self, path: str) -> DatabaseRecord | None:
        rows = self._read("SELECT * FROM databases WHERE path = ?", (path,))
        return DatabaseRecord.from_row(rows[0]) if rows else None

    def get_database_by_uuid(self, database_uuid: str) -> DatabaseRecord | None:
        if not database_uuid:
            return None
        rows = self._read("SELECT * FROM databases WHERE database_uuid = ?", (database_uuid,))
        return DatabaseRecord.from_row(rows[0]) if rows else None

    def add_database(
        self,
        *,
        slug: str,
        name: str,
        path: str,
        database_uuid: str | None,
        format_version: int | None = None,
        read_only: bool = False,
        allow_writes: bool = False,
        options: dict[str, Any] | None = None,
        open_policy: str = "lazy",
        note: str = "",
    ) -> DatabaseRecord:
        valid_slug(slug)
        if open_policy not in {"lazy", "pinned"}:
            raise AppError(
                "open_policy must be 'lazy' or 'pinned'.",
                status=422,
                code="invalid_open_policy",
                field="open_policy",
            )
        if self.get_database_by_path(path) is not None:
            raise conflict(
                "That directory is already registered under another id.",
                code="database_path_taken",
                details={"path": path},
            )
        existing = self.get_database_by_uuid(database_uuid or "")
        if existing is not None:
            raise conflict(
                f"A database with physical identity {database_uuid} is already registered as "
                f"{existing.slug!r}; a physical clone is a different store, not a second view.",
                code="database_uuid_taken",
                details={"slug": existing.slug},
            )
        stamp = now_iso()
        with self._lock:
            try:
                self._connection.execute(
                    "INSERT INTO databases (slug, name, path, database_uuid, format_version, "
                    "read_only, allow_writes, options_json, enabled, open_policy, note, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        slug,
                        name or slug,
                        path,
                        database_uuid,
                        format_version,
                        int(read_only),
                        int(allow_writes),
                        json.dumps(options or {}),
                        1,
                        open_policy,
                        note,
                        stamp,
                        stamp,
                    ),
                )
                self._connection.commit()
            except sqlite3.IntegrityError as exc:
                raise conflict(
                    f"A database with id {slug!r} already exists.",
                    code="database_slug_taken",
                    field="slug",
                ) from exc
        return self.get_database(slug)

    _UPDATABLE = {
        "name",
        "format_version",
        "read_only",
        "allow_writes",
        "enabled",
        "open_policy",
        "note",
        "options",
        "database_uuid",
    }

    def update_database(self, slug: str, changes: dict[str, Any]) -> DatabaseRecord:
        current = self.get_database(slug)
        unknown = sorted(set(changes) - self._UPDATABLE)
        if unknown:
            raise AppError(
                f"Cannot change: {', '.join(unknown)}.",
                status=422,
                code="invalid_database_change",
                field=unknown[0],
            )
        sets: list[str] = []
        values: list[Any] = []
        for key, value in changes.items():
            if key == "options":
                # The column is options_json; the field the caller sees is options.
                value = json.dumps(value or {})
                key = "options_json"
            elif key in {"read_only", "allow_writes", "enabled"}:
                value = int(bool(value))
            sets.append(f"{key} = ?")
            values.append(value)
        if not sets:
            return current
        if (changes.get("read_only") is None and current.read_only) and bool(
            changes.get("allow_writes")
        ):
            raise conflict(
                "This database is registered read-only; clear read_only before allowing writes.",
                code="read_only_conflict",
            )
        values.extend([now_iso(), slug])
        self._write(
            f"UPDATE databases SET {', '.join(sets)}, updated_at = ? WHERE slug = ?",
            tuple(values),
        )
        return self.get_database(slug)

    def remove_database(self, slug: str) -> DatabaseRecord:
        record = self.get_database(slug)
        self._write("DELETE FROM databases WHERE slug = ?", (slug,))
        return record

    def set_database_uuid(self, slug: str, database_uuid: str) -> None:
        self._write(
            "UPDATE databases SET database_uuid = ?, updated_at = ? WHERE slug = ?",
            (database_uuid, now_iso(), slug),
        )

    # -- api keys ----------------------------------------------------------------------

    def create_key(
        self, *, name: str, databases: list[str], allow_writes: bool = False
    ) -> tuple[KeyRecord, str]:
        if not name.strip():
            raise AppError(
                "A key needs a name.", status=422, code="invalid_key_name", field="name"
            )
        slugs = sorted({valid_slug(s) for s in databases})
        secret = generate_api_key()
        with self._lock:
            cursor = self._connection.execute(
                "INSERT INTO api_keys (name, key_hash, key_prefix, fingerprint, allow_writes, "
                "enabled, created_at) VALUES (?,?,?,?,?,1,?)",
                (
                    name.strip(),
                    hash_secret(secret),
                    secret[:10] + "…",
                    fingerprint(secret),
                    int(allow_writes),
                    now_iso(),
                ),
            )
            key_id = int(cursor.lastrowid)
            for slug in slugs:
                row = self._connection.execute(
                    "SELECT id FROM databases WHERE slug = ?", (slug,)
                ).fetchone()
                if row is None:
                    raise not_found(f"Unknown database {slug!r}.", field="databases")
                self._connection.execute(
                    "INSERT INTO api_key_database (api_key_id, database_id) VALUES (?,?)",
                    (key_id, row["id"]),
                )
            self._connection.commit()
        return self.get_key(key_id), secret

    def get_key(self, key_id: int) -> KeyRecord:
        rows = self._read("SELECT * FROM api_keys WHERE id = ?", (key_id,))
        if not rows:
            raise not_found(f"No API key with id {key_id}.")
        return KeyRecord.from_row(rows[0], self._key_slugs(key_id))

    def list_keys(self) -> list[KeyRecord]:
        out: list[KeyRecord] = []
        for row in self._read("SELECT * FROM api_keys ORDER BY id"):
            out.append(KeyRecord.from_row(row, self._key_slugs(row["id"])))
        return out

    def keys_for_database(self, database_id: int) -> list[KeyRecord]:
        rows = self._read(
            "SELECT k.* FROM api_keys k JOIN api_key_database m ON m.api_key_id = k.id "
            "WHERE m.database_id = ? ORDER BY k.id",
            (database_id,),
        )
        return [KeyRecord.from_row(row, self._key_slugs(row["id"])) for row in rows]

    def _key_slugs(self, key_id: int) -> list[str]:
        return [
            row["slug"]
            for row in self._read(
                "SELECT d.slug FROM databases d JOIN api_key_database m ON m.database_id = d.id "
                "WHERE m.api_key_id = ?",
                (key_id,),
            )
        ]

    def authenticate_key(self, secret: str) -> KeyRecord:
        rows = self._read(
            "SELECT * FROM api_keys WHERE key_hash = ?", (hash_secret(secret),)
        )
        if not rows:
            raise AppError(
                "That API key is unknown.", status=401, code="unknown_api_key"
            )
        record = KeyRecord.from_row(rows[0], self._key_slugs(rows[0]["id"]))
        if record.revoked_at or not record.enabled:
            raise AppError("That API key has been revoked.", status=401, code="revoked_api_key")
        return record

    def touch_key(self, key_id: int) -> None:
        self._write("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now_iso(), key_id))

    def set_key_scopes(
        self, key_id: int, *, databases: list[str], allow_writes: bool
    ) -> KeyRecord:
        self.get_key(key_id)
        slugs = sorted({valid_slug(s) for s in databases})
        with self._lock:
            self._connection.execute(
                "DELETE FROM api_key_database WHERE api_key_id = ?", (key_id,)
            )
            for slug in slugs:
                row = self._connection.execute(
                    "SELECT id FROM databases WHERE slug = ?", (slug,)
                ).fetchone()
                if row is None:
                    raise not_found(f"Unknown database {slug!r}.", field="databases")
                self._connection.execute(
                    "INSERT INTO api_key_database (api_key_id, database_id) VALUES (?,?)",
                    (key_id, row["id"]),
                )
            self._connection.execute(
                "UPDATE api_keys SET allow_writes = ? WHERE id = ?",
                (int(allow_writes), key_id),
            )
            self._connection.commit()
        return self.get_key(key_id)

    def revoke_key(self, key_id: int) -> KeyRecord:
        self.get_key(key_id)
        self._write(
            "UPDATE api_keys SET revoked_at = ?, enabled = 0 WHERE id = ?", (now_iso(), key_id)
        )
        return self.get_key(key_id)

    def delete_key(self, key_id: int) -> None:
        self.get_key(key_id)
        self._write("DELETE FROM api_key_database WHERE api_key_id = ?", (key_id,))
        self._write("DELETE FROM api_keys WHERE id = ?", (key_id,))

    # -- admin auth --------------------------------------------------------------------

    def set_admin_token(self, secret: str) -> None:
        self._write(
            "INSERT INTO admin_auth (id, token_hash, rotated_at) VALUES (1,?,?) "
            "ON CONFLICT(id) DO UPDATE SET token_hash = excluded.token_hash, "
            "rotated_at = excluded.rotated_at",
            (hash_secret(secret), now_iso()),
        )

    def admin_token_configured(self) -> bool:
        return bool(self._read("SELECT 1 FROM admin_auth WHERE id = 1"))

    def admin_token_rotated_at(self) -> str | None:
        rows = self._read("SELECT rotated_at FROM admin_auth WHERE id = 1")
        return str(rows[0]["rotated_at"]) if rows else None

    def clear_admin_token(self) -> None:
        """Forget the administrator token, which puts the console back to loopback-only access.

        Sessions are left alone on purpose. They were issued to a browser that proved the token, and
        pulling them out from under an operator who is *turning a wall down* would log them out of
        the work they are standing in.
        """

        self._write("DELETE FROM admin_auth WHERE id = 1", ())

    def check_admin_token(self, candidate: str) -> bool:
        rows = self._read("SELECT token_hash FROM admin_auth WHERE id = 1")
        if not rows:
            return False
        return bytes(rows[0]["token_hash"]) == hash_secret(candidate or "")

    def create_session(self, ttl_hours: float = 12.0) -> str:
        session_id = uuid.uuid4().hex
        expires = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat(
            timespec="seconds"
        )
        self._write(
            "INSERT INTO admin_session (id, created_at, expires_at) VALUES (?,?,?)",
            (session_id, now_iso(), expires),
        )
        return session_id

    def validate_session(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        rows = self._read(
            "SELECT expires_at FROM admin_session WHERE id = ?", (session_id,)
        )
        if not rows:
            return False
        return rows[0]["expires_at"] > now_iso()

    def revoke_session(self, session_id: str | None) -> None:
        if session_id:
            self._write("DELETE FROM admin_session WHERE id = ?", (session_id,))

    def revoke_all_sessions(self) -> int:
        """Sign every browser out. Rotation says "the old token and every session signed in with it
        stop working", and a session that outlives the credential that produced it contradicts that.
        """

        cursor = self._write("DELETE FROM admin_session", ())
        return cursor.rowcount or 0

    def prune_sessions(self) -> int:
        cursor = self._write("DELETE FROM admin_session WHERE expires_at <= ?", (now_iso(),))
        return cursor.rowcount or 0

    # -- jobs --------------------------------------------------------------------------

    def create_job(self, *, database_id: int, kind: str) -> JobRecord:
        job_id = uuid.uuid4().hex
        self._write(
            "INSERT INTO jobs (id, database_id, kind, state, created_at) VALUES (?,?,?,?,?)",
            (job_id, database_id, kind, "queued", now_iso()),
        )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> JobRecord:
        rows = self._read("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not rows:
            raise not_found(f"No job with id {job_id!r}.")
        return JobRecord.from_row(rows[0])

    def list_jobs(self, database_id: int | None = None, limit: int = 50) -> list[JobRecord]:
        if database_id is None:
            rows = self._read("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        else:
            rows = self._read(
                "SELECT * FROM jobs WHERE database_id = ? ORDER BY created_at DESC LIMIT ?",
                (database_id, limit),
            )
        return [JobRecord.from_row(row) for row in rows]

    def active_job(self, database_id: int) -> JobRecord | None:
        rows = self._read(
            "SELECT * FROM jobs WHERE database_id = ? AND state IN ('queued','running') "
            "ORDER BY created_at DESC LIMIT 1",
            (database_id,),
        )
        return JobRecord.from_row(rows[0]) if rows else None

    def set_job_state(
        self, job_id: str, state: str, detail: dict[str, Any] | None = None
    ) -> JobRecord:
        current = self.get_job(job_id)
        values: list[Any] = [state]
        sets = ["state = ?"]
        if state == "running":
            sets.append("started_at = ?")
            values.append(now_iso())
        if state in {"succeeded", "failed", "cancelled"}:
            sets.append("finished_at = ?")
            values.append(now_iso())
        if detail is not None:
            sets.append("detail_json = ?")
            values.append(json.dumps(detail, default=str))
        values.append(job_id)
        del current
        self._write(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", tuple(values))
        return self.get_job(job_id)

    # -- audit -------------------------------------------------------------------------

    def audit(
        self,
        *,
        actor: str,
        action: str,
        database_slug: str | None = None,
        request: dict[str, Any] | None = None,
        outcome: str = "ok",
        rows: int | None = None,
        duration_ms: float | None = None,
    ) -> None:
        database_id: int | None = None
        if database_slug:
            row = self._read("SELECT id FROM databases WHERE slug = ?", (database_slug,))
            database_id = row[0]["id"] if row else None
        self._write(
            "INSERT INTO audit_log (at, actor, action, database_id, request_json, outcome, "
            "rows, duration_ms) VALUES (?,?,?,?,?,?,?,?)",
            (
                now_iso(),
                actor,
                action,
                database_id,
                json.dumps(request or {}, default=str)[:4000],
                outcome,
                rows,
                duration_ms,
            ),
        )

    def list_audit(
        self,
        *,
        actor: str | None = None,
        database_slug: str | None = None,
        action: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT a.*, d.slug AS database_slug FROM audit_log a "
            "LEFT JOIN databases d ON d.id = a.database_id WHERE 1=1"
        )
        values: list[Any] = []
        if actor:
            sql += " AND a.actor = ?"
            values.append(actor)
        if action:
            sql += " AND a.action = ?"
            values.append(action)
        if database_slug:
            sql += " AND d.slug = ?"
            values.append(database_slug)
        sql += " ORDER BY a.id DESC LIMIT ?"
        values.append(max(1, min(limit, 1000)))
        out = []
        for row in self._read(sql, tuple(values)):
            out.append(
                {
                    "id": row["id"],
                    "at": row["at"],
                    "actor": row["actor"],
                    "action": row["action"],
                    "database": row["database_slug"],
                    "request": json.loads(row["request_json"] or "{}"),
                    "outcome": row["outcome"],
                    "rows": row["rows"],
                    "duration_ms": row["duration_ms"],
                }
            )
        return out

    def prune_audit(self, days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
            timespec="seconds"
        )
        cursor = self._write("DELETE FROM audit_log WHERE at < ?", (cutoff,))
        return cursor.rowcount or 0
