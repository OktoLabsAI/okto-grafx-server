import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import { App } from "@/App";
import { mockFetch } from "@/test/setup";

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("the console's own door", () => {
  it("sends an unauthenticated visitor to the login screen", async () => {
    // This is what the daemon actually answers for an anonymous caller: 200 with signed_in false,
    // because the login screen is allowed to ask whether a token is configured at all.
    mockFetch({
      "/admin/api/session": { status: 200, body: { signed_in: false, token_configured: true, server_version: "0.1.0" } },
    });
    renderAt("/databases");
    expect(await screen.findByText(/Sign in with the administrator token/)).toBeInTheDocument();
    expect(screen.getByLabelText("admin token")).toBeInTheDocument();
  });

  it("sends a visitor whose credential was actively refused to the login screen", async () => {
    mockFetch({
      "/admin/api/session": {
        status: 401,
        body: {
          error: {
            code: "admin_session_required",
            message: "An administrator session is required.",
            retryable: false,
            details: {},
          },
        },
      },
    });
    renderAt("/keys");
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
  });

  it("shows the store list, its door and its physical identity once signed in", async () => {
    mockFetch({
      "/admin/api/session": { status: 200, body: { signed_in: true, token_configured: true, server_version: "0.1.0" } },
      "/admin/api/status": {
        status: 200,
        body: { handles: [{ slug: "docs", read_only: false }], open_slugs: ["docs"], runner: {}, ws_sessions: 0, uptime_seconds: 3 },
      },
      "/admin/api/databases": {
        body: {
          databases: [
            {
              slug: "docs",
              name: "docs",
              path: "/srv/grafx/docs",
              database_uuid: "967f62b10a6a4586b77680fe75730347",
              format_version: 2,
              read_only: false,
              allow_writes: true,
              options: {},
              enabled: true,
              open_policy: "pinned",
              open: true,
              open_transactions: 0,
              note: "",
              created_at: "2026-01-01T00:00:00+00:00",
              updated_at: "2026-01-01T00:00:00+00:00",
            },
          ],
        },
      },
    });
    renderAt("/databases");
    // The slug appears three times on purpose: as the row's identity, as its name, and in the
    // store picker at the top of the console.
    expect((await screen.findAllByText("docs")).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("967f62b10a6a4586b77680fe75730347")).toBeInTheDocument();
    expect(screen.getByText("writable")).toBeInTheDocument();
    expect(screen.getByText("admin session")).toBeInTheDocument();
    expect(screen.getByText("/srv/grafx/docs")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "eject" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "close" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "remove" })).toBeInTheDocument();
  });
});
describe("attaching a store by reference", () => {
  const SETTINGS = {
    server_version: "0.1.0",
    bind: { host: "127.0.0.1", port: 8737 },
    state_dir: "/var/lib/okto-grafx-server",
    allowed_roots: ["/srv/grafx"],
    limits: { max_open_handles: 4, readers_per_database: 2, reader_lane_timeout_seconds: 5 },
    security: { tls: false, cors_origins: [], metrics_enabled: false, note: "loopback only" },
    admin_auth: {
      mode: "token",
      token_configured: true,
      token_rotated_at: null,
      commands: {
        require: "oktografx-server --state-dir /var/lib/okto-grafx-server admin-token rotate",
        rotate: "oktografx-server --state-dir /var/lib/okto-grafx-server admin-token rotate",
        stop_requiring: "oktografx-server --state-dir /var/lib/okto-grafx-server admin-token clear",
      },
    },
    database_options: [
      {
        name: "page_size",
        type: "integer",
        default: 8192,
        nullable: false,
        group: "storage",
        label: "page size",
        description: "Fixed when the store is created.",
        minimum: 4096,
        maximum: 32768,
      },
    ],
    maintenance_actions: [],
  };

  it("offers the candidate directories the daemon found, then attaches the one clicked", async () => {
    const { calls } = mockFetch({
      "/admin/api/session": { status: 200, body: { signed_in: true, token_configured: true } },
      "/admin/api/status": { status: 200, body: { handles: [], open_slugs: [], runner: {}, ws_sessions: 0, uptime_seconds: 1 } },
      "/admin/api/databases": [
        { status: 200, body: { databases: [] } },
        { status: 201, body: { slug: "events", name: "events", path: "/srv/grafx/events", database_uuid: "3f2a" } },
      ],
      "/admin/api/roots": {
        status: 200,
        body: {
          roots: [
            {
              path: "/srv/grafx",
              exists: true,
              stores: [
                { path: "/srv/grafx/events", name: "events" },
                { path: "/srv/grafx/notes", name: "notes" },
              ],
            },
          ],
        },
      },
      "/admin/api/settings": { status: 200, body: SETTINGS },
      "/admin/api/databases/inspect": {
        status: 200,
        body: {
          path: "/srv/grafx/events",
          bytes: 8_700_000,
          files: 44,
          marker: "grafx.meta",
          already_registered_as: null,
          uuid_registered_elsewhere: null,
          database_uuid: "3f2a",
          page_size: 8192,
          format_version: 2,
          node_tables: ["Event"],
          relationship_tables: ["CAUSES"],
          vector_spaces: [],
          recovery_required: false,
        },
      },
    });

    renderAt("/databases");
    // The empty state names the button that fixes it; the note about allowed_roots is split by a
    // <code> element, so the button is what the test anchors on.
    await userEvent.click(await screen.findByRole("button", { name: "Add existing store" }));
    await screen.findByText("/srv/grafx");

    await userEvent.click(screen.getByRole("button", { name: "events" }));
    expect(await screen.findByText("uuid 3f2a")).toBeInTheDocument();
    expect(screen.getByText("1 node · 1 relationship tables")).toBeInTheDocument();

    // The slug the console offers is the directory's own name, still editable.
    expect(screen.getByLabelText(/database id/)).toHaveValue("events");
    await userEvent.click(screen.getByRole("button", { name: "attach store" }));

    const attached = calls.find((call) => call.url === "/admin/api/databases" && call.init?.method === "POST");
    expect(attached?.init?.body).toBeDefined();
    expect(JSON.parse(String(attached?.init?.body))).toEqual({
      path: "/srv/grafx/events",
      slug: "events",
      name: "events",
      read_only: true,
      allow_writes: false,
      open_policy: "lazy",
      options: {},
    });
  });

  it("shows the daemon's refusal when the candidate is not a store", async () => {
    mockFetch({
      "/admin/api/session": { status: 200, body: { signed_in: true, token_configured: true } },
      "/admin/api/status": { status: 200, body: { handles: [], open_slugs: [], runner: {}, ws_sessions: 0, uptime_seconds: 1 } },
      "/admin/api/databases": { status: 200, body: { databases: [] } },
      "/admin/api/roots": { status: 200, body: { roots: [] } },
      "/admin/api/settings": { status: 200, body: SETTINGS },
      "/admin/api/databases/inspect": {
        status: 422,
        body: {
          error: {
            code: "not_a_grafx_store",
            message: "That directory has no grafx.meta, so it is not a store.",
            retryable: false,
            details: { path: "/srv/grafx/random" },
            request_id: "req7",
          },
        },
      },
    });
    renderAt("/databases");
    // The empty state names the button that fixes it; the note about allowed_roots is split by a
    // <code> element, so the button is what the test anchors on.
    await userEvent.click(await screen.findByRole("button", { name: "Add existing store" }));
    const pathBox = screen.getByLabelText(/store directory/);
    await userEvent.clear(pathBox);
    await userEvent.type(pathBox, "/srv/grafx/random");
    await userEvent.click(screen.getByRole("button", { name: "inspect" }));
    expect(await screen.findByText("not_a_grafx_store")).toBeInTheDocument();
    expect(screen.getByText(/no grafx.meta/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "attach store" })).toBeDisabled();
  });
});
  it("keeps a signed-in operator on the console instead of bouncing back", async () => {
    // The shell's earlier answer ("not signed in") is cached; signing in has to replace it, or the
    // shell redirects the operator straight back to the screen they just left.
    mockFetch({
      "/admin/api/session": [
        { status: 200, body: { signed_in: false, token_configured: true } },
        { status: 200, body: { signed_in: true, token_configured: true } },
        { status: 200, body: { signed_in: true, token_configured: true } },
      ],
      "/admin/api/login": { status: 200, body: { signed_in: true, expires_in_seconds: 43200 } },
      "/admin/api/status": { status: 200, body: { handles: [], open_slugs: [], runner: {}, ws_sessions: 0, uptime_seconds: 1 } },
      "/admin/api/databases": { status: 200, body: { databases: [] } },
    });
    renderAt("/some/deep/route");
    const tokenBox = await screen.findByLabelText("admin token");
    await userEvent.type(tokenBox, "okga_test_token");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("button", { name: "Add existing store" })).toBeInTheDocument();
    expect(screen.getByText("admin session")).toBeInTheDocument();
  });
  it("does not ask the admin API for anything it cannot be given", async () => {
    // The redirect to /login is one thing; firing off store, key and audit queries that must all
    // answer 401 on the way there is another. An anonymous visitor should cost one request.
    const { calls } = mockFetch({
      "/admin/api/session": { status: 200, body: { signed_in: false, token_configured: true } },
    });
    renderAt("/audit");
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
    // The only question the console is allowed to ask without a session is whether it has one.
    expect(calls.map((call) => call.url).filter((url) => url !== "/admin/api/session")).toEqual([]);
  });

  it("says out loud that the server does not generate embeddings", async () => {
    // A vector search is not a semantic search by itself. The plan asks the search screen to say so,
    // because an operator who believes otherwise will read the hits as meaning something they do not.
    mockFetch({
      "/admin/api/session": { status: 200, body: { signed_in: true, token_configured: true } },
      "/admin/api/status": { status: 200, body: { handles: [], open_slugs: [], runner: {}, ws_sessions: 0, uptime_seconds: 1 } },
      "/admin/api/databases/docs/inventory": {
        status: 200,
        body: {
          slug: "docs",
          indexes: [{ name: "doc_text", kind: "text", table: "Doc", stale: false }],
          vector_indexes: [],
          stale: [],
          maintenance: { recovery_required: false },
          schema: {
            slug: "docs",
            database_uuid: "3f2a",
            read_only: true,
            tables: [
              {
                table_id: 1,
                name: "Doc",
                kind: "node",
                primary_key: "id",
                schema_version: 1,
                columns: [
                  { name: "body", type: "STRING", nullable: true, vector_space: null },
                  { name: "v", type: "VECTOR_F32", nullable: true, vector_space: "semantic" },
                ],
              },
            ],
            relationships: [],
            vector_spaces: [
              { space_id: 1, name: "semantic", dimension: 2, metric: "COSINE", normalized: false, storage_dtype: "float32", state: "active" },
            ],
          },
        },
      },
    });

    renderAt("/databases/docs/search");
    await userEvent.click(await screen.findByRole("button", { name: "Vector" }));
    expect(await screen.findByText(/does not generate embeddings/i)).toBeInTheDocument();
    expect(screen.getByText(/The vector above is yours/i)).toBeInTheDocument();
  });

  it("offers only the indexes that can answer a full-text query", async () => {
    // A real store lists its identity and edge indexes first (`pk_…`, `ef_…`, `et_…`). They are real
    // indexes, and the engine refuses them here with `index_error` — so the picker is built from the
    // kind the store reported, not from "everything that is not a vector index".
    mockFetch({
      "/admin/api/session": { status: 200, body: { signed_in: true, token_configured: true } },
      "/admin/api/status": { status: 200, body: { handles: [], open_slugs: [], runner: {}, ws_sessions: 0, uptime_seconds: 1 } },
      "/admin/api/databases/docs/inventory": {
        status: 200,
        body: {
          slug: "docs",
          indexes: [
            { name: "ef_CITES", kind: "exact", table: "CITES", stale: false },
            { name: "pk_Doc", kind: "exact", table: "Doc", stale: false },
            { name: "vector_Doc_semantic", kind: "vector", table: "Doc", stale: false },
            { name: "doc_text", kind: "text", table: "Doc", stale: false },
          ],
          vector_indexes: [],
          stale: [],
          maintenance: { recovery_required: false },
          schema: {
            slug: "docs",
            database_uuid: "3f2a",
            read_only: true,
            tables: [{ name: "Doc", kind: "node", primary_key: "id", schema_version: 1, columns: [] }],
            relationships: [],
            vector_spaces: [],
          },
        },
      },
    });

    renderAt("/databases/docs/search");
    // O seletor aparece antes do inventário resolver: a asserção espera pelo conteúdo, não pelo elemento.
    const picker = (await screen.findByRole("combobox", { name: /index/i })) as HTMLSelectElement;
    await waitFor(() =>
      expect(Array.from(picker.options).map((option) => option.textContent)).toEqual([
        "doc_text (first available)",
        "doc_text · Doc",
      ]),
    );
  });

  it("names the page budget and the quarantine of every open store", async () => {
    // Memory is the reason the LRU exists, and a quarantined handle is a fact the operator has to
    // see without reading the daemon's log: both come straight out of /admin/api/status.
    mockFetch({
      "/admin/api/session": { status: 200, body: { signed_in: true, token_configured: true } },
      "/admin/api/status": {
        status: 200,
        body: {
          handles: [
            {
              slug: "docs",
              path: "/srv/grafx/docs",
              readers: 2,
              idle_readers: 2,
              writer_open: false,
              active_operations: 0,
              open_transactions: 0,
              idle_seconds: 1.5,
              buffer_budget_bytes: 64 * 1024 * 1024,
              quarantined: false,
            },
            {
              slug: "events",
              path: "/srv/grafx/events",
              readers: 0,
              idle_readers: 0,
              writer_open: false,
              active_operations: 0,
              open_transactions: 0,
              idle_seconds: 0.2,
              buffer_budget_bytes: 128 * 1024 * 1024,
              quarantined: true,
            },
          ],
          open_slugs: ["docs", "events"],
          runner: {},
          ws_sessions: 0,
          uptime_seconds: 1,
        },
      },
      "/admin/api/databases": { status: 200, body: { databases: [] } },
      // The handle table is driven by /admin/api/status alone; the option catalogue is irrelevant.
      "/admin/api/settings": {
        status: 200,
        body: {
          server_version: "0.1.0",
          bind: { host: "127.0.0.1", port: 8737 },
          state_dir: "/var/lib/okto-grafx-server",
          allowed_roots: ["/srv/grafx"],
          limits: { max_open_handles: 4, readers_per_database: 2, reader_lane_timeout_seconds: 5 },
          security: { tls: false, cors_origins: [], metrics_enabled: false, note: "loopback only" },
          admin_auth: {
            mode: "token",
            token_configured: true,
            token_rotated_at: null,
            commands: {
              require: "oktografx-server --state-dir /var/lib/okto-grafx-server admin-token rotate",
              rotate: "oktografx-server --state-dir /var/lib/okto-grafx-server admin-token rotate",
              stop_requiring: "oktografx-server --state-dir /var/lib/okto-grafx-server admin-token clear",
            },
          },
          database_options: [],
          maintenance_actions: [],
        },
      },
    });

    renderAt("/settings");
    await screen.findByText("64.0 MiB");
    expect(screen.getByText("128.0 MiB")).toBeInTheDocument();
    expect(screen.getByText("recovery required")).toBeInTheDocument();
    expect(screen.getByText(/budget times|budget ×/i)).toBeInTheDocument();
  });

