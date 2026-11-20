"""The write door: one transaction per WebSocket session (plan items 7, 8, 13).

A leaked socket would keep the daemon's shutdown waiting, so every session here is opened through
:func:`open_session`, which always exits the underlying transport context manager.
"""

from __future__ import annotations

import contextlib
import json
import time
from typing import Any, Iterator

import pytest

TERMINAL = {"batch_end", "error"}


class Session:
    """A live WebSocket session plus the request/reply plumbing the tests do not repeat."""

    def __init__(self, socket: Any, hello: dict[str, Any]) -> None:
        self.socket = socket
        self.hello = hello

    # -- raw frames --------------------------------------------------------------------

    def send(self, payload: dict) -> None:
        self.socket.send_json(payload)

    def receive(self) -> dict:
        return self.socket.receive_json()

    def until(self, *types: str) -> dict:
        while True:
            message = self.receive()
            if message["type"] in types:
                return message
            if message["type"] == "error":
                raise AssertionError(f"unexpected error while waiting for {types}: {message}")

    # -- transaction verbs -------------------------------------------------------------

    def begin(self, mode: str = "read", request_id: int = 1) -> dict:
        self.send({"type": "begin", "id": request_id, "mode": mode})
        return self.until("begin_ok", "error")

    def execute(
        self,
        query: str,
        parameters: dict | None = None,
        request_id: int = 2,
        **extra: Any,
    ) -> dict:
        self.send(
            {"type": "execute", "id": request_id, "query": query, "parameters": parameters, **extra}
        )
        rows: list[Any] = []
        while True:
            message = self.receive()
            if message["type"] == "batch":
                rows.extend(message["rows"])
            elif message["type"] in TERMINAL:
                message["collected_rows"] = rows
                return message

    def commit(self, request_id: int = 9) -> dict:
        self.send({"type": "commit", "id": request_id})
        return self.until("commit_ok", "error")

    def rollback(self, request_id: int = 9) -> dict:
        self.send({"type": "rollback", "id": request_id})
        return self.until("rollback_ok", "error")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.send({"type": "close"})


@contextlib.contextmanager
def open_session(
    client: Any, token: str | None = None, *, db: str = "docs", mode: str = "read"
) -> Iterator[Session]:
    query = f"?token={token}" if token else ""
    transport = client.websocket_connect(f"/api/v1/ws{query}")
    socket = transport.__enter__()
    try:
        socket.send_json({"type": "hello", "db": db, "mode": mode})
        try:
            hello = socket.receive_json()
        except Exception:  # noqa: BLE001 - a handshake the server refused mid-flight
            hello = {"type": "disconnected"}
        yield Session(socket, hello)
    finally:
        with contextlib.suppress(Exception):
            socket.send_json({"type": "close"})
        with contextlib.suppress(Exception):
            transport.__exit__(None, None, None)


@contextlib.contextmanager
def raw_socket(client: Any, token: str | None = None) -> Iterator[Any]:
    transport = client.websocket_connect(f"/api/v1/ws{f'?token={token}' if token else ''}")
    socket = transport.__enter__()
    try:
        yield socket
    finally:
        with contextlib.suppress(Exception):
            transport.__exit__(None, None, None)


@pytest.fixture
def writable(running):  # noqa: ANN001
    running.attach(running.make_store("docs"), "docs", read_only=False, allow_writes=True)
    secret, _record = running.create_key(["docs"], allow_writes=True)
    return running, secret


# -- handshake -------------------------------------------------------------------------


def test_hello_negotiates_the_session(daemon) -> None:
    with open_session(daemon.client, daemon.secret) as session:
        hello = session.hello
        assert hello["type"] == "hello_ok"
        assert hello["protocol_version"] == 1
        assert hello["db"] == "docs"
        assert hello["read_only"] is True
        assert hello["max_transactions_per_session"] == 1
        assert len(hello["session_id"]) == 32
        assert "One transaction per session" in hello["note"]

        session.send({"type": "ping", "id": 7})
        pong = session.receive()
        assert pong["type"] == "pong" and pong["id"] == 7


