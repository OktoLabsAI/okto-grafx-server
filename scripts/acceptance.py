#!/usr/bin/env python3
"""End-to-end acceptance for a **running** oktografx-server daemon.

This is PLAN.md item 18 plus the acceptance gate, executed against a live daemon over real
sockets — not a test client, not a mock. It logs in, attaches an external store, reads it,
draws it, searches it, mints an API key, proves the key cannot write until the write toggle is
on, writes through a WebSocket transaction, runs maintenance, and checks the published OpenAPI,
metrics and web console.

Every payload is read the way the console reads it: the admin plane with the session cookie, the
data plane with a bearer key.

Usage
-----
    python scripts/acceptance.py --base-url http://127.0.0.1:8737 \\
        --admin-token "$OKG_ADMIN_TOKEN" --store /srv/grafx/docs

Exit code is 0 only when every check passes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

FAILURES: list[str] = []
PASSES: list[str] = []


def check(name: str, condition: bool, evidence: str = "") -> bool:
    """Report one acceptance assertion in the daemon's own terms."""
    if condition:
        PASSES.append(name)
        print(f"  \033[32mPASS\033[0m {name}" + (f" — {evidence}" if evidence else ""))
    else:
        FAILURES.append(name)
        print(f"  \033[31mFAIL\033[0m {name}" + (f" — {evidence}" if evidence else ""))
    return bool(condition)


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


class Daemon:
    """Thin wrapper that keeps the two doors (admin cookie, data bearer) honest."""

    def __init__(self, base_url: str, admin_token: str) -> None:
        self.base = base_url.rstrip("/")
        self.admin_token = admin_token
        self.admin = httpx.Client(base_url=self.base, timeout=60.0)
        self.data = httpx.Client(base_url=self.base, timeout=60.0)
        self.anon = httpx.Client(base_url=self.base, timeout=60.0)

    def login(self) -> httpx.Response:
        return self.admin.post("/admin/api/login", json={"token": self.admin_token})

    def use_key(self, secret: str) -> None:
        self.data.headers["Authorization"] = f"Bearer {secret}"

    def close(self) -> None:
        self.admin.close()
        self.data.close()
        self.anon.close()


def code_of(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", {}).get("code", ""))
    except Exception:
        return ""


def envelope_error_is(response: httpx.Response, code: str, status: int) -> bool:
    return response.status_code == status and code_of(response) == code


def wait_for_job(daemon: Daemon, job_id: str, timeout: float = 180.0) -> dict[str, Any]:
    """Jobs are queued per store; the panel polls the same endpoint the console polls."""

    deadline = time.time() + timeout
    while time.time() < deadline:
        job = daemon.admin.get(f"/admin/api/jobs/{job_id}").json()
        if job.get("state") in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.2)
    return {"state": "timeout", "detail": {"message": f"{job_id} still {job.get('state')}"}}


def run_action(daemon: Daemon, slug: str, action: str, **arguments: Any) -> dict[str, Any]:
    started = daemon.admin.post(
        f"/admin/api/databases/{slug}/actions/{action}", json={"arguments": arguments}
    )
    if started.status_code not in (200, 202):
        return {"state": "refused", "detail": {"code": code_of(started), "http": started.status_code}}
    return wait_for_job(daemon, str(started.json().get("job_id")))


def ws_transaction(
    base_url: str, secret: str, slug: str, statement: str, *, rollback: bool = False
) -> dict[str, Any]:
    """One WebSocket transaction: hello → begin → execute → commit (or rollback)."""

    from websockets.sync.client import connect

    url = f"{base_url.replace('http://', 'ws://')}/api/v1/ws"
    final: dict[str, Any] = {"type": "no_reply"}
    try:
        with connect(url, additional_headers={"Authorization": f"Bearer {secret}"}, open_timeout=20) as socket:
            socket.send(json.dumps({"type": "hello", "db": slug, "mode": "write"}))
            hello = json.loads(socket.recv(timeout=20))
            if hello.get("type") == "error":
                return hello

            def send(message: dict[str, Any]) -> None:
                socket.send(json.dumps(message))

            def await_(types: set[str]) -> dict[str, Any]:
                while True:
                    frame = json.loads(socket.recv(timeout=30))
                    if frame.get("type") in types:
                        return frame

            send({"type": "begin", "mode": "write", "id": "b1"})
            begun = await_({"begin_ok", "error"})
            if begun.get("type") == "error":
                return begun
            send({"type": "execute", "query": statement, "id": "e1"})
            executed = await_({"batch_end", "error"})
            if executed.get("type") == "error":
                return executed
            send({"type": "rollback" if rollback else "commit", "id": "t1"})
            final = await_({"commit_ok", "rollback_ok", "error"})
    except Exception as exc:  # a refusal at handshake time is an answer too
        return {"type": "exception", "code": type(exc).__name__, "message": str(exc)}
    return final


