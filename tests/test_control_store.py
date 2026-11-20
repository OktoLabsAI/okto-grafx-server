"""Control plane: the SQLite state the daemon owns — and nothing about store bytes."""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx_server.control import ControlStore
from okto_grafx_server.control.keys import (
    display_prefix,
    fingerprint,
    generate_admin_token,
    generate_api_key,
    hash_secret,
    looks_like_api_key,
)


@pytest.fixture
def store(tmp_path: Path):
    handle = ControlStore(tmp_path / "state" / "server.db")
    yield handle
    handle.close()


def add_database(handle: ControlStore, slug: str = "docs", **kwargs):
    defaults = dict(
        slug=slug,
        name=slug.title(),
        path=f"/srv/graphs/{slug}",
        database_uuid=None,
        read_only=True,
        allow_writes=False,
    )
    defaults.update(kwargs)
    return handle.add_database(**defaults)


# -- schema ----------------------------------------------------------------------------


def test_migrations_are_idempotent_and_numbered(tmp_path: Path) -> None:
    path = tmp_path / "server.db"
    first = ControlStore(path)
    add_database(first, "keep")
    assert [row["version"] for row in first._read("SELECT version FROM schema_version ORDER BY version")] == [1, 2]
    first.close()

    # Re-opening applies the migrations again without losing anything.
    second = ControlStore(path)
    assert [row["version"] for row in second._read("SELECT version FROM schema_version ORDER BY version")] == [1, 2]
    assert second.get_database("keep").name == "Keep"
    second.close()


def test_format_version_is_recorded(tmp_path: Path) -> None:
    handle = ControlStore(tmp_path / "server.db")
    try:
        record = add_database(handle, "docs", format_version=2, database_uuid="abc")
        assert record.format_version == 2
        assert handle.get_database("docs").public()["format_version"] == 2
    finally:
        handle.close()


# -- admin token and sessions ----------------------------------------------------------


def test_admin_token_is_hashed_and_rotates(store: ControlStore) -> None:
    assert store.admin_token_configured() is False
    token = generate_admin_token()
    store.set_admin_token(token)
    assert store.admin_token_configured() is True
    assert store.check_admin_token(token) is True
    assert store.check_admin_token(token[:-1] + ("a" if token[-1] != "a" else "b")) is False
    assert store.check_admin_token("") is False
    assert store.check_admin_token(None) is False  # type: ignore[arg-type]

    rotated = generate_admin_token()
    store.set_admin_token(rotated)
    assert store.check_admin_token(token) is False
    assert store.check_admin_token(rotated) is True

    with store._lock:
        raw = store._read("SELECT token_hash FROM admin_auth WHERE id = 1")[0]["token_hash"]
    assert raw == hash_secret(rotated)
    assert rotated.encode() not in bytes(raw)


def test_sessions_validate_expire_and_revoke(store: ControlStore) -> None:
    session = store.create_session(ttl_hours=12)
    assert store.validate_session(session) is True
    assert store.validate_session("nope") is False
    assert store.validate_session(None) is False

    store.revoke_session(session)
    assert store.validate_session(session) is False

    expired = store.create_session(ttl_hours=-1)
    assert store.validate_session(expired) is False
    assert store.prune_sessions() == 1


# -- api keys --------------------------------------------------------------------------


def test_keys_are_stored_hashed_and_returned_once(store: ControlStore) -> None:
    add_database(store, "a")
    add_database(store, "b")
    record, secret = store.create_key(name="reader", databases=["a", "b"])

    assert secret.startswith("okg_") and len(secret) == 44
    assert not hasattr(record, "secret")
    assert record.fingerprint == fingerprint(secret)
    assert record.key_prefix == display_prefix(secret)

    authenticated = store.authenticate_key(secret)
    assert authenticated.id == record.id
    assert sorted(authenticated.databases) == ["a", "b"]
    assert store.get_key(record.id).key_prefix == record.key_prefix

    with store._lock:
        raw = store._read("SELECT key_hash FROM api_keys WHERE id = ?", (record.id,))[0]["key_hash"]
    assert bytes(raw) == hash_secret(secret)
    assert secret.encode() not in bytes(raw)


