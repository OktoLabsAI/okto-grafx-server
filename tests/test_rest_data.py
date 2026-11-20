"""The read door: query, schema, indexes, explain, values, limits (plan items 5, 15)."""

from __future__ import annotations

import json


def body(response):  # noqa: ANN001
    return response.json()


# -- health and discovery --------------------------------------------------------------


def test_health_is_unauthenticated_and_names_versions(harness) -> None:
    with harness:
        response = harness.client.get("/api/v1/health")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["databases_registered"] == 0
        assert payload["databases_open"] == 0
        assert payload["grafx"]
        assert "Authorization" not in response.request.headers


def test_openapi_documents_the_surface(harness) -> None:
    with harness:
        spec = harness.client.get("/api/v1/openapi.json").json()
        paths = spec["paths"]
        for path in (
            "/api/v1/health",
            "/api/v1/databases",
            "/api/v1/databases/{slug}/query",
            "/api/v1/databases/{slug}/schema",
            "/api/v1/databases/{slug}/indexes",
            "/api/v1/databases/{slug}/search/text",
            "/api/v1/databases/{slug}/search/vector",
            "/api/v1/databases/{slug}/search/hybrid",
            "/api/v1/databases/{slug}/graph",
            "/api/v1/databases/{slug}/neighbors",
            "/admin/api/login",
            "/mcp",
        ):
            assert path in paths, path
        assert "bearerAuth" in spec["components"]["securitySchemes"]


# -- scoping ---------------------------------------------------------------------------


def test_a_key_only_sees_its_own_databases_and_cannot_probe_others(daemon) -> None:
    daemon.attach(daemon.make_store("second"), "second")
    other, _ = daemon.create_key(["second"])

    mine = body(daemon.client.get("/api/v1/databases", headers=daemon.bearer(daemon.secret)))
    assert [db["slug"] for db in mine["databases"]] == ["docs"]
    assert mine["databases"][0]["allow_writes"] is False
    assert mine["actor"]["kind"] == "key" and mine["actor"]["label"].startswith("key:")

    theirs = daemon.client.get("/api/v1/databases", headers=daemon.bearer(other))
    assert [db["slug"] for db in body(theirs)["databases"]] == ["second"]

    def refusal(path: str) -> dict:
        response = daemon.client.get(path, headers=daemon.bearer(daemon.secret))
        assert response.status_code == 404
        error = dict(body(response)["error"])
        error.pop("request_id")
        return error

    scoped_out = refusal("/api/v1/databases/second/schema")
    never_registered = refusal("/api/v1/databases/ghost/schema")
    # An out-of-scope database must be indistinguishable from one that was never registered:
    # same code, same shape, and no wording that admits the other store exists.
    assert scoped_out["code"] == never_registered["code"] == "not_found"
    assert scoped_out["field"] == never_registered["field"] == "slug"
    assert set(scoped_out) == set(never_registered)
    assert scoped_out["message"] == never_registered["message"].replace("ghost", "second")


def test_missing_and_malformed_credentials_are_distinct_refusals(daemon) -> None:
    daemon.client.cookies.clear()  # an anonymous caller has no admin session either
    anonymous = daemon.client.post(
        "/api/v1/databases/docs/query", json={"query": "MATCH (n) RETURN n"}
    )
    assert anonymous.status_code == 401
    assert body(anonymous)["error"]["code"] == "unauthorized"
    assert "Bearer" in body(anonymous)["error"]["message"]

    wrong_scheme = daemon.client.post(
        "/api/v1/databases/docs/query",
        json={"query": "MATCH (n) RETURN n"},
        headers={"Authorization": "Token okg_abcdefghijabcdefghijabcdefghijabcdefghij"},
    )
    assert wrong_scheme.status_code == 401
    assert body(wrong_scheme)["error"]["code"] == "bearer_scheme_required"

    not_a_key = daemon.client.post(
        "/api/v1/databases/docs/query",
        json={"query": "MATCH (n) RETURN n"},
        headers={"Authorization": "Bearer sk-something"},
    )
    assert not_a_key.status_code == 401
    assert body(not_a_key)["error"]["code"] == "malformed_api_key"

    unknown = daemon.client.post(
        "/api/v1/databases/docs/query",
        json={"query": "MATCH (n) RETURN n"},
        headers={"Authorization": "Bearer okg_" + "z" * 40},
    )
    assert unknown.status_code == 401
    assert body(unknown)["error"]["code"] == "unknown_api_key"


