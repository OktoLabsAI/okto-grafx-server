#!/usr/bin/env bash
#
# Shared driver for install.sh and uninstall.sh.
#
# CI runs it against the working tree; the release workflow runs it against the published artifacts.
# Same script, so "the installer passed in CI" and "the installer you are about to pipe into bash" mean
# the same thing. Every assertion below is something an operator would notice: the command runs, the
# token gets printed once and not re-minted on a rerun, the console answers, an attached store answers a
# query, and uninstalling leaves the data alone.
#
# env
#   OKGIT_INSTALL     path to install.sh      (default ./install.sh)
#   OKGIT_UNINSTALL   path to uninstall.sh    (default ./uninstall.sh)
#   OKGIT_SOURCE      extra install.sh arguments, e.g. "--from-tree /path/to/repo".
#                     Empty means: download the release and verify its checksum, like an operator.
#   OKGIT_PREFIX      --prefix          (default $TMPDIR/oktografx-installer-test/prefix)
#   OKGIT_STATE       --state-dir       (default .../state)
#   OKGIT_ROOT        --root            (default .../graphs)
#   OKGIT_PORT        --port            (default 8791)
#   OKGIT_INSTALLER   auto | uv | pip   (default auto)
#   OKGIT_PYTHON      --python          (default python3)
#
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$HERE")"

OKGIT_INSTALL="${OKGIT_INSTALL:-$REPO_ROOT/install.sh}"
OKGIT_UNINSTALL="${OKGIT_UNINSTALL:-$REPO_ROOT/uninstall.sh}"
OKGIT_SOURCE="${OKGIT_SOURCE:-}"
OKGIT_PORT="${OKGIT_PORT:-8791}"
OKGIT_INSTALLER="${OKGIT_INSTALLER:-auto}"
OKGIT_PYTHON="${OKGIT_PYTHON:-python3}"
# macOS ships /bin/bash 3.2 and GitHub's macOS runners use it, so the scripts have to work there, not
# only in the bash 5 somebody has installed. Override to try another interpreter.
OKGIT_BASH="${OKGIT_BASH:-bash}"

TMP_BASE="${TMPDIR:-/tmp}"
SANDBOX="$(mktemp -d "$TMP_BASE/oktografx-installer-test.XXXXXX")"
OKGIT_PREFIX="${OKGIT_PREFIX:-$SANDBOX/prefix}"
OKGIT_STATE="${OKGIT_STATE:-$SANDBOX/state}"
OKGIT_ROOT="${OKGIT_ROOT:-$SANDBOX/graphs}"

# install.sh resolves its data and state dirs from these, so the whole run stays inside $SANDBOX and
# never touches the developer's or the runner's real ~/.local.
export XDG_DATA_HOME="$SANDBOX/share"
export XDG_STATE_HOME="$SANDBOX/state-home"
export OKTO_GRAFX_SERVER_STATE="$OKGIT_STATE"

BASE="http://127.0.0.1:$OKGIT_PORT"
BIN="$OKGIT_PREFIX/bin/oktografx-server"
LOG="$SANDBOX/log"
COOKIES="$SANDBOX/cookies.txt"
CHECKS=0

FAILED=0
cleanup() {
  if [ -f "$OKGIT_STATE/daemon.pid" ]; then
    kill -TERM "$(tr -d '[:space:]' <"$OKGIT_STATE/daemon.pid")" 2>/dev/null || true
  fi
  # The sandbox goes away; a failure's log does not, or the only copy of what the installer said is gone
  # with it and CI shows a step that exited 1 without a reason.
  if [ "${FAILED:-0}" = "1" ] && [ -f "$LOG" ]; then
    cp "$LOG" "${TMPDIR:-/tmp}/oktografx-installer-failure.log" 2>/dev/null || true
    printf 'the installer output that failed is also at %s\n' "${TMPDIR:-/tmp}/oktografx-installer-failure.log" >&2
  fi
  [ "${OKGIT_KEEP:-0}" = "1" ] || rm -rf "$SANDBOX"
}
trap cleanup EXIT

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok() {
  CHECKS=$((CHECKS + 1))
  printf '  ok  %s\n' "$1"
}
die() {
  FAILED=1
  printf '\n\033[31mFAIL:\033[0m %s\n' "$1" >&2
  if [ -f "$LOG" ]; then
    printf -- '--- last lines of the installer output ---\n' >&2
    tail -30 "$LOG" >&2
  fi
  exit 1
}

