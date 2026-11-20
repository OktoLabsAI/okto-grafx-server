#!/usr/bin/env bash
#
# Okto Grafx Server — installer.
#
#   curl -fsSL https://raw.githubusercontent.com/OktoLabsAI/okto-grafx-server/v0.2.0/install.sh | bash
#
# What it does, in this order:
#
#   1. checks there is a Python ≥ 3.11, or a uv that can supply one;
#   2. downloads the published wheel for one fixed version and verifies its SHA-256 against the
#      release's SHA256SUMS;
#   3. installs it into an environment of its own — `uv tool install` when uv is present, otherwise a
#      venv under ~/.local/share — and puts `oktografx-server` in ~/.local/bin;
#   4. creates the state directory, but only when there is not one already: re-running `init` would
#      mint a new administrator token over the old one, and an installer that surprises you about
#      that is worse than one that stops;
#   5. starts the daemon on 127.0.0.1 and waits for /api/v1/health;
#   6. tells you what opened. By default that is: no token, and the console answers this machine only.
#      With --with-admin-token it prints the token once, keeps a copy at <state>/admin-token (0600),
#      and names the command that mints a new one if that file is ever lost.
#
# Nothing runs as root, nothing is written outside $HOME unless you pass --prefix, and --dry-run prints
# every command without touching the filesystem. Flags through a pipe need `bash -s --`:
#
#   curl -fsSL <url> | bash -s -- --port 8899 --no-run
#
# About the checksum: it comes from the same release as the wheel, so it proves the download was not
# truncated or altered in transit, not that the release is authentic. Pin the URL to a tag (as above),
# or download the two files and check them yourself before running anything.
#
set -euo pipefail

VERSION="0.2.0"
REPO="OktoLabsAI/okto-grafx-server"
BINARY="oktografx-server"
PACKAGE="okto_grafx_server"

PREFIX="${OKG_PREFIX:-$HOME/.local}"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/okto-grafx-server"
STATE_DIR="${OKTO_GRAFX_SERVER_STATE:-}"
GRAPH_ROOT="${OKG_GRAPH_ROOT:-$HOME/graphs}"
PORT="${OKG_PORT:-8737}"
INSTALLER_WANTED="auto"
PYTHON_BIN=""
WHEEL=""
FROM_TREE=""
DRY=0
DO_INIT=1
DO_RUN=1
WRITE_TOKEN_FILE=1
WITH_TOKEN=0

usage() {
  cat <<'USAGE'
Okto Grafx Server installer

  curl -fsSL https://raw.githubusercontent.com/OktoLabsAI/okto-grafx-server/<tag>/install.sh \
    | bash -s -- [options]

options
  --version V        version to install (default: the one this script was published with)
  --prefix DIR       where the oktografx-server command goes (default: ~/.local)
  --state-dir DIR    where server.toml / server.db live (default: ~/.local/state/okto-grafx-server)
  --root DIR         allowed store root passed to init (default: ~/graphs, created if missing)
  --port PORT        loopback port (default: 8737)
  --python PATH      interpreter to build the venv with (needs ≥ 3.11)
  --installer NAME   auto | uv | pip
  --wheel FILE       install this wheel instead of downloading one
  --from-tree DIR    install this source tree instead of a wheel (development, CI)
  --no-init          install the command only, create no state directory
  --no-run           do not start the daemon
  --with-admin-token require an administrator token. Without it the console is open to this machine
                     only, which is the default: nothing is minted and nothing is printed
  --no-token-file    with --with-admin-token, print the token instead of also writing <state>/admin-token
  --dry-run          print the commands, change nothing
  -h, --help         this text

environment
  OKG_PREFIX       default for --prefix
  OKG_GRAPH_ROOT   default for --root
  OKG_PORT         default for --port
  OKG_BASE_URL     release directory to download from (mirror, or file:// for an airgapped install);
                   by default the GitHub release for this version, over https only
USAGE
}

log() { printf '\n\033[1m== %s\033[0m\n' "$1" >&2; }
say() { printf '   %s\n' "$1" >&2; }
fail() { printf '\n\033[31merror:\033[0m %s\n\n' "$1" >&2; exit 1; }

run() {
  if [ "$DRY" = "1" ]; then
    printf '   [dry-run] %s\n' "$*" >&2
    return 0
  fi
  "$@"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="${2:?--version needs a value}"; shift 2 ;;
    --prefix) PREFIX="${2:?--prefix needs a value}"; shift 2 ;;
    --state-dir) STATE_DIR="${2:?--state-dir needs a value}"; shift 2 ;;
    --root) GRAPH_ROOT="${2:?--root needs a value}"; shift 2 ;;
    --port) PORT="${2:?--port needs a value}"; shift 2 ;;
    --python) PYTHON_BIN="${2:?--python needs a value}"; shift 2 ;;
    --installer) INSTALLER_WANTED="${2:?--installer needs a value}"; shift 2 ;;
    --wheel) WHEEL="${2:?--wheel needs a value}"; shift 2 ;;
    --from-tree) FROM_TREE="${2:?--from-tree needs a value}"; shift 2 ;;
    --no-init) DO_INIT=0; shift ;;
    --no-run) DO_RUN=0; shift ;;
    --no-token-file) WRITE_TOKEN_FILE=0; shift ;;
    --with-admin-token) WITH_TOKEN=1; shift ;;
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