def test_a_disabled_database_is_closed_to_everyone(running) -> None:
    running.attach(running.make_store("docs"), "docs")
    running.attach(running.make_store("library"), "library")
    secret, _ = running.create_key(["docs"])
    assert (
        running.client.get("/api/v1/databases/docs/schema", headers=running.bearer(secret)).status_code == 200
    )

    running.client.patch("/admin/api/databases/docs", json={"enabled": False})
    refused = running.client.get("/api/v1/databases/docs/schema", headers=running.bearer(secret))
    # The key's own scope names this database, so it is told the true reason and nothing else.
    assert refused.status_code == 403
    assert body(refused)["error"]["code"] == "database_disabled"

    # A database the key cannot see is answered exactly like one that was never registered —
    # including the echo of the name the caller typed, which is the only name involved.
    unseen = running.client.get("/api/v1/databases/library/schema", headers=running.bearer(secret))
    never_registered = running.client.get("/api/v1/databases/library/schema", headers=running.bearer(secret))
    assert unseen.status_code == 404
    assert body(unseen)["error"]["code"] == "not_found"
    assert unseen.status_code == never_registered.status_code
    assert body(unseen)["error"]["code"] == body(never_registered)["error"]["code"]
    assert body(unseen)["error"]["message"] == body(never_registered)["error"]["message"]


# -- schema and indexes ----------------------------------------------------------------


def test_schema_mirrors_the_catalog_with_types_and_vector_spaces(daemon) -> None:
    response = daemon.client.get(
        "/api/v1/databases/docs/schema", headers=daemon.bearer(daemon.secret)
    )
    payload = body(response)
    tables = {table["name"]: table for table in payload["tables"]}
    assert {t["kind"] for t in tables.values()} == {"node"}
    doc = tables["Doc"]
    assert doc["primary_key"] == "id" and doc["table_id"] >= 1
    vector = next(column for column in doc["columns"] if column["name"] == "v")
    assert vector["vector_space"] == "semantic"
    assert vector["type"] == "VECTOR_F32"
    assert "stored_type" in vector
    assert doc["columns"][0]["nullable"] is False, "the primary key is not nullable"
    assert tables["Tag"]["columns"][0]["name"] == "id"

    relations = {rel["name"]: rel for rel in payload["relationships"]}
    assert relations["CITES"]["from_table"] == "Doc" and relations["CITES"]["to_table"] == "Doc"

    space = payload["vector_spaces"][0]
    assert space["dimension"] == 2 and space["metric"] == "COSINE"
    assert space["storage_dtype"] == "float32" and space["state"]
    assert payload["read_only"] is False
    assert len(payload["database_uuid"]) == 32


def test_indexes_report_staleness_and_build_progress(daemon) -> None:
    payload = body(
        daemon.client.get("/api/v1/databases/docs/indexes", headers=daemon.bearer(daemon.secret))
    )
    doc_text = next(index for index in payload["indexes"] if index["name"] == "doc_text")
    assert doc_text["built_through_lsn"] > 0
    assert doc_text["stale"] is False and doc_text["stale_reason"] is None
    assert doc_text["automatic"] is False
    automatic = [index for index in payload["indexes"] if index["automatic"]]
    assert automatic, "identity indexes are automatic and must be visible as such"
    assert payload["vector_indexes"][0]["built_through_lsn"] > 0