# install.sh and the CLI both speak to stderr; capture combined output so greps see everything.
run_install() {
  # shellcheck disable=SC2086
  "$OKGIT_BASH" "$OKGIT_INSTALL" --prefix "$OKGIT_PREFIX" --state-dir "$OKGIT_STATE" \
    --root "$OKGIT_ROOT" --port "$OKGIT_PORT" --installer "$OKGIT_INSTALLER" \
    --python "$OKGIT_PYTHON" $OKGIT_SOURCE "$@" >"$LOG" 2>&1
}

expect_log() {
  grep -q "$1" "$LOG" || die "the installer output does not contain '$1'"
}

mode_of() {
  ls -l "$1" | awk '{print $1}'
}

if [ ! -f "$OKGIT_INSTALL" ] || [ ! -f "$OKGIT_UNINSTALL" ]; then
  die "installer scripts not found: $OKGIT_INSTALL / $OKGIT_UNINSTALL"
fi

step "Both scripts parse"
"$OKGIT_BASH" -n "$OKGIT_INSTALL"
"$OKGIT_BASH" -n "$OKGIT_UNINSTALL"
ok "both scripts parse under $("$OKGIT_BASH" --version | head -1)"
if [ -x /bin/bash ] && [ "$(command -v /bin/bash)" != "$(command -v "$OKGIT_BASH")" ]; then
  /bin/bash -n "$OKGIT_INSTALL"
  /bin/bash -n "$OKGIT_UNINSTALL"
  ok "and under stock $(/bin/bash --version | head -1 | cut -d, -f1)"
fi
"$OKGIT_PYTHON" --version || true

step "--dry-run describes the run and touches nothing"
if ! run_install --dry-run; then
  die "install.sh --dry-run exited non-zero"
fi
expect_log '\[dry-run\]'
if [ -e "$OKGIT_PREFIX" ] || [ -e "$OKGIT_STATE" ] || [ -e "$OKGIT_ROOT" ]; then
  die "--dry-run created a directory it promised not to create"
fi
ok "nothing was written"

step "Install, init, start"
if ! run_install; then
  die "install.sh exited non-zero"
fi
expect_log 'token       okga_'
[ -x "$BIN" ] || die "$BIN is not executable"
"$BIN" --version >"$SANDBOX/version.txt" 2>&1
grep -Eq 'oktografx-server [0-9]+' "$SANDBOX/version.txt" || die "--version did not print a version"
[ -f "$OKGIT_STATE/server.toml" ] || die "init did not write server.toml"
[ -f "$OKGIT_STATE/server.db" ] || die "init did not write server.db"
[ -f "$OKGIT_STATE/admin-token" ] || die "no admin-token file was written"
ok "$(cat "$SANDBOX/version.txt")"

if [ "$(mode_of "$OKGIT_STATE/admin-token")" = "-rw-------" ]; then
  ok "admin-token is mode 600"
else
  die "admin-token is $(mode_of "$OKGIT_STATE/admin-token"), expected -rw-------"
fi

step "The console answers on loopback"
curl -fsS "$BASE/api/v1/health" >"$SANDBOX/health.json"
grep -q '"status":"ok"' "$SANDBOX/health.json" || die "/api/v1/health did not answer ok"
grep -q '"databases_registered":0' "$SANDBOX/health.json" || die "a fresh state directory should be empty"
curl -fsS "$BASE/" | grep -q 'name="oktografx:console"' || die "/ did not serve the built console"
ok "health and the console shell"

TOKEN="$(cat "$OKGIT_STATE/admin-token")"
curl -fsS -X POST "$BASE/admin/api/login" -H 'Content-Type: application/json' \
  -d "{\"token\":\"$TOKEN\"}" -c "$COOKIES" >"$SANDBOX/login.json"
