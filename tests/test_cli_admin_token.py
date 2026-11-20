"""The credential door from the command line: off by default, opt-in, honest about both."""

from __future__ import annotations

from pathlib import Path

from okto_grafx_server import cli


def _init(tmp_path: Path, *extra: str) -> Path:
    state = tmp_path / "state"
    assert (
        cli.main(["--state-dir", str(state), "init", "--root", str(tmp_path / "graphs"), *extra])
        == 0
    )
    return state


def _run(state: Path, *argv: str) -> int:
    return cli.main(["--state-dir", str(state), "admin-token", *argv])


def _status(state: Path, capsys) -> str:
    assert _run(state, "status") == 0
    return capsys.readouterr().out


def test_init_leaves_the_console_open_to_this_machine(tmp_path: Path, capsys) -> None:
    state = _init(tmp_path)
    out = capsys.readouterr().out
    assert "no token required" in out
    assert "ADMIN TOKEN" not in out
    # The way to turn it on is spelled out with this state directory, not left as an exercise.
    assert f"--state-dir {state} admin-token rotate" in out
    assert not (state / "admin-token").exists()
    assert "no token required" in _status(state, capsys)


def test_init_can_require_a_token_from_the_first_start(tmp_path: Path, capsys) -> None:
    state = _init(tmp_path, "--with-admin-token")
    out = capsys.readouterr().out
    assert "ADMIN TOKEN (shown once, store it now)" in out
    token = next(line.strip() for line in out.splitlines() if line.strip().startswith("okga_"))

    assert "a token is required" in _status(state, capsys)

    assert _run(state, "rotate") == 0
    rotated = capsys.readouterr().out
    fresh = next(line.strip() for line in rotated.splitlines() if line.strip().startswith("okga_"))
    assert fresh != token
    assert "every signed-in browser stop working" in rotated

    assert _run(state, "clear") == 0
    assert "no longer requires a token" in capsys.readouterr().out
    assert "no token required" in _status(state, capsys)

    # Clearing twice says so instead of pretending to undo something.
    assert _run(state, "clear") == 0
    assert "nothing is being required" in capsys.readouterr().out
