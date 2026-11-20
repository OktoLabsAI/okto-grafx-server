import { MultiGraph } from "graphology";
import type { MultiGraph as LooseMultiGraph } from "graphology";
import forceAtlas2 from "graphology-layout-forceatlas2";
import type { GraphEdge, GraphNode, GraphPayload } from "@/api/client";
import {
  degreesOf,
  edgeLabelOf,
  edgeSize,
  GRAPH_CANVAS,
  nodeSize,
  seedCoords,
  truncateLabel,
  type Adjacency,
} from "@/lib/graph-view";

/**
 * Graph payloads use physical identities: `"<table>:<record_id>"`. They are stable, they are not
 * application primary keys, and two different tables can carry the same `record_id` — so the id is
 * the only join key here, and it is never presented as if it were a business key.
 */

export const PALETTE = [
  "#41d7ee", // cyan — the page's accent, so the first table matches the brand
  "#9f78fc", // violet
  "#5c9cef", // blue
  "#44d2e5", // pale cyan
  "#c6a7ef", // lilac
  "#22d3ee", // deep cyan
  "#e2b13c", // amber, kept for a table that means "attention"
  "#f0938c", // rose
];

export interface GraphModel {
  graph: CanvasGraph;
  /** Table → colour, stable for the session so a node keeps its colour across refreshes. */
  colors: Record<string, string>;
}

/**
 * What the canvas needs on each item: geometry for sigma, a palette colour for the legend, and the
 * physical payload untouched as `attr`, so the inspector shows exactly what the store returned.
 */
export interface NodeAttrs {
  x: number;
  y: number;
  size: number;
  color: string;
  label: string;
  attr: GraphNode;
}

export interface EdgeAttrs {
  size: number;
  color: string;
  label: string;
  path: string;
  attr: GraphEdge;
}

export type CanvasGraph = MultiGraph<NodeAttrs, EdgeAttrs, Record<string, unknown>>;

/** The canvas is built before the first capture exists, so it starts from an empty multigraph. */
export function emptyGraph(): CanvasGraph {
  return new MultiGraph<NodeAttrs, EdgeAttrs, Record<string, unknown>>({ allowSelfLoops: true });
}

export function entityId(node: Pick<GraphNode, "table" | "record_id">): string {
  return `${node.table}:${node.record_id}`;
}

export function colorFor(colors: Record<string, string>, category: string): string {
  if (!colors[category]) colors[category] = PALETTE[Object.keys(colors).length % PALETTE.length];
  return colors[category];
}

/**
 * Payload → graphology, with the canvas geometry already decided here rather than in a reducer:
 * degree-sized nodes (okto-pulse's rule), truncated labels, deterministic seeds, and edges drawn as
 * visible strokes with their own label. Parallel edges and self-loops are kept: a multigraph is the
 * honest shape of the data, and merging them would hide a real distinction in the store.
 */
export function toGraph(payload: GraphPayload, colors: Record<string, string> = {}): GraphModel {
  const graph = new MultiGraph<NodeAttrs, EdgeAttrs, Record<string, unknown>>({ allowSelfLoops: true });
  const degrees = degreesOf(payload.nodes, payload.edges);
  payload.nodes.forEach((node, index) => {
    if (graph.hasNode(node.id)) return;
    const { x, y } = seedCoords(index, payload.nodes.length);
    graph.addNode(node.id, {
      label: truncateLabel(nodeLabel(node)),
      size: nodeSize(degrees.get(node.id) ?? 0),
      color: colorFor(colors, node.category || node.table || "?"),
      x,
      y,
      attr: node,
    });
  });
  for (const edge of payload.edges) {
    if (!graph.hasNode(edge.source) || !graph.hasNode(edge.target)) continue;
    if (graph.hasEdge(edge.id)) continue;
    // The graph speaks the store's vocabulary: nodes are keyed by physical id and so are
    // relationships. Then a click on the canvas hands back `TAGGED:12` rather than a key graphology
    // minted, and the inspector can read the payload it was given without a translation layer.
    graph.addDirectedEdgeWithKey(edge.id, edge.source, edge.target, {
      label: edgeLabelOf(edge),
      size: edgeSize(weightOf(edge)),
      color: GRAPH_CANVAS.edge,
      // Sigma 4 draws self-loops on their own path; say which one before the first frame.
      path: edge.source === edge.target ? "loop" : "straight",
      attr: edge,
    });
  }
  return { graph, colors };
}

/**
 * The adjacency the emphasis rules need, asked of graphology rather than of the payload: after
 * `toGraph` the drawable set is the graph, and the two must never disagree about what exists.
 */
export function graphAdjacency(graph: LooseMultiGraph): Adjacency {
  return {
    neighbours: (node) => {
      const set = graph.hasNode(node) ? new Set(graph.neighbors(node)) : new Set<string>();
      set.add(node);
      return set;
    },
    edgesAt: (node) => (graph.hasNode(node) ? new Set(graph.edges(node)) : new Set<string>()),
    extremities: (edge) => (graph.hasEdge(edge) ? [graph.source(edge), graph.target(edge)] : []),
  };
}

export function nodeLabel(node: GraphNode): string {
  const props = node.properties ?? {};
  for (const candidate of ["title", "name", "label", "id"]) {
    const value = props[candidate];
    if (typeof value === "string" || typeof value === "number") return `${candidate}=${value}`;
  }
  return node.id;
}

export function weightOf(edge: GraphEdge): number {
  const value = edge.properties?.weight;
  return typeof value === "number" && Number.isFinite(value) ? Math.min(Math.abs(value), 4) : 0.6;
}

/**
 * Merge a neighbourhood response into an existing picture without duplicating ids: the same
 * physical node reached twice must stay one node, or expansion would look like growth.
 */
export function mergePayloads(current: GraphPayload, addition: GraphPayload): GraphPayload {
  const nodes = new Map<string, GraphNode>();
  for (const node of [...current.nodes, ...addition.nodes]) {
    const previous = nodes.get(node.id);
    // Prefer the copy that carries properties: a projection node has none.
    if (!previous || Object.keys(previous.properties ?? {}).length === 0) nodes.set(node.id, node);
  }
  const edges = new Map<string, GraphEdge>();
  for (const edge of [...current.edges, ...addition.edges]) edges.set(edge.id, edge);
  return {
    ...addition,
    nodes: [...nodes.values()],
    edges: [...edges.values()],
    shown_nodes: nodes.size,
    total_nodes: Math.max(current.total_nodes, addition.total_nodes),
    shown_edges: edges.size,
    total_edges: Math.max(current.total_edges, addition.total_edges),
    truncated: current.truncated || addition.truncated,
    warnings: [...(current.warnings ?? []), ...(addition.warnings ?? [])],
  };
}

export function layout(graph: LooseMultiGraph, kind: "force" | "circle" | "none" = "force"): void {
  if (graph.order === 0) return;
  if (kind === "force") {
    const settings = forceAtlas2.inferSettings(graph);
    forceAtlas2.assign(graph, { iterations: 120, settings });
    return;
  }
  if (kind === "circle") {
    const nodes = [...graph.nodes()];
    const radius = Math.max(40, nodes.length * 6);
    nodes.forEach((node, index) => {
      const angle = (index / nodes.length) * Math.PI * 2;
      graph.setNodeAttribute(node, "x", Math.cos(angle) * radius);
      graph.setNodeAttribute(node, "y", Math.sin(angle) * radius);
    });
  }
}

export function download(name: string, content: string, type = "application/json"): void {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  URL.revokeObjectURL(url);
}
