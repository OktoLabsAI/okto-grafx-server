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
  writes `SHA256SUMS`, publishes a GitHub Release with all six installer scripts, and then installs that
  release on Ubuntu (pip), macOS (uv) and Windows (pip, in-box PowerShell 5.1) with the same harness — the
  only place the published assets, rather than the working tree, are what gets installed.

The Windows installers (`install.ps1`, `uninstall.ps1`) hold the same contract in the other shell, and
`install.cmd` / `uninstall.cmd` are front-ends onto the `.ps1` — they find `powershell.exe` and hand the
arguments over. Nothing else may live in them: two implementations of one install drift, and the operator
pays with a switch that means something slightly different on the other side.

* They target **Windows PowerShell 5.1**, the one that ships with Windows, because the machine running the
  one-liner is a machine where nothing has been installed yet. No `??`, no ternary, no
  `Invoke-WebRequest` conveniences that only exist in PowerShell 7 — and `-UseBasicParsing` everywhere. The
  CI job drives the in-box `powershell.exe`, not the runner's `pwsh`, so this is enforced by execution.
* **Every external program goes through `Invoke-Native`.** 5.1 turns a stderr line from a native command
  into an `ErrorRecord`, which under `$ErrorActionPreference = 'Stop'` becomes a `NativeCommandError` and
  aborts the script on a command that exited 0. And it returns exit code plus merged output as an
  *object*, because PowerShell flattens an array it returns: `@($code, $lines)` arrives as code, first
  line, second line, and the caller ends up indexing into log text.
* **These files are ASCII with CRLF**, and `.gitattributes` marks them `-text` so no checkout rewrites
  them. Two separate reasons: 5.1 decodes a BOM-less script as the machine's ANSI codepage, so one em dash
  in a message reaches the operator as three characters of mojibake that no grep will ever flag; and
  `cmd.exe` parses labels and `goto` in an LF-only batch file differently by build, which is a bug report
  nobody can reproduce. Byte-for-byte comparison of tag against tree in `release.yml` depends on this too.
* `-DryRun` **writes nothing**, including on the uv path — `uv tool install` is the one step in here that
  would otherwise do real work inside a dry run.
* The default state directory mirrors `config.default_state_dir()`'s order (`OKTO_GRAFX_SERVER_STATE`,
  `XDG_STATE_HOME`, then the platform habit). That is deliberate so the installer and the daemon cannot
  disagree about where your registrations are. Change one and you change the other, plus
  `tests/test_config_and_paths.py`, which asserts both platforms by patching `sys.platform` — patching
  `os.name` instead turns every `Path` in the process into a `WindowsPath`.
* There is no umask on NTFS, so the token file's `0600` is an access list: inheritance dropped, every other
  identity purged, one grant to this account's SID. Build it, then **read the entries back**. Judging the
  file by `AreAccessRulesProtected` is a statement about inheritance, not about who can read a secret, and
  the first version of this trusted the flag while the file went on inheriting its folder. When it cannot
  be had, the warning carries the reason rather than the words "could not", because a security promise that
  fails quietly is worse than no promise at all.
* **The two runtimes are not the same API surface.** These scripts run on Windows PowerShell 5.1, whose
  .NET Framework has `File.GetAccessControl`; the driver runs on pwsh 7, whose .NET does not, and reaches
  the same DACL through `Get-Acl` / `Set-Acl`. `Get-FileHash` is the mirror image — present in pwsh 7,
  absent from the runner's 5.1 — which is why the checksum is computed with the framework type. Same
  promise, one door each, and the read-back is what makes that safe to claim.
* **A name that resolves in the driver's session says nothing about the shell that will run the script**, so
  the driver resolves every command name the parser finds in the shipped `.ps1` files against the in-box
  5.1 (`Test-CommandNames -Child`) rather than against itself, and rejects any variable a script reads but
  never assigns. Three invented names got past parsing and past review before that check existed; each one
  only failed on the machine running the one-liner.
* `FileSecurity(string, AccessControlSections)` looks like it parses SDDL and is the overload that takes a
  *file name*: .NET ran `Path.GetFullPath` over `D:P(A;;FA;;;S-1-5-21-…)` — to that API `D:` is the current
  directory on drive D: — and reported a missing file. Do not reach for it.
* **A detached child inherits the caller's handles.** `Start-Process -WindowStyle Hidden` and
  `-RedirectStandardOutput` cannot both be honoured on 5.1, and asking for the first left the daemon holding
  the write end of the caller's stdout — which is not a broken server but a caller waiting for an EOF a live
  server never sends, for as long as the job is willing to wait. Use `-NoNewWindow` with the redirections,
  record the pid of what is really listening, and bound every installer call in the driver (`WaitForExit`)
  so wedging is a named failure with a log rather than a job timeout.
* **Windows cannot replace the executable a running process was started from.** Rerunning the installer is
  the documented upgrade path and uneventful on Unix, where the running binary is unlinked and the daemon
  carries on from its inode; here it is a `WinError 32` from the middle of `pip install`, with the
  environment part old and part new, and nothing in the error mentioning the server that was still up.
  `install.ps1` refuses before it writes anything and names `Stop-Process -Id` — and only for a live process
  that is ours, because a pid left by a crash, a reboot or pid reuse must not stop an install.
* **`pwsh -File` reports the exit code of the last external command** when the script never says how it
  ended, and the last thing the driver does on purpose is ask curl about a port it deliberately left closed.
  It ends with `exit 0`.
* **These scripts are only ever executed on CI.** `scripts/test-installer.ps1` is the Windows twin of the
  POSIX driver and runs on `windows-latest` in `ci.yml` (from the tree) and in `release.yml` (from the
  published assets, with the in-box 5.1). Editing them without that job green is editing blind: there is
  no way to run a `.ps1` or a `.cmd` on the macOS or Linux box you are holding. Every hang and every
  invented identifier in this file was paid for on a runner, because nothing else could see them.

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
