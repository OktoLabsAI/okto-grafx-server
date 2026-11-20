import { describe, expect, it } from "vitest";
import type { GraphEdge, GraphNode, GraphPayload } from "@/api/client";
import { graphAdjacency, toGraph } from "@/lib/graph";
import {
  contrastRatio,
  degreesOf,
  edgeLabelOf,
  edgeSize,
  emphasis,
  GRAPH_CANVAS,
  nodeSize,
  seedCoords,
  truncateLabel,
} from "@/lib/graph-view";

function node(id: string, table: string, record_id: number, properties: Record<string, unknown> = {}): GraphNode {
  return { id, label: id, table, record_id, category: table, properties };
}

function edge(
  id: string,
  source: string,
  target: string,
  table: string,
  properties: Record<string, unknown> = {},
): GraphEdge {
  return { id, source, target, label: table, table, record_id: 1, properties };
}

// Doc:1 → Doc:2 → Doc:3, and Doc:4 → Doc:3 by another relationship: Doc:4 is the unrelated node the
// emphasis rules are supposed to push away, and `e3` the unrelated edge.
const DOC_1 = node("Doc:1", "Doc", 1);
const DOC_2 = node("Doc:2", "Doc", 2);
const DOC_3 = node("Doc:3", "Doc", 3);
const DOC_4 = node("Doc:4", "Doc", 4);
const NODES = [DOC_1, DOC_2, DOC_3, DOC_4];
const E1 = edge("e1", "Doc:1", "Doc:2", "CITES");
const E2 = edge("e2", "Doc:2", "Doc:3", "CITES", { weight: 0.75 });
const E3 = edge("e3", "Doc:4", "Doc:3", "TAGS");
const NODE_IDS = NODES.map((item) => item.id);

function payload(edges: GraphEdge[]): GraphPayload {
  return {
    nodes: NODES,
    edges,
    shown_nodes: NODES.length,
    total_nodes: NODES.length,
    shown_edges: edges.length,
    total_edges: edges.length,
    truncated: false,
    snapshot_lsn: 1,
    database_uuid: "u",
    mode: "query",
    warnings: [],
  };
}

const model = toGraph(payload([E1, E2, E3]));
const adjacency = graphAdjacency(model.graph);
const ALL_EDGES = model.graph.edges();
const edgeKey = (physical: GraphEdge) =>
  ALL_EDGES.find((key) => model.graph.getEdgeAttribute(key, "attr").id === physical.id) as string;
const KEY_1 = edgeKey(E1);
const KEY_2 = edgeKey(E2);
const KEY_3 = edgeKey(E3);