describe("administrator access with no token", () => {
  const STATUS = { handles: [], open_slugs: [], runner: {}, ws_sessions: 0, uptime_seconds: 1 };

  function settingsBody(mode: "token" | "open-loopback") {
    const state = "/var/lib/okto-grafx-server";
    return {
      server_version: "0.1.1",
      bind: { host: "127.0.0.1", port: 8737 },
      state_dir: state,
      allowed_roots: [],
      limits: {},
      security: { tls: false, cors_origins: [], metrics_enabled: false, note: "loopback only" },
      admin_auth: {
        mode,
        token_configured: mode === "token",
        token_rotated_at: mode === "token" ? "2026-11-20T09:00:00+00:00" : null,
        commands: {
          require: `oktografx-server --state-dir ${state} admin-token rotate`,
          rotate: `oktografx-server --state-dir ${state} admin-token rotate`,
          stop_requiring: `oktografx-server --state-dir ${state} admin-token clear`,
        },
      },
      database_options: [],
      maintenance_actions: [],
    };
  }

  const OPEN_SESSION = { signed_in: true, token_configured: false, auth: "open-loopback" };

  it("shows the posture on the rail and offers no sign-out of a door with no lock", async () => {
    mockFetch({
      "/admin/api/session": { status: 200, body: OPEN_SESSION },
      "/admin/api/status": { status: 200, body: STATUS },
      "/admin/api/databases": { status: 200, body: { databases: [] } },
    });

    renderAt("/databases");

    expect(await screen.findByText("this machine only")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /sign out/i })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /token is off/i })).toBeInTheDocument();
  });

  it("offers to require a token while none exists, and shows the value once", async () => {
    const { calls } = mockFetch({
      "/admin/api/session": { status: 200, body: OPEN_SESSION },
      "/admin/api/status": { status: 200, body: STATUS },
      "/admin/api/settings": { status: 200, body: settingsBody("open-loopback") },
      "/admin/api/admin-token": {
        status: 200,
        body: { admin_token: "okga_fresh", shown_once: true, signed_in: true, note: "keep it" },
      },
    });

    renderAt("/settings");
    // The panel states the default in the operator's terms and hands over the command line version.
    expect(await screen.findByText("no token — this machine only")).toBeInTheDocument();
    expect(
      screen.getByText("oktografx-server --state-dir /var/lib/okto-grafx-server admin-token rotate"),
    ).toBeInTheDocument();
    // Turning it on is a deliberate act: arming shows what changes before anything is minted.
    const button = await screen.findByRole("button", { name: /require a token/i });
    expect(screen.queryByText(/Any process on this machine can create API keys/i)).toBeNull();
    await userEvent.click(button);
    expect(screen.getByText(/Any process on this machine can create API keys/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /require a token/i }));

    expect(await screen.findByText("okga_fresh")).toBeInTheDocument();
    expect(
      calls.some((call) => call.url.endsWith("/admin/api/admin-token") && call.init?.method === "POST"),
    ).toBe(true);
  });

  it("offers rotation and the way back out when a token is required", async () => {
    mockFetch({
      "/admin/api/session": { status: 200, body: { signed_in: true, token_configured: true, auth: "token" } },
      "/admin/api/status": { status: 200, body: STATUS },
      "/admin/api/settings": { status: 200, body: settingsBody("token") },
    });

    renderAt("/settings");

    expect(await screen.findByText("token required")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /rotate admin token/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /stop requiring a token/i })).toBeInTheDocument();
    expect(screen.getByText("2026-11-20T09:00:00+00:00")).toBeInTheDocument();
    // Nothing to mint here: minting over a live token is what rotation exists to prevent.
    expect(screen.queryByRole("button", { name: /require a token$/i })).not.toBeInTheDocument();
  });
});
