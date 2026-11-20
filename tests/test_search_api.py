"""Search parity (plan item 9): the server's answers match the engine's, on the same store."""

from __future__ import annotations

import pytest
from okto_grafx import connect


def body(response):  # noqa: ANN001
    return response.json()


@pytest.fixture
def store(daemon):  # noqa: ANN001
    """The same directory the daemon has attached, opened directly for the reference call."""

    record = daemon.context.store.get_database("docs")  # type: ignore[attr-defined]
    handle = connect(record.path)
    yield handle
    handle.close()


# -- full text -------------------------------------------------------------------------


def test_text_search_matches_the_engine(daemon, store) -> None:
    response = daemon.client.post(
        "/api/v1/databases/docs/search/text",
        json={"index": "doc_text", "query": "vector", "k": 5},
        headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
    )
    assert response.status_code == 200, response.text
    payload = body(response)

    direct = store.search_text(index="doc_text", query="vector", k=5)
    assert [hit.record_id for hit in direct.hits] == [hit["record_id"] for hit in payload["hits"]]
    for direct_hit, wire in zip(direct.hits, payload["hits"]):
        assert wire["score"] == pytest.approx(float(direct_hit.score))
        assert wire["id"] == f"{wire['table']}:{wire['record_id']}"

    assert payload["table"] == "Doc"
    assert payload["regime"]
    assert payload["corpus_documents"] >= 4
    assert payload["postings_visited"] > 0
    assert payload["index_built_through_commit"] > 0
    assert payload["snapshot_commit"] > 0


def test_phrase_and_prefix_searches_report_the_regime_they_used(daemon) -> None:
    phrase = body(
        daemon.client.post(
            "/api/v1/databases/docs/search/text",
            json={"index": "doc_text", "query": "write ahead", "phrase": True},
            headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
        )
    )
    assert [hit["id"] for hit in phrase["hits"]] == ["Doc:1"]
    assert "phrase" in phrase["regime"]
    assert phrase["hits"][0]["matched_terms"] == ["ahead", "write"]
    assert phrase["hits"][0]["matched_fields"] == ["body"]

    absent = body(
        daemon.client.post(
            "/api/v1/databases/docs/search/text",
            json={"index": "doc_text", "query": "recovery write ahead", "phrase": True},
            headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
        )
    )
    assert absent["hits"] == [], "word order matters in a phrase: the corpus does not say this"

    prefix = daemon.client.post(
        "/api/v1/databases/docs/search/text",
        json={"index": "doc_text", "query": "vector", "prefix": True},
        headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
    )
    # This index was built without prefix postings. Saying so beats returning a
    # result that quietly was not a prefix search.
    assert prefix.status_code == 400
    assert body(prefix)["error"]["code"] == "unsupported_operation"
    assert "prefix" in body(prefix)["error"]["message"].lower()


def test_positions_are_refused_when_the_index_cannot_answer_them(daemon) -> None:
    refused = daemon.client.post(
        "/api/v1/databases/docs/search/text",
        json={"index": "doc_text", "query": "vector", "return_positions": True},
        headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
    )
    assert refused.status_code == 400
    error = body(refused)["error"]
    assert error["code"] == "unsupported_operation"
    assert "position" in error["message"].lower()


def test_an_unknown_index_is_named_not_ignored(daemon) -> None:
    refused = daemon.client.post(
        "/api/v1/databases/docs/search/text",
        json={"index": "ghost_index", "query": "vector"},
        headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
    )
    assert refused.status_code == 400
    error = body(refused)["error"]
    assert error["code"] == "index_error"
    assert "ghost_index" in error["message"]


# -- vectors ---------------------------------------------------------------------------


def test_vector_search_matches_the_engine(daemon, store) -> None:
    vector = [0.0, 1.0]
    response = daemon.client.post(
        "/api/v1/databases/docs/search/vector",
        json={"space": "semantic", "vector": vector, "k": 2},
        headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
    )
    assert response.status_code == 200, response.text
    payload = body(response)
    assert payload["requested_k"] == 2 and payload["achieved_k"] == 2
    assert len(payload["hits"]) == 2
    assert payload["space"] == "semantic" and payload["table"] == "Doc"
    assert payload["regime"] in {"exact", "hnsw"}

    with store.begin("read") as txn:
        direct = store.search_vectors(txn, space="semantic", query=vector, k=2)
    assert [hit.record_id for hit in direct.hits] == [hit["record_id"] for hit in payload["hits"]]
    for direct_hit, wire in zip(direct.hits, payload["hits"]):
        assert wire["score"] == pytest.approx(float(direct_hit.score))
    assert payload["hits"][0]["retired"] is False


