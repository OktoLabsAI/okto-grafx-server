"""Server configuration (``server.toml``), path policy and the option schema for the UI.

The server owns exactly one kind of durable state of its own: ``server.toml`` (how to bind and
where the control plane lives) and ``server.db`` (what it manages). Everything about a *store*
belongs to ``okto_grafx.DatabaseConfig``, which this module reflects instead of duplicating: the
UI form is generated from the library's own field names, defaults and bounds, so a server upgrade
cannot silently invent a knob that ``connect()`` would refuse.
"""

from __future__ import annotations

import ipaddress
import os
import sys
import tomllib
from dataclasses import MISSING, dataclass, field, fields as dataclass_fields
from pathlib import Path
from typing import Any

from okto_grafx import DatabaseConfig

from .errors import AppError

DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_BIND_PORT = 8737
STORE_MARKER = "grafx.meta"

# The addresses that count as "this machine" for binding and for the administrator door. `localhost`
# belongs here because it is how people type the loopback address; a host name that resolves to
# loopback by way of /etc/hosts does not, and is deliberately not our problem to resolve.
LOOPBACK_BIND_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def host_is_loopback(host: str) -> bool:
    """Whether ``host`` names this machine, with or without a port, brackets included."""

    host = (host or "").strip()
    if host.startswith("["):
        end = host.find("]")
        host = host[1:end] if end > 0 else host.strip("[]")
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    return host in LOOPBACK_BIND_HOSTS

# The plan fixes concurrent write sessions at "default 1, max 4". The engine grants one writer
# lease per store, so anything above four only queues more sessions behind a lease that cannot be
# shared — a larger number would be a knob that lies.
MAX_WRITE_SESSIONS_LIMIT = 4


class PathPolicyError(AppError):
    """A path the server will not touch, for a reason the operator can read."""

    def __init__(self, message: str, *, value: Any = None) -> None:
        super().__init__(message, status=422, code="invalid_path", value=value)


def default_state_dir() -> Path:
    """Where ``server.toml`` and ``server.db`` go when the operator did not say.

    ``OKTO_GRAFX_SERVER_STATE`` wins on every platform, and ``XDG_STATE_HOME`` is honoured on every
    platform too — including Windows — so a test, a container or a portable install can pin the
    directory without knowing which OS it is running on. What is left is the platform's own habit: a
    dot-directory under the home on POSIX, and the per-user application data directory on Windows,
    because someone looking for this folder in Explorer opens ``%LOCALAPPDATA%`` and reads the product
    name, rather than walking into a ``.local`` they never created. The installer reads this function's
    answer rather than carrying a second copy of it, so there is one rule for where state lives.
    """
    override = os.environ.get("OKTO_GRAFX_SERVER_STATE")
    if override:
        return Path(override).expanduser().resolve()
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base).expanduser() / "okto-grafx-server"
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data).expanduser() / "okto-grafx-server"
    return Path.home() / ".local" / "state" / "okto-grafx-server"


