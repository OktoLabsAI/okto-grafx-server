# Contributing to Okto Grafx Server

Thank you for reading this before sending a patch. This server is small on purpose, and the discipline
that keeps it small is documented here.

By contributing you agree that your contribution is licensed under the
[Elastic License 2.0 with the Okto Labs addendum](LICENSE), the same terms as everything else here.

## Set up a development tree

```bash
git clone https://github.com/OktoLabsAI/okto-grafx-server.git
cd okto-grafx-server

python3.11 -m venv .venv                       # or: uv venv --python 3.11
.venv/bin/pip install -e '.[dev]'              # uv: uv pip install -e '.[dev]' --python .venv/bin/python
cd web && npm ci && cd ..                      # only for console work
```

Always use the project's virtualenv interpreter for tests — the suite opens real Okto Grafx stores and the
system Python is usually older than 3.11.

## The gate: what has to pass before a review

```bash
.venv/bin/python -m pytest tests -q                              # 257 backend tests
(cd web && npx vitest run && npx tsc --noEmit && npm run build)   # 74 console tests
.venv/bin/ruff check src tests                                     # lint (line length 110)
./scripts/acceptance.sh                                            # the end-to-end gate
./scripts/test-installer.sh                                        # install → query → uninstall
```

`scripts/acceptance.sh` is the interesting one. It builds a fresh state directory, seeds three real
stores, starts a daemon, runs 84 transport/API checks against two of them, drives the console in a real
browser against the third (34 steps), and then runs **every `curl` example in README.md** against the live
daemon. Documentation drift fails the build on purpose.

`scripts/test-installer.sh` drives `install.sh` and `uninstall.sh` the way an operator would: install,
`init`, start, read the store list **with no credential** (the default posture), require a token and watch
that door close, create a store with the *installed* environment, attach it, query it through the data
plane, get a write refused with a read-only key, uninstall, and check that the state directory survived. Point it at the tree with `OKGIT_SOURCE="--from-tree $PWD"`, or leave
`OKGIT_SOURCE` empty to install the published release and verify its checksum. CI runs it from the tree on
Ubuntu (`--installer pip`) and macOS (`--installer uv`); the release workflow runs it against the artifacts
that tag just published.

```bash
PORT=9000 KEEP=1 ./scripts/acceptance.sh    # keep the workspace; prints how to re-attach
WITH_BROWSER=0 ./scripts/acceptance.sh      # skip Playwright (CI does it in a separate job)
WITH_README=0  ./scripts/acceptance.sh      # skip the documentation examples
```

If a check is unreachable for a legitimate reason, fix the check or the code — do not lower the gate.

## Five rules that survive every refactor

1. **The backend is the contract.** The console and the MCP tools display what the daemon actually
   returned. Never invent a payload to make a screen easier, and never accept a field the server does not
   send. If a screen needs a fact that is not in a response, that is an API change: change the API, its
   OpenAPI entry, `web/src/api/client.ts`, a test, and the README example.
2. **A refusal is a feature.** Say which door closed and what to do next, in the envelope, with a stable
   `code`. New refusals get a code in `errors.py`, a row in the README table, and a test that reads the
   message the way an operator would.
3. **Never guess on the user's behalf.** No implicit analyzer, no generated embedding, no silent
   truncation, no automatic write replay, no partial weight map. If the engine refuses, the server relays
   the refusal and its own statistics.
4. **Nothing destructive without two acts.** A new write path needs a confirmation argument or a
   two-step UI, and it must be audited. `write` in MCP is a model of this: absent unless the key allows
   writes, and still needs `confirm_write: true`.
5. **Say what it is not.** Loopback-only, no TLS, not a sandbox, one writer per store. Documentation and
   error messages keep this honest.

## Console specifics

* React 18 + Vite + Tailwind 4 + react-query 5 + react-router 7, TypeScript strict with
  `noUnusedLocals`/`noUnusedParameters`. Sigma.js 4 + graphology for the graph, CodeMirror 6 for the
  editor.
* **Tests match by accessible name** (`getByRole("button", { name: "Add existing store" })`) and the e2e
  flow keys off visible hint text. So: icons are always `aria-hidden` and must never change a control's
  label; do not reword a button without updating the tests and `web/e2e/console.mjs`.
* **The CSP is `default-src 'self'`.** No CDN fonts, icons or scripts. Add a font with `@fontsource/*` and
  an icon set as an npm dependency (Lucide is already here). It also means **`blob:` workers are refused**:
  `graphology-layout-forceatlas2/worker` spawns one, so the graph would simply stop moving with no error on
  screen. The layout runs on the main thread in bounded chunks instead — keep it that way, or ship a real
  same-origin worker file.