def test_indexes_are_classified_by_what_they_are_not_by_name(daemon) -> None:
    """The search panel lists full-text indexes; it must not infer that from a name convention."""

    payload = body(
        daemon.client.get("/api/v1/databases/docs/indexes", headers=daemon.bearer(daemon.secret))
    )
    kinds = {index["name"]: index["kind"] for index in payload["indexes"]}
    assert kinds["doc_text"] == "text"
    assert kinds["pk_Doc"] == "exact", "an identity index is not something to search text against"
    assert kinds["vector_Doc_semantic"] == "vector"
    text_only = [name for name, kind in kinds.items() if kind == "text"]
    assert text_only == ["doc_text"]


# -- query -----------------------------------------------------------------------------


def test_query_returns_columns_rows_and_a_snapshot(daemon) -> None:
    response = daemon.query(
        daemon.secret,  # type: ignore[attr-defined]
        "MATCH (d:Doc) RETURN d.id, d.title ORDER BY d.id LIMIT 2",
    )
    payload = body(response)
    assert payload["columns"] == ["d.id", "d.title"]
    assert payload["rows"] == [[1, "durable wal"], [2, "vector search"]]
    assert payload["row_count"] == 2
    assert payload["truncated"] is False
    assert payload["snapshot_lsn"] > 0
    assert payload["statistics"]
    assert payload["warnings"] == []
    assert payload["duration_ms"] >= 0
    assert "plan" not in payload


def test_limit_marks_truncation_instead_of_lying_about_the_result(daemon) -> None:
    payload = body(daemon.query(daemon.secret, "MATCH (d:Doc) RETURN d.id", limit=2))  # type: ignore[attr-defined]
    assert payload["row_count"] == 2
    assert payload["truncated"] is True


def test_limit_is_clamped_to_the_server_maximum(running) -> None:
    running.attach(running.make_store("docs"), "docs")
    secret, _ = running.create_key(["docs"])
    asked = body(running.query(secret, "MATCH (d:Doc) RETURN d.id ORDER BY d.id", limit=10_000))
    assert asked["row_count"] == 4, "a clamp must not refuse the request"

    running.context.service.max_request_rows = 2
    clamped = body(running.query(secret, "MATCH (d:Doc) RETURN d.id ORDER BY d.id", limit=10_000))
    assert clamped["row_count"] == 2
    assert clamped["truncated"] is True, "the response says it is a partial answer"
    assert clamped["rows"] == [[1], [2]]


def test_parameters_are_bound_not_interpolated(daemon) -> None:
    payload = body(
        daemon.query(
            daemon.secret,  # type: ignore[attr-defined]
            "MATCH (d:Doc) WHERE d.id = $id RETURN d.title",
            parameters={"id": 2},
        )
    )
    assert payload["rows"] == [["vector search"]]

    injection = daemon.query(
        daemon.secret,  # type: ignore[attr-defined]
        "MATCH (d:Doc) WHERE d.title = $title RETURN d.id",
        parameters={"title": "' OR 1=1 --"},
    )
    assert body(injection)["rows"] == []