case "$INSTALLER_WANTED" in
  auto | uv | pip) ;;
  *) fail "--installer must be auto, uv or pip" ;;
esac
if [ -z "$STATE_DIR" ]; then
  STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/okto-grafx-server"
fi
case "$PORT" in
  '' | *[!0-9]*) fail "--port must be a number" ;;
esac

log "Okto Grafx Server ${VERSION}"
say "command →   $PREFIX/bin/$BINARY"
say "state       $STATE_DIR"
say "store root  $GRAPH_ROOT"
say "console     http://127.0.0.1:$PORT/"
if [ "$DRY" = "1" ]; then
  say "dry run     nothing is executed or created"
fi

if ! command -v curl >/dev/null 2>&1; then
  fail "curl is required to fetch the release"
fi

# --------------------------------------------------------------------------------- interpreter
USING_UV=0
if [ "$INSTALLER_WANTED" = "pip" ]; then
  :
elif [ "$INSTALLER_WANTED" = "uv" ] || command -v uv >/dev/null 2>&1; then
  if ! command -v uv >/dev/null 2>&1; then
    fail "--installer uv, but uv is not on PATH"
  fi
  USING_UV=1
  say "installer   uv $(uv --version 2>/dev/null | awk '{print $2}') — it supplies a Python ≥ 3.11 of its own"
fi

if [ "$USING_UV" = "0" ]; then
  if [ -z "$PYTHON_BIN" ]; then
    for candidate in python3.13 python3.12 python3.11 python3; do
      if command -v "$candidate" >/dev/null 2>&1 &&
        "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        PYTHON_BIN="$(command -v "$candidate")"
        break
      fi
    done
  fi
  if [ -z "$PYTHON_BIN" ]; then
    fail "no Python ≥ 3.11 found. Install one, or install uv (https://docs.astral.sh/uv/) and run this again."
  fi
  say "installer   venv + pip ($("$PYTHON_BIN" --version 2>&1))"
fi

# --------------------------------------------------------------------------------- artifact
WORK="$(mktemp -d "${TMPDIR:-/tmp}/okg-install.XXXXXX")"
if [ "$DRY" != "1" ]; then
  cleanup() { rm -rf "$WORK"; }
  trap cleanup EXIT
fi

TARGET="$FROM_TREE"
if [ -n "$FROM_TREE" ]; then
  if [ ! -d "$FROM_TREE" ]; then
    fail "--from-tree is not a directory: $FROM_TREE"
  fi
  say "source      working tree $FROM_TREE"
elif [ -n "$WHEEL" ]; then
  if [ ! -f "$WHEEL" ]; then
    fail "--wheel is not a file: $WHEEL"
  fi
  TARGET="$WHEEL"
  say "source      $WHEEL"