def test_unknown_revoked_and_disabled_keys_are_refused(store: ControlStore) -> None:
    add_database(store, "a")
    record, secret = store.create_key(name="k", databases=["a"])

    with pytest.raises(Exception) as unknown:
        store.authenticate_key(generate_api_key())
    assert getattr(unknown.value, "code", None) == "unknown_api_key"

    store.revoke_key(record.id)
    with pytest.raises(Exception) as revoked:
        store.authenticate_key(secret)
    assert getattr(revoked.value, "code", None) == "revoked_api_key"
    assert store.get_key(record.id).revoked_at

    second_record, second_secret = store.create_key(name="k2", databases=["a"])
    store._write("UPDATE api_keys SET enabled = 0 WHERE id = ?", (second_record.id,))
    with pytest.raises(Exception) as disabled:
        store.authenticate_key(second_secret)
    assert getattr(disabled.value, "code", None) == "revoked_api_key"


def test_key_scopes_are_replaced_not_merged(store: ControlStore) -> None:
    for slug in ("a", "b", "c"):
        add_database(store, slug)
    record, secret = store.create_key(name="k", databases=["a", "b"])
    updated = store.set_key_scopes(record.id, databases=["b", "c"], allow_writes=True)
    assert sorted(updated.databases) == ["b", "c"]
    assert updated.allow_writes is True
    assert store.authenticate_key(secret).allow_writes is True

    emptied = store.set_key_scopes(record.id, databases=[], allow_writes=False)
    assert emptied.databases == [] and emptied.allow_writes is False


def test_key_scope_to_unknown_database_is_refused(store: ControlStore) -> None:
    with pytest.raises(Exception) as refused:
        store.create_key(name="k", databases=["ghost"])
    assert getattr(refused.value, "status", None) == 404


def test_key_listing_and_lookup_by_database(store: ControlStore) -> None:
    first = add_database(store, "a")
    add_database(store, "b")
    record_one, _ = store.create_key(name="one", databases=["a"])
    record_two, _ = store.create_key(name="two", databases=["a", "b"])

    assert {item.name for item in store.list_keys()} == {"one", "two"}
    assert {item.id for item in store.keys_for_database(first.id)} == {
        record_one.id,
        record_two.id,
    }

    store.delete_key(record_one.id)
    with pytest.raises(Exception):
        store.get_key(record_one.id)
    with pytest.raises(Exception) as gone:
        store.authenticate_key(generate_api_key())
    assert getattr(gone.value, "code", None) == "unknown_api_key"


def test_touch_key_records_last_use(store: ControlStore) -> None:
    add_database(store, "a")
    record, _ = store.create_key(name="k", databases=["a"])
    assert record.last_used_at is None
    store.touch_key(record.id)
    assert store.get_key(record.id).last_used_at


# -- databases -------------------------------------------------------------------------


def test_databases_round_trip_with_options(store: ControlStore) -> None:
    record = add_database(
        store,
        "docs",
        database_uuid="abc",
        read_only=False,
        allow_writes=True,
        options={"page_size": 16384},
        open_policy="pinned",
        note="primary",
    )
    loaded = store.get_database("docs")
    assert loaded.id == record.id
    assert loaded.options == {"page_size": 16384}
    assert loaded.allow_writes is True
    assert loaded.open_policy == "pinned"
    assert loaded.note == "primary"
    assert loaded.path == "/srv/graphs/docs"

    updated = store.update_database("docs", {"options": {}, "allow_writes": False})
    assert updated.options == {} and updated.allow_writes is False

    store.remove_database("docs")
    with pytest.raises(Exception) as missing:
        store.get_database("docs")
    assert getattr(missing.value, "status", None) == 404


def test_path_is_immutable_and_slug_is_validated(store: ControlStore) -> None:
    add_database(store, "docs")
    with pytest.raises(Exception) as refused:
        store.update_database("docs", {"path": "/elsewhere"})
    assert getattr(refused.value, "code", None) == "invalid_database_change"

    with pytest.raises(Exception) as bad_slug:
        add_database(store, "Bad Slug")
    assert getattr(bad_slug.value, "status", None) in (400, 422)


def test_duplicate_identity_is_refused_because_a_clone_is_a_different_store(
    store: ControlStore,
) -> None:
    add_database(store, "a", database_uuid="same")
    with pytest.raises(Exception) as refused:
        add_database(store, "b", database_uuid="same")
    assert getattr(refused.value, "code", None) == "database_uuid_taken"

    with pytest.raises(Exception) as duplicate_path:
        add_database(store, "c", path="/srv/graphs/a")
    assert getattr(duplicate_path.value, "code", None) == "database_path_taken"

    # A store whose identity is not yet known can be registered normally.
    add_database(store, "d")
    add_database(store, "e")
    assert [item.slug for item in store.list_databases()] == ["a", "d", "e"]