describe("canvas geometry", () => {
  it("sizes a node by degree, sub-linearly and with a ceiling", () => {
    expect(nodeSize(0)).toBe(8);
    expect(nodeSize(4)).toBeGreaterThan(nodeSize(1));
    // Sizes are screen pixels: a spoke has to stay clickable, a hub has to stay a picture.
    expect(nodeSize(1)).toBeGreaterThanOrEqual(9);
    // A hub informs instead of covering the canvas.
    expect(nodeSize(400)).toBe(30);
  });

  it("thickens an edge by its stated weight, without letting one edge dominate", () => {
    expect(edgeSize(0)).toBeGreaterThanOrEqual(2.2);
    expect(edgeSize(0)).toBeLessThan(edgeSize(1));
    expect(edgeSize(100)).toBe(edgeSize(4));
    expect(edgeSize(Number.NaN)).toBe(edgeSize(0));
  });

  it("places nodes deterministically, so two captures of a store start from the same shape", () => {
    expect(seedCoords(3, 12)).toEqual(seedCoords(3, 12));
    expect(seedCoords(0, 12)).not.toEqual(seedCoords(1, 12));
  });

  it("truncates a label that would swallow its neighbours", () => {
    const long = "x".repeat(80);
    expect(truncateLabel(long)).toHaveLength(42);
    expect(truncateLabel(long).endsWith("…")).toBe(true);
    expect(truncateLabel("Doc:1")).toBe("Doc:1");
  });

  it("counts degree over drawable edges only", () => {
    // An edge to a node that was not returned cannot be drawn, so it must not inflate a radius.
    const degrees = degreesOf(NODES, [E1, E2, E3, edge("e4", "Doc:1", "Ghost:9", "CITES")]);
    expect(degrees.get("Doc:1")).toBe(1);
    expect(degrees.get("Doc:2")).toBe(2);
    expect(degrees.get("Doc:4")).toBe(1);
    expect(degrees.has("Ghost:9")).toBe(false);
  });

  it("labels an edge with its relationship table and the weight the store stated", () => {
    expect(edgeLabelOf(E1)).toBe("CITES");
    expect(edgeLabelOf(E2)).toBe("CITES · w 0.75");
    expect(edgeLabelOf(edge("e5", "Doc:1", "Doc:2", "CITES", { weight: 2 }))).toBe("CITES · w 2");
    // A property that is not a number is not a weight, so it is not invented into the label.
    expect(edgeLabelOf(edge("e6", "Doc:1", "Doc:2", "CITES", { weight: "heavy" }))).toBe("CITES");
  });

  it("keys the graph by the store's own ids, so a canvas click names a real record", () => {
    // A key graphology invented would be useless to the inspector: the id an operator clicks has to
    // be the id the daemon returned, for relationships exactly as it is for nodes.
    expect(model.graph.nodes()).toEqual(NODE_IDS);
    expect(model.graph.edges()).toEqual(["e1", "e2", "e3"]);
    expect(model.graph.getEdgeAttribute("e2", "attr").id).toBe("e2");
  });

  it("builds the graph with geometry, a visible stroke, and the payload kept as attr", () => {
    expect(model.graph.getNodeAttribute("Doc:2", "size")).toBe(nodeSize(2));
    expect(model.graph.getNodeAttribute("Doc:2", "label")).toBe("Doc:2");
    expect(model.graph.getEdgeAttribute(KEY_2, "color")).toBe(GRAPH_CANVAS.edge);
    expect(model.graph.getEdgeAttribute(KEY_2, "label")).toBe("CITES · w 0.75");
    expect(model.graph.getEdgeAttribute(KEY_2, "path")).toBe("straight");
    expect(model.graph.getEdgeAttribute(KEY_2, "attr").source).toBe("Doc:2");
  });

  it("draws a self-loop on the loop path", () => {
    const withLoop = toGraph(payload([E1, edge("e9", "Doc:1", "Doc:1", "REFLECTS")]));
    const loops = withLoop.graph.filterEdges((key) => withLoop.graph.source(key) === withLoop.graph.target(key));
    expect(loops).toHaveLength(1);
    expect(withLoop.graph.getEdgeAttribute(loops[0], "path")).toBe("loop");
  });
});