else
  WHEEL_NAME="${PACKAGE}-${VERSION}-py3-none-any.whl"
  BASE_URL="${OKG_BASE_URL:-https://github.com/${REPO}/releases/download/v${VERSION}}"
  TARGET="$WORK/$WHEEL_NAME"
  # Pin the transport for the default https endpoint. An explicit mirror (OKG_BASE_URL, e.g. an internal
  # artefact proxy, or a file:// directory for an airgapped machine) is the operator's call, so the pin
  # is only asserted on https URLs.
  CURL=(curl -fL --retry 3)
  case "$BASE_URL" in
    https://*) CURL+=(--proto '=https' --tlsv1.2) ;;
  esac
  log "Downloading $WHEEL_NAME"
  say "from        $BASE_URL"
  if ! run "${CURL[@]}" -o "$WORK/$WHEEL_NAME" "$BASE_URL/$WHEEL_NAME"; then
    fail "could not download $BASE_URL/$WHEEL_NAME — is v$VERSION published? See https://github.com/${REPO}/releases, or pass --version / --wheel."
  fi
  if ! run "${CURL[@]}" -o "$WORK/SHA256SUMS" "$BASE_URL/SHA256SUMS"; then
    fail "could not download $BASE_URL/SHA256SUMS — refusing to install a wheel it cannot verify."
  fi
  if [ "$DRY" != "1" ]; then
    if command -v sha256sum >/dev/null 2>&1; then
      CHECKSUM=(sha256sum)
    elif command -v shasum >/dev/null 2>&1; then
      CHECKSUM=(shasum -a 256)
    else
      fail "neither sha256sum nor shasum is available, so the download cannot be verified"
    fi
    # The manifest lists the sdist too, which we never fetch, so it is read by hand instead of with
    # `-c`: GNU coreutils treats that missing line as a failure and would refuse a perfectly good wheel.
    EXPECTED="$(awk -v f="$WHEEL_NAME" '{ p = $2; sub(/^.*\//, "", p); if (p == f) { print $1; exit } }' "$WORK/SHA256SUMS")"
    ACTUAL="$("${CHECKSUM[@]}" "$TARGET" | awk '{print $1}')"
    if [ -z "$EXPECTED" ]; then
      fail "the release's SHA256SUMS has no line for $WHEEL_NAME — refusing to install it unverified"
    fi
    if [ "$EXPECTED" != "$ACTUAL" ]; then
      printf '\n\033[31merror:\033[0m SHA-256 mismatch for %s\n         expected %s\n         got      %s\n         Refusing to install it. If the release was republished, re-check the URL you pinned.\n\n' "$WHEEL_NAME" "$EXPECTED" "$ACTUAL" >&2
      exit 1
    fi
    say "checksum    $ACTUAL (matches SHA256SUMS)"
  fi
fi

# --------------------------------------------------------------------------------- install
BIN_PATH="$PREFIX/bin/$BINARY"
if [ "$DRY" != "1" ]; then
  mkdir -p "$PREFIX/bin"
fi

if [ "$USING_UV" = "1" ]; then
  log "Installing with uv"
  # uv keeps its own tool bin; the environment variable is the portable way to point it at --prefix
  # (`uv tool install` has no --bin-dir).
  export UV_TOOL_BIN_DIR="$PREFIX/bin"
  if [ -n "$FROM_TREE" ]; then
    run uv tool install --reinstall --editable "$FROM_TREE"
  else
    run uv tool install --reinstall "$TARGET"
  fi
  if [ "$DRY" != "1" ]; then
    RESOLVED="$PREFIX/bin/$BINARY"
    if [ ! -x "$RESOLVED" ]; then
      RESOLVED="$(command -v "$BINARY" || true)"
    fi
    if [ -z "$RESOLVED" ] || [ ! -x "$RESOLVED" ]; then
      fail "uv reported success but no executable $BINARY was found"
    fi
    if [ ! -x "$BIN_PATH" ]; then
      ln -sfn "$RESOLVED" "$BIN_PATH"
    fi
    mkdir -p "$DATA_DIR"
    printf 'uv\n' >"$DATA_DIR/install-method"
  fi
else
  log "Installing into $DATA_DIR/venv"
  if [ "$DRY" = "1" ]; then
    printf '   [dry-run] %s -m venv %s/venv\n' "$PYTHON_BIN" "$DATA_DIR"
    printf '   [dry-run] pip install --upgrade pip setuptools wheel\n'
    printf '   [dry-run] pip install %s\n' "$TARGET"
    printf '   [dry-run] ln -sfn %s/venv/bin/%s %s\n' "$DATA_DIR" "$BINARY" "$BIN_PATH"
  else
    mkdir -p "$DATA_DIR"
    if [ ! -d "$DATA_DIR/venv" ]; then
      "$PYTHON_BIN" -m venv "$DATA_DIR/venv"
    fi
    "$DATA_DIR/venv/bin/python" -m pip install --quiet --upgrade pip setuptools wheel
    "$DATA_DIR/venv/bin/python" -m pip install --quiet "$TARGET"
    ln -sfn "$DATA_DIR/venv/bin/$BINARY" "$BIN_PATH"
    printf 'venv\n' >"$DATA_DIR/install-method"
  fi