@dataclass(frozen=True)
class ServerConfig:
    """Validated runtime configuration of the daemon."""

    state_dir: Path = field(default_factory=default_state_dir)
    bind_host: str = DEFAULT_BIND_HOST
    bind_port: int = DEFAULT_BIND_PORT
    allowed_roots: tuple[str, ...] = ()
    max_open_handles: int = 4
    readers_per_database: int = 2
    max_write_sessions: int = 1
    max_ws_sessions: int = 8
    max_request_rows: int = 1000
    max_graph_nodes: int = 2000
    max_graph_edges: int = 5000
    query_timeout_seconds: float = 30.0
    # How long a request may wait for a reader lane before the server says "busy" instead of
    # letting a queue grow inside the store. Backpressure has to arrive sooner than a query
    # deadline for a caller to be able to retry.
    reader_lane_timeout_seconds: float = 5.0
    idle_session_ttl_seconds: float = 60.0
    handle_idle_ttl_seconds: float = 120.0
    metrics_enabled: bool = False
    metrics_host: str = "127.0.0.1"
    metrics_port: int = 0
    # Per-store metrics are the library's own sink, and it has no authentication or TLS either: a
    # database option that points it off this machine needs the same kind of explicit consent as a
    # non-loopback bind.
    allow_remote_store_metrics: bool = False
    cors_origins: tuple[str, ...] = ()
    audit_retention_days: int = 30
    allow_remote_binding: bool = False
    max_request_bytes: int = 2 * 1024 * 1024
    max_ws_batches: int = 200
    max_query_value_characters: int = 65536

    @property
    def server_db_path(self) -> Path:
        return self.state_dir / "server.db"

    @property
    def secret_path(self) -> Path:
        return self.state_dir / "server.secret"

    @property
    def config_path(self) -> Path:
        return self.state_dir / "server.toml"

    @property
    def log_path(self) -> Path:
        return self.state_dir / "server.log"

    def roots(self) -> tuple[Path, ...]:
        return tuple(Path(root) for root in self.allowed_roots)


def load_config(path: Path | None = None, *, overrides: dict[str, Any] | None = None) -> ServerConfig:
    """Read ``server.toml`` if present and build a validated configuration.

    The state directory is the directory the configuration file lives in, unless the file itself (or
    an override) names another one. Otherwise ``--state-dir`` and a hand-placed ``server.toml`` could
    quietly disagree about where ``server.db`` and the secret live.
    """

    state_dir = (overrides or {}).get("state_dir") or default_state_dir()
    raw: dict[str, Any] = {}
    config_path = Path(state_dir) / "server.toml"
    target = Path(path) if path is not None else config_path
    if target.exists():
        with open(target, "rb") as handle:
            raw = tomllib.load(handle)
    if path is not None:
        raw.setdefault("state_dir", str(target.parent))
    if overrides:
        raw.update({k: v for k, v in overrides.items() if v is not None})
    return build_config(raw)


def build_config(raw: dict[str, Any]) -> ServerConfig:
    """Validate a mapping into a :class:`ServerConfig`, refusing unknown keys by name."""

    known = {f.name for f in dataclass_fields(ServerConfig)} - {"state_dir"}
    unknown = sorted(set(raw) - known - {"state_dir"})
    if unknown:
        raise AppError(
            f"Unknown server configuration keys: {', '.join(unknown)}.",
            status=422,
            code="invalid_configuration",
            field=unknown[0],
        )
    state_dir = Path(raw.get("state_dir") or default_state_dir()).expanduser().resolve()
    kwargs = {k: v for k, v in raw.items() if k in known}
    for name in ("allowed_roots", "cors_origins"):
        if name in kwargs:
            value = kwargs[name]
            # The declared type is a tuple: keep it a tuple whatever TOML gave us.
            kwargs[name] = (value,) if isinstance(value, str) else tuple(value or ())
    try:
        config = ServerConfig(state_dir=state_dir, **kwargs)
    except TypeError as exc:  # pragma: no cover - defensive, unknown keys rejected above
        raise AppError(f"Invalid server configuration: {exc}", status=422, code="invalid_configuration") from exc
    return validate_config(config)


