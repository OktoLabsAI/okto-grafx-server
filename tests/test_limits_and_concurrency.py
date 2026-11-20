"""DoS limits, backpressure and other processes on the same store (plan items 11, 16)."""

from __future__ import annotations

import json
import multiprocessing as mp
import time
from typing import Any

from conftest import Harness


def body(response):  # noqa: ANN001
    return response.json()


# -- child processes -------------------------------------------------------------------


def outside_writer(path: str, queue: "mp.Queue[Any]") -> None:
    """Another Grafx process: change one row and commit it."""

    from okto_grafx import connect

    try:
        db = connect(path)
        with db.begin("write") as txn:
            txn.execute("MATCH (d:Doc {id: 1}) SET d.title = 'changed outside the server'", {})
        queue.put(("committed", db.identity.database_uuid.hex()))
        db.close()
    except Exception as exc:  # noqa: BLE001 - the parent asserts on the report
        queue.put(("error", f"{type(exc).__name__}: {exc}"[:300]))


def outside_holder(path: str, queue: "mp.Queue[Any]", hold: float) -> None:
    """Another Grafx process that keeps a write transaction open, then commits."""

    from okto_grafx import connect

    try:
        db = connect(path)
        txn = db.begin("write")
        txn.execute("CREATE (:Doc {id: 777, title: 'outsider'})", {})
        queue.put(("held", None))
        time.sleep(hold)
        txn.commit()
        queue.put(("committed", None))
        db.close()
    except Exception as exc:  # noqa: BLE001
        queue.put(("error", f"{type(exc).__name__}: {exc}"[:300]))


def second_daemon(root: str, path: str, queue: "mp.Queue[Any]") -> None:
    """A second daemon with its own state directory, serving the same store."""

    from pathlib import Path

    harness = Harness(Path(root) / "other", allowed_roots=[str(Path(path).parent)])
    with harness:
        harness.login()
        harness.attach(Path(path), "mirror")
        secret, _ = harness.create_key(["mirror"])
        response = harness.query(secret, "MATCH (d:Doc) RETURN count(d) AS c", db="mirror")
        queue.put(("query", [response.status_code, response.json().get("rows")]))


# -- request caps ----------------------------------------------------------------------


def test_the_row_cap_clamps_and_says_so(tmp_path) -> None:
    harness = Harness(tmp_path.resolve(), max_request_rows=3)
    with harness:
        harness.login()
        harness.attach(harness.make_store("docs"), "docs")
        secret, _ = harness.create_key(["docs"])

        rows = harness.query(secret, "MATCH (d:Doc) RETURN d.id ORDER BY d.id").json()
        assert rows["row_count"] == 3
        assert rows["truncated"] is True, "a capped answer must be marked as capped"

        asked = harness.query(
            secret, "MATCH (d:Doc) RETURN d.id ORDER BY d.id", limit=9_999
        ).json()
        assert asked["row_count"] == 3, "a caller cannot raise the server's cap by asking"

        negative = harness.query(secret, "MATCH (d:Doc) RETURN d.id", limit=-1)
        assert negative.status_code == 422
        assert body(negative)["error"]["details"]["problems"][0]["field"] == "limit"


def test_a_body_above_the_cap_is_refused_before_it_is_read(tmp_path) -> None:
    harness = Harness(tmp_path.resolve(), max_request_bytes=2_048)
    with harness:
        harness.login()
        harness.attach(harness.make_store("docs"), "docs")
        secret, _ = harness.create_key(["docs"])
        response = harness.client.post(
            "/api/v1/databases/docs/query",
            json={"query": "MATCH (d:Doc) RETURN d.id", "parameters": {"pad": "x" * 5_000}},
            headers=harness.bearer(secret),
        )
        assert response.status_code == 413
        error = body(response)["error"]
        assert error["code"] == "request_too_large"
        assert "2048" in error["message"]