def test_the_first_message_must_be_hello(daemon) -> None:
    with raw_socket(daemon.client, daemon.secret) as socket:
        socket.send_json({"type": "begin", "id": 1})
        message = socket.receive_json()
        assert message["type"] == "error"
        assert message["code"] == "hello_required"


def test_unknown_credentials_close_the_door(daemon) -> None:
    daemon.client.cookies.clear()  # no admin session either
    with raw_socket(daemon.client) as socket:
        socket.send_json({"type": "hello", "db": "docs"})
        message = socket.receive_json()
        assert message["type"] == "error"
        assert message["code"] == "missing_api_key"

    with raw_socket(daemon.client, "x" * 30) as socket:
        socket.send_json({"type": "hello", "db": "docs"})
        assert socket.receive_json()["code"] == "malformed_api_key"

    with raw_socket(daemon.client, "okg_" + "z" * 40) as socket:
        socket.send_json({"type": "hello", "db": "docs"})
        assert socket.receive_json()["code"] == "unknown_api_key"


def test_a_key_can_not_open_a_database_it_was_not_scoped_to(daemon) -> None:
    daemon.attach(daemon.make_store("second"), "second")
    with open_session(daemon.client, daemon.secret, db="second") as session:
        assert session.hello["type"] == "error"
        assert session.hello["code"] == "not_found", "the refusal gives no oracle about other stores"


def test_a_malformed_frame_is_refused_without_killing_the_session(daemon) -> None:
    with open_session(daemon.client, daemon.secret) as session:
        assert session.hello["type"] == "hello_ok"
        session.socket.send_text("this is not json")
        assert session.receive()["code"] == "malformed_message"
        session.send({"type": "teleport", "id": 2})
        refused = session.receive()
        assert refused["code"] == "unknown_message" and refused["id"] == 2
        session.send({"type": "ping", "id": 3})
        assert session.receive()["type"] == "pong"


def test_statements_need_a_transaction(daemon) -> None:
    with open_session(daemon.client, daemon.secret) as session:
        session.send({"type": "execute", "id": 1, "query": "MATCH (n) RETURN n"})
        message = session.receive()
        assert message["type"] == "error" and message["code"] == "no_transaction"
        assert message["id"] == 1, "the refusal carries the caller's correlation id"


# -- read sessions ---------------------------------------------------------------------


def test_a_read_session_streams_batches_and_reports_the_end(daemon) -> None:
    with open_session(daemon.client, daemon.secret) as session:
        begin = session.begin()
        assert begin["type"] == "begin_ok"
        assert begin["mode"] == "read"
        assert begin["tx"] is not None

        done = session.execute("MATCH (d:Doc) RETURN d.id, d.title ORDER BY d.id", batch_size=2)
        assert done["type"] == "batch_end"
        assert done["row_count"] == 4
        assert done["rows_available"] == 4
        assert done["truncated"] is False
        assert done["columns"] == ["d.id", "d.title"]
        assert done["collected_rows"] == [[1, "durable wal"], [2, "vector search"],
                                          [3, "graph views"], [4, "concurrency"]]
        assert done["statistics"]
        assert done["warnings"] == []
        assert done["plan"] is None


def test_batches_arrive_separately_and_in_order(daemon) -> None:
    with open_session(daemon.client, daemon.secret) as session:
        session.begin()
        sequences: list[int] = []
        session.send(
            {
                "type": "execute",
                "id": 1,
                "query": "MATCH (d:Doc) RETURN d.id ORDER BY d.id",
                "batch_size": 1,
            }
        )
        while True:
            message = session.receive()
            if message["type"] == "batch":
                sequences.append(message["seq"])
                assert len(message["rows"]) == 1
            elif message["type"] == "batch_end":
                assert message["seq"] == 4 and message["row_count"] == 4
                break
        assert sequences == [0, 1, 2, 3]


