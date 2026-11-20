"""/metrics: off until it is configured, and a legal exposition once it is.

A page a scraper refuses to parse is worse than no page at all, so these tests read the text rather
than only checking for a 200: every family has to declare its type before its samples, and the
OpenMetrics dialect has to close itself.
"""

from __future__ import annotations

import re
from pathlib import Path

from conftest import Harness

SAMPLE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+\S+$")


def test_metrics_are_closed_until_the_operator_opens_them(running) -> None:
    response = running.client.get("/metrics")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "metrics_disabled"
    # The refusal explains the switch, because the operator who enabled it is the reader.
    assert "metrics_enabled" in response.json()["error"]["message"]


def test_the_prometheus_exposition_declares_every_family(tmp_path: Path) -> None:
    with Harness(tmp_path.resolve(), metrics_enabled=True) as daemon:
        daemon.login()
        # Labelled samples only exist while a handle is live, and those are the ones a scraper is
        # most likely to reject for a missing type declaration.
        daemon.attach(daemon.make_store("docs"), "docs")
        daemon.client.get("/admin/api/databases/docs/inventory")
        response = daemon.client.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")

        declared: set[str] = set()
        samples = 0
        for line in response.text.splitlines():
            if line.startswith("# TYPE"):
                declared.add(line.split()[2])
                continue
            if line.startswith("#"):
                continue
            match = SAMPLE.match(line)
            assert match, f"{line!r} is not a sample line"
            assert match.group("name") in declared, f"{match.group('name')} was sampled without a # TYPE"
            samples += 1
        assert samples >= 9, "the exposition degenerated to nothing"
        assert any('db="docs"' in line for line in response.text.splitlines()), "no per-store sample"
        assert "oktografx_up" in declared


def test_openmetrics_is_negotiated_and_closes_itself(tmp_path: Path) -> None:
    with Harness(tmp_path.resolve(), metrics_enabled=True) as daemon:
        plain = daemon.client.get("/metrics")
        openmetrics = daemon.client.get("/metrics", headers={"accept": "application/openmetrics-text"})
        assert openmetrics.status_code == 200
        assert openmetrics.headers["content-type"].startswith("application/openmetrics-text")
        assert openmetrics.text.rstrip().endswith("# EOF"), "OpenMetrics must end with # EOF"
        assert "oktografx_up 1" in openmetrics.text
        # Counters carry the _total suffix OpenMetrics requires, and the plain page is unchanged.
        assert "oktografx_worker_submitted_total " in openmetrics.text
        assert not plain.text.rstrip().endswith("# EOF")
        assert plain.headers["content-type"] != openmetrics.headers["content-type"]