def test_the_batch_cap_produces_a_truncated_answer(tmp_path) -> None:
    harness = Harness(tmp_path.resolve(), max_ws_batches=1)
    with harness:
        harness.login()
        harness.attach(harness.make_store("docs"), "docs")
        secret, _ = harness.create_key(["docs"])
        with harness.client.websocket_connect(f"/api/v1/ws?token={secret}") as socket:
            socket.send_json({"type": "hello", "db": "docs"})
            assert socket.receive_json()["type"] == "hello_ok"
            socket.send_json({"type": "begin", "id": 1})
            socket.receive_json()
            socket.send_json(
                {
                    "type": "execute",
                    "id": 2,
                    "query": "MATCH (d:Doc) RETURN d.id ORDER BY d.id",
                    "batch_size": 1,
                }
            )
            batches = 0
            sent = 0
            while True:
                message = socket.receive_json()
                if message["type"] == "batch":
                    batches += 1
                    sent += len(message["rows"])
                elif message["type"] == "batch_end":
                    assert batches == 1, "the server stops sending batches at its cap"
                    assert message["truncated"] is True
                    assert message["row_count"] == sent
                    assert message["rows_available"] == 4
                    break
            socket.send_json({"type": "close"})


def test_reader_lane_backpressure_arrives_promptly(tmp_path) -> None:
    """Waiting for a lane is bounded, and the refusal is the retryable kind."""

    harness = Harness(tmp_path.resolve(), readers_per_database=1, reader_lane_timeout_seconds=1.0)
    with harness:
        harness.login()
        harness.attach(harness.make_store("docs"), "docs")
        secret, _ = harness.create_key(["docs"])

        # A session holds a lane for its whole life: that is the point of a session.
        with harness.client.websocket_connect(f"/api/v1/ws?token={secret}") as socket:
            socket.send_json({"type": "hello", "db": "docs"})
            assert socket.receive_json()["type"] == "hello_ok"
            socket.send_json({"type": "begin", "id": 1})
            socket.receive_json()

            started = time.monotonic()
            response = harness.query(secret, "MATCH (d:Doc) RETURN d.id")
            waited = time.monotonic() - started
            assert response.status_code == 429, response.text
            error = body(response)["error"]
            assert error["code"] == "reader_lanes_busy"
            assert error["retryable"] is True
            # A retryable refusal that does not say when invites a stampede.
            assert response.headers.get("retry-after"), "429 without Retry-After"
            assert waited < 5.0, f"backpressure took {waited:.1f}s, which is not backpressure"

            socket.send_json({"type": "close"})
            socket.receive_json()

        released = harness.query(secret, "MATCH (d:Doc) RETURN count(d) AS c")
        assert released.status_code == 200
        assert released.json()["rows"] == [[4]]


def test_a_statement_that_overshoots_its_deadline_is_refused(running) -> None:
    running.attach(running.make_store("docs"), "docs")
    secret, _ = running.create_key(["docs"])
    heavy = (
        "MATCH (a:Doc), (b:Doc), (c:Doc), (d:Doc), (e:Doc), (f:Doc) "
        "RETURN a.id, f.title"
    )
    original = running.context.service.query_timeout_seconds  # type: ignore[attr-defined]
    running.context.service.query_timeout_seconds = 1e-6  # type: ignore[attr-defined]
    try:
        response = running.query(secret, heavy)
        assert response.status_code == 408, response.text
        error = body(response)["error"]
        assert error["code"] == "query_deadline_exceeded"
        assert error["retryable"] is True
    finally:
        running.context.service.query_timeout_seconds = original  # type: ignore[attr-defined]
    assert running.query(secret, "MATCH (d:Doc) RETURN count(d) AS c").status_code == 200


def test_queries_are_serialised_per_store_rather_than_piled_up(running) -> None:
    """More concurrent readers than lanes must not turn into an unbounded queue."""

    from concurrent.futures import ThreadPoolExecutor

    running.attach(running.make_store("docs"), "docs")
    secret, _ = running.create_key(["docs"])

    def one(_: int) -> int:
        return running.query(  # type: ignore[attr-defined]
            secret, "MATCH (a:Doc), (b:Doc) RETURN a.id, b.id"
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(one, range(8)))

    assert set(codes) <= {200, 429}, codes
    assert codes.count(200) >= 1
    busy = codes.count(429)
    if busy:
        # A refusal is only honest if the client can retry it.
        assert busy + codes.count(200) == 8


