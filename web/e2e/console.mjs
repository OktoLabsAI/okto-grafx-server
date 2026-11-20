#!/usr/bin/env node
/**
 * Browser acceptance for the operator console (PLAN.md item 18, front half).
 *
 * It drives the *built* console in Chromium against a live daemon over real HTTP and WebSocket:
 * sign in only if the server asks for a token, attach a store by reference, read its schema, run a statement, draw the result, search
 * it twice, mint an API key and then use that key as the data-plane credential — the same motions
 * an operator makes, with the same two doors the daemon enforces.
 *
 *   node e2e/console.mjs --base-url http://127.0.0.1:8737 --admin-token okga_... \
 *        --root /tmp/okg/graphs --store docs --shots /tmp/okg/shots
 *
 * Exit code is 0 only when every step passed. Screenshots are written even on failure, because a
 * failure you cannot look at is not evidence.
 */

import { mkdirSync, realpathSync } from "node:fs";
import { resolve } from "node:path";
import process from "node:process";
import { chromium } from "playwright";

// --------------------------------------------------------------------------- helpers

const args = parseArgs(process.argv.slice(2));
const shots = resolve(args.shots ?? "e2e/shots");
mkdirSync(shots, { recursive: true });

const passes = [];
const failures = [];

function say(name, ok, evidence = "") {
  const line = `  ${ok ? "\x1b[32mPASS\x1b[0m" : "\x1b[31mFAIL\x1b[0m"} ${name}${evidence ? ` — ${evidence}` : ""}`;
  console.log(line);
  (ok ? passes : failures).push(name);
  return ok;
}

function section(title) {
  console.log(`\n\x1b[1m${title}\x1b[0m`);
}

function parseArguments(argv) {
  const out = {};
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (!token.startsWith("--")) continue;
    const key = token.slice(2).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
    const next = argv[index + 1];
    if (next === undefined || next.startsWith("--")) {
      out[key] = true;
    } else {
      out[key] = next;
      index += 1;
    }
  }
  return out;
}

function parseArgs(argv) {
  return parseArguments(argv);
}

async function shot(page, name) {
  const path = `${shots}/${name}.png`;
  await page.screenshot({ path, fullPage: true });
  return path;
}

/** Click, wait for the daemon's answer, and fail with the page's own text if it never comes. */
async function expectText(page, text, { timeout = 15000, name } = {}) {
  try {
    await page.getByText(text, { exact: false }).first().waitFor({ timeout });
    return true;
  } catch {
      const body = (await page.locator("body").innerText()).replace(/\s+/g, " ").slice(0, 500);
    const errors = consoleErrors.length ? ` | console: ${consoleErrors.slice(-1)[0].slice(0, 120)}` : "";
    say(name ?? `expected to see "${text}"`, false, `page shows: ${body}${errors}`);
    return false;
  }
}

// --------------------------------------------------------------------------- the flow

const baseUrl = args.baseUrl ?? "http://127.0.0.1:8737";
if (!args.store) {
  console.error(
    "usage: node e2e/console.mjs --base-url URL [--admin-token TOKEN] --store PATH [--shots DIR]",
  );
  console.error(
    "       omit --admin-token to drive a server that requires none, which is the default posture",
  );
  process.exit(2);
}
// The daemon resolves store paths without following symlinks, so hand it the real path: on macOS
// /tmp is a link and would be refused as a symlink component.
const storePath = realpathSync(resolve(args.store));
const slug = args.slug ?? storePath.split("/").pop();

