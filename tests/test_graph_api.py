"""Graph payloads for Sigma.js (plan item 10): physical ids, caps, loops, parallel edges."""

from __future__ import annotations

import re

UUID_RE = re.compile(r"^[0-9a-f]{32}$")


def body(response):  # noqa: ANN001
    return response.json()


def graph(daemon, payload):  # noqa: ANN001
    return daemon.client.post(
        "/api/v1/databases/docs/graph",
        json=payload,
        headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
    )


def neighbors(daemon, **params):  # noqa: ANN001
    return daemon.client.get(
        "/api/v1/databases/docs/neighbors",
        params=params,
        headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
    )


# -- projection ------------------------------------------------------------------------


def test_projection_is_topology_only_and_names_the_tables(daemon) -> None:
    payload = body(
        graph(
            daemon,
            {
                "mode": "projection",
                "node_tables": ["Doc", "Tag"],
                "relationship_tables": ["CITES", "TAGGED"],
            },
        )
    )
    assert payload["mode"] == "projection"
    assert payload["shown_nodes"] == 6 and payload["total_nodes"] == 6
    assert payload["shown_edges"] == 8 and payload["truncated"] is False
    assert UUID_RE.match(payload["database_uuid"])
    assert payload["snapshot_lsn"] > 0

    doc = next(node for node in payload["nodes"] if node["id"] == "Doc:1")
    assert doc["label"] == "Doc" and doc["table"] == "Doc" and doc["record_id"] == 1
    assert doc["category"] == "Doc", "the UI colours by category"
    assert doc["properties"] == {}, "projection is topology: it never pretends to carry properties"


def test_projection_refuses_to_choose_tables_for_the_caller(daemon) -> None:
    refused = graph(daemon, {"mode": "projection", "max_nodes": 10})
    assert refused.status_code == 400
    assert body(refused)["error"]["code"] == "graph_selection_required"

    endpoints = graph(
        daemon, {"mode": "projection", "node_tables": ["Tag"], "relationship_tables": ["TAGGED"]}
    )
    assert endpoints.status_code == 400
    error = body(endpoints)["error"]
    assert error["code"] == "graph_endpoints_missing"
    assert error["details"]["missing"] == ["Doc"]
    assert "implicitly" in error["message"]


def test_projection_weights_come_from_a_named_column(daemon) -> None:
    payload = body(
        graph(
            daemon,
            {
                "mode": "projection",
                "node_tables": ["Doc"],
                "relationship_tables": ["CITES"],
                "weight_columns": {"CITES": "weight"},
            },
        )
    )
    assert [edge["properties"]["weight"] for edge in payload["edges"]] == [
        1.0,
        0.5,
        2.0,
        0.25,
        0.75,
    ]
    assert all(edge["id"].startswith("CITES:") for edge in payload["edges"])

    unweighted = body(
        graph(
            daemon,
            {"mode": "projection", "node_tables": ["Doc"], "relationship_tables": ["CITES"]},
        )
    )
    assert all(edge["properties"] == {} for edge in unweighted["edges"])


def test_a_projection_that_does_not_fit_is_refused_not_silently_cut(daemon) -> None:
    refused = graph(
        daemon,
        {"mode": "projection", "node_tables": ["Doc", "Tag"], "relationship_tables": ["CITES"], "max_nodes": 2},
    )
    assert refused.status_code == 413
    error = body(refused)["error"]
    assert error["code"] == "budget_exceeded"
    assert "silently truncate" in error["message"]
    assert error["details"]["max_graph_nodes"] == 2000
    assert "mode='query'" in error["message"]


# -- query mode ------------------------------------------------------------------------


def test_query_mode_carries_properties_and_physical_ids(daemon) -> None:
    payload = body(
        graph(
            daemon,
            {
                "mode": "query",
                "query": "MATCH (a:Doc)-[r:CITES]->(b:Doc) RETURN a, r, b",
            },
        )
    )
    assert payload["mode"] == "query"
    assert payload["shown_nodes"] == 4 and payload["total_nodes"] == 4
    assert payload["shown_edges"] == 5
    assert payload["truncated"] is False

    doc1 = next(node for node in payload["nodes"] if node["id"] == "Doc:1")
    assert doc1["properties"]["title"] == "durable wal"
    assert doc1["properties"]["v"]["kind"] == "vector"
    assert all(UUID_RE.match(payload["database_uuid"]) for _ in [0])


def test_parallel_edges_and_self_loops_survive(daemon) -> None:
    parallel = body(
        graph(
            daemon,
            {
                "mode": "query",
                "query": "MATCH (a:Doc {id: 1})-[r:CITES]->(b:Doc {id: 4}) RETURN a, r, b",
            },
        )
    )
    assert [(edge["id"], edge["source"], edge["target"]) for edge in parallel["edges"]] == [
        ("CITES:4", "Doc:1", "Doc:4"),
        ("CITES:5", "Doc:1", "Doc:4"),
    ], "two edges between the same pair are two edges, not one with a merged weight"

    loop = body(
        graph(
            daemon,
            {
                "mode": "query",
                "query": "MATCH (a:Doc {id: 3})-[r:CITES]->(b:Doc {id: 3}) RETURN a, r, b",
            },
        )
    )
    assert [edge["source"] == edge["target"] for edge in loop["edges"]] == [True]
    assert len(loop["nodes"]) == 1, "a loop visits one node"


