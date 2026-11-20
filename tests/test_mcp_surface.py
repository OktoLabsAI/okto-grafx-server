"""MCP over Streamable HTTP (plan item 14): same key, same scope, agent-shaped refusals."""

from __future__ import annotations

import json

PROTOCOL = "2025-06-18"


def call(client, secret: str, method: str, params: dict, request_id: int = 1):  # noqa: ANN001
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
        headers={"Authorization": f"Bearer {secret}"},
    )


def tool(client, secret: str, name: str, arguments: dict, request_id: int = 1):  # noqa: ANN001
    return call(client, secret, "tools/call", {"name": name, "arguments": arguments}, request_id)


def payload(response):  # noqa: ANN001
    """The structured part of a tools/call result."""

    return response.json()["result"]["structuredContent"]


# -- handshake -------------------------------------------------------------------------


def test_initialize_announces_the_server(daemon) -> None:
    response = call(
        daemon.client,
        daemon.secret,  # type: ignore[attr-defined]
        "initialize",
        {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        },
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["protocolVersion"] == PROTOCOL
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert result["serverInfo"]["name"] == "okto-grafx-server"
    assert "physical" in result["instructions"]


def test_ping_and_notifications(daemon) -> None:
    assert call(daemon.client, daemon.secret, "ping", {}).json()["result"] == {}  # type: ignore[attr-defined]
    notification = daemon.client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
    )
    assert notification.status_code == 202
    assert notification.text == ""


def test_get_is_a_pointed_refusal_and_batching_is_declined(daemon) -> None:
    wrong_method = daemon.client.get("/mcp")
    assert wrong_method.status_code == 405
    assert "JSON-RPC POST" in wrong_method.json()["error"]

    batch = daemon.client.post(
        "/mcp",
        json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
        headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
    )
    assert batch.status_code == 200
    assert batch.json()["error"]["code"] == -32600

    unknown = call(daemon.client, daemon.secret, "tools/nope", {})  # type: ignore[attr-defined]
    assert unknown.json()["error"]["code"] == -32601


def test_credentials_are_jsonrpc_errors(daemon) -> None:
    daemon.client.cookies.clear()  # no admin session either
    response = call(daemon.client, "okg_" + "z" * 40, "ping", {})
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == -32001
    assert error["data"]["code"] == "unknown_api_key"

    anonymous = daemon.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert anonymous.status_code == 401


# -- tools and scope -------------------------------------------------------------------


def test_tool_list_is_scoped_to_the_credential(daemon) -> None:
    writable, _ = daemon.create_key(["docs"], allow_writes=True)

    read_only = [tool["name"] for tool in call(
        daemon.client, daemon.secret, "tools/list", {}  # type: ignore[attr-defined]
    ).json()["result"]["tools"]]
    assert read_only == [
        "list_databases",
        "describe_schema",
        "query",
        "explain",
        "search_text",
        "search_vector",
        "search_hybrid",
        "graph_neighborhood",
        "graph_projection",
    ]
    assert "write" not in read_only, "a read-only key must not be offered a write tool"

    with_writes = [tool["name"] for tool in call(
        daemon.client, writable, "tools/list", {}
    ).json()["result"]["tools"]]
    assert "write" in with_writes

    for tool in call(daemon.client, writable, "tools/list", {}).json()["result"]["tools"]:  # noqa: B007
        assert tool["inputSchema"]["type"] == "object"
        assert set(tool) == {"name", "description", "inputSchema", "annotations"}, tool
        assert "database" not in tool and "_write" not in tool
    write_tool = [item for item in with_writes]
    assert write_tool


def test_list_databases_reports_only_the_keys_databases(daemon) -> None:
    daemon.attach(daemon.make_store("second"), "second")
    mine = payload(tool(daemon.client, daemon.secret, "list_databases", {}))  # type: ignore[attr-defined]
    assert [db["slug"] for db in mine["databases"]] == ["docs"]
    assert mine["databases"][0]["writable"] is False

    other, _ = daemon.create_key(["second"])
    theirs = payload(tool(daemon.client, other, "list_databases", {}))
    assert [db["slug"] for db in theirs["databases"]] == ["second"]


