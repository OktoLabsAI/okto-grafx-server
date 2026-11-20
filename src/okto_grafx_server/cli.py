"""``oktografx-server`` — init, run, and the out-of-band credential doors.

``init`` prints the bootstrap admin token **once** and stores only its digest; if that output is
lost, ``admin-token rotate`` mints another (which revokes the old one). Commands that touch the
control database run while the daemon is stopped: they open the same SQLite file, which is fine
because it is WAL and single-writer, but the daemon owns the stores, so ``db add`` here never opens
one — that happens on first use by the running server.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .config import (
    DEFAULT_BIND_PORT,
    ServerConfig,
    default_state_dir,
    load_config,
    validate_store_options,
    write_default_config,
)
from .control.keys import generate_admin_token
from .control.store import ControlStore, valid_slug
from .errors import AppError, get_request_id

# Rotating file log inside the state directory: 5 MiB x 5 keeps a day of daemon chatter well
# under 30 MiB without ever needing an external logrotate for a laptop-sized deployment.
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5

# Fields the request middleware and job runner attach with `extra=`; anything else a caller logs
# stays in `message` rather than silently widening the log schema.
LOG_CONTEXT_FIELDS = (
    "request_id",
    "method",
    "path",
    "status",
    "duration_ms",
    "principal",
    "code",
    "slug",
    "job_id",
    "tool",
)


class _RedactSecrets(logging.Filter):
    """Scrub credentials from every line, whoever produced it.

    The daemon's own log lines never contain a credential, but third-party loggers propagate to
    the root logger too, and one of them may well record a full URL — and the WebSocket handshake
    carries its bearer in the query string. A rotating file in a state directory is not a secrets
    store, so anything that looks like a credential is rewritten before it is written.
    """

    _ASSIGNMENT = re.compile(
        r"(?i)\b(token|api[_-]?key|apikey|secret|password|authorization)\b(\s*[=:]\s*|\s+)([^\s,;&\"']+)"
    )
    _BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\./=]+")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed record is not a reason to lose the line
            return True
        scrubbed = self._ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)
        scrubbed = self._BEARER.sub("Bearer [redacted]", scrubbed)
        if scrubbed != text:
            record.msg, record.args = scrubbed, ()
        return True


class _JsonLineFormatter(logging.Formatter):
    """One JSON object per line, carrying the `request_id` of the turn that produced it."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003 - logging API
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in LOG_CONTEXT_FIELDS:
            value = getattr(record, field, None)
            if value is None and field == "request_id":
                value = get_request_id()
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _configure_logging(
    level: str,
    log_file: Path | None = None,
    *,
    max_bytes: int = LOG_MAX_BYTES,
    backup_count: int = LOG_BACKUP_COUNT,
) -> None:
    """Structured logs on stderr *and* a rotating file inside the state directory.

    The console keeps the human line an operator tails; the file holds one JSON object per line
    with the active `request_id`, so an incident report quoting a request id can be answered with
    `jq 'select(.request_id=="…")' server.log` instead of grep-and-hope.
    """

    numeric = getattr(logging, str(level).upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric)
    for handler in list(root.handlers):
        if getattr(handler, "_oktografx", False):
            root.removeHandler(handler)
            handler.close()

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    console.addFilter(_RedactSecrets())
    console._oktografx = True  # type: ignore[attr-defined]
    root.addHandler(console)

    if log_file is not None:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        rotating = RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        rotating.setFormatter(_JsonLineFormatter())
        rotating.addFilter(_RedactSecrets())
        rotating._oktografx = True  # type: ignore[attr-defined]
        root.addHandler(rotating)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oktografx-server",
        description="Daemon and web console for Okto Grafx embedded graph stores.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--state-dir", default=None, help="Where server.toml/server.db live.")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create the state directory and the admin token.")
    init.add_argument("--root", action="append", default=[], help="Allowed store root (repeatable).")
    init.add_argument("--port", type=int, default=DEFAULT_BIND_PORT)

    run = sub.add_parser("run", help="Run the daemon in the foreground.")
    run.add_argument("--host", default=None)
    run.add_argument("--port", type=int, default=None)
    run.add_argument(
        "--i-understand-remote-binding",
        action="store_true",
        help="Allow a non-loopback bind. There is no TLS here: use a reverse proxy.",
    )
    run.add_argument(
        "--i-understand-remote-store-metrics",
        action="store_true",
        help="Let database options point the store's OpenMetrics sink at another machine. "
        "That sink has no authentication and no TLS.",
    )

    sub.add_parser("status", help="Registered stores and whether they are open.")
    sub.add_parser("config", help="Print the effective configuration as JSON.")

    db = sub.add_parser("db", help="Manage registered stores.")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    add = db_sub.add_parser("add", help="Attach an existing store by reference (never a copy).")
    add.add_argument("path")
    add.add_argument("--slug", required=True)
    add.add_argument("--name", default=None)
    add.add_argument("--read-only", dest="read_only", action="store_true", default=True)
    add.add_argument("--writable", dest="read_only", action="store_false")
    add.add_argument("--allow-writes", action="store_true")
    add.add_argument("--open-policy", choices=("lazy", "pinned"), default="lazy")
    add.add_argument("--option", action="append", default=[], metavar="NAME=VALUE")
    add.add_argument(
        "--i-understand-remote-store-metrics",
        action="store_true",
        help="Accept options that send this store's metrics off the machine (no auth, no TLS).",
    )
    db_sub.add_parser("list", help="List registered stores.")
    remove = db_sub.add_parser("remove", help="Forget a registration; the files are untouched.")
    remove.add_argument("slug")

    key = sub.add_parser("key", help="Manage API keys.")
    key_sub = key.add_subparsers(dest="key_command", required=True)
    create = key_sub.add_parser("create", help="Create a key scoped to explicit databases.")
    create.add_argument("name")
    create.add_argument("--db", action="append", default=[], help="Database slug (repeatable).")
    create.add_argument("--allow-writes", action="store_true")
    key_sub.add_parser("list", help="List keys (never their secrets).")
    revoke = key_sub.add_parser("revoke", help="Revoke a key by id.")
    revoke.add_argument("key_id", type=int)

    token = sub.add_parser("admin-token", help="Rotate the administrator bootstrap token.")
    token_sub = token.add_subparsers(dest="token_command", required=True)
    token_sub.add_parser("rotate", help="Mint a new token and invalidate the old one.")

    return parser