def test_caps_cut_the_answer_and_say_so(daemon) -> None:
    capped = body(
        graph(
            daemon,
            {
                "mode": "query",
                "query": "MATCH (a:Doc)-[r:CITES]->(b:Doc) RETURN a, r, b",
                "max_nodes": 1,
            },
        )
    )
    assert len(capped["nodes"]) == 1
    assert capped["truncated"] is True
    assert capped["total_nodes"] == 4 and capped["total_edges"] == 5
    assert capped["edges"] == [], "an edge whose endpoints were cut is not shown"

    one_edge = body(
        graph(
            daemon,
            {
                "mode": "query",
                "query": "MATCH (a:Doc)-[r:CITES]->(b:Doc) RETURN a, r, b",
                "max_edges": 1,
            },
        )
    )
    assert len(one_edge["edges"]) == 1 and one_edge["truncated"] is True


def test_a_relationship_without_its_endpoints_is_still_drawn(daemon) -> None:
    payload = body(
        graph(
            daemon,
            {"mode": "query", "query": "MATCH (:Doc)-[r:CITES]->(:Doc) RETURN r"},
        )
    )
    assert payload["edges"], "RETURN r is a legitimate statement"
    stubbed = [warning for warning in payload["warnings"] if warning["kind"] == "endpoint_not_returned"]
    assert stubbed, "the payload says which nodes were added without their properties"
    stub_ids = {warning["id"] for warning in stubbed}
    for node in payload["nodes"]:
        if node["id"] in stub_ids:
            assert node["properties"] == {}
            assert node["label"] == node["table"] == "Doc"


def test_query_mode_passes_parameters_and_reports_query_warnings(daemon) -> None:
    payload = body(
        graph(
            daemon,
            {
                "mode": "query",
                "query": "MATCH (a:Doc {id: $id})-[r:CITES]->(b) RETURN a, r, b",
                "parameters": {"id": 1},
            },
        )
    )
    assert {node["id"] for node in payload["nodes"]} == {"Doc:1", "Doc:2", "Doc:4"}

    bad = graph(daemon, {"mode": "query", "query": "MATCH (a:Nope) RETURN a"})
    assert bad.status_code == 200 and body(bad)["nodes"] == []

    missing_text = graph(daemon, {"mode": "query"})
    assert missing_text.status_code == 400
    assert body(missing_text)["error"]["field"] == "query"

    bad_mode = graph(daemon, {"mode": "magic"})
    assert bad_mode.status_code == 422


# -- neighbours ------------------------------------------------------------------------


def test_neighbours_expand_one_hop_with_properties(daemon) -> None:
    payload = body(neighbors(daemon, table="Doc", record_id=1))
    assert payload["mode"] == "neighbors"
    ids = {node["id"] for node in payload["nodes"]}
    assert ids == {"Doc:1", "Doc:2", "Doc:4", "Tag:1"}
    anchor = next(node for node in payload["nodes"] if node["id"] == "Doc:1")
    assert anchor["properties"]["title"] == "durable wal"
    assert any(edge["label"] == "TAGGED" for edge in payload["edges"])
    # totals describe this node's neighbourhood, not the whole store
    assert payload["total_nodes"] == 4 and payload["total_edges"] == 5
    assert payload["shown_nodes"] == 4 and payload["shown_edges"] == 5


def test_neighbour_limits_are_honest_about_truncation(daemon) -> None:
    payload = body(neighbors(daemon, table="Doc", record_id=1, limit=1))
    assert payload["shown_edges"] == 1
    assert payload["truncated"] is True
    assert payload["total_edges"] == 5, "the total is the number of incident edges, not store-wide"
    assert payload["shown_nodes"] == 2

    everything = body(neighbors(daemon, table="Doc", record_id=1, limit=1000))
    assert everything["shown_edges"] == everything["total_edges"] == 5
    assert everything["truncated"] is False

    over_cap = neighbors(daemon, table="Doc", record_id=1, limit=10_000)
    assert over_cap.status_code == 422, "the neighbour limit is bounded by the documented cap"
    assert body(over_cap)["error"]["details"]["problems"][0]["field"] == "query.limit"

    below_one = neighbors(daemon, table="Doc", record_id=1, limit=0)
    assert below_one.status_code == 422


def test_neighbours_refuse_a_node_that_is_not_there(daemon) -> None:
    ghost = neighbors(daemon, table="Doc", record_id=999)
    assert ghost.status_code == 404
    assert body(ghost)["error"]["code"] == "node_not_found"

    ghost_table = neighbors(daemon, table="Ghost", record_id=1)
    assert ghost_table.status_code == 404
    assert body(ghost_table)["error"]["field"] == "table"

    bad_type = neighbors(daemon, table="Doc", record_id="not-a-number")
    assert bad_type.status_code == 422


def test_the_neighbour_preview_says_what_it_could_not_read(running) -> None:
    running.attach(running.make_store("docs"), "docs")
    secret, _ = running.create_key(["docs"])
    original = running.context.service.max_graph_nodes
    running.context.service.max_graph_nodes = 1
    response = running.client.get(
        "/api/v1/databases/docs/neighbors",
        params={"table": "Doc", "record_id": 1},
        headers=running.bearer(secret),
    )
    running.context.service.max_graph_nodes = original
    if response.status_code == 200:
        kinds = [warning["kind"] for warning in response.json()["warnings"]]
        assert "properties_not_read" in kinds, response.json()["warnings"]
    else:
        assert response.status_code == 413
