"""The console is served by this process — including the paths it must refuse to serve.

Serving a single-page app means answering most requests with `index.html`, so the interesting part of
this surface is not what it returns but what it must never return: a file from outside the bundle,
reached with `..` segments or with their percent-encoded form. A path that walks out of the console
directory has to get the shell back, because the shell is what an unmatched browser route gets anyway.
"""

from __future__ import annotations

from pathlib import Path

from okto_grafx_server import app as app_module


def test_a_browser_route_gets_the_console_shell(daemon) -> None:
    response = daemon.client.get("/databases/docs/graph")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'id="root"' in response.text


def test_a_bundled_asset_is_served(daemon) -> None:
    assets = sorted((app_module.WEB_DIRECTORY / "assets").glob("*.js"))
    assert assets, "the console must be built for this test to mean anything"
    response = daemon.client.get(f"/assets/{assets[0].name}")
    assert response.status_code == 200
    # The name is guessed from the extension, and the exact spelling belongs to the platform; what is
    # promised here is that the bytes are the bundle's own and that they are handed over as script.
    assert response.headers["content-type"].split(";")[0] in {"text/javascript", "application/javascript"}
    assert response.content == assets[0].read_bytes()


def test_a_path_that_leaves_the_bundle_gets_the_shell(daemon) -> None:
    # Two levels above src/okto_grafx_server/web sits the repository root, where real files live.
    # The encoded forms matter: a client normalising `/../` itself never reaches the handler with it.
    for probe in (
        "/../../pyproject.toml",
        "/%2e%2e/%2e%2e/pyproject.toml",
        "/..%2f..%2fpyproject.toml",
        "/%2E%2E%2F%2E%2E%2FREADME.md",
        "/assets/../../../pyproject.toml",
    ):
        response = daemon.client.get(probe)
        assert response.status_code == 200, probe
        assert "[project]" not in response.text, probe
        assert "Okto Grafx Server" not in response.text, probe
        assert 'id="root"' in response.text, probe


def test_an_unbuilt_console_says_what_to_do(monkeypatch, daemon) -> None:
    monkeypatch.setattr(app_module, "WEB_DIRECTORY", Path("/definitely/not/built"))
    response = daemon.client.get("/some/unknown/path")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "console_not_built"
    assert "npm --prefix web run build" in error["message"]

    # The JSON doors keep answering JSON, built console or not.
    api = daemon.client.get("/api/v1/nope")
    assert api.status_code == 404
    assert api.json()["error"]["code"] == "not_found"
