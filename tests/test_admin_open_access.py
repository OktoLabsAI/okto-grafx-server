"""Administrator access with no credential: this machine walks in, everyone else is refused.

These tests deliberately avoid ``TestClient`` for the requests that matter. Its ASGI peer is
``"testclient"``, an address no socket can present, so it can prove a token works but cannot prove
the rule that a token is what stands between this machine and everyone else.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import httpx

from okto_grafx_server.api.context import admin_open_to_loopback, open_loopback_refusal


def _open_harness(tmp_path: Path, *, require_token: bool = False):
    from conftest import Harness

    return Harness(tmp_path.resolve(), require_token=require_token)


def _error_code(response: httpx.Response) -> str:
    return response.json()["error"]["code"]


# ------------------------------------------------------------------ this machine, with no credential


def test_the_console_is_open_to_this_machine_with_no_token(tmp_path: Path) -> None:
    with _open_harness(tmp_path) as harness:
        with closing(harness.as_loopback()) as client:
            session = client.get("/admin/api/session").json()
            assert session == {
                "signed_in": True,
                "token_configured": False,
                "auth": "open-loopback",
                "token_rotated_at": None,
                "server_version": session["server_version"],
            }
            assert client.get("/admin/api/databases").status_code == 200
            # The settings screen needs no token to describe itself, and hands over the commands that
            # change this, spelled with this state directory rather than a placeholder.
            settings = client.get("/admin/api/settings").json()
            assert settings["admin_auth"]["mode"] == "open-loopback"
            assert str(harness.state_dir) in settings["admin_auth"]["commands"]["require"]


def test_a_request_that_arrives_from_elsewhere_is_refused(tmp_path: Path) -> None:
    with _open_harness(tmp_path) as harness:
        with closing(harness.as_loopback(peer="203.0.113.7")) as away:
            response = away.get("/admin/api/databases")
            assert response.status_code == 401
            assert _error_code(response) == "admin_session_required"
            assert "this machine" in response.json()["error"]["message"]


# ---------------------------------------------------------------------------- the browser, and its lies


def test_a_rebound_host_is_refused_even_from_a_loopback_peer(tmp_path: Path) -> None:
    # DNS rebinding puts the peer genuinely on 127.0.0.1 while the browser believes it is talking to
    # the attacker's host. The peer check alone would wave it through; the Host header does not.
    with _open_harness(tmp_path) as harness:
        with closing(harness.as_loopback()) as client:
            response = client.get("/admin/api/databases", headers={"host": "evil.example"})
            assert response.status_code == 401
            assert _error_code(response) == "admin_session_required"


def test_a_page_from_elsewhere_may_not_change_anything(tmp_path: Path) -> None:
    with _open_harness(tmp_path) as harness:
        with closing(harness.as_loopback()) as client:
            response = client.post(
                "/admin/api/keys",
                json={"name": "not-mine", "databases": []},
                headers={"origin": "https://evil.example"},
            )
            assert response.status_code == 403
            assert _error_code(response) == "admin_origin_not_same_side"
            # Refused means refused, not "applied and then argued about".
            assert client.get("/admin/api/keys").json()["keys"] == []


def test_sec_fetch_site_says_no_even_without_an_origin(tmp_path: Path) -> None:
    with _open_harness(tmp_path) as harness:
        with closing(harness.as_loopback()) as client:
            response = client.post(
                "/admin/api/keys",
                json={"name": "not-mine", "databases": []},
                headers={"sec-fetch-site": "cross-site"},
            )
            assert response.status_code == 403


def test_a_client_on_this_machine_with_no_browser_fingerprint_may_still_work(tmp_path: Path) -> None:
    # curl, the CLI and the MCP client send no Origin. They are already on the machine, which is the
    # boundary; making them pretend to be a browser would only push operators to switch the token on
    # for the wrong reason.
    with _open_harness(tmp_path) as harness:
        store = harness.make_store()
        with closing(harness.as_loopback()) as client:
            attached = client.post(
                "/admin/api/databases",
                json={
                    "path": str(store),
                    "slug": "docs",
                    "name": "docs",
                    "read_only": False,
                    "allow_writes": True,
                },
            )
            assert attached.status_code == 201, attached.text
            created = client.post(
                "/admin/api/keys",
                json={"name": "curl", "databases": ["docs"], "allow_writes": False},
            )
            assert created.status_code == 201, created.text
            secret = created.json()["key"]
            queried = client.post(
                "/api/v1/databases/docs/query",
                json={"query": "MATCH (n) RETURN count(n) AS n"},
                headers={"Authorization": f"Bearer {secret}"},
            )
            assert queried.status_code == 200, queried.text


def test_a_websocket_handshake_counts_as_a_change_not_a_read(tmp_path: Path) -> None:
    # A browser always sends Origin on a WebSocket handshake; a write channel is not a safe method.
    with _open_harness(tmp_path) as harness:
        ctx = harness.context

        def websocket(origin: str) -> SimpleNamespace:
            return SimpleNamespace(
                client=SimpleNamespace(host="127.0.0.1", port=40000),
                headers={"host": "127.0.0.1:8737", "origin": origin},
            )

        assert admin_open_to_loopback(ctx, websocket("http://127.0.0.1:8737"), method="WEBSOCKET")
        assert (
            open_loopback_refusal(ctx, websocket("https://evil.example"), method="WEBSOCKET")
            == "origin"
        )


# ------------------------------------------------------------------- the same surface, with a token on


def test_minting_a_token_from_the_open_console_closes_the_door(tmp_path: Path) -> None:
    with _open_harness(tmp_path) as harness:
        with closing(harness.as_loopback()) as client:
            minted = client.post("/admin/api/admin-token")
            assert minted.status_code == 200, minted.text
            token = minted.json()["admin_token"]
            assert minted.json()["shown_once"] is True
            # The browser that asked for the wall is not locked out by its own request.
            assert minted.json()["signed_in"] is True
            assert client.get("/admin/api/databases").status_code == 200

        with closing(harness.as_loopback()) as stranger:
            assert stranger.get("/admin/api/databases").status_code == 401
            assert stranger.post("/admin/api/login", json={"token": token}).status_code == 200
            assert stranger.get("/admin/api/databases").status_code == 200
            # Minting over a live token is refused: that is what rotation exists for.
            assert stranger.post("/admin/api/admin-token").status_code == 409
            assert _error_code(stranger.post("/admin/api/admin-token")) == "admin_token_exists"


def test_clearing_the_token_reopens_this_machine_and_only_this_machine(tmp_path: Path) -> None:
    with _open_harness(tmp_path, require_token=True) as harness:
        harness.login()
        with closing(harness.as_loopback(peer="203.0.113.7")) as away:
            assert away.get("/admin/api/databases").status_code == 401
        assert harness.client.delete("/admin/api/admin-token").status_code == 200
        with closing(harness.as_loopback()) as client:
            assert client.get("/admin/api/databases").status_code == 200
            assert client.get("/admin/api/session").json()["auth"] == "open-loopback"
        with closing(harness.as_loopback(peer="203.0.113.7")) as away:
            assert away.get("/admin/api/databases").status_code == 401


def test_clearing_a_token_that_was_never_set_says_so(tmp_path: Path) -> None:
    with _open_harness(tmp_path) as harness:
        with closing(harness.as_loopback()) as client:
            response = client.delete("/admin/api/admin-token")
            assert response.status_code == 409
            assert _error_code(response) == "admin_token_missing"


def test_rotating_signs_out_every_browser_not_just_the_one_asking(tmp_path: Path) -> None:
    # The installer and the CLI both promise that rotation invalidates "the old one, and every
    # session signed in with it". A session that outlives its token would make that sentence false.
    with _open_harness(tmp_path, require_token=True) as harness:
        harness.login()  # the browser that will rotate
        with closing(harness.as_loopback()) as other:
            signed_in = other.post("/admin/api/login", json={"token": harness.admin_token})
            assert signed_in.status_code == 200

            rotated = harness.client.post("/admin/api/admin-token/rotate")
            assert rotated.status_code == 200, rotated.text
            assert harness.client.get("/admin/api/databases").status_code == 401
            assert other.get("/admin/api/databases").status_code == 401

            again = other.post("/admin/api/login", json={"token": rotated.json()["admin_token"]})
            assert again.status_code == 200
            assert other.get("/admin/api/databases").status_code == 200


def test_a_browser_left_holding_a_dead_cookie_is_not_locked_out(tmp_path: Path) -> None:
    # Rotate, then lift the requirement. Every tab that was open the whole time still holds a session
    # that no longer exists, and there is no longer a sign-in screen to send it to — so a dead cookie
    # must stop being an answer of its own.
    with _open_harness(tmp_path) as harness:
        with closing(harness.as_loopback()) as browser:
            minted = browser.post("/admin/api/admin-token")
            assert minted.status_code == 200, minted.text
            rotated = browser.post("/admin/api/admin-token/rotate")
            assert rotated.status_code == 200, rotated.text
            assert browser.get("/admin/api/databases").status_code == 401  # its session died with the old token

            lifted = browser.delete(
                "/admin/api/admin-token", headers={"X-Admin-Token": rotated.json()["admin_token"]}
            )
            assert lifted.status_code == 200, lifted.text
            assert browser.get("/admin/api/databases").status_code == 200
            assert browser.get("/admin/api/session").json()["auth"] == "open-loopback"
