import type { GraphEdge, GraphNode } from "@/api/client";

/**
 * The graph canvas, dressed for the dark theme.
 *
 * okto-pulse renders its knowledge graph with Sigma + graphology + ForceAtlas2 and the same set of
 * interactions — degree-sized nodes, truncated labels, hover that fades everything the focus node is
 * not attached to, edge hover that forces the edge's own label, click to inspect. Those are the rules
 * this module encodes.
 *
 * The colours are not copied from pulse: its palette is tuned for a slate canvas (`#334155` edges on a
 * `#111827` card, about 1.6:1 — legible where it sits, invisible on ours). This daemon's ground is
 * `#06070b`, where the same stroke vanishes and edge labels disappear with it. So the rules are pulse's
 * and the contrast is ours: every colour below is measured against the page ground, and the pair
 * `lib/graph-view.test.ts` asserts the floor. Edge labels additionally get an opaque plate, because a
 * label that crosses a node is otherwise unreadable in the dark.
 */
export const GRAPH_CANVAS = {
  /** The page ground, painted behind the WebGL layers for the PNG export. */
  background: "#06070b",
  /**
   * A resting edge: 7.4:1 against the ground. Measured, not assumed — a ~2px stroke is antialiased
   * over the void and lands well below its nominal colour, so a stroke chosen at exactly 5:1 composites
   * to something an operator cannot follow across a canvas.
   */
  edge: "#8f9bc7",
  /** An edge attached to the focused node, or one whose endpoint is selected: 10.2:1. */
  edgeAdjacent: "#aeb7df",
  /** The hovered or inspected edge itself — the accent, so a selection cannot be missed: 11.7:1. */
  edgeActive: "#41d7ee",
  /** Recessed on purpose (1.9:1): still there, clearly not the subject. */
  edgeDim: "#343c5e",
  /** Edge label text, and the plate that keeps it readable over strokes and nodes. */
  edgeLabel: "#c9d2f2",
  edgeLabelPlate: "#06070bd9",
  /** Node label: 18.2:1. */
  nodeLabel: "#f3f3fa",
  nodeLabelPlate: "#06070ba6",
  /** A node the focus is not attached to (1.6:1) — faded, not erased, so context survives a hover. */
  nodeDim: "#2b3147",
  /** The plate a hovered/inspected node sits on. */
  nodeBackdrop: "#0b0d14",
  /** A ring, not a halo: enough to say "this one is under the cursor" without lighting the canvas. */
  nodeBackdropShadow: "rgba(65, 215, 238, 0.3)",
} as const;

/** Fonts come from this bundle: the CSP is `default-src 'self'`, and the console is IBM Plex already. */
export const NODE_LABEL_FONT = '"IBM Plex Sans", system-ui, sans-serif';
export const EDGE_LABEL_FONT = '"IBM Plex Mono", ui-monospace, monospace';
export const NODE_LABEL_SIZE = 11;
export const EDGE_LABEL_SIZE = 10;

export const NODE_LABEL_MAX = 42;
export const EDGE_LABEL_MAX = 32;

/**
 * Up to this many relationships, every edge label is drawn. Sigma's own "auto" label placement
 * declines labels on strokes — a 2px line never reaches its size threshold — which is precisely how
 * an edge label becomes invisible on a dark canvas. Below this density the picture is readable with
 * them all on; above it they wait for the focus that touches them instead of turning into noise.
 */
export const FORCE_EDGE_LABELS_MAX = 120;

/**
 * Node radius in screen pixels. Sigma is configured with `itemSizesReference: "screen"`, so these are
 * the pixels an operator actually gets: a spoke is ~9px and a hub of 20 is ~21px, big enough to click
 * and to put a label beside, small enough that a capture of a hundred nodes still reads as a picture.
 * Sub-linear in degree, as in okto-pulse, so a hub informs instead of covering the canvas.
 */
export function nodeSize(degree: number): number {
  return Math.min(8 + Math.sqrt(Math.max(degree, 0)) * 3, 30);
}

/**
 * A relationship's stroke, also in screen pixels. The floor is set by `minEdgeThickness` as well: a
 * hairline antialiased over a near-black ground loses most of its contrast, so nothing draws thinner
 * than 2.2px. A stated weight thickens it, capped so one heavy edge cannot own the view.
 */
export function edgeSize(weight: number): number {
  const w = Number.isFinite(weight) ? Math.min(Math.abs(weight), 4) : 0;
  return 2.2 + w * 0.6;
}