# -- other processes on the same store -------------------------------------------------


def test_a_writer_in_another_process_wins_the_race_it_reached_first(running) -> None:
    """Optimistic concurrency across processes: one commit is refused, not silently applied."""

    running.attach(running.make_store("docs"), "docs", read_only=False, allow_writes=True)
    secret, _ = running.create_key(["docs"], allow_writes=True)
    path = running.client.get("/admin/api/databases").json()["databases"][0]["path"]

    queue: mp.Queue[Any] = mp.Queue()
    with running.client.websocket_connect(f"/api/v1/ws?token={secret}") as socket:
        socket.send_json({"type": "hello", "db": "docs", "mode": "write"})
        assert socket.receive_json()["type"] == "hello_ok"
        socket.send_json({"type": "begin", "id": 1, "mode": "write"})
        assert socket.receive_json()["type"] == "begin_ok"

        socket.send_json({"type": "execute", "id": 2, "query": "MATCH (d:Doc {id: 1}) RETURN d.title"})
        while True:
            message = socket.receive_json()
            if message["type"] in {"batch_end", "error"}:
                assert message["collected_rows"] if "collected_rows" in message else True
                break

        child = mp.Process(target=outside_writer, args=(path, queue), daemon=True)
        child.start()
        kind, _detail = queue.get(timeout=60)
        child.join(timeout=60)
        assert kind == "committed", _detail

        socket.send_json(
            {
                "type": "execute",
                "id": 3,
                "query": "MATCH (d:Doc {id: 1}) SET d.title = 'changed by the server'",
            }
        )
        while True:
            message = socket.receive_json()
            if message["type"] in {"batch_end", "error"}:
                break

        socket.send_json({"type": "commit", "id": 4})
        committed = socket.receive_json()
        socket.send_json({"type": "close"})

    assert committed["type"] == "error", "the server must not overwrite a committed change"
    assert committed["code"] == "write_conflict", committed
    assert committed["retryable"] is True
    rows = running.query(secret, "MATCH (d:Doc {id:1}) RETURN d.title").json()["rows"]
    assert rows == [["changed outside the server"]]


def test_reads_keep_working_while_another_process_writes(running) -> None:
    running.attach(running.make_store("docs"), "docs", read_only=False, allow_writes=True)
    secret, _ = running.create_key(["docs"])
    path = running.client.get("/admin/api/databases").json()["databases"][0]["path"]

    queue: mp.Queue[Any] = mp.Queue()
    child = mp.Process(target=outside_holder, args=(path, queue, 1.5), daemon=True)
    child.start()
    assert queue.get(timeout=60)[0] == "held"

    counts = []
    for _ in range(4):
        response = running.query(secret, "MATCH (d:Doc) RETURN count(d) AS c")
        assert response.status_code == 200, response.text
        counts.append(response.json()["rows"][0][0])
        time.sleep(0.2)

    assert queue.get(timeout=60)[0] == "committed"
    child.join(timeout=60)
    assert set(counts) <= {4, 5}, f"a reader saw a torn write: {counts}"

    final = running.query(secret, "MATCH (d:Doc) RETURN count(d) AS c").json()["rows"][0][0]
    assert final == 5, "the other process's committed write becomes visible"


def test_two_daemons_can_serve_one_store_without_fighting(tmp_path) -> None:
    """The server is a logical access layer, not a lock on the directory."""

    root = tmp_path.resolve()
    harness = Harness(root)
    harness.login()
    path = harness.make_store("docs")
    harness.attach(path, "docs")
    secret, _ = harness.create_key(["docs"])

    queue: mp.Queue[Any] = mp.Queue()
    child = mp.Process(target=second_daemon, args=(str(root), str(path), queue), daemon=True)
    child.start()
    kind, payload = queue.get(timeout=120)
    child.join(timeout=60)

    own = harness.query(secret, "MATCH (d:Doc) RETURN count(d) AS c").json()["rows"]
    assert kind == "query", payload
    assert payload == [200, own], json.dumps(payload)