def _store_options(pairs: Sequence[str], *, allow_remote_metrics: bool = False) -> dict[str, Any]:
    options: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--option expects NAME=VALUE, got {pair!r}")
        name, raw = pair.split("=", 1)
        try:
            value: Any = json.loads(raw)
        except ValueError:
            value = raw
        options[name.strip()] = value
    return validate_store_options(options, allow_remote_metrics=allow_remote_metrics)


def _config(args: argparse.Namespace) -> ServerConfig:
    overrides: dict[str, Any] = {}
    state_dir = getattr(args, "state_dir", None)
    if state_dir:
        overrides["state_dir"] = state_dir
    if getattr(args, "host", None):
        overrides["bind_host"] = args.host
    if getattr(args, "port", None):
        overrides["bind_port"] = args.port
    if getattr(args, "i_understand_remote_binding", False):
        overrides["allow_remote_binding"] = True
    if getattr(args, "i_understand_remote_store_metrics", False):
        overrides["allow_remote_store_metrics"] = True
    # ``--state-dir`` picks which server.toml to read, not merely where files are written: an
    # operator running two daemons must not have the second one read the first one's ports.
    path = Path(state_dir).expanduser() / "server.toml" if state_dir else None
    return load_config(path, overrides=overrides or None)


def _open_store(config: ServerConfig) -> ControlStore:
    return ControlStore(config.server_db_path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.log_level)
    try:
        return _dispatch(args, parser)
    except AppError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.command == "init":
        return _cmd_init(args)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "config":
        config = _config(args)
        print(json.dumps(_config_json(config), indent=2))
        return 0
    if args.command == "db":
        return _cmd_db(args)
    if args.command == "key":
        return _cmd_key(args)
    if args.command == "admin-token":
        return _cmd_admin_token(args)
    parser.error(f"unknown command {args.command}")
    return 2