export function truncateLabel(value: string, max: number = NODE_LABEL_MAX): string {
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

/**
 * An edge's label is its relationship table, plus the weight the capture stated when there is one:
 * `CITES · w 0.75`. A label that invents a property the store did not return would be a lie on canvas.
 */
export function edgeLabelOf(edge: GraphEdge): string {
  const name = edge.label || edge.table || "edge";
  const weight = edge.properties?.weight;
  const suffix = typeof weight === "number" && Number.isFinite(weight) ? ` · w ${trim(weight)}` : "";
  return truncateLabel(`${name}${suffix}`, EDGE_LABEL_MAX);
}

function trim(value: number): string {
  return Number.isInteger(value) ? `${value}` : value.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
}

/**
 * Deterministic placement on a spiral of rings. Sigma needs coordinates before the first frame or
 * everything piles at the origin, and ForceAtlas2 then spreads it; deterministic seeds mean two
 * captures of the same store start from the same shape.
 */
export function seedCoords(index: number, total: number): { x: number; y: number } {
  const angle = (2 * Math.PI * index) / Math.max(total, 1);
  const radius = 50 + (index % 7) * 8;
  return { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius };
}

/** Degree over the *drawn* edges: an edge whose endpoint was not returned is not drawn, so it counts for nothing. */
export function degreesOf(nodes: GraphNode[], edges: GraphEdge[]): Map<string, number> {
  const present = new Set(nodes.map((node) => node.id));
  const degrees = new Map<string, number>();
  for (const edge of edges) {
    if (!present.has(edge.source) || !present.has(edge.target)) continue;
    degrees.set(edge.source, (degrees.get(edge.source) ?? 0) + 1);
    degrees.set(edge.target, (degrees.get(edge.target) ?? 0) + 1);
  }
  return degrees;
}

/** What the canvas is focused on: a node or an edge, from a hover or from a click. */
export type Focus = { kind: "node"; id: string } | { kind: "edge"; id: string } | null;

export interface Adjacency {
  /** Neighbours of a node, including the node itself. */
  neighbours(node: string): Set<string>;
  /** Edges with one endpoint on a node. */
  edgesAt(node: string): Set<string>;
  /** The two endpoints of an edge (one, for a self-loop). */
  extremities(edge: string): string[];
}

/**
 * Who is lit, who is faded, who is hidden.
 *
 * Two focuses, in this precedence: a hover wins over a click, because a hover is a question and the
 * answer should be immediate. For each of them the rules are okto-pulse's:
 *
 *  - the focus node's neighbourhood stays full colour; everything else fades to `nodeDim`, label off;
 *  - edges at the focus node are pulled forward; the others fade — and while the focus is a *hover*
 *    they are hidden outright, which is what makes a dense store readable for one instant;
 *  - a focus edge is itself the subject: it and its two endpoints stay lit, everything else fades.
 *
 * A node selected with a click is also passed in `selected`, so the panel and the canvas agree.
 */
export function emphasis(
  adjacency: Adjacency,
  options: { hover: Focus; selected: Focus; nodes: string[]; edges: string[] },
): {
  dimmedNodes: Set<string>;
  dimmedEdges: Set<string>;
  hiddenEdges: Set<string>;
  litEdges: Set<string>;
  litNodes: Set<string>;
} {
  const focus = options.hover ?? options.selected;
  const dimmedNodes = new Set<string>();
  const dimmedEdges = new Set<string>();
  const hiddenEdges = new Set<string>();
  const litEdges = new Set<string>();
  const litNodes = new Set<string>();

  if (!focus) return { dimmedNodes, dimmedEdges, hiddenEdges, litEdges, litNodes };

  if (focus.kind === "node") {
    const keep = adjacency.neighbours(focus.id);
    const atFocus = adjacency.edgesAt(focus.id);
    for (const node of options.nodes) if (!keep.has(node)) dimmedNodes.add(node);
    for (const edge of options.edges) {
      if (atFocus.has(edge)) {
        litEdges.add(edge);
        for (const end of adjacency.extremities(edge)) litNodes.add(end);
      } else if (options.hover) hiddenEdges.add(edge);
      else dimmedEdges.add(edge);
    }
    litNodes.add(focus.id);
    return { dimmedNodes, dimmedEdges, hiddenEdges, litEdges, litNodes };
  }

  // The focus is an edge: it is the subject, its endpoints are its context, and everything else —
  // including on a hover — only fades, because hiding a whole store to read one relationship would
  // make the picture jump every time the cursor crossed a line.
  const endpoints = new Set(adjacency.extremities(focus.id));
  litEdges.add(focus.id);
  for (const node of options.nodes) if (!endpoints.has(node)) dimmedNodes.add(node);
  for (const edge of options.edges) if (edge !== focus.id) dimmedEdges.add(edge);
  for (const end of adjacency.extremities(focus.id)) litNodes.add(end);
  return { dimmedNodes, dimmedEdges, hiddenEdges, litEdges, litNodes };
}

/** Contrast ratio between two hex colours, exported so the dark-theme floor can be asserted. */
export function contrastRatio(left: string, right: string): number {
  const a = luminance(left);
  const b = luminance(right);
  const [high, low] = a > b ? [a, b] : [b, a];
  return (high + 0.05) / (low + 0.05);
}

function luminance(hex: string): number {
  const value = hex.replace("#", "").slice(0, 6);
  const channel = (index: number) => {
    const raw = parseInt(value.slice(index, index + 2), 16) / 255;
    return raw <= 0.03928 ? raw / 12.92 : ((raw + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(0) + 0.7152 * channel(2) + 0.0722 * channel(4);
}
