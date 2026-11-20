"""Attach-by-reference, control-state durability and maintenance jobs (plan items 9, 12)."""

from __future__ import annotations

import json
import threading
import time

from conftest import Harness


def body(response):  # noqa: ANN001
    return response.json()


def queue(running, action: str, arguments: dict | None = None, slug: str = "docs"):  # noqa: ANN001
    return running.client.post(
        f"/admin/api/databases/{slug}/actions/{action}", json={"arguments": arguments or {}}
    )


# -- attach by reference ---------------------------------------------------------------


def test_the_server_references_the_store_it_does_not_copy_it(running) -> None:
    path = running.make_store("docs")
    before = sorted(p.name for p in path.iterdir())
    record = running.attach(path, "docs")

    assert record["path"] == str(path)
    assert record["database_uuid"], "the physical identity is read from the store itself"
    assert record["format_version"] >= 1
    assert sorted(p.name for p in path.iterdir()) == before, "attaching must not rewrite the store"

    state_files = [p.name for p in running.state_dir.rglob("*") if p.is_file()]
    assert "server.db" in " ".join(state_files)
    assert not [name for name in state_files if name.endswith((".dat", ".wal"))], (
        "no store file may be copied into the state directory"
    )


def test_the_same_store_can_not_be_registered_twice(running) -> None:
    path = running.make_store("docs")
    running.attach(path, "docs")
    duplicate = running.client.post("/admin/api/databases", json={"path": str(path), "slug": "alias"})
    assert duplicate.status_code == 409
    error = body(duplicate)["error"]
    assert error["code"] == "database_uuid_taken"
    assert "docs" in error["message"], "the refusal names the registration that owns the store"


def test_a_store_slug_is_validated_before_anything_is_written(running) -> None:
    response = running.client.post(
        "/admin/api/databases",
        json={"path": str(running.make_store("x")), "slug": "Bad Slug"},
    )
    assert response.status_code == 422
    error = body(response)["error"]
    assert error["code"] == "invalid_slug" and error["field"] == "slug"


def test_removing_a_registration_leaves_the_store_alone(running) -> None:
    path = running.make_store("docs")
    running.attach(path, "docs")
    removed = body(running.client.delete("/admin/api/databases/docs"))
    assert removed["removed"] == "docs"
    assert removed["path_untouched"] == str(path)
    assert (path / "heap.dat").exists(), "the daemon never deletes a store"
    assert running.client.get("/admin/api/databases/docs").status_code == 404

    # Re-attaching the same directory works and sees the same data.
    running.attach(path, "docs-again")
    secret, _ = running.create_key(["docs-again"])
    rows = running.query(secret, "MATCH (d:Doc) RETURN count(d) AS c", db="docs-again").json()
    assert rows["rows"] == [[4]]


# -- control state survives a restart --------------------------------------------------


def test_databases_keys_and_the_admin_token_survive_a_restart(tmp_path) -> None:
    first = Harness(tmp_path.resolve())
    with first:
        first.login()
        path = first.make_store("docs")
        first.attach(path, "docs", open_policy="pinned")
        secret, _ = first.create_key(["docs"])

    second = Harness(tmp_path.resolve())
    with second:
        assert second.admin_token is None, "a restart does not invent a new administrator"
        second.login(first.admin_token)

        databases = body(second.client.get("/admin/api/databases"))["databases"]
        assert [entry["slug"] for entry in databases] == ["docs"]
        assert databases[0]["open_policy"] == "pinned"

        # A pinned store is opened during startup, so the first request does not pay for it.
        opened = body(second.client.get("/admin/api/status"))
        assert "docs" in opened["open_slugs"]

        rows = second.query(secret, "MATCH (d:Doc) RETURN count(d) AS c").json()
        assert rows["rows"] == [[4]], "an existing API key keeps working across a restart"

        wrong = second.client.post("/admin/api/login", json={"token": "ogk_not-the-token"})
        assert wrong.status_code == 401
        assert body(wrong)["error"]["code"] == "invalid_admin_token"