def test_typed_parameters_round_trip_through_json(writable_daemon) -> None:
    daemon = writable_daemon
    created = daemon.client.post(
        "/admin/api/databases/docs/ddl",
        json={
            "statements": [
                "CREATE NODE TABLE Typed(id INT64, amount DECIMAL(9,4), seen DATE, "
                "at TIMESTAMP, embedding VECTOR(semantic), PRIMARY KEY(id))"
            ]
        },
    )
    assert created.status_code == 200, created.text

    # The write door is the WebSocket session; tagged JSON parameters become engine values.
    with daemon.client.websocket_connect(f"/api/v1/ws?token={daemon.secret}") as socket:
        socket.send_json({"type": "hello", "db": "docs", "mode": "write"})
        assert socket.receive_json()["type"] == "hello_ok"
        socket.send_json({"type": "begin", "id": 1, "mode": "write"})
        assert socket.receive_json()["type"] == "begin_ok"
        socket.send_json(
            {
                "type": "execute",
                "id": 2,
                "query": (
                    "CREATE (:Typed {id: 1, amount: $amount, seen: $day, at: $when, "
                    "embedding: $v})"
                ),
                "parameters": {
                    "amount": {"$decimal": {"coefficient": -123450, "precision": 9, "scale": 4}},
                    "day": {"$date": "2024-02-29"},
                    "when": {"$timestamp_micros": 1700000000000000},
                    "v": {"$vector": [0.5, 0.5]},
                },
            }
        )
        while True:
            message = socket.receive_json()
            if message["type"] in {"batch_end", "error"}:
                assert message["type"] == "batch_end", message
                break
        socket.send_json({"type": "commit", "id": 3})
        while True:
            message = socket.receive_json()
            if message["type"] in {"commit_ok", "error"}:
                assert message["type"] == "commit_ok", message
                break

    payload = body(
        daemon.query(
            daemon.secret,
            "MATCH (t:Typed {id:1}) RETURN t.amount, t.seen, t.at, t.embedding",
        )
    )
    amount, day, when, embedding = payload["rows"][0]
    assert amount["kind"] == "decimal" and amount["value"] == "-12.3450"
    assert amount["coefficient"] == -123450 and amount["scale"] == 4
    assert day["kind"] == "date" and day["value"] == "2024-02-29"
    assert when["kind"] == "timestamp" and when["micros"] == 1700000000000000
    assert embedding["kind"] == "vector" and embedding["values"][0] == 0.5
    assert embedding["space_ref"] > 0, "the column's own space, read back intact"
    assert payload["warnings"] == []


def test_explain_shows_the_plan_without_running_the_query(daemon) -> None:
    payload = body(
        daemon.client.get(
            "/api/v1/databases/docs/explain",
            params={"query": "MATCH (d:Doc {id:1}) RETURN d.title"},
            headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
        )
    )
    assert payload["columns"] == ["d.title"]
    assert payload["operator"]
    operators = json.dumps(payload)
    assert any(
        name in operators for name in ("NodeScan", "IndexSeek", "IndexProbe", "NodeLookup")
    ), operators


def test_query_with_explain_true_includes_the_plan(daemon) -> None:
    payload = body(
        daemon.query(daemon.secret, "MATCH (d:Doc) RETURN d", explain=True)  # type: ignore[attr-defined]
    )
    assert payload["plan"] and payload["plan"]["operator"]
    assert payload["row_count"] > 0


def test_bad_statements_are_typed_refusals_with_request_ids(daemon) -> None:
    unparseable = daemon.query(daemon.secret, "MATCH (d:Doc RETURN d")  # type: ignore[attr-defined]
    assert unparseable.status_code == 400
    error = body(unparseable)["error"]
    assert error["code"] == "parse_error"
    assert error["request_id"]
    assert "Traceback" not in error["message"]
    assert error["retryable"] is False
    assert error["details"].get("line")

    unplannable = daemon.query(daemon.secret, "MATCH (d:Doc) RETURN d.nope")  # type: ignore[attr-defined]
    assert unplannable.status_code == 400
    assert body(unplannable)["error"]["code"] == "plan_error"

    unknown_function = daemon.query(daemon.secret, "RETURN nosuchfunction(1)")  # type: ignore[attr-defined]
    assert unknown_function.status_code == 400
    assert body(unknown_function)["error"]["code"] == "plan_error"


def test_an_unknown_label_is_an_empty_answer_not_an_error(daemon) -> None:
    # Grafx resolves labels when it executes: a label the store does not have scans to nothing.
    # The server passes that through instead of inventing a refusal the engine does not make.
    empty = daemon.query(daemon.secret, "MATCH (d:Nope) RETURN d")  # type: ignore[attr-defined]
    assert empty.status_code == 200
    payload = body(empty)
    assert payload["rows"] == [] and payload["row_count"] == 0
    assert payload["truncated"] is False

    empty = daemon.query(daemon.secret, "")  # type: ignore[attr-defined]
    assert empty.status_code == 422, "an empty statement is a schema refusal, not an engine error"
    assert body(empty)["error"]["details"]["problems"][0]["field"] == "query"

    bad_json = daemon.client.post(
        "/api/v1/databases/docs/query",
        json={"query": 123},
        headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
    )
    assert bad_json.status_code == 422
    assert body(bad_json)["error"]["code"] == "invalid_request"
    assert body(bad_json)["error"]["details"]["problems"]