grep -q '"signed_in":true' "$SANDBOX/login.json" || die "the printed token did not log in"
curl -fsS -b "$COOKIES" "$BASE/admin/api/session" | grep -q '"signed_in":true' || die "session cookie not accepted"
ok "the token from the file logs in, and the cookie holds the session"

step "A rerun must not mint a new token"
if ! run_install --no-run; then
  die "a second install.sh exited non-zero"
fi
expect_log 'State directory already exists'
if [ "$TOKEN" != "$(cat "$OKGIT_STATE/admin-token")" ]; then
  die "the administrator token changed on a rerun — the old one would have stopped working"
fi
ok "same token, existing state untouched"

step "Attach a store and read it back over the data plane"
# The interpreter is whatever the installed command is running on — the venv's for a pip install, the
# uv tool environment's for a uv install. It has okto-grafx in it, because the server depends on it,
# which is exactly what we want to prove about the package.
# Which interpreter is behind the installed command? Reading the shebang works until it does not: a shim
# that says `#!/usr/bin/env python3` turns into `env - <store>`, which fails with "is a directory" and
# blames the store. So every plausible candidate is tried against the thing we actually need from it —
# the packages this project installed — and the first one that can import them wins.
SHEBANG="$(head -n 1 "$BIN" 2>/dev/null || true)"
CANDIDATES=()
case "$SHEBANG" in
  *"/env "*) CANDIDATES+=("$(command -v "${SHEBANG##* }" 2>/dev/null || true)") ;;
  '#!'*) CANDIDATES+=("${SHEBANG#'#!'}") ;;
esac
CANDIDATES+=(
  "$XDG_DATA_HOME/uv/tools/okto-grafx-server/bin/python"
  "$XDG_DATA_HOME/okto-grafx-server/venv/bin/python"
  "$HOME/.local/share/uv/tools/okto-grafx-server/bin/python"
  "$OKGIT_PREFIX/bin/python3"
  "$OKGIT_PYTHON"
)
RUNTIME_PY=""
for candidate in "${CANDIDATES[@]}"; do
  [ -n "$candidate" ] && [ -x "$candidate" ] || continue
  if "$candidate" -c 'import okto_grafx, okto_grafx_server' 2>/dev/null; then
    RUNTIME_PY="$candidate"
    break
  fi
done
if [ -z "$RUNTIME_PY" ]; then
  die "no interpreter with okto-grafx installed; $BIN starts with '$SHEBANG'"
fi
# $OKGIT_ROOT was created and resolved by install.sh; on macOS the temp dir is under /tmp, which is a
# symlink, and the daemon refuses a store path with a symlinked component.
ROOT_REAL="$(cd "$OKGIT_ROOT" && pwd -P)"
mkdir -p "$ROOT_REAL/demo.grafx"
"$RUNTIME_PY" - "$ROOT_REAL/demo.grafx" <<'PY'
import sys

from okto_grafx import connect

path = sys.argv[1]
db = connect(path)
with db.begin("write") as tx:
    tx.execute("CREATE NODE TABLE Doc(id INT64, title STRING, PRIMARY KEY(id))")
    tx.execute("CREATE (:Doc {id: 1, title: 'installed by the installer test'})")
# A store the client just wrote still has an un-checkpointed WAL, and the engine refuses a read-only
# open of one rather than guess at its consistency. Read-only registration is the honest default, so the
# fixture does what the README tells an operator to do and checkpoints before the server opens it.
db.checkpoint()
db.close()
PY
ok "the installed environment can create a store"

"$BIN" --state-dir "$OKGIT_STATE" db add "$ROOT_REAL/demo.grafx" --slug demo --read-only \
  >"$SANDBOX/dbadd.json" 2>&1 || die "db add refused a store inside the allowed root: $(cat "$SANDBOX/dbadd.json")"
"$BIN" --state-dir "$OKGIT_STATE" status 2>&1 | grep -q 'demo ' || die "status did not list the store"
ok "db add accepted a store under the installer's root"

KEY_STATUS="$(curl -sS -o "$SANDBOX/key.json" -w '%{http_code}' -X POST "$BASE/admin/api/keys" \
  -b "$COOKIES" -H 'Content-Type: application/json' \
  -d '{"name": "installer-test", "databases": ["demo"], "allow_writes": false}')"
if [ "$KEY_STATUS" != "201" ]; then
  die "POST /admin/api/keys returned $KEY_STATUS: $(cat "$SANDBOX/key.json")"
fi
API_KEY="$("$RUNTIME_PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["key"])' "$SANDBOX/key.json")"
QUERY_STATUS="$(curl -sS -o "$SANDBOX/query.json" -w '%{http_code}' -X POST \
  "$BASE/api/v1/databases/demo/query" -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"query": "MATCH (d:Doc) RETURN d.title AS title", "limit": 5}')"