def test_pinning_a_store_warms_it_and_eject_releases_it(running) -> None:
    running.attach(running.make_store("docs"), "docs", open_policy="pinned")
    assert body(running.client.post("/admin/api/databases/docs/open"))["slug"] == "docs"
    assert "docs" in body(running.client.get("/admin/api/status"))["open_slugs"]

    ejected = body(running.client.post("/admin/api/databases/docs/eject"))
    assert ejected == {"slug": "docs", "open": False, "ejected": True}
    assert "docs" not in body(running.client.get("/admin/api/status"))["open_slugs"]

    # The next request reopens it through the normal path: ejecting is not disabling.
    secret, _ = running.create_key(["docs"])
    assert running.query(secret, "MATCH (d:Doc) RETURN d.id").json()["row_count"] == 4
    assert "docs" in body(running.client.get("/admin/api/status"))["open_slugs"]


def test_a_closed_store_is_reopened_after_a_quiescing_job(running) -> None:
    running.attach(running.make_store("docs"), "docs")
    secret, _ = running.create_key(["docs"])
    running.query(secret, "MATCH (d:Doc) RETURN d.id")
    assert "docs" in body(running.client.get("/admin/api/status"))["open_slugs"]

    job = queue(running, "recover")
    assert job.status_code == 202
    settled = running.wait_for_job(job.json()["job_id"])
    assert settled["state"] == "succeeded", settled

    assert running.query(secret, "MATCH (d:Doc) RETURN count(d) AS c").json()["rows"] == [[4]]


# -- maintenance jobs ------------------------------------------------------------------


def test_read_only_maintenance_reports_what_it_checked(running) -> None:
    running.attach(running.make_store("docs"), "docs")
    job = running.wait_for_job(queue(running, "verify").json()["job_id"])
    assert job["kind"] == "verify"
    assert job["state"] == "succeeded", job["detail"]
    report = job["detail"]["report"]
    assert report["scope"] == "all"
    assert report["findings"] == []
    assert report["pages_checked"] > 0 and report["records_checked"] > 0
    assert "heap.dat" in report["files_checked"]
    assert job["detail"]["duration_ms"] >= 0


def test_a_read_only_registration_needs_a_checkpoint_complete_store(running) -> None:
    """The read-only door has one real precondition, and the refusal says what to do."""

    path = running.make_store("docs")
    running.attach(path, "docs", read_only=True, allow_writes=False)
    secret, _ = running.create_key(["docs"])

    response = running.query(secret, "MATCH (d:Doc) RETURN d.id")
    assert response.status_code == 400
    error = body(response)["error"]
    assert error["code"] == "unsupported_operation"
    assert "checkpoint-complete" in error["message"]
    assert "'recover'" in error["message"] and "'checkpoint'" in error["message"]
    assert error["details"]["hint"] == "recover_then_checkpoint"


def test_read_only_maintenance_after_the_store_was_checkpointed(running) -> None:
    path = running.make_store("docs")
    running.attach(path, "docs")

    # Make the store provable first: recover, then checkpoint, on a writable registration.
    for action in ("recover", "checkpoint"):
        job = running.wait_for_job(queue(running, action).json()["job_id"])
        assert job["state"] == "succeeded", (action, job["detail"])

    patched = running.client.patch(
        "/admin/api/databases/docs", json={"read_only": True, "allow_writes": False}
    )
    assert patched.status_code == 200

    refused = queue(running, "checkpoint")
    assert refused.status_code == 409
    error = body(refused)["error"]
    assert error["code"] == "maintenance_requires_writable"
    assert "read-only" in error["message"]

    verified = running.wait_for_job(queue(running, "verify").json()["job_id"])
    assert verified["state"] == "succeeded", verified["detail"]
    assert verified["detail"]["report"]["findings"] == []

    bloat = running.wait_for_job(queue(running, "bloat").json()["job_id"])
    assert bloat["state"] == "succeeded", bloat["detail"]
    assert bloat["detail"]["report"]["tables"], "the bloat census names the tables it measured"


def test_a_read_only_job_failure_carries_the_actionable_sentence(running) -> None:
    """Before checkpointing, verification on a read-only registration fails — and says why."""

    running.attach(running.make_store("docs"), "docs", read_only=True, allow_writes=False)
    job = running.wait_for_job(queue(running, "verify").json()["job_id"])
    assert job["state"] == "failed"
    assert job["detail"]["code"] == "unsupported_operation"
    assert "recover" in job["detail"]["message"] and "checkpoint" in job["detail"]["message"]