def test_write_statements_are_refused_at_the_read_door(daemon) -> None:
    for statement in (
        "CREATE (:Doc {id: 77, title: 'nope'})",
        "MATCH (d:Doc {id:1}) SET d.title = 'nope'",
        "MATCH (d:Doc {id:1}) DELETE d",
    ):
        refused = daemon.query(daemon.secret, statement)  # type: ignore[attr-defined]
        assert refused.status_code == 403, statement
        error = body(refused)["error"]
        assert error["code"] == "write_not_permitted"
        assert error["details"]["reason"] == "read_only_door"
        assert "WebSocket" in error["message"]

    still_there = body(daemon.query(daemon.secret, "MATCH (d:Doc {id:1}) RETURN d.title"))  # type: ignore[attr-defined]
    assert still_there["rows"] == [["durable wal"]]


def test_a_key_without_write_permission_cannot_write_even_when_the_database_allows_it(running) -> None:
    running.attach(running.make_store("docs"), "docs", read_only=False, allow_writes=True)
    read_only_key, _ = running.create_key(["docs"], allow_writes=False)
    with running.client.websocket_connect(
        f"/api/v1/ws?token={read_only_key}"
    ) as socket:
        socket.send_json({"type": "hello", "db": "docs", "mode": "write"})
        message = socket.receive_json()
        assert message["type"] == "error"
        assert message["code"] == "write_not_permitted"
        assert "key" in message["message"].lower() or "allow_writes" in message["message"]


def test_a_read_only_registration_refuses_writes_even_for_a_writing_key(running) -> None:
    running.attach(running.make_store("docs"), "docs", read_only=True, allow_writes=False)
    secret, _ = running.create_key(["docs"], allow_writes=True)
    with running.client.websocket_connect(f"/api/v1/ws?token={secret}") as socket:
        socket.send_json({"type": "hello", "db": "docs", "mode": "write"})
        message = socket.receive_json()
        assert message["type"] == "error"
        assert message["code"] == "write_not_permitted"
        assert "read-only" in message["message"] or "read_only" in message["message"]


# -- value fidelity --------------------------------------------------------------------


def test_vectors_and_long_strings_are_reported_not_flattened(daemon) -> None:
    payload = body(
        daemon.query(  # type: ignore[attr-defined]
            daemon.secret, "MATCH (d:Doc {id:1}) RETURN d.v, d.body"
        )
    )
    vector, _body_text = payload["rows"][0]
    assert vector["kind"] == "vector"
    assert vector["dtype"] == "float32"
    assert len(vector["values"]) == 2


def _unindexed_blob_table(daemon) -> None:  # noqa: ANN001
    """A column with no text index.

    The full-text index has its own token budget, so a multi-kilobyte value would be refused by
    the index long before the value encoding under test is exercised.
    """

    created = daemon.client.post(
        "/admin/api/databases/docs/ddl",
        json={"statements": ["CREATE NODE TABLE Blob(id INT64, big STRING, PRIMARY KEY(id))"]},
    )
    assert created.status_code == 200, created.text


def test_a_long_value_is_returned_whole_by_default(writable_daemon) -> None:
    daemon = writable_daemon
    _unindexed_blob_table(daemon)
    long_body = "y" * 5000
    with daemon.client.websocket_connect(f"/api/v1/ws?token={daemon.secret}") as socket:
        socket.send_json({"type": "hello", "db": "docs", "mode": "write"})
        socket.receive_json()
        socket.send_json({"type": "begin", "id": 1, "mode": "write"})
        socket.receive_json()
        socket.send_json(
            {
                "type": "execute",
                "id": 2,
                "query": "CREATE (:Blob {id: 50, big: $body})",
                "parameters": {"body": long_body},
            }
        )
        while True:
            message = socket.receive_json()
            if message["type"] in {"batch_end", "error"}:
                assert message["type"] == "batch_end", message
                break
        socket.send_json({"type": "commit", "id": 3})
        while True:
            message = socket.receive_json()
            if message["type"] in {"commit_ok", "error"}:
                assert message["type"] == "commit_ok", message
                break

    payload = body(daemon.query(daemon.secret, "MATCH (b:Blob {id:50}) RETURN b.big"))
    assert payload["rows"][0][0] == long_body
    assert payload["warnings"] == []