if [ "$QUERY_STATUS" != "200" ]; then
  die "POST /api/v1/databases/demo/query returned $QUERY_STATUS: $(cat "$SANDBOX/query.json")"
fi
grep -q 'installed by the installer test' "$SANDBOX/query.json" ||
  die "the query endpoint did not return the seeded row: $(cat "$SANDBOX/query.json")"
ok "a read-only API key queried the attached store"

WRITE_STATUS="$(curl -sS -o "$SANDBOX/write.json" -w '%{http_code}' -X POST \
  "$BASE/api/v1/databases/demo/query" -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' -d '{"query": "CREATE (:Doc {id: 2})"}')"
if ! grep -q 'write_not_permitted' "$SANDBOX/write.json"; then
  die "a read-only key was allowed to write (HTTP $WRITE_STATUS): $(cat "$SANDBOX/write.json")"
fi
ok "the same key is refused a write with write_not_permitted"

curl -fsS -X POST "$BASE/admin/api/databases/demo/close" -b "$COOKIES" >/dev/null
ok "the store closes cleanly"

step "Uninstall keeps the data"
if ! "$OKGIT_BASH" "$OKGIT_UNINSTALL" --prefix "$OKGIT_PREFIX" --state-dir "$OKGIT_STATE" --dry-run >"$LOG" 2>&1; then
  die "uninstall.sh --dry-run exited non-zero"
fi
expect_log '\[dry-run\]'
[ -x "$BIN" ] || die "--dry-run removed the command"
[ -f "$OKGIT_STATE/server.db" ] || die "--dry-run removed the control plane"
ok "uninstall --dry-run removed nothing"

if ! "$OKGIT_BASH" "$OKGIT_UNINSTALL" --prefix "$OKGIT_PREFIX" --state-dir "$OKGIT_STATE" >"$LOG" 2>&1; then
  die "uninstall.sh exited non-zero"
fi
expect_log 'Left behind on purpose'
if [ -e "$BIN" ]; then
  die "the command is still installed after uninstall"
fi
[ -f "$OKGIT_STATE/server.toml" ] || die "uninstall deleted server.toml"
[ -f "$OKGIT_STATE/server.db" ] || die "uninstall deleted server.db"
[ -f "$OKGIT_STATE/admin-token" ] || die "uninstall deleted admin-token"
ok "command gone, state directory kept"

for _ in $(seq 1 40); do
  if curl -fsS --max-time 2 "$BASE/api/v1/health" >/dev/null 2>&1; then
    sleep 0.5
  else
    break
  fi
done
if curl -fsS --max-time 2 "$BASE/api/v1/health" >/dev/null 2>&1; then
  die "the daemon is still answering after uninstall stopped it"
fi
ok "the daemon from this state directory was stopped"

if ! "$OKGIT_BASH" "$OKGIT_UNINSTALL" --prefix "$OKGIT_PREFIX" --state-dir "$OKGIT_STATE" --purge-state >"$LOG" 2>&1; then
  die "uninstall.sh --purge-state exited non-zero"
fi
if [ -d "$OKGIT_STATE" ]; then
  die "--purge-state kept the state directory"
fi
[ -d "$ROOT_REAL" ] || die "--purge-state deleted the store root"
ok "--purge-state removes the control plane and only that"

printf '\n\033[1m%d installer checks passed\033[0m (source: %s, installer: %s)\n' \
  "$CHECKS" "${OKGIT_SOURCE:-published release}" "$OKGIT_INSTALLER"
