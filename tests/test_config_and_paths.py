"""Configuration, path policy and the generated option schema (plan item 4)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from okto_grafx_server.config import (
    PathPolicyError,
    ServerConfig,
    build_config,
    database_option_schema,
    describe_path,
    load_config,
    metrics_target_is_remote,
    resolve_existing_store,
    resolve_store_parent,
    validate_config,
    validate_store_options,
    write_default_config,
)


def config(**overrides) -> ServerConfig:
    raw = {"allowed_roots": ["/srv/graphs"], "state_dir": "/tmp/state"}
    raw.update(overrides)
    return build_config(raw)


def code_of(exc: BaseException) -> str | None:
    return getattr(exc, "code", None)


# -- server.toml -----------------------------------------------------------------------


def test_defaults_are_loopback_and_conservative() -> None:
    built = build_config({"allowed_roots": ["/srv/graphs"], "state_dir": "/tmp/state"})
    assert built.bind_host == "127.0.0.1"
    assert built.bind_port == 8737
    assert built.max_open_handles == 4
    assert built.readers_per_database == 2
    assert built.max_write_sessions == 1
    assert built.max_request_rows == 1000
    assert built.metrics_enabled is False
    assert built.allow_remote_binding is False
    assert built.server_db_path.name == "server.db"
    assert built.secret_path.name == "server.secret"
    assert built.config_path.name == "server.toml"


def test_unknown_keys_are_refused_by_name() -> None:
    with pytest.raises(Exception) as refused:
        build_config({"allowed_roots": ["/srv"], "state_dir": "/tmp/s", "typo_port": 1})
    assert code_of(refused.value) == "invalid_configuration"
    assert "typo_port" in str(refused.value)


def test_missing_allowed_roots_is_refused() -> None:
    with pytest.raises(Exception) as refused:
        build_config({"state_dir": "/tmp/s"})
    assert code_of(refused.value) == "allowed_roots_required"


def test_relative_and_symlinked_roots_are_refused(tmp_path: Path) -> None:
    with pytest.raises(Exception) as refused:
        build_config({"state_dir": "/tmp/s", "allowed_roots": ["graphs"]})
    assert code_of(refused.value) == "invalid_path"

    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(Exception) as refused:
        build_config({"state_dir": "/tmp/s", "allowed_roots": [str(link)]})
    assert code_of(refused.value) == "invalid_path"


def test_remote_binding_needs_explicit_consent() -> None:
    with pytest.raises(Exception) as refused:
        config(bind_host="0.0.0.0")
    assert code_of(refused.value) == "remote_binding_requires_consent"
    assert config(bind_host="0.0.0.0", allow_remote_binding=True).bind_host == "0.0.0.0"

    with pytest.raises(Exception) as metrics:
        config(metrics_enabled=True, metrics_host="0.0.0.0")
    assert code_of(metrics.value) == "remote_metrics_requires_consent"


def test_numeric_bounds_are_checked() -> None:
    for field, value in (
        ("bind_port", 0),
        ("bind_port", 70000),
        ("max_open_handles", 0),
        ("readers_per_database", -1),
        ("max_request_rows", 0),
    ):
        with pytest.raises(Exception) as refused:
            config(**{field: value})
        assert code_of(refused.value) == "invalid_configuration"
        assert refused.value.field == field


def test_store_metrics_that_leave_the_machine_need_explicit_consent() -> None:
    """The library's metrics sink has no auth and no TLS, so neither does the server's permission."""

    remote = {"metrics": "openmetrics", "metrics_destination": "10.0.0.5:9464"}
    assert metrics_target_is_remote(remote)
    with pytest.raises(Exception) as refused:
        validate_store_options(remote)
    assert code_of(refused.value) == "remote_store_metrics_requires_consent"
    assert refused.value.field == "metrics_destination"

    # Consent here means "let the library decide", and the library still needs its own flag.
    with pytest.raises(Exception) as engine_says_no:
        validate_store_options(remote, allow_remote_metrics=True)
    assert code_of(engine_says_no.value) == "configuration_error"
    both = dict(remote, allow_remote_metrics=True)
    assert validate_store_options(both, allow_remote_metrics=True) == both

    # Things that stay on the machine are never asked about.
    assert not metrics_target_is_remote({"metrics": "openmetrics", "metrics_destination": "127.0.0.1:9464"})
    assert not metrics_target_is_remote({"metrics": "openmetrics", "metrics_destination": "[::1]:9464"})
    assert not metrics_target_is_remote({"metrics": "json", "metrics_destination": "/tmp/grafx.json"})
    assert validate_store_options({"metrics": "json", "metrics_destination": "/tmp/grafx.json"})
    # A hostname this server cannot prove local counts as remote.
    assert metrics_target_is_remote({"metrics": "openmetrics", "metrics_destination": "collector:9464"})


def test_write_sessions_stop_being_parallel_at_four() -> None:
    """One writer lease per store is the engine's rule, so the knob stops at four."""

    assert config(max_write_sessions=1).max_write_sessions == 1
    assert config(max_write_sessions=4).max_write_sessions == 4

    with pytest.raises(Exception) as refused:
        config(max_write_sessions=5)
    assert code_of(refused.value) == "invalid_configuration"
    assert refused.value.field == "max_write_sessions"
    assert "writer lease" in refused.value.message


