import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import { App } from "@/App";
import { mockFetch } from "@/test/setup";

/**
 * The graph screen, tested where WebGL does not exist.
 *
 * jsdom has no WebGL, which is exactly the environment the canvas has to survive: a locked-down VM, a
 * server-side render, a GPU-less test runner. The screen answers that by listing the capture instead of
 * drawing it, so the thing worth asserting here is not pixels but the contract operators rely on — a
 * node and a relationship in this view are both clickable, and both show what the store really returned.
 */
const SESSION = {
  "/admin/api/session": { status: 200, body: { signed_in: true, token_configured: true, server_version: "0.1.0" } },
  "/admin/api/status": {
    status: 200,
    body: { handles: [{ slug: "docs", read_only: false }], open_slugs: ["docs"], runner: {}, ws_sessions: 0, uptime_seconds: 3 },
  },
  "/admin/api/databases": {
    status: 200,
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
  "/api/v1/databases/docs/schema": {
    status: 200,
    body: {
      writable: false,
      tables: [{ name: "Doc", record_count: 1, properties: [{ name: "title", type: "text" }] }],
      relationships: [{ name: "TAGGED", record_count: 1, properties: [{ name: "weight", type: "number" }] }],
    },
  },
};

const CAPTURE = {
  nodes: [
    {
      id: "Doc:1",
      label: "Doc:1",
      table: "Doc",
      record_id: 1,
      category: "Doc",
      properties: { title: "durable wal" },
      source_block: "b1",
    },
    {
      id: "Tag:9",
      label: "Tag:9",
      table: "Tag",
      record_id: 9,
      category: "Tag",
      properties: { name: "storage" },
      source_block: "b1",
    },
  ],
  edges: [
    {
      id: "e7",
      source: "Doc:1",
      target: "Tag:9",
      label: "TAGGED",
      table: "TAGGED",
      record_id: 4,
      properties: { weight: 0.75 },
      source_block: "b2",
    },
  ],
  shown_nodes: 2,
  total_nodes: 2,
  shown_edges: 1,
  total_edges: 1,
  truncated: false,
  snapshot_lsn: 12,
  database_uuid: "967f62b10a6a4586b77680fe75730347",
  mode: "query",
  warnings: [],
};

function renderGraph() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter
        initialEntries={["/databases/docs/graph?mode=query&query=MATCH%20(n)-[r%5D->(m)%20RETURN%20n,r,m"]}
      >
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("inspecting a capture without WebGL", () => {
  it("says the picture is listed rather than drawn, instead of failing to render", async () => {
    mockFetch({ ...SESSION, "/api/v1/databases/docs/graph": { status: 200, body: CAPTURE } });
    renderGraph();
    expect(await screen.findByText(/WebGL is unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/loops and parallel edges are kept as drawn/)).toBeInTheDocument();
  });

  it("shows a node's physical identity and the properties the store returned", async () => {
    mockFetch({ ...SESSION, "/api/v1/databases/docs/graph": { status: 200, body: CAPTURE } });
    renderGraph();
    const user = userEvent.setup();
    const nodeButton = await screen.findByRole("button", { name: /title=durable wal/ });
    await user.click(nodeButton);

    expect(await screen.findByText("record 1")).toBeInTheDocument();
    expect(screen.getByText(/"title": "durable wal"/)).toBeInTheDocument();
    // Its relationship is reachable from here rather than mentioned in passing.
    expect(screen.getByRole("button", { name: /TAGGED · → Tag:9/ })).toBeInTheDocument();
  });

  it("shows a relationship with its direction and both endpoints", async () => {
    mockFetch({ ...SESSION, "/api/v1/databases/docs/graph": { status: 200, body: CAPTURE } });
    renderGraph();
    const user = userEvent.setup();
    const edgeButton = await screen.findByRole("button", { name: /TAGGED · Doc:1 → Tag:9/ });
    await user.click(edgeButton);

    // Sigma 4 draws no arrowhead, so the panel has to say which way the relationship points.
    expect(await screen.findByText(/Doc:1 is the source, Tag:9 the target/)).toBeInTheDocument();
    expect(screen.getByText("record 4")).toBeInTheDocument();
    expect(screen.getByText(/"weight": 0.75/)).toBeInTheDocument();
    expect(screen.getByText("physical edge id e7")).toBeInTheDocument();
    // The endpoints are selectable, so a relationship is a way into a node, not a dead end. The same
    // node is reachable twice here — in the listed capture and as this edge's target — and either
    // route must land on the same physical record.
    const endpoints = screen.getAllByRole("button", { name: /Tag · name=storage/ });
    expect(endpoints.length).toBeGreaterThan(1);
    await user.click(endpoints[endpoints.length - 1]);
    expect(await screen.findByText("record 9")).toBeInTheDocument();
  });
});