* **The graph is keyed by the store's own ids** — `addDirectedEdgeWithKey(edge.id, …)`, nodes keyed by
  `Table:record_id` — so a click on the canvas hands back a physical record and the inspector reads the
  payload it was given without a translation layer. Minted graphology keys break edge inspection silently.
* **sigma 4 is state-driven**: styles read `isHovered`/`isHighlighted`/`isHidden` plus this app's
  `isDimmed`/`isAdjacent`, and a hover writes state (`setNodesState(keys, state)` — keys first, one state
  per batch) instead of rebuilding. Style functions are called as `(attributes, state, graphState, graph)`,
  so a state-only function still takes attributes as its first argument.
* **Graph legibility is under test.** `web/src/lib/graph-view.test.ts` asserts contrast ratios against the
  page ground and the emphasis sets; if you retint `GRAPH_CANVAS`, that file is where the floor is argued.
* **Tokens live in `web/src/index.css`.** Colours, type and radii are named there; do not add a hex literal
  to a component when a token exists. Anything unlayered in that file beats Tailwind utilities — that is
  why the base rules are inside `@layer base`.
* A new screen means: a route in `App.tsx`, an entry in `NAV` (`components/Shell.tsx`) with a Lucide icon,
  a vitest case in `src/routes/App.test.tsx` with the exact mock bodies the page reads, and — if it is part
  of the operator story — a step in `web/e2e/console.mjs`.
* Dev against a running daemon: `OKG_DAEMON=http://127.0.0.1:8737 npm run dev --prefix web`.

## Backend specifics

* `api/` is admission and shape; `engine/` owns handles, the runner, jobs and the value codec;
  `control/` owns SQLite. Do not reach from one into another's business: the API layer must not open a
  database handle directly, and the engine layer must not know what an HTTP status is.
* Every state change goes through the control plane so it is auditable, and every long operation goes
  through the job queue so a second request gets `409 job_active` instead of an invisible queue.
* New configuration keys need: a field on `ServerConfig`, validation with a typed refusal, the README
  configuration block, the Settings screen, and a test that the refusal happens **before** the socket
  opens when the value is something the engine cannot honour.
* Store paths are validated against `allowed_roots` at registration *and* at open time. Keep both.

## Packaging and the installer

`install.sh` is the first thing most people run and it is usually piped straight into a shell, so it is
treated as a product surface with its own harness rather than as a convenience script.

* It has to work on **the bash macOS ships (3.2)** — that is what `curl … | bash` runs there. Check with
  `OKGIT_BASH=/bin/bash ./scripts/test-installer.sh`; CI on macOS gets it for free.
* `--dry-run` prints commands and **touches no files at all**. Anything new in the script has to be behind
  the `run`/`remove` wrappers or inside a `[ "$DRY" != "1" ]` branch, and the harness asserts the
  directories stay absent.
* **Never `init` over an existing state directory.** A second `init` with `--with-admin-token` would mint
  a token over the one the operator is holding; the script stops and points at `admin-token rotate`
  instead. (Without that flag `init` mints nothing at all — the default posture has no credential.)
* Resolve `--root` with `cd "$dir" && pwd -P` *after* creating it: the daemon refuses a store path with a
  symlinked component, and on macOS `/tmp` is one, so an unresolved root would turn every later `db add`
  into a confusing refusal. The harness keeps its sandbox under `$TMPDIR` for the same reason.
* The wheel must contain the built console (`web/**` is package-data), and the console emits
  `<meta name="oktografx:console">` so "the console is being served" is a claim a test can check —
  `tests/test_console_static.py`, the installer harness and the release workflow all grep that marker.
  If you change `web/index.html`, rebuild and keep that marker.
* `uv tool install` has no `--bin-dir`; point it at `--prefix` with `UV_TOOL_BIN_DIR`.
* Publishing is pushing a tag: `git tag -a vX.Y.Z -m '…' && git push origin vX.Y.Z`. The release workflow
  builds the console, asserts the wheel carries `index.html` and a hashed asset, builds sdist + wheel,
  writes `SHA256SUMS`, publishes a GitHub Release with both installer scripts, and then installs that
  release on Ubuntu (pip) and macOS (uv) with the same harness.

## Committing

* Rebase-friendly, imperative subject, ~72 columns, and the body explains the constraint rather than the
  diff: *"refuse a read-only open of a non-checkpointed store, and name the remedy"* rather than *"handle
  engine error"*.
* Keep the built console in sync when you change the source: `npm run build` writes
  `src/okto_grafx_server/web/`, which is committed so a `pip install git+…` works without Node.
* Never commit a state directory, a store, a token, or a screenshot that shows real data.

## Reporting a problem

Include the `X-Request-Id` (every response has one, every error envelope repeats it) and the daemon's
`server.log` line for it:

```bash
jq -r 'select(.request_id == "…")' ~/.local/state/okto-grafx-server/server.log
```

The log carries no credentials and no statement values, so it is usually safe to paste the whole line.
