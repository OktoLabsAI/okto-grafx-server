#!/usr/bin/env bash
#
# Okto Grafx Server — uninstaller.
#
#   curl -fsSL https://raw.githubusercontent.com/OktoLabsAI/okto-grafx-server/v0.1.0/uninstall.sh | bash
#
# Removes what install.sh put there: the `oktografx-server` command and the environment it lives in.
#
# It deliberately does not touch the state directory (server.toml, server.db, the administrator token,
# the audit trail) or any Grafx store. A registration is a reference to a store that lives wherever you
# put it, and an uninstall should not quietly rewrite facts about your data. `--purge-state` removes the
# control plane on purpose — and still never deletes a store.
#
set -euo pipefail

BINARY="oktografx-server"
PREFIX="${OKG_PREFIX:-$HOME/.local}"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/okto-grafx-server"
STATE_DIR="${OKTO_GRAFX_SERVER_STATE:-}"
DRY=0
PURGE_STATE=0

usage() {
  cat <<'USAGE'
Okto Grafx Server uninstaller

options
  --prefix DIR     where the oktografx-server command lives (default: ~/.local)
  --state-dir DIR  the state directory, if it is not the default
  --purge-state    also remove the state directory: registrations, keys, audit trail, token
  --dry-run        print what would be removed, remove nothing
  -h, --help       this text
USAGE
}

log() { printf '\n\033[1m== %s\033[0m\n' "$1" >&2; }
say() { printf '   %s\n' "$1" >&2; }
fail() { printf '\n\033[31merror:\033[0m %s\n\n' "$1" >&2; exit 1; }

remove() {
  if [ "$DRY" = "1" ]; then
    printf '   [dry-run] rm -rf %s\n' "$1" >&2
    return 0
  fi
  rm -rf "$1"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="${2:?--prefix needs a value}"; shift 2 ;;
    --state-dir) STATE_DIR="${2:?--state-dir needs a value}"; shift 2 ;;
    --purge-state) PURGE_STATE=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      fail "unknown argument '$1'"
      ;;
  esac
done

if [ -z "$STATE_DIR" ]; then
  STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/okto-grafx-server"
fi

BIN_PATH="$PREFIX/bin/$BINARY"
METHOD="venv"
if [ -f "$DATA_DIR/install-method" ]; then
  METHOD="$(tr -d '[:space:]' <"$DATA_DIR/install-method")"
fi

log "Removing the command"
if [ -e "$BIN_PATH" ] || [ -L "$BIN_PATH" ]; then
  # What install.sh leaves here is either a symlink into our venv or uv's shim, whose shebang points at
  # the uv tool environment. Anything else living at this path belongs to somebody else and stays.
  RESOLVED="$(readlink "$BIN_PATH" 2>/dev/null || printf '%s' "$BIN_PATH")"
  SHEBANG="$(head -n 1 "$BIN_PATH" 2>/dev/null || true)"
  case "$RESOLVED:$SHEBANG" in
    "$DATA_DIR"/* | *"/venv/bin/$BINARY" | *"/uv/tools/okto-grafx-server/"*)
      remove "$BIN_PATH"
      if [ "$DRY" != "1" ]; then say "removed $BIN_PATH"; fi
      ;;
    *)
      say "left $BIN_PATH alone: it resolves to $RESOLVED, which this script did not create"
      ;;
  esac
else
  say "nothing at $BIN_PATH"
fi

log "Removing the environment (method: $METHOD)"
if [ "$METHOD" = "uv" ] && command -v uv >/dev/null 2>&1; then
  if [ "$DRY" = "1" ]; then
    printf '   [dry-run] uv tool uninstall okto-grafx-server\n' >&2
  elif uv tool uninstall okto-grafx-server 2>/dev/null; then
    say "uv removed its tool environment"
  else
    say "uv had no such tool installed"
  fi
fi
if [ -d "$DATA_DIR/venv" ]; then
  remove "$DATA_DIR/venv"
  if [ "$DRY" != "1" ]; then say "removed $DATA_DIR/venv"; fi
fi
remove "$DATA_DIR/install-method"
if [ -d "$DATA_DIR" ] && [ -z "$(ls -A "$DATA_DIR" 2>/dev/null)" ]; then
  remove "$DATA_DIR"
fi

if [ -f "$STATE_DIR/daemon.pid" ]; then
  PID="$(tr -d '[:space:]' <"$STATE_DIR/daemon.pid")"
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    log "A daemon is still running from this state directory"
    if [ "$DRY" = "1" ]; then
      printf '   [dry-run] kill %s\n' "$PID" >&2
    elif kill -TERM "$PID" 2>/dev/null; then
      say "stopped pid $PID"
    fi
    [ "$DRY" = "1" ] || rm -f "$STATE_DIR/daemon.pid"
  fi
fi

log "Left behind on purpose"
if [ -d "$STATE_DIR" ]; then
  say "state directory  $STATE_DIR"
  say "                 server.toml, server.db, server.log and admin-token: the registrations, API keys"
  say "                 and audit trail. Every registration is a *reference*, so your Grafx stores are"
  say "                 untouched wherever they are on disk."
else
  say "no state directory at $STATE_DIR"
fi

if [ "$PURGE_STATE" = "1" ]; then
  log "Purging the control plane"
  if [ -d "$STATE_DIR" ]; then
    say "removing $STATE_DIR — the stores it points at are not inside it and are not deleted"
    remove "$STATE_DIR"
  else
    say "nothing to remove"
  fi
elif [ -d "$STATE_DIR" ]; then
  say "to remove the control plane too, rerun with --purge-state (registrations, keys, audit, token)"
fi

log "Done"
if command -v "$BINARY" >/dev/null 2>&1; then
  say "$BINARY is still on PATH at $(command -v "$BINARY") — that copy was not made by this script"
fi
say "reinstall: curl -fsSL https://raw.githubusercontent.com/OktoLabsAI/okto-grafx-server/<tag>/install.sh | bash"
exit 0