def test_quiescent_maintenance_needs_the_operators_confirmation(running) -> None:
    running.attach(running.make_store("docs"), "docs")
    refused = queue(running, "vacuum")
    assert refused.status_code == 409
    error = body(refused)["error"]
    assert error["code"] == "maintenance_confirmation_required"
    assert "own handles" in error["message"], (
        "the refusal must say that the server cannot see other processes"
    )

    confirmed = running.wait_for_job(
        queue(running, "vacuum", {"confirm_quiescent": True}).json()["job_id"]
    )
    assert confirmed["state"] == "succeeded", confirmed["detail"]
    assert confirmed["detail"]["report"]["complete"] is True


def test_dry_run_index_cleanup_still_needs_confirmation(running) -> None:
    running.attach(running.make_store("docs"), "docs")
    assert queue(running, "cleanup_indexes", {"dry_run": True}).status_code == 409
    job = running.wait_for_job(
        queue(running, "cleanup_indexes", {"dry_run": True, "confirm_quiescent": True}).json()["job_id"]
    )
    assert job["state"] == "succeeded", job["detail"]
    assert job["detail"]["report"]["dry_run"] is True


def test_an_action_that_needs_an_argument_says_which_one(running) -> None:
    running.attach(running.make_store("docs"), "docs")
    refused = queue(running, "rebuild_index")
    assert refused.status_code == 422
    error = body(refused)["error"]
    assert error["code"] == "maintenance_argument_required"
    assert error["details"]["missing"] == ["name"]

    unknown = queue(running, "defragment_the_universe")
    assert unknown.status_code == 422
    message = body(unknown)["error"]["message"]
    assert "verify" in message and "vacuum" in message, "the refusal lists the real actions"


def test_maintenance_waits_for_local_quiescence(running) -> None:
    running.attach(running.make_store("docs"), "docs")
    secret, _ = running.create_key(["docs"], allow_writes=True)

    with running.client.websocket_connect(f"/api/v1/ws?token={secret}") as socket:
        socket.send_json({"type": "hello", "db": "docs"})
        assert socket.receive_json()["type"] == "hello_ok"
        socket.send_json({"type": "begin", "id": 1, "mode": "read"})
        assert socket.receive_json()["type"] == "begin_ok"

        refused = queue(running, "recover")
        assert refused.status_code == 409
        error = body(refused)["error"]
        assert error["code"] == "maintenance_not_quiesced"
        # A read session holding a lane is enough to break quiescence.
        assert error["details"]["active_operations"] == 1
        assert "sessions" in error["message"]

        socket.send_json({"type": "close"})
        socket.receive_json()

    job = running.wait_for_job(queue(running, "recover").json()["job_id"])
    assert job["state"] == "succeeded", job["detail"]
    assert job["detail"]["report"]["outcome"] in {"clean", "replayed"}


def test_an_open_write_transaction_is_counted_against_quiescence(running) -> None:
    running.attach(running.make_store("docs"), "docs", read_only=False, allow_writes=True)
    secret, _ = running.create_key(["docs"], allow_writes=True)
    with running.client.websocket_connect(f"/api/v1/ws?token={secret}") as socket:
        socket.send_json({"type": "hello", "db": "docs", "mode": "write"})
        assert socket.receive_json()["type"] == "hello_ok"
        socket.send_json({"type": "begin", "id": 1, "mode": "write"})
        assert socket.receive_json()["type"] == "begin_ok"

        refused = queue(running, "recover")
        assert refused.status_code == 409
        details = body(refused)["error"]["details"]
        assert details["open_transactions"] == 1, details

        socket.send_json({"type": "rollback", "id": 2})
        assert socket.receive_json()["type"] == "rollback_ok"
        socket.send_json({"type": "close"})
        socket.receive_json()

    assert queue(running, "recover").status_code == 202


def test_a_read_session_holding_a_lane_does_not_block_a_query(running) -> None:
    """Snapshot isolation: maintenance is what has to wait, not the reader."""

    running.attach(running.make_store("docs"), "docs")
    secret, _ = running.create_key(["docs"])
    with running.client.websocket_connect(f"/api/v1/ws?token={secret}") as socket:
        socket.send_json({"type": "hello", "db": "docs"})
        socket.receive_json()
        socket.send_json({"type": "begin", "id": 1})
        socket.receive_json()
        rows = running.query(secret, "MATCH (d:Doc) RETURN count(d) AS c").json()
        assert rows["rows"] == [[4]]