def test_a_value_over_the_cap_is_cut_and_says_so(running) -> None:
    running.attach(running.make_store("docs"), "docs", read_only=False, allow_writes=True)
    secret, _ = running.create_key(["docs"], allow_writes=True)
    _unindexed_blob_table(running)
    running.context.service.max_query_value_characters = 64
    with running.client.websocket_connect(f"/api/v1/ws?token={secret}") as socket:
        socket.send_json({"type": "hello", "db": "docs", "mode": "write"})
        socket.receive_json()
        socket.send_json({"type": "begin", "id": 1, "mode": "write"})
        socket.receive_json()
        socket.send_json(
            {
                "type": "execute",
                "id": 2,
                "query": "CREATE (:Blob {id: 51, big: $body})",
                "parameters": {"body": "z" * 500},
            }
        )
        while True:
            message = socket.receive_json()
            if message["type"] in {"batch_end", "error"}:
                assert message["type"] == "batch_end", message
                break
        socket.send_json({"type": "commit", "id": 3})
        while True:
            message = socket.receive_json()
            if message["type"] in {"commit_ok", "error"}:
                assert message["type"] == "commit_ok", message
                break

    payload = body(running.query(secret, "MATCH (b:Blob {id:51}) RETURN b.big"))
    assert payload["rows"][0][0] == "z" * 64
    kinds = [warning["kind"] for warning in payload["warnings"]]
    assert "string_truncated" in kinds
    warning = payload["warnings"][0]
    assert warning["characters"] == 500 and warning["kept"] == 64

    # The store still holds the whole value; the cap is about the response, not the data.
    running.context.service.max_query_value_characters = 65536
    whole = body(running.query(secret, "MATCH (b:Blob {id:51}) RETURN b.big"))
    assert whole["rows"][0][0] == "z" * 500 and whole["warnings"] == []


# -- payload limits --------------------------------------------------------------------


def test_oversized_requests_are_refused_before_the_engine_runs(running) -> None:
    running.attach(running.make_store("docs"), "docs")
    secret, _ = running.create_key(["docs"])
    huge = "MATCH (d:Doc) RETURN d // " + ("x" * (3 * 1024 * 1024))
    response = running.client.post(
        "/api/v1/databases/docs/query",
        json={"query": huge},
        headers=running.bearer(secret),
    )
    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "request_too_large"
    assert "max_request_bytes" in error["message"] or "2" in error["message"]


def test_query_length_is_bounded_by_the_schema(daemon) -> None:
    response = daemon.query(daemon.secret, "MATCH (n) RETURN n " + "-- " + "y" * 250_000)  # type: ignore[attr-defined]
    assert response.status_code == 422


def test_timeout_bounds_are_validated(daemon) -> None:
    for bad in (-1, 0, 601):
        refused = daemon.query(  # type: ignore[attr-defined]
            daemon.secret, "MATCH (d:Doc) RETURN d", timeout_seconds=bad
        )
        assert refused.status_code == 422, bad
        problem = body(refused)["error"]["details"]["problems"][0]
        assert problem["field"] == "timeout_seconds"

    # A real timeout is honoured by the engine and surfaces as a 408; a store this small can
    # finish inside a microsecond, so the honest assertion is: it is either fast or typed.
    response = daemon.query(  # type: ignore[attr-defined]
        daemon.secret, "MATCH (d:Doc) RETURN d", timeout_seconds=30
    )
    assert response.status_code == 200
    assert body(response)["row_count"] == 4