def validate_config(config: ServerConfig) -> ServerConfig:
    """Enforce the safety invariants the plan fixes as defaults."""

    if not (1 <= config.bind_port <= 65535):
        raise AppError(
            f"bind_port must be between 1 and 65535, got {config.bind_port}.",
            status=422,
            code="invalid_configuration",
            field="bind_port",
        )
    remote_host = not host_is_loopback(config.bind_host)
    if remote_host and not config.allow_remote_binding:
        raise AppError(
            f"bind_host {config.bind_host!r} is not loopback. The server has no TLS and no "
            "network authentication layer: pass allow_remote_binding = true (or "
            "--i-understand-remote-binding) only behind a reverse proxy you control.",
            status=422,
            code="remote_binding_requires_consent",
            field="bind_host",
        )
    if config.metrics_enabled and config.metrics_host not in {"127.0.0.1", "localhost", "::1"}:
        raise AppError(
            "OpenMetrics has no authentication or TLS. metrics_host must stay loopback.",
            status=422,
            code="remote_metrics_requires_consent",
            field="metrics_host",
        )
    for name in (
        "max_open_handles",
        "readers_per_database",
        "max_write_sessions",
        "max_ws_sessions",
        "max_request_rows",
        "max_graph_nodes",
        "max_graph_edges",
        "max_query_value_characters",
    ):
        value = getattr(config, name)
        if not isinstance(value, int) or value < 1:
            raise AppError(
                f"{name} must be a positive integer, got {value!r}.",
                status=422,
                code="invalid_configuration",
                field=name,
            )
    if config.max_write_sessions > MAX_WRITE_SESSIONS_LIMIT:
        raise AppError(
            f"max_write_sessions must be at most {MAX_WRITE_SESSIONS_LIMIT}, got "
            f"{config.max_write_sessions}. The engine holds one writer lease per store, so more "
            "concurrent write sessions would only queue behind a lease that cannot be shared.",
            status=422,
            code="invalid_configuration",
            field="max_write_sessions",
        )
    if not config.allowed_roots:
        raise AppError(
            "allowed_roots is required: the server may only open stores under directories you "
            "declare explicitly.",
            status=422,
            code="allowed_roots_required",
            field="allowed_roots",
        )
    for root in config.allowed_roots:
        candidate = Path(root)
        if not candidate.is_absolute():
            raise PathPolicyError(f"allowed_roots entries must be absolute: {root!r}.", value=root)
        if candidate.exists() and (candidate.is_symlink() or not candidate.is_dir()):
            raise PathPolicyError(
                f"allowed_roots entry must be a real directory without symlink components: {root!r}.",
                value=root,
            )
    return config