describe("emphasis rules", () => {
  it("does nothing while nothing is focused", () => {
    const plan = emphasis(adjacency, { hover: null, selected: null, nodes: NODE_IDS, edges: ALL_EDGES });
    expect(plan.dimmedNodes.size).toBe(0);
    expect(plan.dimmedEdges.size).toBe(0);
    expect(plan.hiddenEdges.size).toBe(0);
    expect(plan.litEdges.size).toBe(0);
  });

  it("on a hovered node, keeps its neighbourhood and hides every unrelated edge", () => {
    const plan = emphasis(adjacency, {
      hover: { kind: "node", id: "Doc:2" },
      selected: null,
      nodes: NODE_IDS,
      edges: ALL_EDGES,
    });
    expect([...plan.dimmedNodes]).toEqual(["Doc:4"]);
    expect([...plan.litNodes].sort()).toEqual(["Doc:1", "Doc:2", "Doc:3"]);
    expect([...plan.litEdges].sort()).toEqual([KEY_1, KEY_2].sort());
    // Nothing unrelated is drawn at all while the hover lasts: that is what makes a dense store
    // readable for the instant someone is asking about one node.
    expect([...plan.hiddenEdges]).toEqual([KEY_3]);
    expect(plan.dimmedEdges.size).toBe(0);
  });

  it("on a selected node, fades unrelated edges instead of hiding them", () => {
    const plan = emphasis(adjacency, {
      hover: null,
      selected: { kind: "node", id: "Doc:2" },
      nodes: NODE_IDS,
      edges: ALL_EDGES,
    });
    expect([...plan.dimmedEdges]).toEqual([KEY_3]);
    expect(plan.hiddenEdges.size).toBe(0);
    expect([...plan.litEdges].sort()).toEqual([KEY_1, KEY_2].sort());
    expect([...plan.dimmedNodes]).toEqual(["Doc:4"]);
  });

  it("focuses an edge by lighting it and its two endpoints only", () => {
    const plan = emphasis(adjacency, {
      hover: { kind: "edge", id: KEY_1 },
      selected: null,
      nodes: NODE_IDS,
      edges: ALL_EDGES,
    });
    expect([...plan.litEdges]).toEqual([KEY_1]);
    expect([...plan.dimmedEdges].sort()).toEqual([KEY_2, KEY_3].sort());
    // An edge focus never hides: hiding a whole store to read one relationship would make the
    // picture jump every time the cursor crossed a line.
    expect(plan.hiddenEdges.size).toBe(0);
    expect([...plan.dimmedNodes].sort()).toEqual(["Doc:3", "Doc:4"]);
  });

  it("lights a self-loop with its own node as both endpoints", () => {
    const loopModel = toGraph(payload([E1, edge("e9", "Doc:1", "Doc:1", "REFLECTS")]));
    const loopAdjacency = graphAdjacency(loopModel.graph);
    const loops = loopModel.graph.filterEdges((key) => loopModel.graph.source(key) === loopModel.graph.target(key));
    const plan = emphasis(loopAdjacency, {
      hover: { kind: "edge", id: loops[0] },
      selected: null,
      nodes: loopModel.graph.nodes(),
      edges: loopModel.graph.edges(),
    });
    expect([...plan.litNodes]).toEqual(["Doc:1"]);
    expect(plan.dimmedNodes.has("Doc:2")).toBe(true);
  });

  it("lets a hover win over a click, because a hover is the question asked right now", () => {
    const plan = emphasis(adjacency, {
      hover: { kind: "edge", id: KEY_1 },
      selected: { kind: "node", id: "Doc:3" },
      nodes: NODE_IDS,
      edges: ALL_EDGES,
    });
    expect([...plan.litEdges]).toEqual([KEY_1]);
  });
});

describe("legibility on this dark theme", () => {
  // okto-pulse's palette is tuned for its own slate canvas; on `#06070b` the same resting stroke is
  // about 1.6:1, which is exactly the complaint this port had to answer. These are the floors, so a
  // token change that makes the graph unreadable fails here instead of in a screenshot.
  it("keeps a resting edge clearly visible against the ground", () => {
    expect(contrastRatio(GRAPH_CANVAS.edge, GRAPH_CANVAS.background)).toBeGreaterThanOrEqual(4.5);
  });

  it("keeps an edge label readable", () => {
    expect(contrastRatio(GRAPH_CANVAS.edgeLabel, GRAPH_CANVAS.background)).toBeGreaterThanOrEqual(10);
  });

  it("keeps a node label readable", () => {
    expect(contrastRatio(GRAPH_CANVAS.nodeLabel, GRAPH_CANVAS.background)).toBeGreaterThanOrEqual(12);
  });

  it("orders the emphasis states by contrast, and keeps the dim state non-zero", () => {
    const ground = GRAPH_CANVAS.background;
    expect(contrastRatio(GRAPH_CANVAS.edgeActive, ground)).toBeGreaterThan(contrastRatio(GRAPH_CANVAS.edge, ground));
    expect(contrastRatio(GRAPH_CANVAS.edgeAdjacent, ground)).toBeGreaterThan(contrastRatio(GRAPH_CANVAS.edge, ground));
    expect(contrastRatio(GRAPH_CANVAS.edge, ground)).toBeGreaterThan(contrastRatio(GRAPH_CANVAS.edgeDim, ground));
    expect(contrastRatio(GRAPH_CANVAS.nodeDim, ground)).toBeGreaterThan(1);
  });

  it("plates labels so they survive crossing a stroke or a node", () => {
    // Eight-digit hex: translucent, but not so translucent that a stroke reads through the text.
    expect(GRAPH_CANVAS.edgeLabelPlate).toHaveLength(9);
    expect(GRAPH_CANVAS.nodeLabelPlate).toHaveLength(9);
    expect(contrastRatio(GRAPH_CANVAS.edgeLabel, GRAPH_CANVAS.edgeLabelPlate.slice(0, 7))).toBeGreaterThan(10);
  });
});