fi

if [ "$DRY" != "1" ]; then
  if [ ! -x "$BIN_PATH" ]; then
    fail "installed, but $BIN_PATH is not executable"
  fi
  say "command     $("$BIN_PATH" --version 2>&1 | head -1)"
  say "            $BIN_PATH"
  case ":$PATH:" in
    *":$PREFIX/bin:"*) ;;
    *) say "note        $PREFIX/bin is not on PATH — add it, or call the full path" ;;
  esac
fi

# --------------------------------------------------------------------------------- state
TOKEN=""
if [ "$DO_INIT" = "1" ]; then
  if [ -f "$STATE_DIR/server.toml" ]; then
    log "State directory already exists"
    say "leaving $STATE_DIR untouched"
    say "'init' a second time would mint a new administrator token over yours;"
    say "require one deliberately with: $BINARY --state-dir $STATE_DIR admin-token rotate"
  else
    log "Creating the state directory"
    if [ "$DRY" = "1" ]; then
      printf '   [dry-run] mkdir -p %s\n' "$GRAPH_ROOT"
      if [ "$WITH_TOKEN" = "1" ]; then
        printf '   [dry-run] %s --state-dir %s init --root %s --port %s --with-admin-token\n' "$BIN_PATH" "$STATE_DIR" "$GRAPH_ROOT" "$PORT"
      else
        printf '   [dry-run] %s --state-dir %s init --root %s --port %s\n' "$BIN_PATH" "$STATE_DIR" "$GRAPH_ROOT" "$PORT"
      fi
    else
      # `init` records allowed_roots; a root that does not exist yet would make every later `db add`
      # under it a refusal, so the installer creates the one it just promised.
      mkdir -p "$GRAPH_ROOT"
      # And it records the *resolved* path, because the daemon refuses to follow a symlinked component:
      # on macOS `/tmp` is a link, and a root stored as `/tmp/...` would reject every store underneath it
      # with a message about symlinks that points nowhere near the installer.
      RESOLVED_ROOT="$(cd "$GRAPH_ROOT" && pwd -P)"
      if [ "$RESOLVED_ROOT" != "$GRAPH_ROOT" ]; then
        say "store root  $RESOLVED_ROOT (symlinks resolved from $GRAPH_ROOT)"
      fi
      GRAPH_ROOT="$RESOLVED_ROOT"
      if [ "$WITH_TOKEN" = "1" ]; then
        if ! INIT_OUT="$("$BIN_PATH" --state-dir "$STATE_DIR" init --root "$GRAPH_ROOT" --port "$PORT" --with-admin-token 2>&1)"; then
          printf '%s\n' "$INIT_OUT" | sed 's/^/   /' >&2
          fail "'init' refused to create $STATE_DIR"
        fi
        # awk over a here-string rather than `printf | sed | head`: the token line is the only thing
        # we want out of init's output, and a pipeline whose reader quits early is a SIGPIPE waiting
        # to be mistaken for a failed command under `set -e`/`pipefail`.
        TOKEN="$(awk '{ if (match($0, /okga_[A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-][A-Za-z0-9_-]+/)) { print substr($0, RSTART, RLENGTH); exit } }' <<<"$INIT_OUT")"
        if [ -z "$TOKEN" ]; then
          printf '%s\n' "$INIT_OUT" | sed 's/^/   /' >&2
          fail "init succeeded but printed no administrator token"
        fi
        if [ "$WRITE_TOKEN_FILE" = "1" ]; then
          (
            umask 177
            printf '%s\n' "$TOKEN" >"$STATE_DIR/admin-token"
          )
          say "token file  $STATE_DIR/admin-token (mode 600)"
        fi
      else
        if ! INIT_OUT="$("$BIN_PATH" --state-dir "$STATE_DIR" init --root "$GRAPH_ROOT" --port "$PORT" 2>&1)"; then
          printf '%s\n' "$INIT_OUT" | sed 's/^/   /' >&2
          fail "'init' refused to create $STATE_DIR"
        fi
        say "no administrator token minted: the console will answer this machine only"
      fi
      say "server.toml and server.db written"
    fi
  fi