def test_limit_truncates_and_says_so(daemon) -> None:
    with open_session(daemon.client, daemon.secret) as session:
        session.begin()
        done = session.execute("MATCH (d:Doc) RETURN d.id ORDER BY d.id", limit=2)
        assert done["truncated"] is True
        assert done["row_count"] == 2
        assert done["rows_available"] == 4
        assert done["limit"] == 2


def test_explain_in_a_session_returns_the_plan(daemon) -> None:
    with open_session(daemon.client, daemon.secret) as session:
        session.begin()
        done = session.execute("MATCH (d:Doc) RETURN d", explain=True)
        assert done["plan"] and done["plan"]["operator"]


def test_a_read_session_refuses_to_write(daemon) -> None:
    with open_session(daemon.client, daemon.secret) as session:
        refused = session.begin("write")
        assert refused["type"] == "error"
        assert refused["code"] == "write_not_permitted"
        assert "mode='write'" in refused["message"]


# -- write sessions --------------------------------------------------------------------


def test_multi_statement_write_commits_atomically(writable) -> None:
    running, secret = writable
    with open_session(running.client, secret, mode="write") as session:
        begin = session.begin("write")
        assert begin["type"] == "begin_ok" and begin["mode"] == "write"
        assert session.execute("CREATE (:Doc {id: 100, title: 'one'})")["type"] == "batch_end"
        assert (
            session.execute(
                "MATCH (d:Doc {id: $id}) SET d.title = $title",
                {"id": 100, "title": "renamed"},
            )["type"]
            == "batch_end"
        )
        committed = session.commit()
        assert committed["durable"] is True and committed["csn"] > 0

    assert running.query(secret, "MATCH (d:Doc {id:100}) RETURN d.title").json()["rows"] == [
        ["renamed"]
    ]


def test_a_rolled_back_transaction_leaves_nothing_behind(writable) -> None:
    running, secret = writable
    with open_session(running.client, secret, mode="write") as session:
        session.begin("write")
        session.execute("CREATE (:Doc {id: 101, title: 'ghost'})")
        assert session.rollback()["type"] == "rollback_ok"

        again = session.begin("write", request_id=20)
        assert again["type"] == "begin_ok"
        session.execute("CREATE (:Doc {id: 102, title: 'kept'})")
        assert session.commit()["type"] == "commit_ok"

    assert running.query(secret, "MATCH (d:Doc {id:101}) RETURN d").json()["rows"] == []
    assert running.query(secret, "MATCH (d:Doc {id:102}) RETURN d.title").json()["rows"] == [["kept"]]


def test_dropping_the_connection_rolls_the_transaction_back(writable) -> None:
    running, secret = writable
    with open_session(running.client, secret, mode="write") as session:
        session.begin("write")
        done = session.execute("CREATE (:Doc {id: 103, title: 'abandoned'})")
        assert done["type"] == "batch_end"
        # No commit, no rollback: the connection simply goes away.

    time.sleep(0.2)
    assert running.query(secret, "MATCH (d:Doc {id:103}) RETURN d").json()["rows"] == [], (
        "an uncommitted transaction must not survive its connection"
    )


def test_only_one_transaction_per_session(writable) -> None:
    running, secret = writable
    with open_session(running.client, secret, mode="write") as session:
        session.begin("write")
        refused = session.begin("write", request_id=2)
        assert refused["type"] == "error"
        assert refused["code"] == "transaction_open"
        assert "Commit or roll it back" in refused["message"]


def test_a_write_session_can_open_transactions_in_sequence(writable) -> None:
    running, secret = writable
    with open_session(running.client, secret, mode="write") as session:
        session.begin("write")
        session.execute("CREATE (:Doc {id: 104})")
        assert session.commit(request_id=5)["type"] == "commit_ok"
        assert session.begin("write", request_id=6)["type"] == "begin_ok"
        assert session.rollback(request_id=7)["type"] == "rollback_ok"
    assert running.query(secret, "MATCH (d:Doc {id:104}) RETURN d").json()["rows"] != []


