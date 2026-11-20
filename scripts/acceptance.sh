#!/usr/bin/env bash
# Bring up a daemon exactly the way an operator does, run the end-to-end acceptance flow against
# it, and tear it down. Everything lives in a throwaway directory unless WORK_DIR is set.
#
#   ./scripts/acceptance.sh                 # temp directory, port 8737
#   PORT=9000 ./scripts/acceptance.sh       # another port
#   KEEP=1 ./scripts/acceptance.sh          # leave the daemon's directory behind
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
PORT="${PORT:-8737}"
WORK_DIR="${WORK_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/okg-acceptance.XXXXXX")}"
KEEP="${KEEP:-0}"

STATE_DIR="$WORK_DIR/state"
GRAPHS_DIR="$WORK_DIR/graphs"
LOG="$WORK_DIR/daemon.log"

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  # A path or a command name: CI installs the project into the interpreter the workflow set up and has
  # no .venv, so `PYTHON=python3` has to be as honourable as `PYTHON=./.venv/bin/python`.
  echo "no interpreter at $PYTHON (set PYTHON=... to a virtualenv python, or one on PATH)" >&2
  exit 2
fi

say "workspace: $WORK_DIR"
mkdir -p "$GRAPHS_DIR"

say "seeding three Grafx stores"
"$PYTHON" "$ROOT/scripts/acceptance.py" --seed "$GRAPHS_DIR/docs" "$GRAPHS_DIR/library" "$GRAPHS_DIR/console"

say "oktografx-server init"
INIT_OUTPUT="$("$PYTHON" -m okto_grafx_server.cli --state-dir "$STATE_DIR" init \
  --root "$GRAPHS_DIR" --port "$PORT")"
echo "$INIT_OUTPUT" | sed -n '1,8p'
# The gate exercises the default posture: init creates the state directory and mints no credential, so
# every stage below — API, browser, README — runs the way a first-time operator's server runs. A token
# round-trip is checked inside the flow, and ends by opening the door again.
if printf '%s\n' "$INIT_OUTPUT" | grep -q "ADMIN TOKEN"; then
  echo "init minted an administrator token; this run should be testing the open default" >&2
  exit 2
fi
ADMIN_TOKEN=""

# The acceptance flow checks the metrics surface, so turn it on the way an operator would: the
# starter configuration ships `metrics_enabled = false` because the exposition has no auth of its own.
"$PYTHON" - "$STATE_DIR/server.toml" <<'PYFLOW'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
flipped = text.replace("metrics_enabled = false", "metrics_enabled = true")
assert flipped != text, f"{path} does not say metrics_enabled = false"
path.write_text(flipped, encoding="utf-8")
print(f"metrics_enabled = true in {path}")
PYFLOW

say "oktografx-server run (127.0.0.1:$PORT)"
"$PYTHON" -m okto_grafx_server.cli --state-dir "$STATE_DIR" run --port "$PORT" >"$LOG" 2>&1 &
DAEMON_PID=$!
cleanup() {
  if kill -0 "$DAEMON_PID" 2>/dev/null; then
    kill "$DAEMON_PID" 2>/dev/null || true
    wait "$DAEMON_PID" 2>/dev/null || true
  fi
}
trap 'cleanup; [ "$KEEP" = "1" ] || rm -rf "$WORK_DIR"' EXIT

for _ in $(seq 1 60); do
  if ! kill -0 "$DAEMON_PID" 2>/dev/null; then break; fi
  if curl -fsS "http://127.0.0.1:$PORT/api/v1/health" >/dev/null 2>&1; then break; fi
  sleep 0.5
done
if ! kill -0 "$DAEMON_PID" 2>/dev/null; then
  # A port already taken makes the probe answer from somebody else's daemon, and the whole run would
  # then be tested against a server with a different admin token and a different control database.
  echo "the daemon we started exited (was 127.0.0.1:$PORT already in use?); log:" >&2
  tail -40 "$LOG" >&2
  exit 1
fi
if ! curl -fsS "http://127.0.0.1:$PORT/api/v1/health" >/dev/null 2>&1; then
  echo "daemon did not come up; log:" >&2
  tail -40 "$LOG" >&2
  exit 1
fi
echo "daemon pid $DAEMON_PID is healthy"

say "acceptance flow"
status=0
OKG_ADMIN_TOKEN="$ADMIN_TOKEN" "$PYTHON" "$ROOT/scripts/acceptance.py" \
  --base-url "http://127.0.0.1:$PORT" --store "$GRAPHS_DIR/docs" || status=$?

say "a second store, attached from the same root"
OKG_ADMIN_TOKEN="$ADMIN_TOKEN" "$PYTHON" "$ROOT/scripts/acceptance.py" \
  --base-url "http://127.0.0.1:$PORT" --store "$GRAPHS_DIR/library" || status=$?