def test_read_only_registration_cannot_allow_writes(store: ControlStore) -> None:
    add_database(store, "docs", read_only=True, allow_writes=False)
    with pytest.raises(Exception) as refused:
        store.update_database("docs", {"allow_writes": True})
    assert getattr(refused.value, "code", None) == "read_only_conflict"

    store.update_database("docs", {"read_only": False})
    assert store.update_database("docs", {"allow_writes": True}).allow_writes is True


def test_uuid_is_backfilled_on_first_open(store: ControlStore) -> None:
    record = add_database(store, "docs")
    assert record.database_uuid is None
    store.set_database_uuid("docs", "deadbeef")
    assert store.get_database("docs").database_uuid == "deadbeef"


# -- jobs and audit --------------------------------------------------------------------


def test_jobs_track_state_and_detail(store: ControlStore) -> None:
    database = add_database(store, "docs")
    job = store.create_job(database_id=database.id, kind="verify")
    assert job.state == "queued"
    assert store.active_job(database.id).id == job.id

    store.set_job_state(job.id, "running", {"scope": "all"})
    store.set_job_state(job.id, "succeeded", {"report": {"findings": []}})

    reloaded = store.get_job(job.id)
    assert reloaded.state == "succeeded"
    assert reloaded.detail == {"report": {"findings": []}}
    assert reloaded.started_at and reloaded.finished_at
    assert store.active_job(database.id) is None
    assert [item.id for item in store.list_jobs(database_id=database.id)] == [job.id]
    assert store.list_jobs()[0].id == job.id


def test_audit_is_queryable_and_pruned_by_age(store: ControlStore) -> None:
    database = add_database(store, "docs")
    for index in range(5):
        store.audit(
            actor="key:test",
            action="query",
            database_slug="docs",
            request={"n": index},
            rows=index,
            duration_ms=1.5,
        )
    store.audit(actor="admin", action="attach", database_slug=None, outcome="error")

    entries = store.list_audit()
    assert len(entries) == 6
    assert entries[0]["action"] == "attach" and entries[0]["database"] is None
    assert [entry["rows"] for entry in entries if entry["action"] == "query"] == [4, 3, 2, 1, 0]
    assert all(entry["database"] == "docs" for entry in entries if entry["action"] == "query")
    assert entries[1]["request"] == {"n": 4} and entries[1]["duration_ms"] == 1.5

    assert len(store.list_audit(actor="admin")) == 1
    assert len(store.list_audit(action="query", database_slug="docs")) == 5
    assert len(store.list_audit(database_slug="other")) == 0
    assert len(store.list_audit(limit=2)) == 2

    with store._lock:
        store._write(
            "UPDATE audit_log SET at = ? WHERE action = 'query'", ("2000-01-01T00:00:00+00:00",)
        )
    assert store.prune_audit(days=30) == 5
    assert len(store.list_audit()) == 1
    assert database.slug == "docs"


def test_removal_cascades_jobs_and_scopes(store: ControlStore) -> None:
    database = add_database(store, "docs")
    record, secret = store.create_key(name="k", databases=["docs"])
    job = store.create_job(database_id=database.id, kind="checkpoint")
    store.remove_database("docs")
    assert store.list_keys()[0].databases == []
    assert store.list_jobs(database_id=database.id) == []
    assert store.get_key(record.id).id == record.id
    with pytest.raises(Exception):
        store.get_job(job.id)
    assert store.authenticate_key(secret).databases == []


def test_key_helpers() -> None:
    secret = generate_api_key()
    assert looks_like_api_key(secret)
    assert not looks_like_api_key("sk-" + "x" * 40)
    assert not looks_like_api_key("okg_short")
    assert not looks_like_api_key("")
    assert hash_secret(secret) != hash_secret(generate_api_key())
    assert len(hash_secret(secret)) == 32


def test_store_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "server.db"
    handle = ControlStore(path)
    add_database(handle, "docs")
    _, secret = handle.create_key(name="k", databases=["docs"])
    token = generate_admin_token()
    handle.set_admin_token(token)
    handle.close()

    reopened = ControlStore(path)
    try:
        assert reopened.authenticate_key(secret).name == "k"
        assert reopened.get_database("docs").name == "Docs"
        assert reopened.check_admin_token(token) is True
    finally:
        reopened.close()