def test_a_tool_can_not_reach_a_database_the_key_cannot(daemon) -> None:
    daemon.attach(daemon.make_store("second"), "second")
    refused = tool(
        daemon.client,
        daemon.secret,  # type: ignore[attr-defined]
        "query",
        {"db": "second", "cypher": "MATCH (n) RETURN n"},
    )
    assert refused.status_code == 200, "an MCP refusal travels as a tool result, not an HTTP error"
    body = refused.json()["result"]
    assert body["isError"] is True
    assert body["structuredContent"]["code"] == "not_found"

    # The same answer for a database that was never registered: no oracle.
    ghost = tool(
        daemon.client, daemon.secret, "query", {"db": "ghost", "cypher": "MATCH (n) RETURN n"}  # type: ignore[attr-defined]
    )
    def comparable(frame: dict) -> dict:
        error = dict(frame["structuredContent"])
        error.pop("request_id", None)
        # The message quotes the caller's own slug, which is not information about the server.
        error.pop("message", None)
        return error

    assert comparable(ghost.json()["result"]) == comparable(body)
    assert "second" in body["structuredContent"]["message"]
    assert "ghost" in ghost.json()["result"]["structuredContent"]["message"]


def test_a_missing_database_argument_is_named(daemon) -> None:
    refused = tool(daemon.client, daemon.secret, "query", {"cypher": "MATCH (n) RETURN n"})  # type: ignore[attr-defined]
    assert refused.json()["result"]["isError"] is True
    assert "No database registered with id ''" in refused.json()["result"]["content"][0]["text"]


# -- reads -----------------------------------------------------------------------------


def test_query_tool_returns_the_same_rows_as_rest(daemon) -> None:
    mcp_rows = payload(tool(daemon.client, daemon.secret, "query", {  # type: ignore[attr-defined]
        "db": "docs",
        "cypher": "MATCH (d:Doc) RETURN d.id ORDER BY d.id LIMIT 2",
    }))
    rest = daemon.query(daemon.secret, "MATCH (d:Doc) RETURN d.id ORDER BY d.id LIMIT 2")  # type: ignore[attr-defined]
    assert mcp_rows["rows"] == rest.json()["rows"]
    assert mcp_rows["columns"] == rest.json()["columns"]
    assert mcp_rows["truncated"] is False


def test_text_vector_and_hybrid_tools_answer(daemon) -> None:
    text = payload(tool(daemon.client, daemon.secret, "search_text", {  # type: ignore[attr-defined]
        "db": "docs", "index": "doc_text", "query": "vector", "k": 2
    }))
    assert text["hits"][0]["id"] == "Doc:2"

    vector = payload(tool(daemon.client, daemon.secret, "search_vector", {  # type: ignore[attr-defined]
        "db": "docs", "space": "semantic", "vector": [0.0, 1.0], "k": 1
    }))
    assert vector["hits"][0]["id"] == "Doc:2"

    hybrid = payload(tool(daemon.client, daemon.secret, "search_hybrid", {  # type: ignore[attr-defined]
        "db": "docs", "table": "Doc", "index": "doc_text", "query": "vector",
        "space": "semantic", "vector": [0.0, 1.0], "k": 2,
    }))
    assert hybrid["regime"] == "complete"


def test_graph_and_schema_tools_use_physical_ids(daemon) -> None:
    schema = payload(tool(daemon.client, daemon.secret, "describe_schema", {"db": "docs"}))  # type: ignore[attr-defined]
    assert {table["name"] for table in schema["tables"]} == {"Doc", "Tag"}

    neighborhood = payload(tool(daemon.client, daemon.secret, "graph_neighborhood", {"db": "docs", "table": "Doc", "record_id": 1}))  # type: ignore[attr-defined]
    assert "Doc:1" in [node["id"] for node in neighborhood["nodes"]]
    assert all(":" in node["id"] for node in neighborhood["nodes"])

    projection = payload(tool(daemon.client, daemon.secret, "graph_projection", {
        "db": "docs", "node_tables": ["Doc"], "relationship_tables": ["CITES"]
    }))  # type: ignore[attr-defined]
    assert projection["mode"] == "projection"
    assert projection["edges"]

    plan = payload(tool(daemon.client, daemon.secret, "explain", {"db": "docs", "cypher": "MATCH (d:Doc) RETURN d"}))  # type: ignore[attr-defined]
    assert plan["operator"]


