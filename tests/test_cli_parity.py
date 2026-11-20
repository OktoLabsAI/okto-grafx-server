"""The library's own CLI is the oracle, and it is also the second process.

Two integration promises from the plan, both checked against the real `oktografx` binary rather than
a reimplementation of its logic in this file:

* §Testes 9 — `search_text` / `search_vector` / `search_hybrid` **match the corresponding CLI result
  on the same store**;
* §Testes 16 — a **second process** reading the same store while the server answers: MVCC holds and
  nothing is left with a broken lock.

The CLI's search verbs open read-only, and a read-only open can only prove a checkpoint-complete
store (§Fundamentos). So each test runs the server's own `checkpoint` action first — the same remedy
the console suggests when a store refuses a read-only open.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import Harness

CLI = Path(sys.executable).parent / "oktografx"
requires_cli = pytest.mark.skipif(
    not CLI.exists(), reason=f"the oktografx CLI is not installed at {CLI}"
)


def run_cli(*args: str) -> dict[str, Any]:
    """`oktografx … --json`, with the refusal surfaced when there is one."""

    process = subprocess.run(
        [str(CLI), *[str(argument) for argument in args], "--json"],
        capture_output=True,
        text=True,
        timeout=180,
    )
    try:
        payload = json.loads(process.stdout or "{}")
    except json.JSONDecodeError as exc:  # pragma: no cover - the CLI always answers in JSON
        raise AssertionError(
            f"oktografx {' '.join(args)} did not answer in JSON: {process.stdout[:400]!r}"
            f"\n{process.stderr[:400]}"
        ) from exc
    assert process.returncode == 0, (
        f"oktografx {' '.join(args)} refused: {json.dumps(payload.get('error'))[:600]}"
    )
    return payload


def checkpoint(harness: Harness, slug: str = "docs") -> None:
    """Make the store provable to a read-only open, the way an operator would."""

    queued = harness.client.post(f"/admin/api/databases/{slug}/actions/checkpoint", json={})
    assert queued.status_code == 202, queued.text
    job = harness.wait_for_job(queued.json()["job_id"])
    assert job["state"] == "succeeded", job


def store_path(harness: Harness, slug: str = "docs") -> str:
    return harness.context.store.get_database(slug).path


@requires_cli
def test_the_text_search_answers_what_the_cli_answers(daemon: Harness) -> None:
    path = store_path(daemon)
    checkpoint(daemon)

    tool = run_cli("search", "text", path, "--index", "doc_text", "--query", "wal", "--k", "3")
    served = daemon.client.post(
        "/api/v1/databases/docs/search/text",
        json={"index": "doc_text", "query": "wal", "k": 3},
        headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
    )
    assert served.status_code == 200, served.text
    api = served.json()
    engine = tool["search"]

    assert api["regime"] == engine["regime"]
    assert api["corpus_documents"] == engine["corpus_documents"]
    assert api["candidates"] == engine["candidates"]
    assert len(api["hits"]) == len(engine["hits"]) > 0, "the oracle found nothing to compare"
    for served_hit, cli_hit in zip(api["hits"], engine["hits"]):
        # The server adds the physical id it can link into the graph; the numbers are the engine's.
        assert served_hit["record_id"] == cli_hit["record_id"]
        assert served_hit["score"] == pytest.approx(cli_hit["score"], rel=0, abs=1e-12)
        assert served_hit["matched_terms"] == cli_hit["matched_terms"]
        assert served_hit["matched_fields"] == cli_hit["matched_fields"]
        assert served_hit["id"] == f"Doc:{cli_hit['record_id']}"


@requires_cli
def test_the_vector_search_answers_what_the_cli_answers(daemon: Harness) -> None:
    path = store_path(daemon)
    checkpoint(daemon)

    tool = run_cli("search", "vector", path, "--space", "semantic", "--vector", "[1,0]", "--k", "3")
    served = daemon.client.post(
        "/api/v1/databases/docs/search/vector",
        json={"space": "semantic", "vector": [1.0, 0.0], "k": 3},
        headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
    )
    assert served.status_code == 200, served.text
    api, engine = served.json(), tool["search"]

    assert api["regime"] == engine["regime"]
    assert api["space"] == engine["space"]
    assert api["requested_k"] == engine["requested_k"]
    assert api["achieved_k"] == engine["achieved_k"]
    assert [hit["record_id"] for hit in api["hits"]] == [hit["record_id"] for hit in engine["hits"]]
    for served_hit, cli_hit in zip(api["hits"], engine["hits"]):
        assert served_hit["score"] == pytest.approx(cli_hit["score"], rel=0, abs=1e-12)
        assert served_hit["retired"] == cli_hit["retired"]


@requires_cli
def test_the_hybrid_search_answers_what_the_cli_answers(daemon: Harness) -> None:
    path = store_path(daemon)
    checkpoint(daemon)

    tool = run_cli(
        "search",
        "hybrid",
        path,
        "--table",
        "Doc",
        "--index",
        "doc_text",
        "--space",
        "semantic",
        "--query",
        "wal",
        "--vector",
        "[1,0]",
        "--k",
        "2",
    )
    served = daemon.client.post(
        "/api/v1/databases/docs/search/hybrid",
        json={
            "table": "Doc",
            "index": "doc_text",
            "space": "semantic",
            "query": "wal",
            "vector": [1.0, 0.0],
            "k": 2,
        },
        headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
    )
    assert served.status_code == 200, served.text
    api, engine = served.json(), tool["search"]

    # Every regime, the fusion identity and each rank/score pair must agree: hybrid search is where a
    # server could quietly invent its own ranking.
    assert api["regime"] == engine["regime"]
    assert api["fusion"] == engine["fusion"]
    assert api["lexical_regime"] == engine["lexical_regime"]
    assert api["vector_regime"] == engine["vector_regime"]
    assert api["source_errors"] == engine["source_errors"] == []
    assert len(api["hits"]) == len(engine["hits"]) > 0
    for served_hit, cli_hit in zip(api["hits"], engine["hits"]):
        assert served_hit["table"] == cli_hit["table"]
        assert served_hit["record_id"] == cli_hit["record_id"]
        for key in ("score", "lexical_score", "lexical_rank", "vector_score", "vector_rank"):
            if served_hit[key] is None or cli_hit[key] is None:
                assert served_hit[key] is cli_hit[key] is None, key
            else:
                assert served_hit[key] == pytest.approx(cli_hit[key], rel=0, abs=1e-12), key


@requires_cli
def test_another_process_reads_the_same_store_while_the_server_serves(daemon: Harness) -> None:
    path = store_path(daemon)
    checkpoint(daemon)
    statement = "MATCH (d:Doc) RETURN d.id ORDER BY d.id"

    before = daemon.query(daemon.secret, statement)  # type: ignore[attr-defined]
    assert before.status_code == 200, before.text

    tool = run_cli("query", path, statement, "--read-only")
    assert tool["result"] == "ok"
    assert tool["columns"] == before.json()["columns"]
    assert [row[0] for row in tool["rows"]] == [row[0] for row in before.json()["rows"]]

    # Two processes on one store: the server's own snapshot afterwards is still clean, and the
    # stranger did not leave anything behind that the store has to recover from.
    after = daemon.query(daemon.secret, "MATCH (d:Doc) RETURN count(d) AS n")  # type: ignore[attr-defined]
    assert after.status_code == 200, after.text
    assert after.json()["rows"][0][0] == 4
    assert daemon.context.pool.open_transactions("docs") == 0

    inventory = daemon.client.get("/admin/api/databases/docs/inventory")
    assert inventory.status_code == 200
    assert inventory.json()["maintenance"]["recovery_required"] is False


@requires_cli
def test_the_cli_reports_the_same_schema_the_server_serves(daemon: Harness) -> None:
    """The console mirrors `oktografx schema --json`; that only means something if it is checked."""

    path = store_path(daemon)
    checkpoint(daemon)
    tool = run_cli("schema", path)
    served = daemon.client.get(
        "/api/v1/databases/docs/schema", headers=daemon.bearer(daemon.secret)  # type: ignore[attr-defined]
    )
    assert served.status_code == 200, served.text
    api = served.json()

    cli_tables = {table["name"]: table for table in tool["tables"]}
    assert sorted(api["tables"], key=lambda table: table["name"]) and len(cli_tables) == tool[
        "total_tables"
    ]
    assert sorted(table["name"] for table in api["tables"]) == sorted(
        name for name, table in cli_tables.items() if table["kind"] == "node"
    )
    assert sorted(rel["name"] for rel in api["relationships"]) == sorted(
        name for name, table in cli_tables.items() if table["kind"] == "rel"
    )

    # Columns are where a wrapper silently loses information (a vector column's space, a decimal's
    # scale), so they are compared field by field rather than by count.
    for served_table in api["tables"] + api["relationships"]:
        origin = cli_tables[served_table["name"]]
        assert served_table["table_id"] == origin["table_id"]
        assert served_table["kind"] == origin["kind"]
        if served_table["kind"] == "rel":
            assert served_table["from_table"] == origin["from_table"]
            assert served_table["to_table"] == origin["to_table"]
            # The CLI prints a relationship's physical endpoint columns alongside its properties; the
            # server names the endpoints instead and lists only the declared properties.
            physical = {"_from", "_to"}
            assert [
                (column["name"], column["type"], column["nullable"])
                for column in served_table["columns"]
            ] == [
                (column["name"], column["type"], column["nullable"])
                for column in origin["columns"]
                if column["name"] not in physical
            ]
            assert {column["name"] for column in origin["columns"]} >= physical
        else:
            assert served_table["primary_key"] == origin["primary_key"]
            assert [
                (column["name"], column["type"], column["nullable"], column["vector_space"])
                for column in served_table["columns"]
            ] == [
                (column["name"], column["type"], column["nullable"], column["vector_space"])
                for column in origin["columns"]
            ]

    cli_spaces = {space["name"]: space for space in tool["spaces"]}
    assert sorted(space["name"] for space in api["vector_spaces"]) == sorted(cli_spaces)
    for served_space in api["vector_spaces"]:
        origin = cli_spaces[served_space["name"]]
        assert served_space["dimension"] == origin["dimension"]
        assert served_space["metric"].lower() == origin["metric"].lower()
        assert served_space["storage_dtype"] == origin["storage_dtype"]
        assert served_space["state"] == origin["state"]