# The console is a client of the same HTTP surface, so it is accepted the same way: driven against
# the running daemon by a real browser. WITH_BROWSER=0 skips it (headless machines without the
# Playwright download); it is on by default because the operator's journey is part of the contract.
if [ "${WITH_BROWSER:-1}" = "1" ]; then
  say "the operator's console, driven by a browser"
  # A store no other flow touched: the console has to attach it by reference itself.
  STORE_PATH="$("$PYTHON" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$GRAPHS_DIR/console")"
  if [ ! -d "$ROOT/web/node_modules/playwright" ]; then
    echo "web/node_modules is missing: run (cd web && npm ci && npx playwright install chromium)" >&2
    status=1
  else
    (cd "$ROOT/web" && node e2e/console.mjs \
      --base-url "http://127.0.0.1:$PORT" \
      --admin-token "$ADMIN_TOKEN" \
      --store "$STORE_PATH" \
      --shots "$WORK_DIR/shots") || status=$?
    echo "screenshots: $WORK_DIR/shots"
  fi
else
  say "browser console flow skipped (WITH_BROWSER=0)"
fi

if [ "${WITH_README:-1}" = "1" ]; then
  say "the README's curl examples, run verbatim"
  # The README's first control-plane example trades a token for a cookie, so this stage is run against a
  # server that requires one; the door is opened again straight after, for anything that follows.
  README_TOKEN="$("$PYTHON" -m okto_grafx_server.cli --state-dir "$STATE_DIR" admin-token rotate \
    | tr -d '[:space:]' | grep -o 'okga_[A-Za-z0-9_-]*' | head -1)"
  if [ -z "$README_TOKEN" ]; then
    echo "could not mint a token for the README flow" >&2
    status=1
  else
    "$ROOT/scripts/readme-check.sh" "http://127.0.0.1:$PORT" "$README_TOKEN" docs || status=$?
    "$PYTHON" -m okto_grafx_server.cli --state-dir "$STATE_DIR" admin-token clear >/dev/null
  fi
else
  say "README example check skipped (WITH_README=0)"
fi

say "the daemon's structured log in the state directory"
"$PYTHON" - "$STATE_DIR/server.log" <<'PYLOG' || status=$?
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_file():
    raise SystemExit(f"no structured log at {path}")
records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
if not records:
    raise SystemExit(f"{path} is empty after a full acceptance run")
for record in records:
    for key in ("ts", "level", "logger", "message"):
        assert key in record, f"log line is not a structured record: {record}"
logged = [r for r in records if r.get("request_id") not in (None, "", "-")]
assert logged, "no log line carries a real request_id, so a refusal cannot be traced"
requests = [r for r in logged if r.get("status")]
assert requests, "no request was logged with its status and its request_id"
said = json.dumps(records)
assert "Bearer okg" not in said and "Bearer okga" not in said, "a bearer reached the log"
assert not re.search(r"token=[A-Za-z0-9_-]{8,}", said), "a credential in a query string reached the log"
refusals = [r for r in requests if int(r["status"]) >= 400]
print(f"{len(records)} structured lines, {len(requests)} requests, {len(refusals)} refusals, "
      f"rotating at 5 MiB x 5")
PYLOG

say "a configuration the engine cannot honour is refused before the socket opens"
BAD_DIR="$WORK_DIR/bad-state"
mkdir -p "$BAD_DIR"
sed 's/^max_write_sessions = .*/max_write_sessions = 9/' "$STATE_DIR/server.toml" > "$BAD_DIR/server.toml"
if "$PYTHON" -m okto_grafx_server.cli --state-dir "$BAD_DIR" config >"$WORK_DIR/bad-config.out" 2>&1; then
  echo "max_write_sessions = 9 was accepted; the plan caps it at 4" >&2
  status=1
else
  grep -q 'max_write_sessions' "$WORK_DIR/bad-config.out" || { echo "the refusal did not name the field" >&2; status=1; }
  sed -n '1,2p' "$WORK_DIR/bad-config.out"
fi

say "daemon log (last 20 lines)"
tail -20 "$LOG"

say "curl examples against the live daemon"
curl -sS -X POST "http://127.0.0.1:$PORT/admin/api/login" \
  -H 'content-type: application/json' -d "{\"token\":\"$ADMIN_TOKEN\"}" \
  -c "$WORK_DIR/cookies.txt" | head -c 200; echo
curl -sS -b "$WORK_DIR/cookies.txt" "http://127.0.0.1:$PORT/admin/api/status" | head -c 300; echo

if [ "$KEEP" = "1" ]; then
  trap - EXIT
  echo
  echo "kept: $WORK_DIR (no administrator token: this machine is the administrator)"
  echo "re-run the daemon with: $PYTHON -m okto_grafx_server.cli --state-dir $STATE_DIR run --port $PORT"
fi

exit $status
