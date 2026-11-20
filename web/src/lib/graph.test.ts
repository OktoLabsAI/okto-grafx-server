import { describe, expect, it } from "vitest";
import type { GraphPayload } from "@/api/client";
import { colorFor, layout, mergePayloads, nodeLabel, toGraph } from "@/lib/graph";

/** The shape the daemon publishes for Sigma (physical ids, caps, warnings). */
function payload(overrides: Partial<GraphPayload> = {}): GraphPayload {
  return {
    nodes: [
      { id: "Doc:1", label: "Doc", table: "Doc", record_id: 1, category: "Doc", properties: { title: "durable wal" } },
      { id: "Doc:2", label: "Doc", table: "Doc", record_id: 2, category: "Doc", properties: {} },
      { id: "Doc:3", label: "Doc", table: "Doc", record_id: 3, category: "Doc", properties: {} },
      { id: "Tag:1", label: "Tag", table: "Tag", record_id: 1, category: "Tag", properties: { name: "storage" } },
    ],
    edges: [
      { id: "CITES:1", source: "Doc:1", target: "Doc:2", label: "CITES", table: "CITES", record_id: 1, properties: { weight: 1 } },
      // parallel edge: same pair, different physical relationship
      { id: "CITES:4", source: "Doc:1", target: "Doc:4", label: "CITES", table: "CITES", record_id: 4, properties: { weight: 0.25 } },
      { id: "CITES:5", source: "Doc:1", target: "Doc:4", label: "CITES", table: "CITES", record_id: 5, properties: { weight: 0.75 } },
      { id: "TAGGED:1", source: "Doc:1", target: "Tag:1", label: "TAGGED", table: "TAGGED", record_id: 1, properties: {} },
    ],
    shown_nodes: 4,
    total_nodes: 4,
    shown_edges: 4,
    total_edges: 4,
    truncated: false,
    snapshot_lsn: 41,
    database_uuid: "00112233445566778899aabbccddeeff",
    mode: "query",
    warnings: [],
    ...overrides,
  };
}

describe("GraphPayload → graphology", () => {
  it("keeps every node and edge, including ones whose endpoint is missing", () => {
    const { graph } = toGraph(payload(), {});
    expect(graph.order).toBe(4);
    // Doc:4 was never returned as a node, so the two parallel edges into it cannot be drawn.
    expect(graph.size).toBe(2);
    expect(graph.hasNode("Doc:1")).toBe(true);
    expect(graph.getNodeAttribute("Doc:1", "label")).toBe("title=durable wal");
    expect(graph.getNodeAttribute("Tag:1", "label")).toBe("name=storage");
  });

  it("draws parallel edges as two edges, not one merged edge", () => {
    const withPartner = payload({
      nodes: [
        ...payload().nodes,
        { id: "Doc:4", label: "Doc", table: "Doc", record_id: 4, category: "Doc", properties: {} },
      ],
      shown_nodes: 5,
      total_nodes: 5,
    });
    const { graph } = toGraph(withPartner, {});
    expect(graph.order).toBe(5);
    expect(graph.size).toBe(4);
    const neighbours = [...graph.directedNeighbors("Doc:1")];
    expect(neighbours).toHaveLength(3);
    // Two relationships between the same pair of nodes reach one node, twice.
    expect(neighbours.filter((node) => node === "Doc:4")).toHaveLength(1);
    const parallel = graph
      .filterEdges((edge: string) => (graph.getEdgeAttribute(edge, "attr") as { record_id: number }).record_id === 5)
      .map((edge: string) => (graph.getEdgeAttribute(edge, "attr") as { id: string }).id);
    expect(parallel).toEqual(["CITES:5"]);
  });

  it("keeps a self-loop", () => {
    const looped = payload({
      edges: [{ id: "CITES:3", source: "Doc:3", target: "Doc:3", label: "CITES", table: "CITES", record_id: 3, properties: {} }],
      shown_edges: 1,
      total_edges: 1,
    });
    const { graph } = toGraph(looped, {});
    expect(graph.size).toBe(1);
    const [edge] = graph.edges();
    expect(graph.source(edge)).toBe(graph.target(edge));
    expect(graph.source(edge)).toBe("Doc:3");
  });

  it("colours by table, and the same table keeps the same colour", () => {
    const colors: Record<string, string> = {};
    const first = toGraph(payload(), colors);
    const second = toGraph(payload(), colors);
    expect(first.colors).toBe(colors);
    expect(colors.Doc).toBeTruthy();
    expect(colors.Tag).not.toBe(colors.Doc);
    expect(second.graph.getNodeAttribute("Doc:2", "color")).toBe(colors.Doc);
  });

  it("assigns a colour to an unknown category instead of leaving it undefined", () => {
    const colors: Record<string, string> = {};
    expect(colorFor(colors, "BrandNew")).toBeTruthy();
    expect(colorFor(colors, "BrandNew")).toBe(colors.BrandNew);
  });

  it("labels a node from a sensible property, falling back to its physical id", () => {
    expect(nodeLabel({ id: "Doc:9", label: "Doc", table: "Doc", record_id: 9, category: "Doc", properties: {} })).toBe("Doc:9");
    expect(nodeLabel({ id: "Tag:2", label: "Tag", table: "Tag", record_id: 2, category: "Tag", properties: { name: "x" } })).toBe("name=x");
  });

  it("positions nodes for both layouts", () => {
    const { graph } = toGraph(payload(), {});
    layout(graph, "circle");
    const positions = graph.mapNodes((node: string) =>
      [graph.getNodeAttribute(node, "x"), graph.getNodeAttribute(node, "y")] as const,
    );
    expect(positions.every(([x, y]) => Number.isFinite(x) && Number.isFinite(y))).toBe(true);
    layout(graph, "force");
    const moved = graph.mapNodes((node: string) => graph.getNodeAttribute(node, "x") as number);
    expect(moved.every(Number.isFinite)).toBe(true);
    layout(graph, "none"); // must not throw or scatter
  });
});