def test_starter_config_documents_the_two_multipliers(tmp_path: Path) -> None:
    """The file an operator edits should say what those numbers cost."""

    written = write_default_config(tmp_path / "server.toml", allowed_roots=["/srv/graphs"])
    text = written.read_text()
    assert "buffer_budget_bytes" in text, "handles multiply the page-cache budget"
    assert "writer lease" in text, "max_write_sessions has a ceiling, and the file says why"


def test_load_config_reads_toml_and_overrides(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    write_default_config(state / "server.toml", allowed_roots=["/srv/graphs"], port=9100)
    loaded = load_config(state / "server.toml", overrides={"state_dir": str(state)})
    assert loaded.bind_port == 9100
    assert loaded.allowed_roots == ("/srv/graphs",)
    assert loaded.state_dir == state.resolve()

    # A starter file is never overwritten by a second init.
    before = (state / "server.toml").read_text()
    write_default_config(state / "server.toml", allowed_roots=["/other"], port=1)
    assert (state / "server.toml").read_text() == before

    # And the starter file actually parses into a valid configuration.
    (state / "server.toml").write_text(
        before.replace('allowed_roots = [\n    "~/graphs",\n]', f'allowed_roots = ["{tmp_path}"]')
    )
    assert load_config(state / "server.toml", overrides={"state_dir": str(state)}).bind_port == 9100


def test_the_starter_file_is_readable_toml_on_any_platform(tmp_path: Path) -> None:
    # A Windows store root is `D:\\graphs`, and a single backslash is not legal inside a TOML basic
    # string: the starter file interpolated roots between plain quotes, so `init` on Windows produced a
    # server.toml that tomllib refused - a server that cannot read its own configuration, only on the
    # platform that uses that separator. The three shapes below are what real paths do.
    import tomllib

    roots = [r"D:\graphs", r"C:\Users\O'Brien\graphs", 'C:\\Users\\"quoted\"']
    path = tmp_path / "server.toml"
    write_default_config(path, allowed_roots=roots, port=9123)

    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    assert raw["allowed_roots"] == roots
    assert raw["bind_port"] == 9123
    assert raw["bind_host"] == "127.0.0.1"


def test_environment_state_dir_is_resolved(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OKTO_GRAFX_SERVER_STATE", str(tmp_path / "from-env"))
    from okto_grafx_server.config import default_state_dir

    assert default_state_dir() == (tmp_path / "from-env").resolve()


def test_the_state_dir_default_is_the_platform_habit(tmp_path: Path, monkeypatch) -> None:
    # The installer asks this function where state lives rather than carrying a second answer, and the
    # two platforms answer differently. Both are asserted here, on a machine that is neither, by
    # replacing the two things the function is allowed to look at. `sys.platform` rather than `os.name`
    # because Path() itself consults os.name on every construction: patching that turns every path in
    # the process into a WindowsPath and the test dies inside pathlib instead of here.
    for var in ("OKTO_GRAFX_SERVER_STATE", "XDG_STATE_HOME", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)

    from okto_grafx_server.config import default_state_dir

    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    monkeypatch.setattr(sys, "platform", "darwin")
    assert default_state_dir() == home / ".local" / "state" / "okto-grafx-server"

    local = tmp_path / "AppData" / "Local"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    assert default_state_dir() == local / "okto-grafx-server"

    # The escape hatch is not platform-specific: a container or a portable install pins the directory
    # the same way wherever it runs, and it is honoured before LOCALAPPDATA is even read.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "pinned"))
    assert default_state_dir() == tmp_path / "pinned" / "okto-grafx-server"


# -- path policy -----------------------------------------------------------------------


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, ...]:
    graphs = tmp_path / "graphs"
    graphs.mkdir()
    return (graphs,)


def test_paths_must_be_absolute_and_expanded(tmp_path: Path, roots) -> None:
    for candidate in ("graphs/docs", "~/graphs/docs", "relative/../docs"):
        with pytest.raises(PathPolicyError) as refused:
            resolve_store_parent(Path(candidate), roots)
        assert code_of(refused.value) == "invalid_path"
        assert refused.value.status == 422


def test_unc_and_url_shapes_are_refused(roots) -> None:
    for candidate in ("\\\\server\\share\\store", "//host/share/store"):
        with pytest.raises(PathPolicyError):
            resolve_store_parent(Path(candidate), roots)


def test_outside_allowed_roots_is_refused(tmp_path: Path, roots) -> None:
    elsewhere = tmp_path / "elsewhere" / "store"
    with pytest.raises(PathPolicyError) as refused:
        resolve_store_parent(elsewhere, roots)
    assert "allowed root" in str(refused.value)


def test_symlink_component_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    roots = (tmp_path,)
    with pytest.raises(PathPolicyError) as refused:
        resolve_store_parent(link / "store", roots)
    assert "symlink" in str(refused.value)


def test_traversal_cannot_escape_the_root(tmp_path: Path) -> None:
    graphs = tmp_path / "graphs"
    (graphs / "inner").mkdir(parents=True)
    (tmp_path / "secret").mkdir()
    with pytest.raises(PathPolicyError):
        resolve_store_parent(graphs / "inner" / ".." / ".." / "secret", (graphs,))


def test_the_root_itself_is_not_a_store_target(tmp_path: Path) -> None:
    graphs = tmp_path / "graphs"
    graphs.mkdir()
    (graphs / "grafx.meta").write_text("x")
    with pytest.raises(PathPolicyError):
        resolve_store_parent(graphs, (graphs,))


def test_a_store_must_have_the_grafx_marker(tmp_path: Path) -> None:
    graphs = tmp_path / "graphs"
    plain = graphs / "plain"
    plain.mkdir(parents=True)
    with pytest.raises(PathPolicyError) as refused:
        resolve_existing_store(plain, (graphs,))
    assert "grafx.meta" in str(refused.value)

    store = graphs / "store"
    store.mkdir()
    (store / "grafx.meta").write_text("x")
    assert resolve_existing_store(store, (graphs,)) == store


def test_a_missing_directory_is_reported(tmp_path: Path) -> None:
    graphs = tmp_path / "graphs"
    graphs.mkdir()
    with pytest.raises(PathPolicyError) as refused:
        resolve_existing_store(graphs / "ghost", (graphs,))
    assert "does not exist" in str(refused.value)

    (graphs / "file.txt").write_text("x")
    with pytest.raises(PathPolicyError) as not_dir:
        resolve_existing_store(graphs / "file.txt", (graphs,))
    assert "not a directory" in str(not_dir.value)


def test_describe_path_counts_files_and_bytes(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    (store / "grafx.meta").write_bytes(b"meta")
    (store / "heap.dat").write_bytes(b"0123456789")
    (store / "sub").mkdir()
    (store / "sub" / "wal").write_bytes(b"abc")

    facts = describe_path(store)
    assert facts["bytes"] == 4 + 10 + 3
    assert facts["files"] == 3
    assert facts["marker"] == "grafx.meta"
    assert facts["path"] == str(store)


def test_validate_config_is_idempotent() -> None:
    built = config()
    assert validate_config(built) is built


# -- DatabaseConfig option schema ------------------------------------------------------


def test_schema_reflects_the_library_fields() -> None:
    from dataclasses import fields as dataclass_fields
    from okto_grafx import DatabaseConfig

    schema = database_option_schema()
    by_name = {entry["name"]: entry for entry in schema}
    names = {info.name for info in dataclass_fields(DatabaseConfig)}

    assert names - {"path"} <= set(by_name), "every connect() option must be reachable"
    assert "path" not in by_name, "the path is the registration, not an option"

    page_size = by_name["page_size"]
    assert page_size["type"] == "integer"
    assert page_size["default"] == 8192
    assert page_size["minimum"] == 4096
    assert page_size["group"] == "storage"

    # Defaults come from the field definitions, not from frozen class attributes.
    assert isinstance(by_name["read_only"]["default"], bool)
    assert by_name["read_only"]["type"] == "boolean"
    assert by_name["lease_ttl_seconds"]["type"] == "number"
    assert by_name["recovery_policy"]["choices"]
    assert by_name["wal_max_bytes"]["nullable"] is True
    for entry in schema:
        assert entry["label"] and entry["group"]
        assert isinstance(entry["default"], (int, float, str, bool, type(None)))


def test_options_validate_against_the_real_config_object() -> None:
    cleaned = validate_store_options({"page_size": 16384, "wal_max_bytes": None})
    assert cleaned == {"page_size": 16384}, "nulls mean 'leave the default'"

    with pytest.raises(Exception) as refused:
        validate_store_options({"page_size": 1234})
    assert code_of(refused.value) == "configuration_error"

    with pytest.raises(Exception) as unknown:
        validate_store_options({"not_an_option": 1})
    assert code_of(unknown.value) == "unknown_database_options"
    assert unknown.value.field == "not_an_option"

    with pytest.raises(Exception) as path_attempt:
        validate_store_options({"path": "/tmp/whatever"})
    assert code_of(path_attempt.value) == "unknown_database_options"

    with pytest.raises(Exception) as not_object:
        validate_store_options(["page_size"])  # type: ignore[arg-type]
    assert code_of(not_object.value) == "invalid_options"


def test_every_offered_default_is_accepted_by_connect() -> None:
    # The form is generated from the library, so every default it can offer must be something
    # connect() accepts — otherwise the UI would ship a value the store refuses.
    for entry in database_option_schema():
        if entry["default"] is None:
            continue
        assert validate_store_options({entry["name"]: entry["default"]}) == {
            entry["name"]: entry["default"]
        }, entry["name"]


def test_declared_bounds_are_the_library_bounds() -> None:
    from okto_grafx.runtime.config import MAX_PAGE_SIZE, RECOVERY_POLICIES

    by_name = {entry["name"]: entry for entry in database_option_schema()}
    assert by_name["page_size"]["maximum"] == int(MAX_PAGE_SIZE)
    assert by_name["recovery_policy"]["choices"] == sorted(RECOVERY_POLICIES)

    # The library's own floor is enforced, not a number this server invented.
    with pytest.raises(Exception) as refused:
        validate_store_options({"buffer_budget_bytes": 1})
    assert code_of(refused.value) == "configuration_error"
    assert "buffer_budget_bytes" in str(refused.value)
