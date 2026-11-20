"""Admin surface: session, registration, keys, settings, audit (plan §6)."""

from __future__ import annotations

from pathlib import Path



def code_of(exc):  # noqa: ANN001
    return getattr(exc, "code", None)


# -- session ---------------------------------------------------------------------------


def test_store_metrics_may_not_leave_the_machine_without_saying_so(tmp_path: Path) -> None:
    """The registration door refuses a remote metrics sink; an explicit config key unlocks it."""

    from conftest import Harness

    with Harness(tmp_path.resolve()) as harness:
        harness.login()
        path = harness.make_store("docs")
        remote = {"metrics": "openmetrics", "metrics_destination": "10.0.0.5:9464"}

        refused = harness.client.post(
            "/admin/api/databases", json={"path": str(path), "slug": "docs", "options": remote}
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["code"] == "remote_store_metrics_requires_consent"
        assert harness.client.get("/admin/api/databases").json()["databases"] == []

        # The console is told the same truth it is refused by.
        assert (
            harness.client.get("/admin/api/settings").json()["security"][
                "allow_remote_store_metrics"
            ]
            is False
        )

        # Loopback stays available: the guard is about leaving the machine, not about metrics.
        local = dict(remote, metrics_destination="127.0.0.1:9464")
        accepted = harness.client.post(
            "/admin/api/databases", json={"path": str(path), "slug": "docs", "options": local}
        )
        assert accepted.status_code == 201, accepted.text

    consented_root = tmp_path.parent / "consented"
    consented_root.mkdir(parents=True, exist_ok=True)
    with Harness(consented_root, allow_remote_store_metrics=True) as harness:
        harness.login()
        other = harness.make_store("docs")
        both = {
            "metrics": "openmetrics",
            "metrics_destination": "10.0.0.5:9464",
            # Two opt-ins on purpose: this server's consent key, and the library's own flag.
            "allow_remote_metrics": True,
        }
        allowed = harness.client.post(
            "/admin/api/databases", json={"path": str(other), "slug": "docs", "options": both}
        )
        assert allowed.status_code == 201, allowed.text
        assert (
            harness.client.get("/admin/api/settings").json()["security"][
                "allow_remote_store_metrics"
            ]
            is True
        )


def test_admin_token_is_exchanged_for_an_httponly_session_cookie(harness) -> None:
    with harness:
        anonymous = harness.client.get("/admin/api/session").json()
        assert anonymous["signed_in"] is False
        assert anonymous["token_configured"] is True

        refused = harness.client.get("/admin/api/databases")
        assert refused.status_code == 401
        assert refused.json()["error"]["code"] == "admin_session_required"

        response = harness.client.post("/admin/api/login", json={"token": harness.admin_token})
        assert response.status_code == 200, response.text
        cookie = response.cookies.get("okg_session")
        assert cookie and len(cookie) == 32
        set_cookie = response.headers["set-cookie"].lower()
        assert "httponly" in set_cookie and "samesite=strict" in set_cookie

        assert harness.client.get("/admin/api/session").json()["signed_in"] is True

        assert harness.client.post("/admin/api/logout").status_code == 200
        assert harness.client.get("/admin/api/session").json()["signed_in"] is False
        assert harness.client.get("/admin/api/databases").status_code == 401


def test_a_bad_token_does_not_sign_in(harness) -> None:
    with harness:
        response = harness.client.post("/admin/api/login", json={"token": "okga_nope"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_admin_token"
        assert harness.client.get("/admin/api/session").json()["signed_in"] is False


def test_an_api_key_cannot_use_the_admin_door(daemon) -> None:
    # A data-plane credential, with no admin session in the jar, must not reach the admin door.
    daemon.client.cookies.clear()
    response = daemon.client.get(
        "/admin/api/databases", headers=daemon.bearer(daemon.secret)  # type: ignore[attr-defined]
    )
    assert response.status_code == 401, "data-plane credentials never reach the admin surface"
    assert response.json()["error"]["code"] == "admin_session_required"


def test_rotating_the_token_revokes_the_old_one_and_the_session(running) -> None:
    rotated = running.client.post("/admin/api/admin-token/rotate")
    assert rotated.status_code == 200, rotated.text
    body = rotated.json()
    assert body["shown_once"] is True

    # The session was tied to the old token: it is gone.
    assert running.client.get("/admin/api/session").json()["signed_in"] is False
    assert running.client.post("/admin/api/login", json={"token": running.admin_token}).status_code == 401

    running.admin_token = body["admin_token"]
    running.login()
    assert running.client.get("/admin/api/session").json()["signed_in"] is True


# -- registration ----------------------------------------------------------------------


def test_attach_is_a_reference_and_removal_leaves_the_files_alone(running) -> None:
    path = running.make_store("docs")
    before = sorted(p.name for p in path.iterdir())
    bytes_before = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())

    inspected = running.client.post("/admin/api/databases/inspect", json={"path": str(path)})
    assert inspected.status_code == 200, inspected.text
    facts = inspected.json()
    assert facts["marker"] == "grafx.meta"
    assert facts["node_tables"] == ["Doc", "Tag"]
    assert facts["relationship_tables"] == ["CITES", "TAGGED"]
    assert facts["vector_spaces"] == ["semantic"]
    assert facts["database_uuid"]
    assert facts["already_registered_as"] is None
    assert facts["bytes"] > 0

    record = running.attach(path, "docs")
    assert record["slug"] == "docs" and record["format_version"] == facts["format_version"]

    after = sorted(p.name for p in path.iterdir())
    assert after == before, "attaching must not write, copy or rename anything in the store"

    removed = running.client.delete("/admin/api/databases/docs")
    assert removed.status_code == 200
    assert removed.json()["path_untouched"] == str(path)
    assert path.exists(), "removing a registration never deletes the store"
    bytes_after = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    assert bytes_after == bytes_before


def test_inspect_rejects_a_non_store_and_a_path_outside_the_roots(running) -> None:
    plain = running.graphs / "plain"
    plain.mkdir()
    refused = running.client.post("/admin/api/databases/inspect", json={"path": str(plain)})
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "invalid_path"
    assert "grafx.meta" in refused.json()["error"]["message"]

    outside = running.graphs.parent / "outside-roots"
    outside.mkdir()
    refused_outside = running.client.post(
        "/admin/api/databases/inspect", json={"path": str(outside)}
    )
    assert refused_outside.status_code == 422
    assert refused_outside.json()["error"]["code"] == "invalid_path"
    assert "allowed root" in refused_outside.json()["error"]["message"]

    empty = running.client.post("/admin/api/databases/inspect", json={})
    assert empty.status_code == 400


def test_attach_validates_path_slug_options_and_conflicts(running) -> None:
    path = running.make_store("docs")

    outside = running.client.post(
        "/admin/api/databases", json={"path": "/tmp/docs", "slug": "tmp"}
    )
    assert outside.status_code == 422 and outside.json()["error"]["code"] == "invalid_path"

    tilde = running.client.post(
        "/admin/api/databases", json={"path": "~/graphs/docs", "slug": "docs"}
    )
    assert tilde.status_code == 422 and "~" in tilde.json()["error"]["message"]

    bad_options = running.client.post(
        "/admin/api/databases",
        json={"path": str(path), "slug": "docs", "options": {"page_size": 1234}},
    )
    assert bad_options.status_code == 400
    assert bad_options.json()["error"]["code"] == "configuration_error"

    conflict = running.client.post(
        "/admin/api/databases",
        json={"path": str(path), "slug": "docs", "read_only": True, "allow_writes": True},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "read_only_conflict"

    assert running.attach(path, "docs")["slug"] == "docs"

    duplicate_slug = running.client.post(
        "/admin/api/databases", json={"path": str(path), "slug": "docs"}
    )
    assert duplicate_slug.status_code == 409

    # The same physical store under a second id is a clone, not a view.
    clone = running.graphs / "clone"
    clone.mkdir()
    for item in path.rglob("*"):
        if item.is_file():
            target = clone / item.relative_to(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item.read_bytes())
    second = running.client.post(
        "/admin/api/databases", json={"path": str(clone), "slug": "docs-clone"}
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "database_uuid_taken"


def test_database_patch_reopens_with_new_options(running) -> None:
    path = running.make_store("docs")
    running.attach(path, "docs")
    assert running.client.post("/admin/api/databases/docs/open").status_code == 200
    assert running.client.get("/admin/api/databases/docs").json()["open"] is True

    patched = running.client.patch(
        "/admin/api/databases/docs", json={"options": {"buffer_budget_bytes": 2 * 1024 * 1024}}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["options"] == {"buffer_budget_bytes": 2 * 1024 * 1024}

    reopened = running.client.post("/admin/api/databases/docs/open")
    assert reopened.status_code == 200, reopened.text
    assert running.client.get("/admin/api/databases/docs").json()["open"] is True

    empty = running.client.patch("/admin/api/databases/docs", json={})
    assert empty.status_code == 400 and empty.json()["error"]["code"] == "empty_patch"

    assert running.client.patch("/admin/api/databases/docs", json={"note": "prod"}).status_code == 200
    assert running.client.get("/admin/api/databases/docs").json()["note"] == "prod"


def test_inventory_reports_schema_indexes_and_status(running) -> None:
    path = running.make_store("docs")
    running.attach(path, "docs")
    response = running.client.get("/admin/api/databases/docs/inventory")
    assert response.status_code == 200, response.text
    body = response.json()

    tables = {table["name"]: table for table in body["schema"]["tables"]}
    assert set(tables) == {"Doc", "Tag"}
    assert tables["Doc"]["kind"] == "node" and tables["Doc"]["primary_key"] == "id"
    assert {column["name"] for column in tables["Doc"]["columns"]} == {
        "id",
        "title",
        "body",
        "v",
    }
    vector_column = next(c for c in tables["Doc"]["columns"] if c["name"] == "v")
    assert vector_column["vector_space"] == "semantic"
    relations = {rel["name"]: rel for rel in body["schema"]["relationships"]}
    assert set(relations) == {"CITES", "TAGGED"}
    assert relations["TAGGED"]["from_table"] == "Doc" and relations["TAGGED"]["to_table"] == "Tag"
    assert body["schema"]["vector_spaces"][0]["dimension"] == 2

    indexes = {index["name"]: index for index in body["indexes"]}
    assert indexes["doc_text"]["columns"] == ["body", "title"]
    assert indexes["doc_text"]["stale"] is False
    assert body["vector_indexes"][0]["space"] == "semantic"
    assert any(name.startswith("pk_") for name in indexes), "identity indexes are visible"
    assert body["maintenance"]["recovery_required"] is False
    assert body["maintenance"]["wal_bytes"] > 0
    assert body["stale"] == []


def test_open_and_close_report_honestly(running) -> None:
    path = running.make_store("docs")
    running.attach(path, "docs")
    assert running.client.get("/admin/api/databases/docs").json()["open"] is False

    opened = running.client.post("/admin/api/databases/docs/open")
    assert opened.status_code == 200, opened.text
    assert opened.json()["opened"] is True
    assert opened.json()["format_version"] == 2
    assert opened.json()["tables"] >= 4
    assert running.client.get("/admin/api/status").json()["open_slugs"] == ["docs"]

    closed = running.client.post("/admin/api/databases/docs/close")
    assert closed.status_code == 200 and closed.json()["open"] is False
    assert running.client.get("/admin/api/status").json()["open_slugs"] == []

    missing = running.client.post("/admin/api/databases/ghost/open")
    assert missing.status_code == 404


# -- settings --------------------------------------------------------------------------


def test_settings_describe_the_server_and_generate_the_option_form(running) -> None:
    body = running.client.get("/admin/api/settings").json()
    assert body["bind"] == {"host": "127.0.0.1", "port": 8737}
    assert body["allowed_roots"] == [str(running.graphs)]
    assert body["state_dir"] == str(running.state_dir)
    assert body["limits"]["max_write_sessions"] == 1
    assert body["security"]["tls"] is False
    assert "not an OS sandbox" in body["security"]["note"]

    options = {entry["name"]: entry for entry in body["database_options"]}
    assert options["page_size"]["default"] == 8192
    assert options["page_size"]["group"] == "storage"
    assert options["recovery_policy"]["choices"]


def test_roots_lists_candidate_stores(running) -> None:
    running.make_store("alpha")
    (running.graphs / "not-a-store").mkdir()
    body = running.client.get("/admin/api/roots").json()
    assert body["roots"][0]["exists"] is True
    assert [store["name"] for store in body["roots"][0]["stores"]] == ["alpha"]


# -- keys ------------------------------------------------------------------------------


def test_key_secret_is_shown_once_and_scoped(running) -> None:
    running.attach(running.make_store("a"), "a")
    running.attach(running.make_store("b"), "b")

    created = running.client.post(
        "/admin/api/keys", json={"name": "reader", "databases": ["a"], "allow_writes": False}
    )
    assert created.status_code == 201
    body = created.json()
    secret = body["key"]
    key_id = body["id"]
    assert secret.startswith("okg_") and body["shown_once"] is True
    assert secret.startswith(body["prefix"].rstrip("…"))
    assert secret not in running.client.get("/admin/api/keys").text

    listed = running.client.get("/admin/api/keys").json()["keys"][0]
    assert listed["databases"] == ["a"] and listed["allow_writes"] is False

    patched = running.client.patch(
        f"/admin/api/keys/{key_id}", json={"databases": ["a", "b"], "allow_writes": True}
    )
    assert patched.json()["databases"] == ["a", "b"]
    assert patched.json()["allow_writes"] is True
    assert running.client.get("/admin/api/keys").json()["keys"][0]["allow_writes"] is True

    revoked = running.client.post(f"/admin/api/keys/{key_id}/revoke")
    assert revoked.status_code == 200 and revoked.json()["revoked_at"]
    assert running.client.post(
        "/api/v1/databases/a/query", json={"query": "MATCH (n) RETURN n"}, headers={"Authorization": f"Bearer {secret}"}
    ).status_code == 401

    assert running.client.delete(f"/admin/api/keys/{key_id}").status_code == 200
    assert running.client.get("/admin/api/keys").json()["keys"] == []


def test_key_creation_validates_its_scope(running) -> None:
    running.attach(running.make_store("a"), "a")
    unknown = running.client.post(
        "/admin/api/keys", json={"name": "k", "databases": ["ghost"]}
    )
    assert unknown.status_code == 404
    unnamed = running.client.post("/admin/api/keys", json={"name": " ", "databases": []})
    assert unnamed.status_code in (400, 422)


# -- audit -----------------------------------------------------------------------------


def test_audit_records_admin_and_key_activity_with_actors(running) -> None:
    running.attach(running.make_store("a"), "a", allow_writes=False)
    secret, record = running.create_key(["a"])
    running.client.post("/admin/api/databases/a/open")
    running.client.post(
        "/api/v1/databases/a/query", json={"query": "MATCH (d:Doc) RETURN d.id"}, headers=running.bearer(secret)
    )

    entries = running.client.get("/admin/api/audit").json()["entries"]
    actions = {entry["action"] for entry in entries}
    assert {"database.attach", "key.create", "database.open", "login"} <= actions
    assert {entry["actor"] for entry in entries} >= {"admin", f"key:{record['fingerprint']}"}
    assert all(entry["outcome"] == "ok" for entry in entries)

    failed = running.client.post(
        "/api/v1/databases/a/query",
        json={"query": "this is not cypher"},
        headers=running.bearer(secret),
    )
    assert failed.status_code == 400
    key_entries = [
        entry
        for entry in running.client.get("/admin/api/audit").json()["entries"]
        if entry["outcome"] != "ok"
    ]
    assert key_entries and key_entries[0]["actor"].startswith("key:")
    assert key_entries[0]["action"] == "query"
    assert key_entries[0]["outcome"] == "parse_error"

    filtered = running.client.get("/admin/api/audit", params={"database": "a"}).json()["entries"]
    assert filtered and all(entry["database"] == "a" for entry in filtered)
    assert running.client.get("/admin/api/audit", params={"limit": 2}).json()["entries"].__len__() == 2


def test_audit_filter_by_action(running) -> None:
    running.attach(running.make_store("a"), "a")
    entries = running.client.get("/admin/api/audit", params={"action": "database.attach"}).json()
    assert len(entries["entries"]) == 1


# -- DDL -------------------------------------------------------------------------------


def test_ddl_from_the_console_creates_schema(running) -> None:
    path = running.make_store("docs")
    running.attach(path, "docs", read_only=False, allow_writes=True)
    response = running.client.post(
        "/admin/api/databases/docs/ddl",
        json={"statements": ["CREATE NODE TABLE Note(id INT64, text STRING, PRIMARY KEY(id))"]},
    )
    assert response.status_code == 200, response.text
    statement = response.json()["statements"][0]
    assert statement["statement"].startswith("CREATE NODE TABLE Note")
    assert statement["statistics"]["tables_created"] == 1
    assert response.json()["commit"] == {"csn": response.json()["commit"]["csn"], "durable": True, "wrote": True}
    assert "Note" in [
        table["name"]
        for table in running.client.get("/admin/api/databases/docs/inventory").json()["schema"]["tables"]
    ]

    refused = running.client.post(
        "/admin/api/databases/docs/ddl", json={"statements": ["DROP TABLE Doc"]}
    )
    assert refused.status_code == 400, "Grafx 0.0.8 has no DROP TABLE: nothing is silently applied"
    assert refused.json()["error"]["code"] == "parse_error"


def test_identity_indexes_open_once_and_say_what_appeared(running) -> None:
    """The library's catalog v1→v2 door is exposed, audited, and idempotent where the library is."""

    running.attach(running.make_store("docs"), "docs", read_only=False, allow_writes=True)
    response = running.client.post("/admin/api/schema/docs/identity-indexes")
    assert response.status_code == 200, response.text
    first = response.json()
    assert isinstance(first["activated"], list)
    assert isinstance(first["already_present"], list)
    assert first["indexes"]["slug"] == "docs"

    again = running.client.post("/admin/api/schema/docs/identity-indexes")
    assert again.status_code == 200, again.text
    assert again.json()["activated"] == [], "the second call must be the no-op the library promises"

    actions = [
        entry["action"]
        for entry in running.client.get("/admin/api/audit", params={"limit": 50}).json()["entries"]
    ]
    assert actions.count("schema.identity_indexes") == 2


def test_identity_indexes_need_the_write_door(running) -> None:
    running.attach(running.make_store("docs"), "docs", read_only=True, allow_writes=False)
    refused = running.client.post("/admin/api/schema/docs/identity-indexes")
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "write_not_permitted"
    # The admin DDL door reports which of its own toggles stopped it.
    assert refused.json()["error"]["details"].get("reason") == "database_read_only"


def test_ddl_is_refused_for_a_read_only_registration(running) -> None:
    running.attach(running.make_store("docs"), "docs", read_only=True, allow_writes=False)
    response = running.client.post(
        "/admin/api/databases/docs/ddl", json={"statements": ["CREATE NODE TABLE X(id INT64, PRIMARY KEY(id))"]}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "write_not_permitted"
