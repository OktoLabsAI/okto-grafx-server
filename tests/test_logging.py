"""The daemon's own log: structured, rotating, inside the state directory, with a request id.

The plan asks for `log estruturado em stdout + arquivo rotativo no state dir` and for the server
log to carry the `request_id`. A log that cannot be tied to the request id shown to a client is a
log that cannot answer an incident report, so that link is asserted here rather than hoped for.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from conftest import Harness

from okto_grafx_server.cli import LOG_BACKUP_COUNT, LOG_MAX_BYTES, _configure_logging
from okto_grafx_server.errors import get_request_id, set_request_id


def _oktografx_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if getattr(h, "_oktografx", False)]


@pytest.fixture(autouse=True)
def _restore_root_logging():
    """Logging is process-global; a test must not leave its file handler on the root logger."""

    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in saved:
            root.removeHandler(handler)
            handler.close()
    root.handlers[:] = saved
    root.setLevel(saved_level)


def _records(path: Path) -> list[dict[str, Any]]:
    assert path.exists(), f"{path} was never created"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_the_log_is_a_rotating_json_file_inside_the_state_dir(tmp_path: Path) -> None:
    log_file = tmp_path / "state" / "server.log"
    _configure_logging("INFO", log_file)

    assert log_file.parent.is_dir(), "writing the log must create the state directory"
    previous = get_request_id()
    set_request_id("req-structured")
    try:
        logging.getLogger("okto_grafx_server.probe").info("opened store %s", "docs")
    finally:
        set_request_id(previous)

    [record] = _records(log_file)
    assert record["level"] == "INFO"
    assert record["logger"] == "okto_grafx_server.probe"
    assert record["message"] == "opened store docs"
    # The request id is taken from the active request when the caller did not pass one.
    assert record["request_id"] == "req-structured"
    assert record["ts"].endswith("+00:00")

    # The defaults are a rotation policy, not an unbounded append.
    assert LOG_MAX_BYTES == 5 * 1024 * 1024
    assert LOG_BACKUP_COUNT == 5


def test_context_fields_reach_the_line_and_rotation_keeps_the_file_bounded(tmp_path: Path) -> None:
    log_file = tmp_path / "server.log"
    _configure_logging("INFO", log_file, max_bytes=1024, backup_count=2)

    logging.getLogger("okto_grafx_server.probe").info(
        "refused",
        extra={"method": "POST", "path": "/api/v1/databases/docs/query", "status": 403,
               "duration_ms": 12.5, "principal": "key:okg_abcd", "code": "write_not_permitted"},
    )
    [record] = _records(log_file)
    assert record["code"] == "write_not_permitted"
    assert record["principal"] == "key:okg_abcd"
    assert record["status"] == 403
    assert "query" in record["path"]

    for index in range(200):
        logging.getLogger("okto_grafx_server.probe").info("line %03d %s", index, "x" * 40)

    rotated = sorted(p.name for p in tmp_path.glob("server.log*"))
    assert "server.log.1" in rotated, f"rotation never happened: {rotated}"
    assert len(rotated) <= 3, f"more backups than the policy allows: {rotated}"
    # Every line survives as parseable JSON, including the ones that were rolled over.
    for name in rotated:
        _records(tmp_path / name)


def test_configuring_logging_twice_does_not_double_every_line(tmp_path: Path) -> None:
    first = tmp_path / "a.log"
    second = tmp_path / "b.log"
    _configure_logging("INFO", first)
    _configure_logging("INFO", second)

    assert len(_oktografx_handlers()) == 2, "expected exactly one console handler and one file"
    logging.getLogger("okto_grafx_server.probe").info("once")
    assert len(_records(first)) == 0
    assert len(_records(second)) == 1


def test_a_refused_request_is_logged_with_the_request_id_the_client_saw(daemon: Harness) -> None:
    log_file = daemon.config.log_path
    _configure_logging("INFO", log_file)

    unknown_key = daemon.bearer("okg_" + "x" * 40)
    unauthenticated = daemon.client.get("/api/v1/databases", headers=unknown_key)
    assert unauthenticated.status_code == 401

    key = getattr(daemon, "secret", None)
    assert key, "the daemon fixture creates a read-only key"
    refused = daemon.client.post(
        "/api/v1/databases/docs/query",
        json={"query": "CREATE (:Doc {id: 99, title: 'nope'})"},
        headers=daemon.bearer(key),
    )
    assert refused.status_code == 403

    records = _records(log_file)
    by_request = {record.get("request_id"): record for record in records}

    denied_id = unauthenticated.headers["x-request-id"]
    assert denied_id in by_request, "a refusal must be findable by the request id it returned"
    anonymous = by_request[denied_id]
    assert anonymous["status"] == 401
    assert anonymous["code"] == "unknown_api_key"
    assert anonymous["path"] == "/api/v1/databases"
    assert anonymous.get("principal") is None, "nobody authenticated, nobody is named"

    write_id = refused.headers["x-request-id"]
    assert write_id in by_request
    refusal = by_request[write_id]
    assert refusal["status"] == 403
    assert refusal["code"] == "write_not_permitted"
    # The actor is named with the same vocabulary the audit trail uses, and never as a secret.
    assert str(refusal["principal"]).startswith("key:")
    assert daemon.secret not in json.dumps(refusal)

    # A credential never reaches the log, not even when another library logs the URL that carries it: the WebSocket handshake puts its bearer in the query string.
    handshake_token = "okg_" + "s" * 40
    needle = "tok" + "en="  # built, so no secret-looking literal appears in this file
    daemon.client.get("/api/v1/ws?" + needle + handshake_token)
    foreign = [record for record in _records(log_file) if needle in json.dumps(record)]
    assert foreign, "a third-party logger recorded the handshake URL"
    for record in _records(log_file):
        line = json.dumps(record)
        assert handshake_token not in line, "the credential itself was written to disk"
        if needle in line:
            assert "[redacted]" in line, "the credential was logged in the clear"


def test_health_polling_stays_quiet_below_the_default_level(tmp_path: Path) -> None:
    log_file = tmp_path / "server.log"
    _configure_logging("INFO", log_file)
    logger = logging.getLogger("okto_grafx_server")
    logger.debug("GET /api/v1/health -> %s", 200)
    logger.warning("GET /api/v1/databases -> %s", 401, extra={"status": 401})
    written = _records(log_file)
    assert len(written) == 1, "the console's heartbeat must not fill the rotating file"
    assert written[0]["status"] == 401