def test_a_statement_error_does_not_end_the_transaction(writable) -> None:
    running, secret = writable
    with open_session(running.client, secret, mode="write") as session:
        session.begin("write")
        session.execute("CREATE (:Doc {id: 105, title: 'kept'})")
        failed = session.execute("MATCH (d:Doc) RETURN d.nope", request_id=3)
        assert failed["type"] == "error"
        assert failed["code"] == "plan_error"
        assert failed["id"] == 3
        assert session.commit()["type"] == "commit_ok"
    assert running.query(secret, "MATCH (d:Doc {id:105}) RETURN d.title").json()["rows"] == [["kept"]]


def test_a_statement_error_inside_a_write_leaves_nothing_after_rollback(writable) -> None:
    running, secret = writable
    with open_session(running.client, secret, mode="write") as session:
        session.begin("write")
        session.execute("CREATE (:Doc {id: 106, title: 'first'})")
        refused = session.execute("CREATE (:Doc {id: 106})", request_id=3)  # duplicate primary key
        assert refused["type"] == "error", "a duplicate key must not be swallowed"
        assert session.rollback()["type"] == "rollback_ok"

    assert running.query(secret, "MATCH (d:Doc {id:106}) RETURN d").json()["rows"] == [], (
        "the statement before the failure rolls back with it"
    )


# -- conflicts and write capacity ------------------------------------------------------


def test_a_second_writer_is_refused_rather_than_queued(writable) -> None:
    running, secret = writable
    with open_session(running.client, secret, mode="write") as first:
        first.begin("write")
        first.execute("CREATE (:Doc {id: 200, title: 'mine'})")
        assert first.commit()["type"] == "commit_ok"

        # The engine keeps one writer lease per store, so a second concurrent writer is refused
        # with a retryable code instead of queueing invisibly behind the caller's back.
        with open_session(running.client, secret, mode="write") as second:
            assert second.hello["type"] == "error"
            assert second.hello["code"] == "write_lane_busy"
            assert second.hello["retryable"] is True

    assert running.query(secret, "MATCH (d:Doc {id:200}) RETURN d.title").json()["rows"] == [
        ["mine"]
    ]


def test_the_write_slot_is_released_when_the_holder_leaves(writable) -> None:
    running, secret = writable
    with open_session(running.client, secret, mode="write") as holder:
        holder.begin("write")
        with open_session(running.client, secret, mode="write") as blocked:
            assert blocked.hello["code"] == "write_lane_busy"
        holder.close()

    # Leaving the connection returns the slot: no restart, no stuck daemon.
    with open_session(running.client, secret, mode="write") as reopened:
        assert reopened.hello["type"] == "hello_ok"
        assert reopened.begin("write")["type"] == "begin_ok"


def test_a_second_writer_on_the_same_store_is_refused_not_queued(tmp_path) -> None:
    """Even with a second server-wide write slot, one store has one writer lane."""

    from conftest import Harness, seed_store

    harness = Harness(tmp_path.resolve(), max_write_sessions=2)
    with harness:
        harness.login()
        harness.attach(
            seed_store(harness.graphs / "docs"), "docs", read_only=False, allow_writes=True
        )
        secret, _ = harness.create_key(["docs"], allow_writes=True)

        with open_session(harness.client, secret, mode="write") as first:
            assert first.hello["type"] == "hello_ok"
            first.begin("write")
            with open_session(harness.client, secret, mode="write") as second:
                assert second.hello["type"] == "error"
                assert second.hello["code"] == "write_lane_busy"
                assert second.hello["retryable"] is True
                assert "docs" in second.hello["message"], "the refusal names the contended store"