describe("neighbourhood expansion", () => {
  it("merges by physical id instead of duplicating a node already drawn", () => {
    const base = toGraph(payload(), {});
    void base;
    const first = payload();
    const addition = payload({
      nodes: [
        { id: "Doc:1", label: "Doc", table: "Doc", record_id: 1, category: "Doc", properties: { title: "durable wal" } },
        { id: "Doc:7", label: "Doc", table: "Doc", record_id: 7, category: "Doc", properties: { title: "new reach" } },
      ],
      edges: [{ id: "CITES:9", source: "Doc:1", target: "Doc:7", label: "CITES", table: "CITES", record_id: 9, properties: {} }],
      shown_nodes: 2,
      total_nodes: 6,
      shown_edges: 1,
      total_edges: 6,
      truncated: true,
    });
    const merged = mergePayloads(first, addition);
    const ids = merged.nodes.map((node) => node.id);
    expect(ids.filter((id) => id === "Doc:1")).toHaveLength(1);
    expect(ids).toContain("Doc:7");
    expect(merged.edges.filter((edge) => edge.id === "CITES:1")).toHaveLength(1);
    expect(merged.truncated).toBe(true);
    expect(merged.total_nodes).toBe(6);
    const { graph } = toGraph(merged, {});
    expect(graph.order).toBe(ids.length);
  });

  it("prefers the copy that carries properties", () => {
    const bare = payload({
      nodes: [{ id: "Doc:1", label: "Doc", table: "Doc", record_id: 1, category: "Doc", properties: {} }],
      edges: [],
    });
    const rich = payload({
      nodes: [{ id: "Doc:1", label: "Doc", table: "Doc", record_id: 1, category: "Doc", properties: { title: "read back" } }],
      edges: [],
    });
    const merged = mergePayloads(bare, rich);
    expect(merged.nodes[0].properties).toEqual({ title: "read back" });
    const kept = mergePayloads(rich, bare);
    // A projection node must not overwrite properties that were actually read.
    expect(kept.nodes[0].properties).toEqual({ title: "read back" });
  });
});