fi

# --------------------------------------------------------------------------------- start
DAEMON_RUNNING=0
if [ "$DO_RUN" = "1" ]; then
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/v1/health" >/dev/null 2>&1; then
    log "Something already answers on 127.0.0.1:$PORT"
    DAEMON_RUNNING=1
    say "not starting a second daemon; stop the existing one first if you meant to replace it"
  elif [ "$DRY" = "1" ]; then
    printf '   [dry-run] nohup %s --state-dir %s run --port %s &\n' "$BIN_PATH" "$STATE_DIR" "$PORT"
  else
    log "Starting the daemon"
    nohup "$BIN_PATH" --state-dir "$STATE_DIR" run --port "$PORT" >>"$STATE_DIR/server.stdout" 2>&1 &
    printf '%s\n' "$!" >"$STATE_DIR/daemon.pid"
    for _ in $(seq 1 60); do
      if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/v1/health" >/dev/null 2>&1; then
        DAEMON_RUNNING=1
        break
      fi
      sleep 0.5
    done
    if [ "$DAEMON_RUNNING" != "1" ]; then
      fail "the daemon did not answer on 127.0.0.1:$PORT — see $STATE_DIR/server.stdout"
    fi
    say "pid $(cat "$STATE_DIR/daemon.pid") · /api/v1/health is answering"
  fi
fi

# --------------------------------------------------------------------------------- handover
log "Ready"
say "console     http://127.0.0.1:$PORT/"
TOKEN_SHOWN=0
if [ -n "$TOKEN" ]; then
  say "token       $TOKEN"
  say "            minted now, printed once by the server; the copy is ${STATE_DIR}/admin-token (600)"
  TOKEN_SHOWN=1
elif [ -f "$STATE_DIR/admin-token" ]; then
  # Showing it again is the point. An installer that says "it was printed once, sometime, in an earlier
  # run" leaves the operator to guess where the value went, and the value is already in a file this user
  # owns and can read.
  say "token       $(tr -d '[:space:]' <"$STATE_DIR/admin-token")"
  say "            minted by an earlier run; this is the copy in ${STATE_DIR}/admin-token (600)"
  TOKEN_SHOWN=1
else
  # Nothing to show. Whether that means "no token exists" or "one exists and you have lost it" is the
  # server's answer, not ours to guess: the control database knows, a terminal scrollback does not.
  TOKEN_REQUIRED=0
  if [ "$DRY" != "1" ] && [ -x "$BIN_PATH" ] \
    && "$BIN_PATH" --state-dir "$STATE_DIR" admin-token status 2>/dev/null | grep -q "a token is required"; then
    TOKEN_REQUIRED=1
  fi
  if [ "$TOKEN_REQUIRED" = "1" ]; then
    say "token       required, and not in ${STATE_DIR}/admin-token. The server keeps only a hash, so"
    say "            nothing can be recovered from it: mint one you can use"
    say "            $BINARY --state-dir $STATE_DIR admin-token rotate"
  else
    say "access      no token required. The console answers this machine only — any process that can"
    say "            reach 127.0.0.1 is administrator here, anything from elsewhere is refused"
    say "require one $BINARY --state-dir $STATE_DIR admin-token rotate"
    say "            or the same switch in Settings, which shows the value once"
  fi
fi
if [ "$TOKEN_SHOWN" = "1" ]; then
  say "login       open http://127.0.0.1:$PORT/ and paste it into the sign-in screen"
  say "lost it?    $BINARY --state-dir $STATE_DIR admin-token rotate"
  say "            mints a new token and invalidates the old one, and every session signed in with it"
fi
say "attach      $BINARY db add /path/to/store.grafx --slug mystore --read-only"
say "stores      $BINARY status"
say "stop        kill \$(cat ${STATE_DIR}/daemon.pid)"
say "logs        ${STATE_DIR}/server.log"
say "uninstall   curl -fsSL https://raw.githubusercontent.com/${REPO}/v${VERSION}/uninstall.sh | bash"
printf '\n   The store stays embedded and on local disk. This daemon holds it open for you; a local\n'
printf '   process with filesystem access can open the same store directly, so what sits here is\n'
printf '   logical access control, not an operating-system sandbox. Read-only until you say otherwise.\n'
exit 0