def test_a_write_that_an_other_writer_already_changed_is_a_conflict(writable) -> None:
    """Optimistic concurrency: the store refuses a write the session could not have seen."""

    from okto_grafx import connect

    running, secret = writable
    path = running.client.get("/admin/api/databases").json()["databases"][0]["path"]

    with open_session(running.client, secret, mode="write") as session:
        session.begin("write")
        # Read the row inside this transaction, so the session holds a snapshot that says "title
        # is still 'durable wal'".
        seen = session.execute("MATCH (d:Doc {id: 1}) RETURN d.title")
        assert seen["collected_rows"] == [["durable wal"]]

        outsider = connect(path)
        with outsider.begin("write") as txn:
            txn.execute("MATCH (d:Doc {id: 1}) SET d.title = 'changed outside the server'")
        outsider.close()

        session.execute("MATCH (d:Doc {id: 1}) SET d.title = 'changed by the session'")
        clash = session.commit()

    assert clash["type"] == "error", "the stale writer must not silently win"
    assert clash["code"] == "write_conflict", clash
    assert clash["retryable"] is True
    assert running.query(
        secret, "MATCH (d:Doc {id:1}) RETURN d.title"
    ).json()["rows"] == [["changed outside the server"]]


def test_write_sessions_are_capped_across_the_server(tmp_path) -> None:
    from conftest import Harness, seed_store

    harness = Harness(tmp_path.resolve(), max_write_sessions=1)
    with harness:
        harness.login()
        harness.attach(seed_store(harness.graphs / "alpha"), "alpha", read_only=False, allow_writes=True)
        harness.attach(seed_store(harness.graphs / "beta"), "beta", read_only=False, allow_writes=True)
        secret, _ = harness.create_key(["alpha", "beta"], allow_writes=True)

        with open_session(harness.client, secret, db="alpha", mode="write") as first:
            assert first.hello["type"] == "hello_ok"
            with open_session(harness.client, secret, db="beta", mode="write") as second:
                assert second.hello["code"] == "write_lane_busy"
                assert "1 concurrent" in second.hello["message"]


# -- cancellation ----------------------------------------------------------------------


def test_an_idle_cancel_says_there_is_nothing_to_cancel(daemon) -> None:
    with open_session(daemon.client, daemon.secret) as session:
        session.begin()
        session.send({"type": "cancel", "id": 40})
        idle = session.receive()
        assert idle["type"] == "cancel_ack"
        assert idle["cancelled"] is False
        assert idle["id"] == 40
        assert "No read statement" in idle["note"]


def test_control_frames_are_answered_while_a_statement_runs(daemon, monkeypatch) -> None:
    """The session keeps serving its own connection while a statement is in flight.

    This store answers in microseconds, so the statement is held at the worker seam with a gate.
    What is under test is the server's control pump: a ping and a cancel that arrive *during* a
    statement must be answered, and the cancellation must reach the engine's token.
    """

    import threading

    runner = daemon.context.runner  # type: ignore[attr-defined]
    original = runner.submit_thread
    gate = threading.Event()
    armed = {"once": True}

    def gated_submit(fn):  # noqa: ANN001
        if not armed["once"]:
            return original(fn)
        armed["once"] = False

        def wrapped():  # noqa: ANN001
            gate.wait(5)
            return fn()

        return original(wrapped)

    with open_session(daemon.client, daemon.secret) as session:
        session.begin()
        monkeypatch.setattr(runner, "submit_thread", gated_submit)
        try:
            session.send(
                {"type": "execute", "id": 41, "query": "MATCH (d:Doc) RETURN d.id ORDER BY d.id"}
            )
            time.sleep(0.1)

            session.send({"type": "ping", "id": 42})
            assert session.receive()["type"] == "pong", "ping must be answered mid-statement"

            session.send({"type": "cancel", "id": 43})
            ack = session.receive()
            assert ack["type"] == "cancel_ack" and ack["cancelled"] is True and ack["id"] == 43

            # A second statement is not lost: it waits its turn and is replayed afterwards.
            session.send({"type": "ping", "id": 44})
            gate.set()
            answered = False
            settled = None
            for _ in range(16):
                message = session.receive()
                if message["type"] == "pong" and message["id"] == 44:
                    answered = True
                if message["type"] in TERMINAL:
                    settled = message
                    if answered:
                        break
            assert settled is not None, "the held statement must end with an answer"
            if settled["type"] == "error":
                assert settled["code"] == "query_cancelled", settled
                assert settled["retryable"] is True, "a cancelled statement may be retried"
            assert answered, "a frame queued during the statement must be replayed, not dropped"
        finally:
            gate.set()
            monkeypatch.undo()