const browser = await chromium.launch({
  // Headless Chromium has no GPU here: SwiftShader gives Sigma a real WebGL2 context.
  args: ["--enable-unsafe-swiftshader", "--use-gl=angle", "--use-angle=swiftshader"],
});
const context = await browser.newContext({ viewport: { width: 1500, height: 1000 } });
const page = await context.newPage();
const consoleErrors = [];
const httpErrors = [];
// Refusals the flow asks for on purpose. They are the point of a step, not noise: they are listed
// here so the final sweep can tell a door working from a page falling over.
const expectedRefusals = [];
page.on("response", (response) => {
  if (response.status() >= 400) httpErrors.push(`${response.status()} ${response.url().replace(baseUrl, "")}`);
});
page.on("console", (message) => {
  // A non-2xx response is reported by the browser as a console error too; it is tracked by URL in
  // httpErrors, where an expected refusal can be told apart from a surprise.
  if (message.type() === "error" && !/^Failed to load resource/.test(message.text())) {
    consoleErrors.push(message.text());
  }
});
page.on("pageerror", (error) => consoleErrors.push(String(error)));

try {
  if (!args.adminToken) {
    section("1. this machine is the administrator: there is no sign-in screen");
    await page.goto(`${baseUrl}/databases`, { waitUntil: "networkidle" });
    say("no redirect to /login", !page.url().includes("/login"), page.url());
    say("the rail names the posture", (await expectText(page, "this machine only")) === true);
    say(
      "it offers the switch rather than a sign-out of a door with no lock",
      (await page.getByRole("button", { name: /sign out/i }).count()) === 0 &&
        (await page.getByRole("link", { name: /token is off/i }).count()) === 1,
    );
    await shot(page, "01-open");

    section("2. Settings carries the switch, and the commands around it");
    await page.goto(`${baseUrl}/settings`, { waitUntil: "networkidle" });
    say(
      "the panel says no token is required",
      (await expectText(page, "no token — this machine only")) === true,
    );
    say(
      "and prints the command-line version of the same choice",
      (await page.getByText(/admin-token rotate/).first().count()) > 0,
    );
    // Armed, not sprung: requiring a token here would change the posture under every step that follows.
    say(
      "the switch waits to be confirmed before it does anything",
      (await page.getByRole("button", { name: /require a token/i }).count()) === 1 &&
        (await page.getByText(/Any process on this machine can create API keys/i).count()) === 0,
    );
    await shot(page, "02-settings");
    await page.goto(`${baseUrl}/databases`, { waitUntil: "networkidle" });
  } else {
    section("1. an unauthenticated visitor is sent to the login screen");
    await page.goto(`${baseUrl}/databases`, { waitUntil: "networkidle" });
    say("the console redirects to /login", page.url().includes("/login"), page.url());
    await shot(page, "01-login");
    say(
      "the login screen says what it wants and where the session lives",
      (await page.getByText("Sign in with the administrator token").count()) > 0,
    );

    section("2. the administrator signs in");
    await page.getByLabel("admin token").fill(args.adminToken);
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.waitForURL(/\/databases$/, { timeout: 15000 });
    say("the store list loads", (await page.getByRole("button", { name: "Add existing store" }).count()) === 1);
    say("the admin door is named in the header", (await expectText(page, "admin session")) === true);
    await shot(page, "02-databases");
  }

  section("3. the store is attached through the console");
  const alreadyAttached = (await page.getByRole("link", { name: slug }).count()) > 0;
  if (alreadyAttached) {
    say("the store is already registered", true, "walking into it instead of attaching again");
    await page.getByRole("link", { name: slug }).first().click();
    await page.waitForURL(new RegExp(`/databases/${slug}$`), { timeout: 15000 });
  } else {
    await page.getByRole("button", { name: "Add existing store" }).click();
    await expectText(page, "add an existing store by reference");
    await page.getByLabel(/store directory/).fill(storePath);
    await page.getByRole("button", { name: "inspect" }).click();
    const inspected = await expectText(page, "uuid ", { name: "the daemon describes the candidate store" });
    if (!inspected) {
      const refusal = await page.locator("body").innerText();
      const match = refusal.match(/(refused|invalid_path|not_a_grafx_store)[^\n]*/i);
      console.log(`      daemon said: ${(match?.[0] ?? refusal).replace(/\s+/g, " ").slice(0, 200)}`);
    }
    if (inspected) {
      const facts = await page.locator("text=/format v\\d+ · page \\d+/").first().innerText();
      say("the facts are the store's own", /page \d+/.test(facts), facts);
    }
    await page.getByRole("button", { name: "attach store" }).click();
    await page.waitForURL(new RegExp(`/databases/${slug}$`), { timeout: 20000 });
    say("attaching lands on the store's own page", page.url().endsWith(`/databases/${slug}`), page.url());
  }
  await shot(page, "03-store");


/**
 * Type a statement into the CodeMirror editor and wait for the daemon's answer to be on screen.
 * The editor is a contenteditable, so the text goes in through the keyboard like an operator's would.
 */
async function runStatement(statement) {
  const editor = page.locator(".cm-content").first();
  await editor.click();
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.type(statement);
  await page.getByRole("button", { name: /^Run/ }).click();
  await page.waitForFunction(
    () =>
      Boolean(document.querySelector('[role="alert"]')) ||
      (/rows · /.test(document.body.innerText) && !/executing…/.test(document.body.innerText)),
    { timeout: 25000 },
  );
}

/** Run one maintenance action through the maintenance panel and wait for the daemon's job result. */
async function runMaintenance(action) {
  const card = page.locator(`[data-action="${action}"]`);
  await card.getByRole("button", { name: "run", exact: true }).click();
  await card.getByRole("button", { name: `run ${action}` }).click();
  for (let attempt = 0; attempt < 150; attempt += 1) {
    const response = await page.request.get(`${baseUrl}/admin/api/jobs?limit=20`);
    const jobs = (await response.json()).jobs.filter((job) => job.kind === action);
    if (jobs.length && ["succeeded", "failed", "cancelled"].includes(jobs[0].state)) {
      return jobs[0];
    }
    await page.waitForTimeout(400);
  }
  return { state: "timeout" };
}

/**
 * Put the store's `read_only` door where the step needs it, whether or not it is already there.
 *
 * A door-opening change asks to be confirmed, so this clicks the confirmation only when the panel
 * asked for it; a change that is already in place must not be "saved" again, because the panel
 * disables the button when nothing differs.
 */
async function setRegistration(readOnly) {
  const select = page.getByLabel("read_only");
  if ((await select.inputValue()) !== String(readOnly)) {
    await select.selectOption(String(readOnly));
    const save = page.getByRole("button", { name: "save registration" });
    await save.waitFor({ timeout: 15000 });
    if (await save.isEnabled()) {
      await save.click();
      const confirm = page.getByRole("button", { name: /open the write path|allow writes/ });
      if (await confirm.isVisible().catch(() => false)) await confirm.click();
    }
  }
  await page.waitForFunction(() => /nothing changed/.test(document.body.innerText), { timeout: 20000 });
  // Ask the daemon, not the form: the registration is server state, and the panel could be showing a
  // draft that was never saved.
  const record = await (await page.request.get(`${baseUrl}/admin/api/databases/${slug}`)).json();
  say(readOnly ? "the read-only door is back on" : "the write path is open for maintenance",
      record.read_only === readOnly, `daemon says read_only=${record.read_only}, allow_writes=${record.allow_writes}`);
}

  // A store created minutes ago has an un-checkpointed WAL, so the very first inventory through the
  // read door is refused on purpose — step 3b is entirely about that refusal, so it is not a surprise.
  expectedRefusals.push(`400 /admin/api/databases/${slug}/inventory`);

  section("3b. the first-run remedy, done from the console");
  // A store created moments ago is not checkpoint-complete, so the read door the attach form chose
  // by default is refused — with the remedy in the message. The operator follows it here.
  // The panel's own hint mentions the precondition too, so look at the refusal itself.
  const refused = (await page.locator('[role="alert"]').filter({ hasText: "checkpoint-complete" }).count()) > 0;
  if (refused) {
    say("the refusal states what has to happen first", true, "the daemon names the precondition");
    await expectText(page, "recover", { name: "the remedy names the action to run" });
  } else {
    say("the store already opens read-only", true, "recovery and checkpoint have run on it before");
  }
  await setRegistration(false);
  await shot(page, "03b-registration");

  await page.getByRole("button", { name: "Maintenance", exact: true }).click();
  const recovered = await runMaintenance("recover");
  say("recover runs from the panel", recovered.state === "succeeded",
      JSON.stringify(recovered.detail?.report ?? recovered.detail ?? {}).slice(0, 90));
  const firstCheckpoint = await runMaintenance("checkpoint");
  say("checkpoint runs from the panel", firstCheckpoint.state === "succeeded",
      JSON.stringify(firstCheckpoint.detail?.report ?? {}).slice(0, 90));
  await shot(page, "03c-maintenance");

  // Put the safe door back, and let the next read prove that a read-only handle works.
  await setRegistration(true);
  await page.getByRole("button", { name: "Overview", exact: true }).click();
  await expectText(page, "wal bytes", { name: "a read-only handle can now read the store" });


  await page.getByRole("button", { name: "Schema", exact: true }).click();
  for (const table of ["Doc", "Tag", "CITES", "TAGGED"]) {
    await expectText(page, table, { name: `schema lists ${table}` });
  }
  say("vector spaces are shown", (await page.getByText("semantic").count()) > 0);
  say("the primary key is named", (await page.getByText("primary key").count()) > 0);
  await shot(page, "04-schema");
  await page.getByRole("button", { name: "Indexes & DDL", exact: true }).click();
  await expectText(page, "doc_text", { name: "the text index is inventoried" });
  say("the vector index is inventoried", (await page.getByText("vector_Doc_semantic").count()) > 0);
  await shot(page, "04b-indexes");

  section("5. a statement in the query console");
  await page.getByRole("link", { name: "query" }).first().click();
  await page.waitForURL(/\/query$/);
  await runStatement("MATCH (d:Doc)-[r:CITES]->(o:Doc) RETURN d, r, o ORDER BY d.id LIMIT 5");
  const summary = await expectText(page, "rows ·", { name: "the result reports its own shape" });
  if (summary) {
    const line = await page.locator("text=/\\d+ rows · [\\d.]+ ms · lsn \\d+/").first().innerText();
    say("rows, duration and snapshot lsn are all in the header", /rows · .* ms · lsn \d+/.test(line), line);
  }
  await expectText(page, "Doc:1", { name: "entities come back as physical ids, not flattened rows" });
  await expectText(page, "CITES", { name: "the relationship table is named on its cell" });
  // A cell is an entity, so opening it is how an operator reads its properties.
  await page.getByText("Doc:1").first().click();
  const cellSection = await expectText(page, "cell · d", { name: "opening a cell shows what it holds" });
  if (cellSection) {
    await expectText(page, "durable wal", { name: "and the properties are there" });
  }
  await shot(page, "05-query");

  section("6. the same answer as a graph");
  const graphButton = page.getByRole("button", { name: /view as graph/ });
  const graphLabel = (await graphButton.innerText().catch(() => "")).replace(/\s+/g, " ");
  say("“view as graph” counts the entities it will draw", /\d+ entities\)/.test(graphLabel), graphLabel);
  await graphButton.click();
  await page.waitForURL(/graph\?mode=query/, { timeout: 15000 });
  await page.locator("canvas").first().waitFor({ timeout: 20000 });
  say("Sigma drew to a WebGL canvas", (await page.locator("canvas").count()) >= 1);
  const drawn = await expectText(page, "nodes ·", { name: "the capture says what it drew" });
  if (drawn) {
    const stats = await page.locator("text=/\\d+ nodes · \\d+ edges/").first().innerText();
    say("nodes and edges are counted separately", /\d+ nodes · \d+ edges/.test(stats), stats);
  }
  // Legibility is measured on the pixels the browser composited, not assumed from a constant: an edge
  // that is 5:1 in the source and invisible on the ground is the exact failure this canvas had.
  const canvasBox = await page.locator(".card canvas").first().boundingBox();
  const shot1 = await page.screenshot({ clip: canvasBox });
  const legible = await page.evaluate(
    async ({ dataUrl, edge, edgeLabel }) => {
      const img = new Image();
      img.src = dataUrl;
      await img.decode();
      const c = document.createElement("canvas");
      c.width = img.width;
      c.height = img.height;
      const ctx = c.getContext("2d");
      ctx.drawImage(img, 0, 0);
      const d = ctx.getImageData(0, 0, c.width, c.height).data;
      const near = (i, q, tol) =>
        Math.abs(d[i] - q[0]) <= tol && Math.abs(d[i + 1] - q[1]) <= tol && Math.abs(d[i + 2] - q[2]) <= tol;
      let strokes = 0;
      let labels = 0;
      for (let i = 0; i < d.length; i += 4) {
        if (near(i, edge, 26)) strokes += 1;
        if (near(i, edgeLabel, 22)) labels += 1;
      }
      return { strokes, labels };
    },
    { dataUrl: `data:image/png;base64,${shot1.toString("base64")}`, edge: [143, 155, 199], edgeLabel: [201, 210, 242] },
  );
  say("relationships are drawn as visible strokes", legible.strokes > 120, `${legible.strokes} px`);
  say("relationship labels are drawn", legible.labels > 60, `${legible.labels} px`);
  await expectText(page, "Click a node or an edge to inspect it", {
    name: "the panel says what is clickable before anything is clicked",
  });
  await expectText(page, "drag a node to move it", { name: "the canvas states its own interactions" });
  await shot(page, "06-graph");

  section("7. search: text, then vectors, then both fused");
  await page.goto(`${baseUrl}/databases/${slug}/search`, { waitUntil: "networkidle" });
  await page.getByRole("textbox", { name: /boolean terms/i }).fill("durable");
  await page.getByRole("button", { name: "search", exact: true }).click();
  await expectText(page, "Doc:1", { name: "full-text search found the document" });
  await expectText(page, "regime exact_index", { name: "the answer says which regime produced it" });
  await shot(page, "07-search-text");

  await page.getByRole("button", { name: "Vector", exact: true }).click();
  await page.getByRole("textbox", { name: /literal vector/i }).fill("1, 0");
  await page.getByRole("button", { name: "search", exact: true }).click();
  await expectText(page, "Doc:1", { name: "vector search found the nearest document" });
  await expectText(page, "score 1.0000", { name: "the exact scan reports its score" });
  await expectText(page, "regime exact", { name: "and says the scan was exact, not approximate" });
  await shot(page, "08-search-vector");

  await page.getByRole("button", { name: "Hybrid", exact: true }).click();
  await page.getByRole("textbox", { name: /which table owns the space/i }).fill("Doc");
  await page.getByRole("textbox", { name: /boolean terms/i }).fill("durable");
  await page.getByRole("textbox", { name: /literal vector/i }).fill("1, 0");
  await page.getByRole("button", { name: "search", exact: true }).click();
  await expectText(page, "lex ", { name: "the fusion keeps both signals visible per hit" });
  await shot(page, "08b-search-hybrid");

  section("8. an API key, minted and then used");
  await page.goto(`${baseUrl}/keys`, { waitUntil: "networkidle" });
  await page.getByLabel("name").fill("browser acceptance");
  await page.getByRole("button", { name: slug, exact: true }).click();
  await page.getByRole("button", { name: "issue key" }).click();
  await page.getByRole("button", { name: "issue key" }).click();
  const secretLocator = page.locator("code", { hasText: /^okg_/ }).first();
  await secretLocator.waitFor({ timeout: 15000 });
  const secret = (await secretLocator.innerText()).trim();
  say("the secret is shown exactly once, in the console", secret.startsWith("okg_"), `${secret.slice(0, 12)}…`);
  await expectText(page, "only time the secret is shown", { name: "and the console says so" });
  await shot(page, "09-key");

  await page.getByRole("button", { name: "use here" }).click();
  await expectText(page, "querying as a scoped key", { name: "the console now acts as that key" });
  await page.goto(`${baseUrl}/databases/${slug}/query`, { waitUntil: "networkidle" });
  await runStatement("MATCH (d:Doc) RETURN d.id ORDER BY d.id");
  await expectText(page, "4 rows", { name: "the key reads through the same console" });
  await shot(page, "10-key-read");

  section("9. the doors the daemon holds shut");
  // A write statement on a read-only key, over the HTTP door. The 403 is the result we came for.
  expectedRefusals.push(`403 /api/v1/databases/${slug}/query`);
  await runStatement("CREATE (:Doc {id: 999, title: 'written from the browser'})");
  await expectText(page, "write_not_permitted", { name: "an HTTP write is refused" });
  await expectText(page, "read door", { name: "the refusal says which door blocked it" });
  // The machine-readable reason lives behind "details", exactly where an operator goes for it.
  await page.getByRole("button", { name: "details" }).last().click();
  await expectText(page, "read_only_door", { name: "the details carry the reason code" });
  await shot(page, "11-refusal");

  // The same key against the WebSocket door: reads work, a write session is refused at hello.
  const ws = await page.evaluate(
    async ({ secret, slug }) => {
      // Drive the socket the way the protocol says: hello, then a transaction, then the statement.
      const run = (mode, statement) =>
        new Promise((resolve) => {
          const frames = [];
          const socket = new WebSocket(`ws://${location.host}/api/v1/ws?token=${encodeURIComponent(secret)}`);
          let step = 0;
          const send = (message) => socket.send(JSON.stringify(message));
          const finish = () => {
            try { socket.close(); } catch { /* already closed */ }
            resolve(frames);
          };
          socket.addEventListener("open", () => send({ type: "hello", db: slug, mode }));
          socket.addEventListener("message", (event) => {
            const frame = JSON.parse(event.data);
            frames.push(frame);
            if (frame.type === "hello_ok" && mode === "read") {
              send({ type: "begin", mode: "read", id: ++step });
            } else if (frame.type === "begin_ok") {
              send({ type: "execute", id: ++step, query: statement });
            } else if (frame.type === "batch_end") {
              send({ type: "rollback", id: ++step });
              setTimeout(finish, 300);
            } else if (frame.type === "error") {
              setTimeout(finish, 200);
            }
          });
          socket.addEventListener("close", finish);
          socket.addEventListener("error", finish);
          setTimeout(finish, 12000);
        });

      const readFrames = await run("read", "MATCH (d:Doc) RETURN d.id AS id ORDER BY d.id");
      const writeFrames = await run("write", "CREATE (:Doc {id: 999})");
      const rows = readFrames
        .filter((frame) => frame.type === "batch")
        .reduce((total, frame) => total + (frame.rows?.length ?? 0), 0);
      const code = (frames) => frames.find((frame) => frame.type === "error")?.code ?? null;
      return {
        read: readFrames.map((frame) => frame.type).join(" → "),
        readCode: code(readFrames),
        rows,
        write: writeFrames.map((frame) => frame.type).join(" → "),
        writeCode: code(writeFrames),
      };
    },
    { secret, slug },
  );
  say("a read session runs a statement over the socket",
      ws.read.includes("begin_ok") && ws.rows > 0 && !ws.readCode, `${ws.read} · ${ws.rows} rows`);
  say("the same key cannot open a write session", ws.writeCode === "write_not_permitted",
      `${ws.write} → ${ws.writeCode}`);

  section("10. the maintenance the read door still allows");
  await page.goto(`${baseUrl}/databases/${slug}`, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "Maintenance", exact: true }).click();
  const verified = await runMaintenance("verify");
  say("verify runs read-only", verified.state === "succeeded",
      JSON.stringify(verified.detail?.report ?? {}).slice(0, 110));
  const bloat = await runMaintenance("bloat");
  say("bloat runs read-only", bloat.state === "succeeded", JSON.stringify(bloat.detail?.report ?? {}).slice(0, 110));
  await shot(page, "12-maintenance");

  section("11. the audit trail names what happened");
  await page.goto(`${baseUrl}/audit`, { waitUntil: "networkidle" });
  for (const action of ["key.create", "query", "maintenance.verify", "database.update"]) {
    await expectText(page, action, { name: `the trail recorded ${action}` });
  }
  await page.getByLabel("action").fill("maintenance.verify");
  await page.waitForFunction(
    () => document.querySelectorAll("tbody tr").length <= 6,
    { timeout: 15000 },
  ).catch(() => {});
  const filtered = await page.locator("tbody tr").count();
  say("the action filter narrows the trail", filtered > 0 && filtered < 30, `${filtered} rows`);
  await shot(page, "13-audit");

  section("12. the machine-readable surfaces");
  const openapi = await page.request.get(`${baseUrl}/api/v1/openapi.json`);
  const spec = await openapi.json();
  const paths = Object.keys(spec.paths ?? {});
  say(`GET /api/v1/openapi.json — ${openapi.status()}, ${paths.length} paths`, openapi.status() === 200 && paths.length > 30);
  say("the error envelope is in the schema", JSON.stringify(spec.components ?? {}).includes("ErrorEnvelope"));
  say("both doors are documented separately",
      paths.some((path) => path.startsWith("/api/v1/") && path.includes("/query"))
        && paths.some((path) => path.startsWith("/admin/api/")),
      `${paths.filter((path) => path.startsWith("/api/v1/")).length} data-plane · ${paths.filter((path) => path.startsWith("/admin/api/")).length} admin`);
  const metrics = await page.request.get(`${baseUrl}/metrics`);
  const metricsText = await metrics.text();
  say("GET /metrics answers in Prometheus format",
      metrics.status() === 200 && /oktografx_/.test(metricsText),
      metricsText.split("\n").filter((line) => line.startsWith("oktografx_")).slice(0, 2).join(" "));
  await page.goto(`${baseUrl}/settings`, { waitUntil: "networkidle" });
  await expectText(page, "reader_lane_timeout_seconds", { name: "the limits table is the daemon's own" });
  await expectText(page, "logical access control, not an OS sandbox", { name: "the security boundary is stated on screen" });
  await page.getByText("show the option schema").click();
  await expectText(page, "page_size", { name: "the DatabaseConfig schema the form is built from" });
  await shot(page, "14-settings");

  say("the page never logged a JavaScript error", consoleErrors.length === 0, consoleErrors.slice(0, 2).join(" | "));
  const surprises = [...new Set(httpErrors.filter((entry) => !expectedRefusals.some((path) => entry.includes(path))))];
  say(`no HTTP failures beyond the ${expectedRefusals.length} refusal(s) this flow asked for`, surprises.length === 0,
      surprises.slice(0, 4).join(" | "));

} catch (error) {
  await shot(page, "99-crash").catch(() => "");
  say(`the flow completed (${error.message.slice(0, 90)})`, false);
} finally {
  await browser.close();
  console.log(`\n\x1b[1m${passes.length} steps passed, ${failures.length} failed\x1b[0m`);
  console.log(`screenshots: ${shots}`);
  for (const name of failures) console.log(`  failed: ${name}`);
  process.exit(failures.length === 0 ? 0 : 1);
}