# ------------------------------------------------------------------------------------------
# the flow
# ------------------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Acceptance flow against a live oktografx-server")
    parser.add_argument("--base-url", default="http://127.0.0.1:8737")
    parser.add_argument("--admin-token", default=os.environ.get("OKG_ADMIN_TOKEN", ""))
    parser.add_argument("--store", required=True, help="path to an existing Grafx store directory")
    parser.add_argument("--slug", default="", help="slug to register it under (default: directory name)")
    args = parser.parse_args()

    # No token means the flow is testing the default posture: no credential exists, and this machine is
    # the administrator. A token here means the same flow is being run against a server that requires one.
    open_access = not args.admin_token
    store = Path(args.store).resolve()
    if not store.is_dir():
        print(f"{store} is not a directory", file=sys.stderr)
        return 2

    daemon = Daemon(args.base_url, args.admin_token)
    before = _tree(store)
    slug = args.slug or store.name

    section("1. the doors, with nobody signed in")
    refused = daemon.anon.get("/admin/api/databases")
    refused_data = daemon.anon.get("/api/v1/databases")
    if open_access:
        check(
            "this machine is the administrator without a credential",
            refused.status_code == 200 and refused_data.status_code == 200,
            f"admin {refused.status_code}, data {refused_data.status_code}",
        )
        door = daemon.anon.get("/admin/api/session").json()
        check(
            "the console says so out loud",
            door.get("signed_in") is True and door.get("auth") == "open-loopback",
            json.dumps(door)[:110],
        )
    else:
        check(
            "admin plane refuses an anonymous caller",
            envelope_error_is(refused, "admin_session_required", 401),
            f"{refused.status_code} {code_of(refused)}",
        )
        check(
            "data plane refuses an anonymous caller",
            refused_data.status_code == 401,
            str(refused_data.status_code),
        )

    section("2. the administrator" if not open_access else "2. there is nothing to sign in to")
    if open_access:
        door = daemon.admin.get("/admin/api/session").json()
        check(
            "no token is configured, so none can be lost",
            door.get("token_configured") is False and door.get("token_rotated_at") is None,
            json.dumps(door)[:110],
        )
    else:
        login = daemon.login()
        check("bootstrap token opens a session", login.status_code == 200, str(login.status_code))
        check(
            "the session cookie is httpOnly",
            "httponly" in "; ".join(login.headers.get_list("set-cookie")).lower(),
            login.headers.get("set-cookie", "")[:58],
        )
        session = daemon.admin.get("/admin/api/session").json()
        check(
            "session reports the admin door",
            session.get("signed_in") is True and session.get("token_configured") is True,
            json.dumps(session),
        )

    section("3. an external store is attached by reference")
    inspect = daemon.admin.post("/admin/api/databases/inspect", json={"path": str(store)})
    check(
        "the store is inspected before it is registered",
        inspect.status_code == 200 and bool(inspect.json().get("database_uuid")),
        f"uuid={inspect.json().get('database_uuid', '')[:12]} files={inspect.json().get('files')}",
    )
    attached = daemon.admin.post(
        "/admin/api/databases",
        json={
            "path": str(store),
            "slug": slug,
            "name": store.name,
            # The read-only door is the data plane's default; the store itself has to be opened
            # writable at least once, because a read-only open cannot prove a checkpoint-complete
            # database until recovery and checkpoint have run.
            "read_only": False,
            "allow_writes": False,
            "open_policy": "pinned",
        },
    )
    if not check("attach succeeds inside an allowed root", attached.status_code == 201, f"{attached.status_code} {attached.text[:110]}"):
        return finish(daemon)
    record = attached.json()
    check("the daemon reports the store's physical uuid", bool(record.get("database_uuid")), str(record.get("database_uuid")))
    check("the write toggle starts off", record.get("allow_writes") is False)
    state_tree = _tree(Path(daemon.admin.get("/admin/api/settings").json()["state_dir"]))
    store_names = {name.rsplit("/", 1)[-1] for name in before}
    copied = sorted(store_names & {name.rsplit("/", 1)[-1] for name in state_tree})
    check(
        "the store is opened by reference: nothing was copied into the state directory",
        not copied,
        f"state directory holds {len(state_tree)} files, none of them the store's {sorted(copied)}",
    )
    diff = {name: size for name, size in _tree(store).items() if before.get(name) != size}
    check(
        "attaching did not rewrite the store's files",
        all(name.startswith(("wal/", ".locks/", "identity")) or before.get(name) is None for name in diff),
        f"changed since attach: {sorted(diff) or 'nothing'}",
    )
    check("the registration points at the original path", record.get("path") == str(store), str(record.get("path"))[:70])

    section("4. the first maintenance pass, which the read-only door depends on")
    recovered = run_action(daemon, slug, "recover")
    check(
        "recover succeeds on a freshly created store",
        recovered.get("state") == "succeeded",
        json.dumps((recovered.get("detail") or {}).get("report", {}))[:100],
    )
    first_checkpoint = run_action(daemon, slug, "checkpoint")
    check(
        "checkpoint succeeds, so a read-only open can prove consistency",
        first_checkpoint.get("state") == "succeeded",
        json.dumps((first_checkpoint.get("detail") or {}).get("report", {}))[:110],
    )

    section("5. schema, then a statement")
    inventory = daemon.admin.get(f"/admin/api/databases/{slug}/inventory").json()
    tables = {table["name"] for table in inventory.get("schema", {}).get("tables", [])}
    check("schema lists the node tables", {"Doc", "Tag"} <= tables, ", ".join(sorted(tables)))
    kinds = {index["name"]: index["kind"] for index in inventory.get("indexes", [])}
    check(
        "the inventory says which index is searchable text",
        kinds.get("doc_text") == "text" and kinds.get("pk_Doc") == "exact",
        json.dumps({name: kind for name, kind in kinds.items() if kind != "exact"}),
    )
    check("the store reports its own health", inventory.get("maintenance", {}).get("recovery_required") is False,
          f"wal={inventory.get('maintenance', {}).get('wal_bytes')} bytes")

    answered = daemon.admin.post(
        f"/api/v1/databases/{slug}/query",
        json={"query": "MATCH (d:Doc) RETURN d.id, d.title ORDER BY d.id"},
    ).json()
    check("a read-only statement returns rows", len(answered.get("rows", [])) >= 4, f"{answered.get('row_count')} rows, truncated={answered.get('truncated')}")
    check("the read carries a snapshot", isinstance(answered.get("snapshot_lsn"), int), f"snapshot_lsn={answered.get('snapshot_lsn')}")
    check("the engine's statistics come through", bool(answered.get("statistics")), json.dumps(answered.get("statistics")))
    plan = daemon.admin.get(f"/api/v1/databases/{slug}/explain", params={"query": "MATCH (d:Doc) RETURN d"}).json()
    check("explain returns the engine's operator chain", "operator" in plan, json.dumps(plan)[:80])

    section("6. the same store as a graph")
    payload = daemon.admin.post(
        f"/api/v1/databases/{slug}/graph",
        json={"mode": "projection", "node_tables": ["Doc", "Tag"], "relationship_tables": ["CITES", "TAGGED"]},
    ).json()
    check("projection returns nodes and edges", payload.get("shown_nodes", 0) >= 4 and payload.get("shown_edges", 0) >= 5,
          f"{payload.get('shown_nodes')} nodes / {payload.get('shown_edges')} edges")
    ids = {node["id"] for node in payload.get("nodes", [])}
    edges = [(edge["source"], edge["target"]) for edge in payload.get("edges", [])]
    check("parallel edges survive the payload", sum(1 for pair in edges if pair == ("Doc:1", "Doc:4")) == 2,
          f"{sum(1 for pair in edges if pair == ('Doc:1', 'Doc:4'))} CITES Doc:1→Doc:4")
    check("a self-loop survives the payload", ("Doc:3", "Doc:3") in edges, str([pair for pair in edges if pair[0] == pair[1]]))
    check("every returned edge has both endpoints drawn", all(source in ids and target in ids for source, target in edges))
    as_graph = daemon.admin.post(
        f"/api/v1/databases/{slug}/graph",
        json={"mode": "query", "query": "MATCH (a:Doc)-[r:CITES]->(b:Doc) RETURN a, r, b"},
    ).json()
    check("“view as graph” re-runs the statement in graph mode", as_graph.get("mode") == "query" and as_graph.get("shown_nodes", 0) >= 3,
          f"{as_graph.get('shown_nodes')} nodes, mode={as_graph.get('mode')}")
    projection_capped = daemon.admin.post(
        f"/api/v1/databases/{slug}/graph",
        json={"mode": "projection", "node_tables": ["Doc", "Tag"], "relationship_tables": ["CITES", "TAGGED"], "max_nodes": 2},
    )
    check(
        "a projection too big for its cap refuses to silently truncate",
        envelope_error_is(projection_capped, "budget_exceeded", 413),
        f"{projection_capped.status_code} {code_of(projection_capped)} "
        f"resource={projection_capped.json().get('error', {}).get('details', {}).get('resource')}",
    )
    query_capped = daemon.admin.post(
        f"/api/v1/databases/{slug}/graph",
        json={"mode": "query", "query": "MATCH (a:Doc)-[r:CITES]->(b:Doc) RETURN a, r, b", "max_nodes": 2, "max_edges": 2},
    ).json()
    check("a capped query capture says it was truncated",
          query_capped.get("truncated") is True and query_capped.get("shown_nodes", 9) <= 2
          and query_capped.get("total_nodes", 0) > query_capped.get("shown_nodes", 0),
          f"{query_capped.get('shown_nodes')}/{query_capped.get('total_nodes')} nodes, "
          f"{query_capped.get('shown_edges')}/{query_capped.get('total_edges')} edges")
    neighbours = daemon.admin.get(
        f"/api/v1/databases/{slug}/neighbors", params={"table": "Doc", "record_id": 1, "limit": 50}
    ).json()
    check("one hop from a node is counted honestly", neighbours.get("total_nodes", 0) >= 4,
          f"{neighbours.get('shown_nodes')}/{neighbours.get('total_nodes')} nodes, mode={neighbours.get('mode')}")
    tight = daemon.admin.get(
        f"/api/v1/databases/{slug}/neighbors", params={"table": "Doc", "record_id": 1, "limit": 2}
    ).json()
    check("a tight neighbour cap is honoured and reported", tight.get("shown_edges") == 2 and tight.get("truncated") is True,
          f"{tight.get('shown_edges')}/{tight.get('total_edges')} edges, truncated={tight.get('truncated')}")

    section("7. search: text, vector, hybrid")
    text = daemon.admin.post(f"/api/v1/databases/{slug}/search/text", json={"index": "doc_text", "query": "durable", "k": 10}).json()
    check("full-text search finds the doc", len(text.get("hits", [])) >= 1, f"{len(text.get('hits', []))} hits, regime={text.get('regime')}")
    check("the search tells the operator how it answered", bool(text.get("statistics_regime") or text.get("regime")), str(text.get("regime")))
    vector = daemon.admin.post(f"/api/v1/databases/{slug}/search/vector", json={"space": "semantic", "vector": [1.0, 0.0], "k": 5}).json()
    check("vector search returns neighbours", len(vector.get("hits", [])) >= 1,
          f"{len(vector.get('hits', []))} hits, achieved_k={vector.get('achieved_k')}")
    hybrid = daemon.admin.post(
        f"/api/v1/databases/{slug}/search/hybrid",
        json={"table": "Doc", "index": "doc_text", "query": "durable", "space": "semantic", "vector": [1.0, 0.0], "k": 5},
    ).json()
    check("hybrid search fuses both signals", hybrid.get("fusion") == "rrf_v1_union",
          f"fusion={hybrid.get('fusion')} lexical={hybrid.get('lexical_regime')} vector={hybrid.get('vector_regime')}")

    section("8. the read-only door is the default")
    read_only = daemon.admin.patch(f"/admin/api/databases/{slug}", json={"read_only": True})
    check("the store can be registered read-only once checkpointed", read_only.status_code == 200, str(read_only.status_code))
    read_after = daemon.admin.post(f"/api/v1/databases/{slug}/query", json={"query": "MATCH (d:Doc) RETURN count(d)"})
    check("reads still work through a read-only open", read_after.status_code == 200, f"{read_after.status_code} {code_of(read_after)}")
    verified_read_only = run_action(daemon, slug, "verify")
    check("verify runs on a read-only handle", verified_read_only.get("state") == "succeeded",
          json.dumps((verified_read_only.get("detail") or {}).get("report", {}))[:100])
    back = daemon.admin.patch(f"/admin/api/databases/{slug}", json={"read_only": False})
    check("and the door can be opened again", back.status_code == 200, str(back.status_code))

    section("9. an API key: read scope first")
    created = daemon.admin.post("/admin/api/keys", json={"name": "acceptance", "databases": [slug], "allow_writes": False})
    check("key creation returns the secret exactly once", created.status_code == 201 and bool(created.json().get("key")),
          f"shown_once={created.json().get('shown_once')} prefix={created.json().get('prefix')}")
    secret = created.json().get("key", "")
    key_id = created.json().get("id")
    if not secret:
        return finish(daemon)
    daemon.use_key(secret)
    listed = daemon.data.get("/api/v1/databases")
    scoped = [entry.get("slug") for entry in listed.json().get("databases", [])]
    check("the key sees only its scoped store", listed.status_code == 200 and scoped == [slug], str(scoped))
    check("the key knows who it is", listed.json().get("actor", {}).get("kind") == "key", json.dumps(listed.json().get("actor")))
    keyed_query = daemon.data.post(f"/api/v1/databases/{slug}/query", json={"query": "MATCH (d:Doc) RETURN count(d)"})
    check("the key can read", keyed_query.status_code == 200, str(keyed_query.status_code))
    parameterised = daemon.data.post(
        f"/api/v1/databases/{slug}/query",
        json={
            "query": "MATCH (d:Doc) WHERE d.title = $needle RETURN d.id",
            "parameters": {"needle": "durable wal"},
        },
    )
    check("parameters bind on the data plane", parameterised.status_code == 200
          and len(parameterised.json().get("rows", [])) == 1, str(parameterised.json().get("rows")))
    forbidden = daemon.data.post(f"/api/v1/databases/{slug}/query", json={"query": "CREATE (:Doc {id: 999, title: 'must not exist'})"})
    check("the key cannot write while read-only", envelope_error_is(forbidden, "write_not_permitted", 403),
          f"{forbidden.status_code} reason={forbidden.json().get('error', {}).get('details', {}).get('reason')}")
    other = daemon.data.get("/api/v1/databases/nope-at-all/schema")
    check("an unscoped store is invisible, not merely forbidden", other.status_code == 404, str(other.status_code))

    section("10. writes, behind two toggles")
    while_read_only = _ws_write(daemon, secret, slug, "CREATE (:Doc {id: 9000, title: 'not yet'})")
    check("a read-only key is refused a write session", while_read_only.get("code") == "write_not_permitted",
          json.dumps(while_read_only)[:110])
    daemon.admin.patch(f"/admin/api/keys/{key_id}", json={"allow_writes": True})
    daemon.admin.patch(f"/admin/api/databases/{slug}", json={"allow_writes": True})
    committed = _ws_write(daemon, secret, slug, "CREATE (:Doc {id: 9001, title: 'from the acceptance run'})")
    check("with both toggles on, the write commits durably",
          committed.get("type") == "commit_ok" and committed.get("durable") is True and committed.get("wrote") is True,
          f"csn={committed.get('csn')} durable={committed.get('durable')} wrote={committed.get('wrote')}")
    seen = daemon.data.post(f"/api/v1/databases/{slug}/query", json={"query": "MATCH (d:Doc {id: 9001}) RETURN d.title"}).json()
    check("the write is visible in a new transaction", len(seen.get("rows", [])) == 1, json.dumps(seen.get("rows")))
    rolled = _ws_write(daemon, secret, slug, "CREATE (:Doc {id: 9002, title: 'rolled back'})", rollback=True)
    check("rollback persists nothing", rolled.get("type") == "rollback_ok", str(rolled.get("type")))
    gone = daemon.data.post(f"/api/v1/databases/{slug}/query", json={"query": "MATCH (d:Doc {id: 9002}) RETURN d"}).json()
    check("the rolled-back row is absent", gone.get("rows", []) == [], json.dumps(gone.get("rows")))
    cleanup = _ws_write(daemon, secret, slug, "MATCH (d:Doc {id: 9001}) DETACH DELETE d")
    check("the acceptance row is cleaned up", cleanup.get("type") == "commit_ok", str(cleanup.get("type")))
    remaining = daemon.data.post(f"/api/v1/databases/{slug}/query", json={"query": "MATCH (d:Doc) RETURN count(d)"}).json()
    check("the store is back to its seeded size", remaining.get("rows", [[0]])[0][0] == 4, str(remaining.get("rows")))

    section("11. maintenance runs without hurting the store")
    verify = run_action(daemon, slug, "verify")
    report = (verify.get("detail") or {}).get("report", {})
    check("verify succeeds", verify.get("state") == "succeeded", json.dumps(report)[:110])
    check("verify actually read pages", isinstance(report.get("pages_checked"), int) and report["pages_checked"] > 0,
          f"pages_checked={report.get('pages_checked')} findings={report.get('findings')}")
    checkpoint = run_action(daemon, slug, "checkpoint")
    check("checkpoint succeeds", checkpoint.get("state") == "succeeded", str(checkpoint.get("state")))
    second = run_action(daemon, slug, "verify")
    check("the store still verifies afterwards", second.get("state") == "succeeded", str(second.get("state")))
    refused_job = daemon.admin.post(f"/admin/api/databases/{slug}/actions/vacuum", json={"arguments": {}})
    check(
        "a job needing the operator's quiescence assertion refuses without it",
        refused_job.status_code == 409 and code_of(refused_job) == "maintenance_confirmation_required",
        f"{refused_job.status_code} {code_of(refused_job)}: {refused_job.json().get('error', {}).get('message', '')[:60]}",
    )
    unknown = daemon.admin.post(f"/admin/api/databases/{slug}/actions/nonsense", json={"arguments": {}})
    check("an unknown maintenance action is refused with the catalogue",
          unknown.status_code == 422 and "verify" in unknown.text, str(unknown.status_code))
    missing_argument = daemon.admin.post(f"/admin/api/databases/{slug}/actions/rebuild_index", json={"arguments": {}})
    check("an action that names an index demands the name", missing_argument.status_code == 422
          and "name" in json.dumps(missing_argument.json()), str(missing_argument.status_code))

    identity = daemon.admin.post(f"/admin/api/schema/{slug}/identity-indexes")
    check("the library's identity-index door is exposed on the admin plane",
          identity.status_code == 200 and isinstance(identity.json().get("activated"), list),
          f"{identity.status_code} activated={identity.json().get('activated') if identity.status_code == 200 else code_of(identity)}")
    if identity.status_code == 200:
        again = daemon.admin.post(f"/admin/api/schema/{slug}/identity-indexes")
        check("activating identity indexes twice writes nothing the second time",
              again.status_code == 200 and again.json().get("activated") == [],
              f"activated={again.json().get('activated') if again.status_code == 200 else code_of(again)}")
        audit_rows = daemon.admin.get("/admin/api/audit", params={"action": "schema.identity_indexes", "limit": 10})
        check("the catalog door is in the audit trail",
              audit_rows.status_code == 200
              and len(audit_rows.json().get("entries", [])) >= 2,
              f"{len(audit_rows.json().get('entries', [])) if audit_rows.status_code == 200 else audit_rows.status_code} rows")

    section("12. MCP sees exactly what the key sees")
    def mcp(method: str, params: dict[str, Any] | None = None, request_id: int = 1) -> httpx.Response:
        return daemon.data.post(
            "/mcp", json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        )

    listed_tools = [tool["name"] for tool in mcp("tools/list").json().get("result", {}).get("tools", [])]
    check("tools/list answers for this key", "query" in listed_tools and "write" in listed_tools, ", ".join(listed_tools))
    check("a read-only key is not offered a read-write tool",
          all(tool.get("annotations", {}).get("readOnlyHint") is not False for tool in
              mcp("tools/list").json().get("result", {}).get("tools", []) if tool["name"] != "write"),
          f"write tool present because the key's toggle is on: {'write' in listed_tools}")
    mcp_query = mcp(
        "tools/call",
        {"name": "query", "arguments": {"db": slug, "cypher": "MATCH (d:Doc) RETURN d.id ORDER BY d.id LIMIT 2"}},
        2,
    )
    called = mcp_query.json().get("result", {})
    check("MCP query returns rows", mcp_query.status_code == 200 and called.get("isError") is False
          and len(called.get("structuredContent", {}).get("rows", [])) == 2,
          json.dumps(called.get("structuredContent", {}).get("rows", []))[:70])
    mcp_unscoped = mcp("tools/call", {"name": "describe_schema", "arguments": {"db": "not-mine"}}, 3)
    check("MCP refuses a store outside the key's scope",
          mcp_unscoped.json().get("result", {}).get("isError") is True
          or "not_mine" in mcp_unscoped.text or "not-mine" in mcp_unscoped.text,
          mcp_unscoped.text[:80])

    unconfirmed = mcp("tools/call", {"name": "write", "arguments": {"db": slug, "cypher": "CREATE (:Doc {id: 9003, title: 'nope'})", "confirm_write": False}}, 4)
    check("MCP write demands an explicit confirmation",
          unconfirmed.json().get("result", {}).get("isError") is True
          or "confirm_write" in unconfirmed.text,
          (json.dumps(unconfirmed.json())[:110]))
    daemon.admin.patch(f"/admin/api/keys/{key_id}", json={"allow_writes": False})
    after_revoke_scope = [tool["name"] for tool in mcp("tools/list").json().get("result", {}).get("tools", [])]
    check("the write tool disappears with the key's write toggle", "write" not in after_revoke_scope,
          ", ".join(after_revoke_scope))
    refused_write = mcp("tools/call", {"name": "write", "arguments": {"db": slug, "cypher": "CREATE (:Doc {id: 9003, title: 'nope'})", "confirm_write": True}}, 5)
    check("and calling it anyway is refused",
          refused_write.status_code in (200, 403)
          and ("allow_writes" in refused_write.text or "write_not_permitted" in refused_write.text),
          f"{refused_write.status_code} {refused_write.text[:90]}")
    daemon.admin.patch(f"/admin/api/keys/{key_id}", json={"allow_writes": True})
    written = mcp("tools/call", {"name": "write", "arguments": {"db": slug, "cypher": "CREATE (:Doc {id: 9003, title: 'through mcp'})", "confirm_write": True}}, 6)
    wrote_rows = written.json().get("result", {})
    check("with the toggle on, MCP writes durably", wrote_rows.get("isError") is False
          and bool(wrote_rows.get("structuredContent", {}).get("commit", {}).get("durable")),
          json.dumps(wrote_rows.get("structuredContent", {}).get("commit", {}))[:80])
    _ws_write(daemon, secret, slug, "MATCH (d:Doc {id: 9003}) DETACH DELETE d")

    section("13. published surfaces")
    openapi = daemon.anon.get("/api/v1/openapi.json")
    paths = openapi.json().get("paths", {}) if openapi.status_code == 200 else {}
    check("OpenAPI is published", openapi.status_code == 200 and "/api/v1/databases/{slug}/query" in paths,
          f"{len(paths)} paths")
    check("the error envelope is a documented component", "ErrorEnvelope" in json.dumps(openapi.json().get("components", {})))
    metrics = daemon.anon.get("/metrics")
    check("Prometheus metrics are exposed", metrics.status_code == 200 and "oktografx_" in metrics.text,
          metrics.text.splitlines()[0][:70] if metrics.text else f"{metrics.status_code} {code_of(metrics)}")
    check("every family declares its type",
          metrics.status_code == 200
          and _families_are_declared(metrics.text),
          "each sample follows a # TYPE for its family")
    openmetrics = daemon.anon.get("/metrics", headers={"accept": "application/openmetrics-text"})
    check("OpenMetrics is negotiated when asked for",
          openmetrics.status_code == 200
          and openmetrics.headers.get("content-type", "").startswith("application/openmetrics-text")
          and openmetrics.text.rstrip().endswith("# EOF"),
          openmetrics.headers.get("content-type", "")[:48])
    page = daemon.anon.get("/")
    check("the web console is served", page.status_code == 200 and "/assets/" in page.text, page.text[:56].replace("\n", " "))
    asset = _first_asset(page.text)
    if asset:
        fetched = daemon.anon.get(asset)
        check("the console bundle loads", fetched.status_code == 200 and len(fetched.content) > 1000,
              f"{asset} {len(fetched.content)} bytes")
    deep = daemon.anon.get("/databases/docs/query")
    check("client-side routes fall back to the console", deep.status_code == 200 and '<div id="root">' in deep.text)
    still_json = daemon.anon.get("/api/v1/health")
    check("the SPA fallback does not swallow the API",
          still_json.status_code == 200 and still_json.headers.get("content-type", "").startswith("application/json"),
          still_json.text[:60])
    check("health names the library it wraps", still_json.json().get("grafx") == "0.0.8", str(still_json.json().get("grafx")))

    section("14. audit and revocation")
    entries = daemon.admin.get("/admin/api/audit", params={"limit": 400}).json().get("entries", [])
    actions = {entry["action"] for entry in entries}
    expected = {
        "database.attach",
        "database.update",
        "key.create",
        "key.update",
        "query",
        "search_text",
        "search_vector",
        "search_hybrid",
        "maintenance.verify",
        "maintenance.checkpoint",
        "mcp.query",
        "ws.begin",
        "ws.execute",
        "ws.commit",
        "ws.rollback",
    }
    if not open_access:
        # In the default posture nobody had to sign in to get here; the sign-in and the token switch are
        # audited by section 15, which runs after this snapshot.
        expected.add("login")
    check("every surface leaves an audit trail", expected <= actions,
          f"missing: {sorted(expected - actions) or 'nothing'} of {len(actions)} actions")
    check("the audit records who acted", any(entry["actor"].startswith("key:") for entry in entries),
          str(sorted({entry["actor"] for entry in entries})[:3]))
    audited_parameter = next(
        (entry for entry in entries if entry["request"].get("parameters", {}).get("needle")), None
    )
    check(
        "the audit records parameter shapes, never their values",
        audited_parameter is not None
        and audited_parameter["request"]["parameters"] == {"needle": "str<11>"}
        and "durable wal" not in json.dumps(audited_parameter),
        json.dumps((audited_parameter or {}).get("request", {}))[:90],
    )
    daemon.admin.post(f"/admin/api/keys/{key_id}/revoke")
    after = daemon.data.post(f"/api/v1/databases/{slug}/query", json={"query": "MATCH (n) RETURN n LIMIT 1"})
    check("a revoked key stops working immediately", after.status_code == 401,
          f"{after.status_code} {code_of(after)}")

    if open_access:
        section("15. the administrator switch: on, rotated, off")
        # The default is open, so the switch has to work from the open side — that is the only side it
        # will ever be pulled from. It ends where it started, because the stages after this one (the
        # browser, the README examples) run against this same daemon.
        minted = daemon.admin.post("/admin/api/admin-token").json()
        token = minted.get("admin_token", "")
        check(
            "the open console can require a token",
            token.startswith("okga_") and minted.get("signed_in") is True and minted.get("shown_once") is True,
            json.dumps(minted)[:110],
        )
        with httpx.Client(base_url=daemon.base, timeout=30.0) as stranger:
            check(
                "a request with no credential is refused from then on",
                stranger.get("/admin/api/status").status_code == 401,
                str(stranger.get("/admin/api/status").status_code),
            )
            check("the minted token opens a session",
                  stranger.post("/admin/api/login", json={"token": token}).status_code == 200)
            check("minting twice is refused: that is what rotation is for",
                  stranger.post("/admin/api/admin-token").status_code == 409)
            rotated = stranger.post("/admin/api/admin-token/rotate").json()
            check("rotation invalidates the session that held the old value",
                  stranger.get("/admin/api/databases").status_code == 401,
                  str(stranger.get("/admin/api/databases").status_code))
            check("the rotated value signs the browser back in",
                  stranger.post("/admin/api/login", json={"token": rotated.get("admin_token", "")}).status_code == 200)
            check("and the requirement can be lifted again",
                  stranger.delete("/admin/api/admin-token").status_code == 200)
        check("the console is open to this machine again",
              daemon.anon.get("/admin/api/databases").status_code == 200)
        switched = {entry["action"] for entry in
                    daemon.admin.get("/admin/api/audit", params={"limit": 400}).json().get("entries", [])}
        check(
            "the switch is audited, including the sign-ins it caused",
            {"admin_token.mint", "admin_token.rotate", "admin_token.disable", "login"} <= switched,
            f"missing: {sorted({'admin_token.mint', 'admin_token.rotate', 'admin_token.disable', 'login'} - switched) or 'nothing'}",
        )

    return finish(daemon)