def test_a_cancelled_write_statement_is_refused_with_a_reason(writable) -> None:
    running, secret = writable
    with open_session(running.client, secret, mode="write") as session:
        session.begin("write")
        session.send({"type": "cancel", "id": 50})
        message = session.receive()
        assert message["type"] == "error"
        assert message["code"] == "cancellation_unavailable"
        assert "roll the transaction back" in message["message"].lower()


# -- capacity and idle -----------------------------------------------------------------


def test_too_many_sessions_are_refused_by_number(tmp_path) -> None:
    from conftest import Harness

    harness = Harness(tmp_path.resolve(), max_ws_sessions=2, readers_per_database=8)
    with harness:
        harness.login()
        harness.attach(harness.make_store("docs"), "docs")
        secret, _ = harness.create_key(["docs"])
        with open_session(harness.client, secret) as first, open_session(harness.client, secret) as second:
            assert first.hello["type"] == "hello_ok" and second.hello["type"] == "hello_ok"
            with raw_socket(harness.client, secret) as socket:
                socket.send_json({"type": "hello", "db": "docs"})
                message = socket.receive_json()
                assert message["type"] == "error"
                assert message["code"] == "too_many_sessions"
                assert message["retryable"] is True


def test_an_idle_session_is_closed_with_a_message(tmp_path) -> None:
    from conftest import Harness

    harness = Harness(tmp_path.resolve(), idle_session_ttl_seconds=1.0)
    with harness:
        harness.login()
        harness.attach(harness.make_store("docs"), "docs")
        secret, _ = harness.create_key(["docs"])
        with raw_socket(harness.client, secret) as socket:
            socket.send_json({"type": "hello", "db": "docs"})
            assert socket.receive_json()["type"] == "hello_ok"
            deadline = time.monotonic() + 8
            notice = None
            while time.monotonic() < deadline and notice is None:
                try:
                    message = socket.receive_json()
                except Exception:  # noqa: BLE001 - the close frame ends the loop
                    break
                if message["type"] == "idle_timeout":
                    notice = message
            assert notice is not None, "an idle session must not linger silently"
            assert "rolled back" in notice["message"]


def test_a_session_can_be_closed_cleanly(daemon) -> None:
    with raw_socket(daemon.client, daemon.secret) as socket:
        socket.send_json({"type": "hello", "db": "docs"})
        hello = socket.receive_json()
        socket.send_json({"type": "close"})
        closed = socket.receive_json()
        assert closed["type"] == "closed" and closed["session_id"] == hello["session_id"]


def test_sessions_are_counted_and_released(daemon) -> None:
    before = daemon.client.get("/admin/api/status").json()["ws_sessions"]
    with open_session(daemon.client, daemon.secret):
        assert daemon.client.get("/admin/api/status").json()["ws_sessions"] == before + 1
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if daemon.client.get("/admin/api/status").json()["ws_sessions"] == before:
            break
        time.sleep(0.05)
    assert daemon.client.get("/admin/api/status").json()["ws_sessions"] == before


def test_a_read_only_store_refuses_a_write_session_even_for_a_writing_key(running) -> None:
    running.attach(running.make_store("docs"), "docs", read_only=True, allow_writes=False)
    secret, _ = running.create_key(["docs"], allow_writes=True)
    with open_session(running.client, secret, mode="write") as session:
        # Both toggles have to be on: the key may allow writes, the store does not.
        assert session.hello["type"] == "error"
        assert session.hello["code"] == "write_not_permitted"
        assert session.hello["details"]["reason"] == "database_read_only"