def _cmd_init(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir or default_state_dir()).expanduser().resolve()
    roots = [str(Path(root).expanduser().resolve()) for root in args.root] or [str(Path.home() / "grafx")]
    config_path = write_default_config(state_dir / "server.toml", allowed_roots=roots, port=args.port)
    config = load_config(config_path)
    store = _open_store(config)
    # Re-running init in a state directory that already has a token is an operator action with a
    # consequence: the previous token stops working. Say so, because the old secret is usually the
    # thing that got lost in the first place.
    if store.admin_token_configured():
        print(
            f"note: {config.server_db_path} already has an administrator token; this mints a new "
            "one and the old one stops working (`admin-token rotate` does the same on purpose).",
            file=sys.stderr,
        )
    token = generate_admin_token()
    store.set_admin_token(token)
    store.close()
    print(f"state directory: {state_dir}")
    print(f"configuration:   {config_path}")
    print(f"control database:{config.server_db_path}")
    print(f"console:         http://{config.bind_host}:{config.bind_port}/")
    print("\nADMIN TOKEN (shown once, store it now):")
    print(f"  {token}")
    print("\nSign in at the console, or call:")
    print(f"  curl -X POST http://{config.bind_host}:{config.bind_port}/admin/api/login \\")
    print(f"       -H 'content-type: application/json' -d '{{\"token\":\"{token}\"}}'")
    print(f"\nEdit {config_path} to set allowed_roots, then run:")
    print("  oktografx-server run")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    import uvicorn

    config = _config(args)
    _configure_logging(args.log_level, config.log_path)
    store = _open_store(config)
    if not store.admin_token_configured():
        token = generate_admin_token()
        store.set_admin_token(token)
        print(
            "No admin token was configured; a new one was created and is shown once:\n  " + token,
            file=sys.stderr,
        )
    store.close()
    print(
        f"okto-grafx-server {__version__} listening on http://{config.bind_host}:{config.bind_port} "
        f"(state: {config.state_dir})",
        file=sys.stderr,
    )
    # uvicorn imports the factory in a fresh context: tell it which state directory this process
    # resolved, or the daemon would serve the default one.
    os.environ["OKTO_GRAFX_SERVER_STATE"] = str(config.state_dir)
    uvicorn.run(
        "okto_grafx_server.cli:create_asgi_app",
        factory=True,
        host=config.bind_host,
        port=config.bind_port,
        log_level="info",
        log_config=None,
        ws="websockets",
        access_log=False,
    )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    config = _config(args)
    store = _open_store(config)
    databases = store.list_databases()
    keys = store.list_keys()
    print(f"state: {config.state_dir}")
    print(f"bind:  http://{config.bind_host}:{config.bind_port}")
    print(f"roots: {', '.join(config.allowed_roots) or '(none)'}")
    print(f"databases: {len(databases)}, api keys: {len(keys)}")
    for record in databases:
        print(
            f"  {record.slug:<24} {'ro' if record.read_only else 'rw'}"
            f"{'/w' if record.allow_writes else '  '} {record.path}"
        )
    for key in keys:
        state = "revoked" if key.revoked_at else "active"
        print(f"  key #{key.id} {key.name:<20} {state:<8} dbs={','.join(key.databases) or '-'}")
    store.close()
    return 0


def _cmd_db(args: argparse.Namespace) -> int:
    config = _config(args)
    store = _open_store(config)
    try:
        if args.db_command == "list":
            for record in store.list_databases():
                print(json.dumps(record.public()))
            return 0
        if args.db_command == "add":
            from .engine.handles import HandlePool
            from .engine.jobs import JobRunner
            from .engine.runner import EngineRunner
            from .engine.service import DatabaseService

            pool = HandlePool(max_open_handles=config.max_open_handles,
                              readers_per_database=config.readers_per_database)
            service = DatabaseService(
                store=store, pool=pool, allowed_roots=config.allowed_roots
            )
            record = service.attach(
                raw_path=args.path,
                slug=valid_slug(args.slug),
                name=args.name,
                read_only=args.read_only,
                allow_writes=args.allow_writes,
                options=_store_options(
                    args.option, allow_remote_metrics=config.allow_remote_store_metrics
                ),
                open_policy=args.open_policy,
                note="added from the command line",
            )
            print(json.dumps(record.public(), indent=2))
            return 0
        if args.db_command == "remove":
            record = store.remove_database(args.slug)
            print(json.dumps({"removed": args.slug, "path_untouched": record.path}))
            return 0
    finally:
        store.close()
    return 2


def _cmd_key(args: argparse.Namespace) -> int:
    config = _config(args)
    store = _open_store(config)
    try:
        if args.key_command == "list":
            for key in store.list_keys():
                print(json.dumps(key.public()))
            return 0
        if args.key_command == "create":
            record, secret = store.create_key(
                name=args.name, databases=args.db, allow_writes=args.allow_writes
            )
            print(json.dumps({**record.public(), "key": secret, "shown_once": True}, indent=2))
            return 0
        if args.key_command == "revoke":
            print(json.dumps(store.revoke_key(args.key_id).public(), indent=2))
            return 0
    finally:
        store.close()
    return 2


def _cmd_admin_token(args: argparse.Namespace) -> int:
    config = _config(args)
    store = _open_store(config)
    try:
        token = generate_admin_token()
        store.set_admin_token(token)
        store.prune_sessions()
        print("A new admin token was created; the previous token and all sessions are invalid:")
        print(f"  {token}")
        return 0
    finally:
        store.close()


def _config_json(config: ServerConfig) -> dict[str, Any]:
    return {
        "state_dir": str(config.state_dir),
        "bind_host": config.bind_host,
        "bind_port": config.bind_port,
        "allowed_roots": list(config.allowed_roots),
        "max_open_handles": config.max_open_handles,
        "readers_per_database": config.readers_per_database,
        "max_write_sessions": config.max_write_sessions,
        "max_ws_sessions": config.max_ws_sessions,
        "max_request_rows": config.max_request_rows,
        "max_graph_nodes": config.max_graph_nodes,
        "max_graph_edges": config.max_graph_edges,
        "query_timeout_seconds": config.query_timeout_seconds,
        "metrics_enabled": config.metrics_enabled,
        "allow_remote_store_metrics": config.allow_remote_store_metrics,
        "cors_origins": list(config.cors_origins),
    }


def create_asgi_app() -> Any:
    """The ASGI factory uvicorn imports; the config is resolved from the state directory."""

    from .app import create_app

    config = load_config()
    return create_app(config=config)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