def test_a_vector_of_the_wrong_shape_is_refused(daemon) -> None:
    refused = daemon.client.post(
        "/api/v1/databases/docs/search/vector",
        json={"space": "semantic", "vector": [1.0, 2.0, 3.0], "k": 2},
        headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
    )
    assert refused.status_code == 400
    error = body(refused)["error"]
    assert error["code"] == "vector_validation"
    assert "2 components" in error["message"]

    unknown_space = daemon.client.post(
        "/api/v1/databases/docs/search/vector",
        json={"space": "ghost", "vector": [1.0, 0.0], "k": 1},
        headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
    )
    assert unknown_space.status_code == 404
    assert body(unknown_space)["error"]["code"] == "unknown_vector_space"


def test_a_shared_space_makes_the_caller_name_its_owner(daemon) -> None:
    # Two tables carry a column in the same space: picking "the first index" would be a guess.
    created = daemon.client.post(
        "/admin/api/databases/docs/ddl",
        json={
            "statements": [
                "CREATE NODE TABLE Also(id INT64, v VECTOR(semantic), PRIMARY KEY(id))"
            ]
        },
    )
    assert created.status_code == 200, created.text

    ambiguous = daemon.client.post(
        "/api/v1/databases/docs/search/vector",
        json={"space": "semantic", "vector": [1.0, 0.0], "k": 3},
        headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
    )
    assert ambiguous.status_code == 409
    error = body(ambiguous)["error"]
    assert error["code"] == "ambiguous_vector_owner"
    assert set(owners_of(error)) >= {"Doc", "Also"}, error["details"]
    assert "table" in error["message"]

    ghost_owner = daemon.client.post(
        "/api/v1/databases/docs/search/vector",
        json={"space": "semantic", "vector": [1.0, 0.0], "k": 3, "table": "Ghost"},
        headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
    )
    assert ghost_owner.status_code == 400
    error = body(ghost_owner)["error"]
    assert "Ghost" in error["message"] and error["field"] == "table"

    # And naming an owner that exists answers the question instead of guessing between two tables.
    resolved = daemon.client.post(
        "/api/v1/databases/docs/search/vector",
        json={"space": "semantic", "vector": [1.0, 0.0], "k": 3, "table": "Doc"},
        headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
    )
    assert resolved.status_code == 200, resolved.text
    assert {hit["table"] for hit in body(resolved)["hits"]} == {"Doc"}


def owners_of(error: dict) -> list[str]:  # noqa: ANN201
    return [item.split(":")[0] for item in error["details"]["owners"]]


# -- hybrid ----------------------------------------------------------------------------


def test_hybrid_search_matches_the_engine(daemon, store) -> None:
    payload = body(
        daemon.client.post(
            "/api/v1/databases/docs/search/hybrid",
            json={
                "table": "Doc",
                "index": "doc_text",
                "query": "vector",
                "space": "semantic",
                "vector": [0.0, 1.0],
                "k": 3,
            },
            headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
        )
    )
    assert payload["regime"] == "complete"
    assert payload["fusion"] == "rrf_v1_union"
    assert payload["lexical_regime"] == "exact_index"
    assert payload["source_errors"] == []
    top = payload["hits"][0]
    assert top["lexical_rank"] == 1 and top["vector_rank"] == 1
    assert top["score"] > 0

    direct = store.search_hybrid(
        table="Doc",
        index="doc_text",
        query="vector",
        space="semantic",
        vector=[0.0, 1.0],
        k=3,
    )
    assert [hit.record_id for hit in direct.hits] == [hit["record_id"] for hit in payload["hits"]]


def test_an_unavailable_hybrid_source_is_a_refusal_not_a_quiet_omission(daemon) -> None:
    refused = daemon.client.post(
        "/api/v1/databases/docs/search/hybrid",
        json={
            "table": "Doc",
            "index": "ghost_index",
            "query": "vector",
            "space": "semantic",
            "vector": [0.0, 1.0],
            "k": 3,
        },
        headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
    )
    assert refused.status_code == 400
    error = body(refused)["error"]
    assert error["code"] == "unsupported_operation"
    assert error["details"]["sources"]

    partial = body(
        daemon.client.post(
            "/api/v1/databases/docs/search/hybrid",
            json={
                "table": "Doc",
                "index": "doc_text",
                "query": "vector",
                "k": 3,
                "allow_partial": True,
            },
            headers=daemon.bearer(daemon.secret),  # type: ignore[attr-defined]
        )
    )
    # With the caller's permission the answer comes back, but it says which source is missing.
    assert partial["regime"] == "partial"
    assert partial["vector_regime"] == "unavailable"
    assert partial["source_errors"]
    assert all(hit["vector_rank"] is None for hit in partial["hits"])