def test_a_read_only_key_refuses_a_write_session_on_a_writable_store(running) -> None:
    running.attach(running.make_store("docs"), "docs", read_only=False, allow_writes=True)
    secret, _ = running.create_key(["docs"], allow_writes=False)
    with open_session(running.client, secret, mode="write") as session:
        assert session.hello["type"] == "error"
        assert session.hello["code"] == "write_not_permitted"
        assert session.hello["details"]["reason"] == "key_read_only"


# -- audit -----------------------------------------------------------------------------


def test_a_write_transaction_is_audited_without_its_values(writable) -> None:  # noqa: ANN001
    """The write door is the WebSocket, so an audit trail without it would miss every change."""

    harness, secret = writable
    with open_session(harness.client, secret, mode="write") as session:
        assert session.hello["type"] == "hello_ok"
        session.send({"type": "begin", "mode": "write", "id": 1})
        assert session.receive()["type"] == "begin_ok"
        session.send(
            {
                "type": "execute",
                "query": "CREATE (:Doc {id: $id, title: $title})",
                "parameters": {"id": 777, "title": "a value that must not be audited"},
                "id": 2,
            }
        )
        while True:
            frame = session.receive()
            if frame["type"] in {"batch_end", "error"}:
                break
        committed = None
        while committed is None:
            session.send({"type": "commit", "id": 3})
            answer = session.receive()
            if answer["type"] == "commit_ok":
                committed = answer
        assert committed["durable"] is True and committed["wrote"] is True

    entries = harness.client.get("/admin/api/audit", params={"limit": 50}).json()["entries"]
    by_action = {entry["action"]: entry for entry in entries}
    assert {"ws.begin", "ws.execute", "ws.commit"} <= set(by_action), sorted(by_action)

    began = by_action["ws.begin"]
    assert began["outcome"] == "ok" and began["request"]["mode"] == "write"
    assert began["actor"].startswith("key:"), "the trail names the key, not the admin"

    executed = by_action["ws.execute"]
    assert executed["request"]["query"].startswith("CREATE (:Doc")
    # Metadata only: names and shapes, never the value the operator sent.
    assert executed["request"]["parameters"] == {"id": "int", "title": "str<32>"}
    assert "must not be audited" not in json.dumps(executed)
    assert executed["request"]["mode"] == "write"

    commit = by_action["ws.commit"]
    assert commit["outcome"] == "ok" and commit["request"]["durable"] is True
    assert commit["request"]["wrote"] is True and commit["request"]["csn"] == committed["csn"]


def test_a_rolled_back_transaction_is_audited_as_such(writable) -> None:  # noqa: ANN001
    harness, secret = writable
    with open_session(harness.client, secret, mode="write") as session:
        session.send({"type": "begin", "mode": "write", "id": 1})
        assert session.receive()["type"] == "begin_ok"
        session.send({"type": "execute", "query": "CREATE (:Doc {id: 778, title: 'x'})", "id": 2})
        while session.receive()["type"] not in {"batch_end", "error"}:
            pass
        session.send({"type": "rollback", "id": 3})
        assert session.receive()["type"] == "rollback_ok"

    actions = [
        entry["action"]
        for entry in harness.client.get("/admin/api/audit", params={"limit": 20}).json()["entries"]
    ]
    assert "ws.rollback" in actions and "ws.commit" not in actions


def test_a_refused_statement_is_audited_with_its_code(writable) -> None:  # noqa: ANN001
    harness, secret = writable
    with open_session(harness.client, secret, mode="write") as session:
        session.send({"type": "begin", "mode": "write", "id": 1})
        assert session.receive()["type"] == "begin_ok"
        session.send({"type": "execute", "query": "MATCH (n) RETURNS n", "id": 2})
        refused = None
        while refused is None:
            frame = session.receive()
            if frame["type"] == "error":
                refused = frame
        assert refused["code"]

    entry = next(
        candidate
        for candidate in harness.client.get("/admin/api/audit", params={"action": "ws.execute"}).json()["entries"]
        if candidate["outcome"] != "ok"
    )
    assert entry["outcome"] == refused["code"]