def write_default_config(path: Path, *, allowed_roots: list[str], port: int = DEFAULT_BIND_PORT) -> Path:
    """Create a starter ``server.toml``; never overwrite an existing one."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path
    roots = "\n".join(f'    "{r}",' for r in allowed_roots) or '    "~/graphs",'
    path.write_text(
        "# okto-grafx-server configuration\n"
        f'bind_host = "{DEFAULT_BIND_HOST}"\n'
        f"bind_port = {port}\n"
        "\n"
        "# The server only opens stores inside these directories.\n"
        "allowed_roots = [\n"
        f"{roots}\n"
        "]\n"
        "\n"
        "max_open_handles = 4\n"
        "# Every handle gets its own page-cache budget, so memory grows with handles x\n"
        "# buffer_budget_bytes: the pool closes idle stores before it lets that product run away.\n"
        "readers_per_database = 2\n"
        "# The engine grants one writer lease per store: 1 serialises write sessions, 4 is the\n"
        "# ceiling, and anything higher would only queue behind a lease it cannot share.\n"
        "max_write_sessions = 1\n"
        "max_ws_sessions = 8\n"
        "max_request_rows = 1000\n"
        "max_graph_nodes = 2000\n"
        "max_graph_edges = 5000\n"
        "query_timeout_seconds = 30.0\n"
        "reader_lane_timeout_seconds = 5.0\n"
        "metrics_enabled = false\n"
        "# A store's own OpenMetrics sink has no auth and no TLS either: options that point it at "
        "# another machine are refused while this stays false.\n"
        "allow_remote_store_metrics = false\n"
        "cors_origins = []\n"
        "audit_retention_days = 30\n",
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------------------
# Path policy (mirrors okto_grafx.CatalogPathPolicy: allowlist, no symlinks, no UNC)
# --------------------------------------------------------------------------------------


def check_no_symlink(path: Path) -> None:
    """Reject a path with any symlink component, including its ancestors."""

    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise PathPolicyError(
                f"Path {str(path)!r} has a symlink component at {str(current)!r}; the server "
                "resolves stores without following links.",
                value=str(path),
            )


def resolve_store_parent(candidate: Path, roots: tuple[Path, ...]) -> Path:
    """Validate a directory that does not exist yet but must live under an allowed root."""

    text = str(candidate)
    if not text:
        raise PathPolicyError("A path is required.", value=text)
    if text.startswith("~"):
        raise PathPolicyError(
            "The server does not expand '~'; pass an absolute path.", value=text
        )
    if text.startswith("\\\\") or text.startswith("//"):
        raise PathPolicyError("UNC and remote URLs are refused.", value=text)
    path = Path(text)
    if not path.is_absolute():
        raise PathPolicyError(f"An absolute path is required, got {text!r}.", value=text)
    normalised = Path(os.path.normpath(str(path)))
    check_no_symlink(normalised)
    _require_allowed_root(normalised, roots)
    return normalised


def _require_allowed_root(path: Path, roots: tuple[Path, ...]) -> None:
    for root in roots:
        if root.is_symlink():
            continue
        normalised_root = Path(os.path.normpath(str(root)))
        try:
            common = os.path.commonpath([str(normalised_root), str(path)])
        except ValueError:
            continue
        if common == str(normalised_root) and path != normalised_root:
            return
    raise PathPolicyError(
        f"{str(path)!r} is not inside a configured allowed root. Add the parent directory to "
        "allowed_roots in server.toml.",
        value=str(path),
    )


def resolve_existing_store(candidate: Path, roots: tuple[Path, ...]) -> Path:
    """Validate an existing Grafx store directory and return its normalised path."""

    path = resolve_store_parent(candidate, roots)
    if not path.exists():
        raise PathPolicyError(f"{str(path)!r} does not exist.", value=str(path))
    if not path.is_dir():
        raise PathPolicyError(f"{str(path)!r} is not a directory.", value=str(path))
    if not (path / STORE_MARKER).exists():
        raise PathPolicyError(
            f"{str(path)!r} has no {STORE_MARKER}: it is not an Okto Grafx store. The server "
            "attaches existing stores only and never creates one from this door.",
            value=str(path),
        )
    return path


def describe_path(candidate: Path) -> dict[str, Any]:
    """Cheap, honest facts about a store directory for the inspect door."""

    total = 0
    files = 0
    for root, _dirs, names in os.walk(candidate):
        for name in names:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
            files += 1
    return {"path": str(candidate), "bytes": total, "files": files, "marker": STORE_MARKER}


# --------------------------------------------------------------------------------------
# DatabaseConfig option schema (the dynamic settings form)
# --------------------------------------------------------------------------------------

_GROUPS: dict[str, str] = {
    "page_size": "storage",
    "partitions_per_table": "storage",
    "identity_lease_size": "storage",
    "descriptor_revalidation": "storage",
    "read_only": "storage",
    "recovery_policy": "storage",
    "buffer_budget_bytes": "memory",
    "max_open_files": "memory",
    "index_key_cache_pages": "memory",
    "index_key_cache_bytes": "memory",
    "query_memory_budget_bytes": "memory",
    "wal_segment_bytes": "wal",
    "wal_max_bytes": "wal",
    "checkpoint_interval_records": "wal",
    "max_wal_batch_bytes": "wal",
    "lease_ttl_seconds": "concurrency",
    "lease_timeout_seconds": "concurrency",
    "commit_lock_timeout_seconds": "concurrency",
    "reader_stall_threshold_seconds": "concurrency",
    "max_statement_writes": "budgets",
    "max_result_rows": "budgets",
    "max_intermediate_rows": "budgets",
    "max_traversal_expansions": "budgets",
    "max_traversal_paths": "budgets",
    "max_transaction_rows": "budgets",
    "max_transaction_bytes": "budgets",
    "max_index_build_entries": "budgets",
    "max_query_value_characters": "budgets",
    "vector_exact_scan_threshold": "vector",
    "vector_ef_search": "vector",
    "vector_hnsw_memory_budget_bytes": "vector",
    "vector_hnsw_total_memory_budget_bytes": "vector",
    "vector_math": "vector",
    "automatic_index_expected_cardinality": "vector",
    "metrics": "metrics",
    "metrics_destination": "metrics",
    "allow_remote_metrics": "metrics",
    "codec": "engines",
    "checksum": "engines",
}

_HINTS: dict[str, str] = {
    "page_size": "Page size in bytes; a power of two fixed when the store is created.",
    "partitions_per_table": "Conflict partitions; fixed at creation by the store identity.",
    "buffer_budget_bytes": "Page cache budget for this handle. More handles multiply it.",
    "max_open_files": "Descriptor cache bound for this handle; the process limit is shared.",
    "recovery_policy": "'replay' recovers on open; 'refuse' refuses a damaged WAL tail.",
    "read_only": "Open read-only. Requires checkpoint-complete state and can refuse recent writes.",
    "metrics": "Metrics sink: noop, openmetrics or json.",
    "descriptor_revalidation": "'strict' revalidates descriptors; 'generation' is for a managed dir.",
    "vector_ef_search": "HNSW beam for approximate search.",
}

_BOUNDS: dict[str, dict[str, Any]] = {}


def _build_bounds() -> dict[str, dict[str, Any]]:
    from okto_grafx.runtime.config import (
        CHECKSUM_SELECTORS,
        DESCRIPTOR_REVALIDATION_MODES,
        MAX_PARTITIONS_PER_TABLE,
        MAX_PAGE_SIZE,
        MAX_VECTOR_EF_SEARCH,
        METRICS_SINKS,
        PAGE_CODEC_SELECTORS,
        RECOVERY_POLICIES,
        VECTOR_MATH_SELECTORS,
    )

    bounds: dict[str, dict[str, Any]] = {
        "page_size": {"minimum": 4096, "maximum": int(MAX_PAGE_SIZE)},
        "partitions_per_table": {"minimum": 1, "maximum": int(MAX_PARTITIONS_PER_TABLE)},
        "vector_ef_search": {"minimum": 1, "maximum": int(MAX_VECTOR_EF_SEARCH)},
        "recovery_policy": {"choices": sorted(RECOVERY_POLICIES)},
        "metrics": {"choices": sorted(METRICS_SINKS)},
        "codec": {"choices": sorted(PAGE_CODEC_SELECTORS)},
        "vector_math": {"choices": sorted(VECTOR_MATH_SELECTORS)},
        "checksum": {"choices": sorted(CHECKSUM_SELECTORS)},
        "descriptor_revalidation": {"choices": sorted(DESCRIPTOR_REVALIDATION_MODES)},
    }
    for name in ("lease_ttl_seconds", "lease_timeout_seconds", "commit_lock_timeout_seconds",
                 "reader_stall_threshold_seconds"):
        bounds[name] = {"minimum": 0.001}
    return bounds


_BOUNDS = _build_bounds()

_OPTIONAL_FIELDS = {
    "wal_max_bytes",
    "max_statement_writes",
    "max_result_rows",
    "max_intermediate_rows",
    "max_traversal_expansions",
    "max_traversal_paths",
    "max_transaction_rows",
    "max_transaction_bytes",
    "max_wal_batch_bytes",
    "max_index_build_entries",
    "automatic_index_expected_cardinality",
    "vector_hnsw_memory_budget_bytes",
    "vector_hnsw_total_memory_budget_bytes",
    "query_memory_budget_bytes",
}

_NOT_EDITABLE = {"path"}


def database_option_schema() -> list[dict[str, Any]]:
    """Every ``DatabaseConfig`` field as a form descriptor (name/type/default/bounds/group)."""

    out: list[dict[str, Any]] = []
    for info in dataclass_fields(DatabaseConfig):
        if info.name in _NOT_EDITABLE:
            continue
        # A frozen slots dataclass exposes a member_descriptor on the class, so the default is
        # read from the field definition, never from the class attribute.
        default = info.default
        if default is None and info.default_factory is not MISSING:  # type: ignore[misc]
            default = info.default_factory()  # type: ignore[misc]
        kind = "integer"
        if isinstance(default, bool):
            kind = "boolean"
        elif isinstance(default, float):
            kind = "number"
        elif isinstance(default, str):
            kind = "string"
        elif default is None:
            kind = "integer"
        entry: dict[str, Any] = {
            "name": info.name,
            "type": kind,
            "default": default,
            "nullable": info.name in _OPTIONAL_FIELDS,
            "group": _GROUPS.get(info.name, "other"),
            "label": info.name.replace("_", " "),
            "description": _HINTS.get(info.name, ""),
        }
        entry.update(_BOUNDS.get(info.name, {}))
        out.append(entry)
    return out


# Hosts the library itself treats as local for its metrics sink (a numeric loopback literal).
LOOPBACK_METRICS_HOSTS = frozenset({"127.0.0.1", "::1"})


def metrics_target_is_remote(options: dict[str, Any]) -> bool:
    """Would these store options push Grafx metrics off this machine?

    The library allows a remote OpenMetrics destination only with `allow_remote_metrics=True`, and it
    has no auth or TLS on that sink. The server adds a second gate, because these options arrive from
    a form and a config file rather than from a terminal where somebody typed them knowingly. The
    loopback test is the library's own: a numeric literal that is a loopback address.
    """

    if options.get("allow_remote_metrics"):
        return True
    if str(options.get("metrics", "")) != "openmetrics":
        # `json` writes a file and `noop` writes nothing: neither leaves the machine.
        return False
    destination = options.get("metrics_destination")
    if not isinstance(destination, str) or not destination.strip():
        return False  # no destination means the library's own loopback default
    host = destination.rsplit(":", 1)[0] if ":" in destination else destination
    if host.strip("[]") in LOOPBACK_METRICS_HOSTS:
        return False
    try:
        return not ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return True  # a host this server cannot prove local is treated as remote


def validate_store_options(
    options: dict[str, Any], *, allow_remote_metrics: bool = False
) -> dict[str, Any]:
    """Validate stored per-database ``connect`` overrides by opening nothing.

    ``connect()`` refuses an unknown option by name; the server refuses it earlier so a bad
    settings form never reaches a live store.
    """

    from okto_grafx.runtime.config import DatabaseConfig as _Config

    if not isinstance(options, dict):
        raise AppError("Database options must be an object.", status=422, code="invalid_options")
    allowed = {f.name for f in dataclass_fields(_Config)} - _NOT_EDITABLE
    unknown = sorted(set(options) - allowed)
    if unknown:
        raise AppError(
            f"Unknown database options: {', '.join(unknown)}.",
            status=422,
            code="unknown_database_options",
            field=unknown[0],
        )
    cleaned = {k: v for k, v in options.items() if v is not None}
    if not allow_remote_metrics and metrics_target_is_remote(cleaned):
        raise AppError(
            "These database options send the store's OpenMetrics somewhere outside this machine. "
            "The library's metrics sink has no authentication and no TLS, so the server refuses it "
            "until allow_remote_store_metrics = true says you meant it.",
            status=422,
            code="remote_store_metrics_requires_consent",
            field="metrics_destination",
        )
    try:
        _Config(path=":memory:", **cleaned)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 - typed configuration refusal surface
        from .errors import map_grafx_error

        raise map_grafx_error(exc) from exc
    return cleaned