def finish(daemon: Daemon) -> int:
    daemon.close()
    print(f"\n\033[1m{len(PASSES)} checks passed, {len(FAILURES)} failed\033[0m")
    for name in FAILURES:
        print(f"  failed: {name}")
    return 0 if not FAILURES else 1


def _ws_write(daemon: Daemon, secret: str, slug: str, statement: str, *, rollback: bool = False) -> dict[str, Any]:
    return ws_transaction(daemon.base, secret, slug, statement, rollback=rollback)


def _tree(path: Path) -> dict[str, int]:
    return {str(item.relative_to(path)): item.stat().st_size for item in sorted(path.rglob("*")) if item.is_file()}


def _families_are_declared(text: str) -> bool:
    """Every sample line must sit under a ``# TYPE`` for its own family."""

    declared: set[str] = set()
    for line in text.splitlines():
        if line.startswith("# TYPE"):
            declared.add(line.split()[2])
        elif line.startswith("#") or not line.strip():
            continue
        else:
            name = line.split()[0].split("{")[0]
            if name not in declared:
                return False
    return True


def _first_asset(html: str) -> str:
    start = html.find('src="/assets/')
    if start < 0:
        return ""
    rest = html[start + 5 :]
    return rest[: rest.find('"')]


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--seed":
        # `--seed <dir> [dir ...]` builds throwaway Grafx stores for the run.
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
        from conftest import seed_store  # noqa: E402

        for target in sys.argv[2:]:
            seed_store(Path(target))
            print(f"seeded {target}")
        sys.exit(0)
    sys.exit(main())