def test_engine_refusals_are_tool_results_with_the_same_codes(daemon) -> None:
    refused = tool(daemon.client, daemon.secret, "query", {"db": "docs", "cypher": "MATCH (d:Doc) RETURN d.nope"})  # type: ignore[attr-defined]
    result = refused.json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "plan_error"

    unknown_tool = tool(daemon.client, daemon.secret, "make_me_a_sandwich", {"db": "docs"})  # type: ignore[attr-defined]
    text = unknown_tool.json()["result"]["content"][0]["text"]
    assert "Unknown tool" in text and "list_databases" in text

    malformed = call(daemon.client, daemon.secret, "tools/call", {"name": "query", "arguments": "nope"})  # type: ignore[attr-defined]
    assert malformed.json()["result"]["isError"] is True
    assert "arguments" in malformed.json()["result"]["content"][0]["text"]


# -- writes ----------------------------------------------------------------------------


def test_a_write_tool_needs_both_the_credential_and_confirmation(writable_daemon) -> None:
    daemon = writable_daemon

    unconfirmed = tool(daemon.client, daemon.secret, "write", {"db": "docs", "cypher": "CREATE (:Doc {id: 200})"})
    assert unconfirmed.json()["result"]["isError"] is True
    assert "confirm_write" in unconfirmed.json()["result"]["content"][0]["text"]
    assert daemon.query(daemon.secret, "MATCH (d:Doc {id:200}) RETURN d").json()["rows"] == []

    confirmed = tool(daemon.client, daemon.secret, "write", {
        "db": "docs", "cypher": "CREATE (:Doc {id: 201, title: 'via mcp'})", "confirm_write": True
    })
    assert confirmed.json()["result"]["isError"] is False
    commit = confirmed.json()["result"]["structuredContent"]["commit"]
    assert commit["durable"] is True and commit["csn"] > 0

    assert daemon.query(daemon.secret, "MATCH (d:Doc {id:201}) RETURN d.title").json()["rows"] == [
        ["via mcp"]
    ]


def test_a_read_only_key_has_no_write_path_at_all(daemon) -> None:
    missing = tool(daemon.client, daemon.secret, "write", {"db": "docs", "cypher": "CREATE (:Doc {id: 202})", "confirm_write": True})  # type: ignore[attr-defined]
    assert missing.json()["result"]["isError"] is True
    assert "Unknown tool" in missing.json()["result"]["content"][0]["text"]

    smuggled = tool(daemon.client, daemon.secret, "query", {"db": "docs", "cypher": "CREATE (:Doc {id: 203})", "confirm_write": True})  # type: ignore[attr-defined]
    assert smuggled.json()["result"]["isError"] is True
    assert smuggled.json()["result"]["structuredContent"]["code"] == "write_not_permitted"
    assert daemon.query(daemon.secret, "MATCH (d:Doc {id:203}) RETURN d").json()["rows"] == []  # type: ignore[attr-defined]


def test_the_write_tool_states_its_mutations_in_the_audit_log(writable_daemon) -> None:
    tool(writable_daemon.client, writable_daemon.secret, "write", {
        "db": "docs", "cypher": "CREATE (:Doc {id: 210, title: 'audited'})", "confirm_write": True
    })
    entries = writable_daemon.client.get("/admin/api/audit").json()["entries"]
    write_entries = [entry for entry in entries if entry["action"].startswith("mcp")]
    assert write_entries, json.dumps(entries[:3], default=str)
    assert write_entries[0]["actor"].startswith("key:")