def test_one_maintenance_job_per_database(running, monkeypatch) -> None:
    running.attach(running.make_store("docs"), "docs")
    runner = running.context.jobs  # type: ignore[attr-defined]
    original = runner._execute
    gate = threading.Event()

    def slow_execute(*args, **kwargs):  # noqa: ANN002, ANN003
        gate.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(runner, "_execute", slow_execute)
    try:
        first = queue(running, "verify").json()["job_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if running.client.get(f"/admin/api/jobs/{first}").json()["state"] == "running":
                break
            time.sleep(0.02)

        second = queue(running, "verify")
        assert second.status_code == 409
        error = body(second)["error"]
        assert error["code"] == "job_active"
        assert error["details"]["slug"] == "docs"
    finally:
        gate.set()

    assert running.wait_for_job(first)["state"] == "succeeded"


def test_a_failing_job_reports_the_typed_refusal(running) -> None:
    running.attach(running.make_store("docs"), "docs")
    job = running.wait_for_job(
        queue(running, "rebuild_index", {"name": "no_such_index"}).json()["job_id"]
    )
    assert job["state"] == "failed"
    assert job["detail"]["code"], "a failed job carries a machine-readable code"
    assert job["detail"]["message"]


def test_job_history_is_queryable(running) -> None:
    running.attach(running.make_store("docs"), "docs")
    job_id = queue(running, "bloat").json()["job_id"]
    running.wait_for_job(job_id)

    everything = body(running.client.get("/admin/api/jobs"))["jobs"]
    assert [job["id"] for job in everything].count(job_id) == 1
    by_database = body(running.client.get("/admin/api/jobs", params={"database": "docs"}))["jobs"]
    assert job_id in [job["id"] for job in by_database]
    by_kind = body(running.client.get("/admin/api/jobs", params={"state": "succeeded"}))["jobs"]
    assert job_id in [job["id"] for job in by_kind]
    assert running.client.get("/admin/api/jobs/nope").status_code == 404


def test_maintenance_is_audited(running) -> None:
    running.attach(running.make_store("docs"), "docs")
    running.wait_for_job(queue(running, "verify").json()["job_id"])
    entries = body(running.client.get("/admin/api/audit", params={"action": "maintenance.verify"}))[
        "entries"
    ]
    assert entries, json.dumps(entries)
    assert entries[0]["actor"] == "admin"
    assert entries[0]["database"] == "docs"
    assert "job" in entries[0]["request"]


# -- the console's own description of what it can ask for ------------------------------


def test_the_settings_payload_describes_every_action_and_option(running) -> None:
    settings = body(running.client.get("/admin/api/settings"))
    actions = {entry["action"]: entry for entry in settings["maintenance_actions"]}
    assert set(actions) >= {"verify", "bloat", "status", "checkpoint", "recover", "vacuum"}
    assert actions["verify"]["read_only_ok"] is True
    assert actions["verify"]["quiescing"] is True
    assert actions["checkpoint"]["read_only_ok"] is False
    assert actions["vacuum"]["confirm_quiescent"] is True
    assert actions["rebuild_index"]["requires"] == ["name"]

    option_names = {option["name"] for option in settings["database_options"]}
    assert {"page_size", "buffer_budget_bytes", "read_only", "max_result_rows"} <= option_names


def test_store_options_are_validated_when_the_registration_is_patched(running) -> None:
    running.attach(running.make_store("docs"), "docs")
    patched = running.client.patch(
        "/admin/api/databases/docs", json={"options": {"max_result_rows": 50}}
    )
    assert patched.status_code == 200
    assert body(patched)["options"] == {"max_result_rows": 50}

    absurd = running.client.patch(
        "/admin/api/databases/docs", json={"options": {"page_size": 4097}}
    )
    assert absurd.status_code == 400, "the engine's own refusal is passed through"
    error = body(absurd)["error"]
    assert error["code"] == "configuration_error"
    assert error["field"] == "page_size"
    assert "power of two" in error["message"]

    invented = running.client.patch(
        "/admin/api/databases/docs", json={"options": {"make_it_rain": True}}
    )
    assert invented.status_code == 422
    assert body(invented)["error"]["code"] == "unknown_database_options"
