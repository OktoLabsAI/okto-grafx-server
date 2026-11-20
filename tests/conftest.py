"""Shared fixtures: a real daemon, a real store, real HTTP.

The server is exercised through its own surfaces (HTTP/WS/MCP) against real Grafx stores in a
temporary directory — the concurrency, durability and refusal behaviour under test is the library's,
and a fake database would test nothing about this server's promises.

``tmp_path`` is resolved because the path policy refuses symlink components, and macOS keeps its
temporary tree behind ``/var -> /private/var``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import pytest
from fastapi.testclient import TestClient
from okto_grafx import connect

from okto_grafx_server.app import build_context, create_app
from okto_grafx_server.config import ServerConfig, build_config
from okto_grafx_server.control.keys import generate_admin_token

DOCS = [
    (1, "durable wal", "write ahead log recovery and checkpoints", [1.0, 0.0]),
    (2, "vector search", "hnsw beams and exact scans over vectors", [0.0, 1.0]),
    (3, "graph views", "named logical read views over a snapshot", [0.6, 0.4]),
    (4, "concurrency", "snapshot isolation with optimistic conflicts", [0.9, 0.1]),
]


def seed_store(path: Path, *, text_index: bool = True, vector_space: bool = True) -> Path:
    """Create a store with nodes, relationships, a text index and a vector space."""

    path.mkdir(parents=True, exist_ok=True)
    db = connect(str(path))
    vector_column = ", v VECTOR(semantic)" if vector_space else ""
    with db.begin("write") as tx:
        if vector_space:
            tx.execute("CREATE VECTOR SPACE semantic {dimension: 2, metric: 'cosine'}")
        tx.execute(
            f"CREATE NODE TABLE Doc(id INT64, title STRING, body STRING{vector_column}, "
            "PRIMARY KEY(id))"
        )
        tx.execute("CREATE NODE TABLE Tag(id INT64, name STRING, PRIMARY KEY(id))")
        tx.execute("CREATE REL TABLE TAGGED(FROM Doc TO Tag)")
        tx.execute("CREATE REL TABLE CITES(FROM Doc TO Doc, weight DOUBLE)")
        for doc_id, title, body, vector in DOCS:
            if vector_space:
                tx.execute(
                    "CREATE (:Doc {id: $id, title: $title, body: $body, v: $v})",
                    {"id": doc_id, "title": title, "body": body, "v": vector},
                )
            else:
                tx.execute(
                    "CREATE (:Doc {id: $id, title: $title, body: $body})",
                    {"id": doc_id, "title": title, "body": body},
                )
        tx.execute("CREATE (:Tag {id: 10, name: 'storage'})")
        tx.execute("CREATE (:Tag {id: 11, name: 'search'})")
        tx.execute("MATCH (d:Doc {id:1}), (t:Tag {id:10}) CREATE (d)-[:TAGGED]->(t)")
        tx.execute("MATCH (d:Doc {id:2}), (t:Tag {id:11}) CREATE (d)-[:TAGGED]->(t)")
        tx.execute("MATCH (d:Doc {id:2}), (t:Tag {id:10}) CREATE (d)-[:TAGGED]->(t)")
        tx.execute(
            "MATCH (a:Doc {id:1}), (b:Doc {id:2}) CREATE (a)-[:CITES {weight: 1.0}]->(b)"
        )
        tx.execute(
            "MATCH (a:Doc {id:2}), (b:Doc {id:1}) CREATE (a)-[:CITES {weight: 0.5}]->(b)"
        )
        # A self-loop and a parallel edge: the graph payload must survive both.
        tx.execute("MATCH (a:Doc {id:3}) CREATE (a)-[:CITES {weight: 2.0}]->(a)")
        tx.execute(
            "MATCH (a:Doc {id:1}), (b:Doc {id:4}) CREATE (a)-[:CITES {weight: 0.25}]->(b)"
        )
        tx.execute(
            "MATCH (a:Doc {id:1}), (b:Doc {id:4}) CREATE (a)-[:CITES {weight: 0.75}]->(b)"
        )
    db.ensure_identity_indexes()
    if text_index:
        db.create_text_index("doc_text", "Doc", ("body", "title"))
    db.close()
    return path


class Harness:
    """A daemon under test, plus the ordinary operator motions."""

    def __init__(
        self,
        root: Path,
        *,
        admin_token: str | None = None,
        require_token: bool = True,
        **overrides: Any,
    ) -> None:
        self.root = root
        self.state_dir = root / "state"
        self.graphs = root / "graphs"
        self.graphs.mkdir(parents=True, exist_ok=True)
        settings: dict[str, Any] = {
            "state_dir": str(self.state_dir),
            "allowed_roots": [str(self.graphs)],
            "bind_port": 8737,
            "readers_per_database": 2,
            "handle_idle_ttl_seconds": 5.0,
            # A leaked test socket must not hold the daemon's shutdown hostage.
            "idle_session_ttl_seconds": 20.0,
        }
        settings.update(overrides)
        self.config: ServerConfig = build_config(settings)
        self.context = build_context(self.config)
        # ``init`` bootstraps the admin token once and never silently rotates it, so a restarted
        # harness keeps the credential the first one created. Pass ``admin_token=`` to log in as
        # the operator of a state directory that already exists.
        if self.context.store.admin_token_configured():
            self.admin_token = admin_token  # None until the caller supplies the known token
        elif require_token:
            self.admin_token = admin_token or generate_admin_token()
            self.context.store.set_admin_token(self.admin_token)
        else:
            # A state directory exactly as ``init`` leaves it by default: no credential at all, so
            # the administrator surface is this-machine-only.
            self.admin_token = None
        self.app = create_app(context=self.context)
        self.client = TestClient(self.app)
        self._entered = False

    def as_loopback(self, *, peer: str = "127.0.0.1", port: int = 8737) -> TestClient:
        """A client whose ASGI peer is a real loopback address.

        ``TestClient`` reports its peer as ``testclient`` — an address no socket can present, so it
        proves a token works but cannot prove that only loopback may walk in without one. Enter the
        *harness* for the lifespan; leave this one un-entered so the context is not started twice.
        """

        return TestClient(self.app, client=(peer, 50000), base_url=f"http://127.0.0.1:{port}")

    # -- lifecycle ---------------------------------------------------------------------

    def __enter__(self) -> "Harness":
        self.client.__enter__()
        self._entered = True
        return self

    def __exit__(self, *exc: Any) -> None:
        self.client.__exit__(*exc)
        self._entered = False

    # -- operator motions --------------------------------------------------------------

    def login(self, token: str | None = None) -> None:
        token = token if token is not None else self.admin_token
        assert token, "this state directory already has an admin token; pass it to login()"
        response = self.client.post("/admin/api/login", json={"token": token})
        assert response.status_code == 200, response.text

    def make_store(self, name: str = "docs", **kwargs: Any) -> Path:
        path = self.graphs / name
        return seed_store(path, **kwargs)

    def attach(self, path: Path, slug: str = "docs", **kwargs: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": str(path),
            "slug": slug,
            "name": kwargs.pop("name", slug),
            "read_only": kwargs.pop("read_only", False),
            "allow_writes": kwargs.pop("allow_writes", True),
        }
        payload.update(kwargs)
        response = self.client.post("/admin/api/databases", json=payload)
        assert response.status_code == 201, response.text
        return response.json()

    def create_key(self, databases: Iterable[str], *, allow_writes: bool = False, name: str = "test"):
        response = self.client.post(
            "/admin/api/keys",
            json={"name": name, "databases": list(databases), "allow_writes": allow_writes},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        return body["key"], body

    @staticmethod
    def bearer(secret: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {secret}"}

    # -- convenience -------------------------------------------------------------------

    def query(self, secret: str, cypher: str, **kwargs: Any):
        return self.client.post(
            f"/api/v1/databases/{kwargs.pop('db', 'docs')}/query",
            json={"query": cypher, **kwargs},
            headers=self.bearer(secret),
        )

    def wait_for_job(self, job_id: str, timeout: float = 30.0) -> dict[str, Any]:
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.client.get(f"/admin/api/jobs/{job_id}").json()
            if job["state"] in {"succeeded", "failed", "cancelled"}:
                return job
            time.sleep(0.05)
        raise AssertionError(f"job {job_id} did not settle: {job}")


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    """An unstarted daemon (the TestClient context starts it)."""

    return Harness(tmp_path.resolve())


@pytest.fixture
def running(harness: Harness) -> Harness:
    """A daemon with lifespan active and an admin session in its cookie jar."""

    with harness:
        harness.login()
        yield harness


@pytest.fixture
def daemon(running: Harness) -> Harness:
    """A daemon with one attached store and one read-only key, ready to serve."""

    path = running.make_store("docs")
    running.attach(path, "docs")
    secret, _record = running.create_key(["docs"])
    running.secret = secret  # type: ignore[attr-defined]
    return running


@pytest.fixture
def writable_daemon(running: Harness) -> Harness:
    path = running.make_store("docs")
    running.attach(path, "docs", read_only=False, allow_writes=True)
    secret, _record = running.create_key(["docs"], allow_writes=True)
    running.secret = secret  # type: ignore[attr-defined]
    return running
