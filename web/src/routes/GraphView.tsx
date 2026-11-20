import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Camera, FileJson, ImageDown, Maximize, MonitorX, RotateCcw, ZoomIn, ZoomOut } from "lucide-react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Sigma from "sigma";
import type { Coordinates } from "sigma/types";
import forceAtlas2, { inferSettings } from "graphology-layout-forceatlas2";
import { data, type GraphEdge, type GraphPayload } from "@/api/client";
import {
  Badge,
  ConfirmButton,
  Empty,
  ErrorBanner,
  Field,
  JsonBlock,
  Section,
  Spinner,
} from "@/components/ui";
import {
  download,
  emptyGraph,
  graphAdjacency,
  layout as applyCircleLayout,
  mergePayloads,
  nodeLabel,
  toGraph,
  type CanvasGraph,
  type EdgeAttrs,
  type NodeAttrs,
} from "@/lib/graph";
import {
  EDGE_LABEL_FONT,
  EDGE_LABEL_SIZE,
  emphasis,
  FORCE_EDGE_LABELS_MAX,
  GRAPH_CANVAS,
  NODE_LABEL_FONT,
  NODE_LABEL_SIZE,
  type Focus,
} from "@/lib/graph-view";
import { projectionWeightColumns } from "@/lib/graph-request";
import { parseEntityId } from "@/lib/format";

type Mode = "projection" | "query";
type LayoutKind = "force" | "circle" | "keep";

const EMPTY: GraphPayload = {
  nodes: [],
  edges: [],
  shown_nodes: 0,
  total_nodes: 0,
  shown_edges: 0,
  total_edges: 0,
  truncated: false,
  snapshot_lsn: 0,
  database_uuid: "",
  mode: "",
  warnings: [],
};

/** How long ForceAtlas2 flings the graph before it is allowed to settle, as in okto-pulse. */
const LAYOUT_SETTLE_MS = 2500;
/** Iterations per frame: enough to settle in that window, few enough to keep hover and pan responsive. */
const LAYOUT_ITERATIONS_PER_FRAME = 6;

/** What the canvas adds to sigma's own per-item state. */
type NodeState = { isDimmed: boolean };
type EdgeState = { isAdjacent: boolean };

/**
 * sigma calls a style function as `(attributes, state, graphState, graph)`, so every function below
 * takes attributes first even when it only reads state — getting that order wrong silently reads
 * attributes as state.
 */
type FullState = {
  isHidden?: boolean;
  isHovered?: boolean;
  isHighlighted?: boolean;
  isDimmed?: boolean;
  isAdjacent?: boolean;
};

/** sigma's own bookkeeping for relationships that share two endpoints. */
type ParallelState = { parallelIndex: number; parallelCount: number };

/**
 * Sigma renders through WebGL and throws if it cannot get a context — jsdom, GPU disabled, a locked-down
 * VM. Probe first so the canvas can degrade into a list instead of crashing the page: the data has to
 * stay inspectable wherever WebGL is not.
 */
function hasWebGL(): boolean {
  try {
    const probe = document.createElement("canvas");
    return Boolean(
      probe.getContext("webgl2") || probe.getContext("webgl") || probe.getContext("experimental-webgl"),
    );
  } catch {
    return false;
  }
}

export function GraphView() {
  const { slug = "" } = useParams();
  const [params] = useSearchParams();
  const queryClient = useQueryClient();

  const [payload, setPayload] = useState<GraphPayload>(EMPTY);
  const [mode, setMode] = useState<Mode>((params.get("mode") as Mode) ?? "projection");
  const [statement, setStatement] = useState(params.get("query") ?? "");
  const [layoutKind, setLayoutKind] = useState<LayoutKind>("force");
  const [selected, setSelected] = useState<Focus>(null);
  const [maxNodes, setMaxNodes] = useState(600);
  const [maxEdges, setMaxEdges] = useState(1500);
  const [weightColumn, setWeightColumn] = useState("");
  const [payloadError, setPayloadError] = useState<unknown>(null);
  const [settling, setSettling] = useState(false);

  const schema = useQuery({ queryKey: ["schema", slug], queryFn: () => data.schema(slug), retry: false });
  const nodeTables = useMemo(() => (schema.data?.tables ?? []).map((table) => table.name), [schema.data]);
  const relTables = useMemo(
    () => (schema.data?.relationships ?? []).map((table) => table.name),
    [schema.data],
  );
  const [selectedNodes, setSelectedNodes] = useState<string[]>([]);
  const [selectedRels, setSelectedRels] = useState<string[]>([]);

  const [webglOk] = useState(hasWebGL);
  const container = useRef<HTMLDivElement | null>(null);
  const renderer = useRef<Sigma<NodeAttrs, EdgeAttrs, Record<string, unknown>, NodeState, EdgeState> | null>(
    null,
  );
  const layoutFrame = useRef<number | null>(null);
  const model = useRef<{ graph: CanvasGraph; colors: Record<string, string> } | null>(null);
  // Hover and selection are read inside sigma's frame loop and in its event handlers, so they live in
  // refs; React state exists for the inspector, which is allowed to be one frame behind the canvas.
  const selectedRef = useRef<Focus>(null);
  const hoverRef = useRef<Focus>(null);
  // The next capture re-centres the camera; a re-render or an expansion must not move the view.
  const freshViewRef = useRef(true);

  useEffect(() => {
    if (nodeTables.length && selectedNodes.length === 0) setSelectedNodes(nodeTables.slice(0, 2));
    if (relTables.length && selectedRels.length === 0) setSelectedRels(relTables.slice(0, 1));
  }, [nodeTables, relTables, selectedNodes.length, selectedRels.length]);

  /**
   * Stop a layout that is mid-flight: a new capture, a re-run, or leaving the screen must not leave
   * an animation frame pushing the old graph around.
   */
  const stopLayout = useCallback(() => {
    if (layoutFrame.current !== null) cancelAnimationFrame(layoutFrame.current);
    layoutFrame.current = null;
    setSettling(false);
  }, []);

  /**
   * Push focus into sigma's per-item state. The styles in `canvasOptions` read only that state, so a
   * hover costs a state write and one repaint — no rebuild, no reducer recomputing the whole graph.
   */
  const applyEmphasis = useCallback(() => {
    const instance = renderer.current;
    const graph = model.current?.graph;
    if (!instance || !graph) return;
    const nodes = graph.nodes();
    const edges = graph.edges();
    const plan = emphasis(graphAdjacency(graph), {
      hover: hoverRef.current,
      selected: selectedRef.current,
      nodes,
      edges,
    });
    // sigma writes one partial state per batch of keys, so the sets become buckets. Anything not in a
    // bucket is explicitly reset, or a stale dim would survive the next hover.
    const nodeBatches: Array<[Record<string, boolean>, string[]]> = [
      [{ isDimmed: true, isHighlighted: false }, []],
      [{ isDimmed: false, isHighlighted: true }, []],
      [{ isDimmed: false, isHighlighted: false }, []],
    ];
    for (const node of nodes) {
      const bucket = plan.dimmedNodes.has(node) ? 0 : plan.litNodes.has(node) ? 1 : 2;
      nodeBatches[bucket][1].push(node);
    }
    const edgeBatches: Array<[Record<string, boolean>, string[]]> = [
      // Hovering a node hides everything it is not attached to: the picture resolves for one instant.
      [{ isHidden: true, isDimmed: false, isAdjacent: false, isHighlighted: false }, []],
      [{ isHidden: false, isDimmed: false, isAdjacent: false, isHighlighted: true }, []],
      [{ isHidden: false, isDimmed: false, isAdjacent: true, isHighlighted: false }, []],
      [{ isHidden: false, isDimmed: true, isAdjacent: false, isHighlighted: false }, []],
      [{ isHidden: false, isDimmed: false, isAdjacent: false, isHighlighted: false }, []],
    ];
    for (const edge of edges) {
      const bucket = plan.hiddenEdges.has(edge)
        ? 0
        : selectedRef.current?.kind === "edge" && selectedRef.current.id === edge
          ? 1
          : plan.litEdges.has(edge)
            ? 2
            : plan.dimmedEdges.has(edge)
              ? 3
              : 4;
      edgeBatches[bucket][1].push(edge);
    }
    for (const [state, keys] of nodeBatches) if (keys.length) instance.setNodesState(keys, state);
    for (const [state, keys] of edgeBatches) if (keys.length) instance.setEdgesState(keys, state);
  }, []);

  const select = useCallback(
    (next: Focus) => {
      selectedRef.current = next;
      setSelected(next);
      applyEmphasis();
    },
    [applyEmphasis],
  );

  /** The canvas contract: styles read state, never a rebuild. Colours are tuned for `#06070b`. */
  const canvasOptions = useMemo(
    () => ({
      settings: {
        renderLabels: true,
        renderEdgeLabels: true,
        // Without edge events an edge cannot be clicked at all. A label counts as its item, so the
        // text of a node or a relationship is clickable too — often the only wide-enough target.
        enableEdgeEvents: true,
        nodeLabelEvents: "extend" as const,
        edgeLabelEvents: "extend" as const,
        labelRenderedSizeThreshold: 0,
        labelDensity: 1,
        // A 1.2px stroke on a dark ground has to keep its thickness when zoomed out, and its hit
        // area has to stay bigger than the stroke itself.
        minEdgeThickness: 2.2,
        // Less feathering: a crisp edge keeps its colour where a soft one blends into the ground.
        antiAliasingFeather: 0.6,
        antialiasEdges: true,
        edgePickingPadding: 6,
        labelPickingPadding: 12,
        // Sizes in screen pixels, not graph units: an edge stays a visible line when the capture is
        // zoomed out, and a node stays a clickable disc instead of collapsing to a dot.
        itemSizesReference: "screen" as const,
        // The first fit has to account for the drawn circles, or a large hub spills over the frame.
        autoRescale: true as const,
        autoRescaleContent: "nodes" as const,
        enableNodeDrag: true,
        // Where a dragged node ends up: written back onto the graph, so the layout never snaps a
        // hand-placed node back and an export keeps the arrangement the operator made.
        dragPositionToAttributes: (position: Coordinates) => ({ x: position.x, y: position.y }),
        minCameraRatio: 0.05,
        maxCameraRatio: 12,
        doubleClickZoomingRatio: 1.8,
      },
      customNodeState: { isDimmed: false },
      customEdgeState: { isAdjacent: false },
      styles: {
        nodes: {
          x: { attribute: "x" },
          y: { attribute: "y" },
          size: (attributes: { size?: number }, state: { isHighlighted?: boolean; isHovered?: boolean }) =>
            (attributes.size ?? 10) + (state.isHighlighted ? 3 : state.isHovered ? 1.5 : 0),
          color: (attributes: { color?: string }, state: { isDimmed?: boolean }) =>
            state.isDimmed ? GRAPH_CANVAS.nodeDim : (attributes.color ?? "#66759f"),
          visibility: (_attributes: NodeAttrs, state: FullState) => (state.isHidden ? "hidden" : "visible"),
          zIndex: (_attributes: NodeAttrs, state: FullState) =>
            state.isHighlighted || state.isHovered ? 2 : state.isDimmed ? -1 : 0,
          cursor: "pointer",
          label: { attribute: "label" },
          labelColor: GRAPH_CANVAS.nodeLabel,
          labelSize: NODE_LABEL_SIZE,
          labelFont: NODE_LABEL_FONT,
          // A plate, not a halo: on a near-black ground the label needs its own ground where it
          // crosses a stroke or a node.
          labelBackgroundColor: GRAPH_CANVAS.nodeLabelPlate,
          labelBackgroundPadding: 2,
          labelVisibility: (_attributes: NodeAttrs, state: FullState) =>
            state.isDimmed ? "hidden" : state.isHighlighted || state.isHovered ? "visible" : "auto",
          backdropVisibility: (_attributes: NodeAttrs, state: FullState) =>
            state.isHighlighted || state.isHovered ? "visible" : "hidden",
          backdropColor: GRAPH_CANVAS.nodeBackdrop,
          backdropShadowColor: GRAPH_CANVAS.nodeBackdropShadow,
          backdropShadowBlur: 6,
          backdropPadding: 4,
          backdropArea: "both" as const,
        },
        edges: {
          path: { attribute: "path", defaultValue: "straight" },
          size: (
            attributes: { size?: number },
            state: { isHighlighted?: boolean; isHovered?: boolean; isAdjacent?: boolean },
          ) => (attributes.size ?? 1.2) + (state.isHighlighted || state.isHovered ? 1.2 : state.isAdjacent ? 0.6 : 0),
          color: (
            attributes: { color?: string },
            state: { isHighlighted?: boolean; isHovered?: boolean; isAdjacent?: boolean; isDimmed?: boolean },
          ) =>
            state.isHighlighted || state.isHovered
              ? GRAPH_CANVAS.edgeActive
              : state.isAdjacent
                ? GRAPH_CANVAS.edgeAdjacent
                : state.isDimmed
                  ? GRAPH_CANVAS.edgeDim
                  : (attributes.color ?? GRAPH_CANVAS.edge),
          visibility: (_attributes: EdgeAttrs, state: FullState) =>
            state.isHidden ? "hidden" : "visible",
          zIndex: (_attributes: EdgeAttrs, state: FullState) =>
            state.isHighlighted || state.isHovered ? 2 : state.isAdjacent ? 1 : 0,
          cursor: "pointer",
          label: { attribute: "label" },
          labelColor: (_attributes: EdgeAttrs, state: FullState) =>
            state.isHighlighted || state.isHovered ? GRAPH_CANVAS.edgeActive : GRAPH_CANVAS.edgeLabel,
          labelSize: EDGE_LABEL_SIZE,
          labelFont: EDGE_LABEL_FONT,
          // Edge labels are the point of a graph you can read: an opaque plate keeps them legible
          // over strokes, and sigma's own placement declines labels on thin lines — which is exactly
          // how they disappear on a dark canvas — so a capture light enough to read gets them all.
          labelBackgroundColor: GRAPH_CANVAS.edgeLabelPlate,
          labelBackgroundPadding: 2,
          labelPosition: (_attributes: EdgeAttrs, state: ParallelState) =>
            // Two relationships between the same pair are drawn apart, and their labels have to be too,
            // or the picture shows one word where the store holds two.
            state.parallelCount > 1
              ? 0.3 + (0.4 * state.parallelIndex) / (state.parallelCount - 1)
              : 0.5,
          labelVisibility: (
            _attributes: EdgeAttrs,
            state: FullState,
            _graphState: unknown,
            graph: { size: number },
          ) =>
            state.isDimmed
              ? "hidden"
              : state.isHighlighted || state.isHovered || state.isAdjacent
                ? "visible"
                : graph.size <= FORCE_EDGE_LABELS_MAX
                  ? "visible"
                  : "auto",
        },
      },
    }),
    [],
  );

  // Sigma is created once against the container, and data is swapped underneath it: rebuilding the
  // renderer on every capture would throw away the camera, the layout and the drag positions.
  useEffect(() => {
    if (!webglOk || !container.current) return;
    const built = model.current ?? { graph: emptyGraph(), colors: {} };
    model.current = built;
    const instance = new Sigma(built.graph, container.current, canvasOptions);
    renderer.current = instance;

    const focusOn = (focus: Focus) => {
      hoverRef.current = focus;
      applyEmphasis();
    };
    instance.on("enterNode", ({ node }) => focusOn({ kind: "node", id: node }));
    instance.on("enterEdge", ({ edge }) => focusOn({ kind: "edge", id: edge }));
    instance.on("leaveNode", () => focusOn(null));
    instance.on("leaveEdge", () => focusOn(null));
    // A click inspects; clicking the same thing again puts it back. The label of an item is part of
    // the item, so clicking the text selects the node or the edge it belongs to.
    const toggle = (focus: Focus) =>
      select(selectedRef.current?.kind === focus?.kind && selectedRef.current?.id === focus?.id ? null : focus);
    instance.on("clickNode", ({ node }) => toggle({ kind: "node", id: node }));
    instance.on("clickEdge", ({ edge }) => toggle({ kind: "edge", id: edge }));
    instance.on("clickStage", () => select(null));
    // Double-click centres rather than zooms: the question is "which one is that", not "bigger".
    instance.on("doubleClickNode", ({ node, event }) => {
      event.preventSigmaDefault();
      const graphInstance = model.current?.graph;
      if (!graphInstance || !graphInstance.hasNode(node)) return;
      void instance.getCamera().animate(
        { x: graphInstance.getNodeAttribute(node, "x") as number, y: graphInstance.getNodeAttribute(node, "y") as number, ratio: 1 },
        { duration: 240 },
      );
    });

    const observer =
      typeof ResizeObserver !== "undefined"
        ? new ResizeObserver(() => {
            instance.resize();
            instance.refresh();
          })
        : null;
    observer?.observe(container.current);

    return () => {
      observer?.disconnect();
      stopLayout();
      instance.kill();
      renderer.current = null;
    };
  }, [canvasOptions, stopLayout, webglOk, applyEmphasis, select]);

  const runForceLayout = useCallback((graph: CanvasGraph, fresh: boolean) => {
    stopLayout();
    if (graph.order < 2) return;
    // Why not `graphology-layout-forceatlas2/worker`, which is what okto-pulse uses: its supervisor
    // spawns a `blob:` worker, and this console is served under `script-src 'self'` — the browser
    // refuses the worker and the graph never moves, a failure with no visible cause on screen. The
    // same algorithm runs here instead, in bounded chunks per frame, over the same settle window.
    const inferred = (inferSettings(graph) ?? {}) as Record<string, unknown>;
    const settings = {
      ...inferred,
      slowDown: 1,
      // Repulsion at its default is tuned for graphs of hundreds of nodes; on the handful a projection
      // usually returns it throws the components so far apart that the fitted view is mostly empty
      // space. Kept modest, with collisions resolved instead, so strokes stay short enough to read as
      // connections rather than as loose threads crossing a void.
      scalingRatio: 2,
      gravity: 1,
      strongGravityMode: false,
      adjustSizes: true,
      barnesHutOptimize: graph.order > 80,
    };
    setSettling(true);
    const deadline = performance.now() + LAYOUT_SETTLE_MS;
    const step = () => {
      // `assign` writes x/y onto the graph; sigma reads them when it is refreshed.
      forceAtlas2.assign(graph, { iterations: LAYOUT_ITERATIONS_PER_FRAME, settings });
      renderer.current?.refresh();
      if (performance.now() < deadline) {
        layoutFrame.current = requestAnimationFrame(step);
        return;
      }
      layoutFrame.current = null;
      setSettling(false);
      // The camera is re-fitted once the graph has stopped moving, not while it is flying.
      if (fresh) void renderer.current?.getCamera().reset({ duration: 300 });
    };
    layoutFrame.current = requestAnimationFrame(step);
  }, []);

  const render = useCallback(
    (next: GraphPayload) => {
      setPayload(next);
      const built = toGraph(next, model.current?.colors ?? {});
      model.current = built;
      const fresh = freshViewRef.current;
      freshViewRef.current = false;

      const instance = renderer.current;
      if (instance) instance.setGraph(built.graph);
      // A capture that no longer contains the selection must not leave the inspector describing it.
      if (selectedRef.current) {
        const still =
          selectedRef.current.kind === "node"
            ? built.graph.hasNode(selectedRef.current.id)
            : built.graph.hasEdge(selectedRef.current.id);
        if (!still) select(null);
        else applyEmphasis();
      }
      if (layoutKind === "force") runForceLayout(built.graph, fresh);
      else {
        if (layoutKind === "circle") applyCircleLayout(built.graph, "circle");
        instance?.refresh();
        if (fresh) void instance?.getCamera().reset({ duration: 240 });
      }
    },
    [applyEmphasis, layoutKind, runForceLayout, select],
  );

  /** Centre the camera on one node: this is what a search hit deep-links into. */
  const focusNode = useCallback((id: string) => {
    const instance = renderer.current;
    const graph = model.current?.graph;
    if (!instance || !graph || !graph.hasNode(id)) return;
    void instance
      .getCamera()
      .animate(
        { x: graph.getNodeAttribute(id, "x") as number, y: graph.getNodeAttribute(id, "y") as number, ratio: 1 },
        { duration: 240 },
      );
  }, []);

  const rerunLayout = useCallback(() => {
    const graph = model.current?.graph;
    freshViewRef.current = true;
    if (!graph) return;
    if (layoutKind === "force") runForceLayout(graph, true);
    else applyCircleLayout(graph, layoutKind === "circle" ? "circle" : "circle");
    renderer.current?.refresh();
  }, [layoutKind, runForceLayout]);

  const load = useMutation({
    mutationFn: async () => {
      setPayloadError(null);
      if (mode === "query") {
        if (!statement.trim())
          throw new Error("write a statement that returns nodes and relationships");
        return data.graph(slug, {
          mode: "query",
          query: statement,
          max_nodes: maxNodes,
          max_edges: maxEdges,
        });
      }
      if (selectedNodes.length === 0 || selectedRels.length === 0) {
        throw new Error(
          "projection mode needs the node tables and the relationship tables to be selected: an unselected endpoint is not followed implicitly",
        );
      }
      return data.graph(slug, {
        mode: "projection",
        node_tables: selectedNodes,
        relationship_tables: selectedRels,
        weight_columns: projectionWeightColumns(selectedRels, weightColumn),
        max_nodes: maxNodes,
        max_edges: maxEdges,
      });
    },
    onSuccess: (next) => {
      freshViewRef.current = true;
      render(next);
    },
    onError: (error) => setPayloadError(error),
  });

  const expand = useMutation({
    mutationFn: async (id: string) => {
      const parsed = parseEntityId(id);
      if (!parsed) throw new Error(`${id} is not a physical id`);
      return data.neighbors(slug, parsed.table, parsed.record_id, 200);
    },
    onSuccess: (addition) => {
      // Merging by id: the same physical node reached twice must stay one node.
      render(mergePayloads(payload, addition));
      queryClient.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (error) => setPayloadError(error),
  });

  const focusFromParams = params.get("focus");
  // A search hit can deep-link to one node: draw it from its neighbourhood, and select it so the
  // inspector is already telling you what you clicked through for.
  useEffect(() => {
    if (!focusFromParams) return;
    const parsed = parseEntityId(focusFromParams);
    if (!parsed) return;
    data
      .neighbors(slug, parsed.table, parsed.record_id, 200)
      .then((neighbourhood) => {
        freshViewRef.current = true;
        render(neighbourhood);
        select({ kind: "node", id: focusFromParams });
        window.setTimeout(() => focusNode(focusFromParams), 60);
      })
      .catch((error) => setPayloadError(error));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusFromParams, slug]);

  // A deep link that carries a statement is a request to draw it, not to prefill a form: “view as
  // graph” in the query console lands here and the operator expects to see the picture.
  const deepLinked = useRef(false);
  useEffect(() => {
    if (deepLinked.current || focusFromParams) return;
    if (params.get("mode") !== "query" || !(params.get("query") ?? "").trim()) return;
    deepLinked.current = true;
    load.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(
    () => () => {
      stopLayout();
      renderer.current?.kill();
    },
    [stopLayout],
  );

  const exportPng = () => {
    const host = container.current;
    if (!host) return;
    const canvases = [...host.querySelectorAll("canvas")];
    if (!canvases.length) return;
    const width = Math.max(...canvases.map((canvas) => canvas.width));
    const height = Math.max(...canvases.map((canvas) => canvas.height));
    const merged = document.createElement("canvas");
    merged.width = width;
    merged.height = height;
    const context = merged.getContext("2d");
    if (!context) return;
    // The WebGL canvases are transparent where nothing is drawn: without this the PNG would carry
    // whatever the browser puts behind a transparent canvas, which is not this dark ground.
    context.fillStyle = GRAPH_CANVAS.background;
    context.fillRect(0, 0, width, height);
    for (const canvas of canvases) context.drawImage(canvas, 0, 0);
    const anchor = document.createElement("a");
    anchor.href = merged.toDataURL("image/png");
    anchor.download = `${slug}-graph.png`;
    anchor.click();
  };

  const zoom = (direction: "in" | "out") => {
    const camera = renderer.current?.getCamera();
    if (!camera) return;
    void (direction === "in" ? camera.zoomIn({ duration: 200 }) : camera.zoomOut({ duration: 200 }));
  };
  const fit = () => void renderer.current?.getCamera().reset({ duration: 300 });

  const selectedNode =
    selected?.kind === "node" ? payload.nodes.find((node) => node.id === selected.id) ?? null : null;
  const selectedEdge =
    selected?.kind === "edge" ? payload.edges.find((edge) => edge.id === selected.id) ?? null : null;
  const incident = selectedNode
    ? payload.edges.filter((edge) => edge.source === selectedNode.id || edge.target === selectedNode.id)
    : [];
  const labelOf = (id: string) => {
    const node = payload.nodes.find((item) => item.id === id);
    return node ? `${node.table ?? "?"} · ${nodeLabel(node)}` : id;
  };

  return (
    <div className="space-y-3">
      <header className="flex flex-wrap items-center gap-3">
        <Link className="btn" to={`/databases/${slug}`}>
          ← {slug}
        </Link>
        <h1 className="text-[16px] font-semibold tracking-tight">graph</h1>
        <Badge tone={payload.mode ? "ok" : "muted"}>{payload.mode || "no capture yet"}</Badge>
        {payload.snapshot_lsn ? <Badge tone="muted">snapshot lsn {payload.snapshot_lsn}</Badge> : null}
        {payload.database_uuid ? <Badge tone="muted">uuid {payload.database_uuid.slice(0, 12)}…</Badge> : null}
        <div className="ml-auto flex items-center gap-2">
          <button
            className="btn"
            disabled={!payload.nodes.length}
            onClick={() => download(`${slug}-graph.json`, JSON.stringify(payload, null, 2))}
            type="button"
          >
            <FileJson size={14} strokeWidth={2} aria-hidden />
            export JSON
          </button>
          <button className="btn" disabled={!payload.nodes.length} onClick={exportPng} type="button">
            <ImageDown size={14} strokeWidth={2} aria-hidden />
            export PNG
          </button>
        </div>
      </header>

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(0,3fr)]">
        <div className="space-y-3">
          <Section title="capture">
            <div className="space-y-3">
              <div className="flex gap-1">
                {(["projection", "query"] as Mode[]).map((option) => (
                  <button
                    className={`btn ${option === mode ? "btn-primary" : ""}`}
                    key={option}
                    onClick={() => setMode(option)}
                    type="button"
                  >
                    {option}
                  </button>
                ))}
              </div>

              {mode === "projection" ? (
                <>
                  <MultiPick label="node tables" onChange={setSelectedNodes} options={nodeTables} value={selectedNodes} />
                  <MultiPick label="relationship tables" onChange={setSelectedRels} options={relTables} value={selectedRels} />
                  <Field
                    hint="A numeric column used as edge weight. One relationship table at a time: every selected table would need its own weight, or the capture is refused."
                    label="weight column"
                  >
                    <input
                      className="input font-mono"
                      disabled={selectedRels.length !== 1}
                      onChange={(event) => setWeightColumn(event.target.value.trim())}
                      placeholder={selectedRels.length === 1 ? "weight" : "one relationship table"}
                      value={weightColumn}
                    />
                  </Field>
                  <p className="text-[11.5px] text-muted">
                    Projection is topology only: it is cheap, and it carries no properties. Both endpoints of a
                    relationship must be selected — an unselected endpoint is refused rather than followed
                    implicitly.
                  </p>
                </>
              ) : (
                <Field
                  hint="Return nodes, relationships or paths: their properties come with them. This is the same statement the console runs."
                  label="statement"
                >
                  <textarea
                    className="input h-28 font-mono"
                    onChange={(event) => setStatement(event.target.value)}
                    value={statement}
                  />
                </Field>
              )}

              <div className="grid grid-cols-2 gap-3">
                <Field hint="server cap 2000" label="max nodes">
                  <input
                    className="input"
                    onChange={(event) => setMaxNodes(Number(event.target.value))}
                    type="number"
                    value={maxNodes}
                  />
                </Field>
                <Field hint="server cap 5000" label="max edges">
                  <input
                    className="input"
                    onChange={(event) => setMaxEdges(Number(event.target.value))}
                    type="number"
                    value={maxEdges}
                  />
                </Field>
              </div>

              <div className="flex items-center gap-2">
                <button className="btn btn-primary" disabled={load.isPending} onClick={() => load.mutate()} type="button">
                  <Camera size={14} strokeWidth={2} aria-hidden />
                  {load.isPending ? "capturing…" : "capture"}
                </button>
                <select
                  className="input w-32"
                  onChange={(event) => setLayoutKind(event.target.value as LayoutKind)}
                  value={layoutKind}
                >
                  <option value="force">force atlas</option>
                  <option value="circle">circle</option>
                  <option value="keep">keep positions</option>
                </select>
              </div>
              {payloadError ? <ErrorBanner error={payloadError} onRetry={() => load.mutate()} /> : null}
              {load.error ? <ErrorBanner error={load.error} onRetry={() => load.mutate()} /> : null}
            </div>
          </Section>

          <Section
            title={
              selectedNode
                ? `node ${selectedNode.id}`
                : selectedEdge
                  ? `edge ${selectedEdge.id}`
                  : "selection"
            }
          >
            {selectedNode ? (
              <div className="space-y-2">
                <div className="flex flex-wrap items-center gap-1.5">
                  <Badge tone="ok">{selectedNode.table ?? "?"}</Badge>
                  <Badge>record {selectedNode.record_id ?? "?"}</Badge>
                  <Badge tone="muted">{nodeLabel(selectedNode)}</Badge>
                </div>
                {Object.keys(selectedNode.properties).length === 0 ? (
                  <p className="text-[12px] text-muted">
                    no properties: a projection carries topology only. Capture in query mode, or expand the
                    neighbourhood, to read them.
                  </p>
                ) : (
                  <JsonBlock maxHeight={200} value={selectedNode.properties} />
                )}
                <div className="text-[12px] text-muted">
                  {plural(incident.length, "edge")} in this view — node size is degree, so a hub is large on
                  purpose.
                </div>
                {incident.length ? (
                  <ul className="space-y-1">
                    {incident.map((edge) => {
                      const other = edge.source === selectedNode.id ? edge.target : edge.source;
                      return (
                        <li key={edge.id}>
                          <button
                            className="btn w-full justify-start font-mono text-[11.5px]"
                            onClick={() => select({ kind: "edge", id: edge.id })}
                            type="button"
                          >
                            {edge.label || edge.table} · {edge.source === selectedNode.id ? "→" : "←"} {other}
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                ) : null}
                <div className="flex flex-wrap gap-2">
                  <button className="btn" onClick={() => focusNode(selectedNode.id)} type="button">
                    centre
                  </button>
                  <ConfirmButton
                    confirmLabel="expand again"
                    detail="calls /neighbors and merges by physical id: an already-drawn node is not duplicated"
                    disabled={expand.isPending}
                    label="expand neighbourhood"
                    onConfirm={() => expand.mutate(selectedNode.id)}
                  />
                </div>
                {expand.error ? <ErrorBanner error={expand.error} /> : null}
              </div>
            ) : null}

            {selectedEdge ? <EdgeInspector edge={selectedEdge} onSelect={select} titleOf={labelOf} /> : null}

            {!selectedNode && !selectedEdge ? (
              <Empty>
                Click a node or an edge to inspect it. Ids are physical: table plus record id, never an
                application key. Hovering fades everything the focus is not attached to.
              </Empty>
            ) : null}
          </Section>
        </div>

        <div className="space-y-3">
          {payload.truncated ? (
            <p className="rounded-md border border-[#e2b13c45] bg-[#1a1508] px-2.5 py-1.5 text-[12px] text-warn">
              truncated: {payload.shown_nodes}/{payload.total_nodes} nodes and {payload.shown_edges}/
              {payload.total_edges} edges at snapshot lsn {payload.snapshot_lsn}. Raise the caps or narrow the
              selection — this view never pretends to be the whole store.
            </p>
          ) : null}
          {payload.warnings.length ? (
            <ul className="space-y-1">
              {payload.warnings.map((warning, index) => (
                <li
                  className="rounded-md border border-[#e2b13c45] bg-[#1a1508] px-2.5 py-1 text-[12px] text-warn"
                  key={index}
                >
                  <code>{warning.kind}</code>{" "}
                  {Object.entries(warning)
                    .filter(([key]) => key !== "kind")
                    .map(([key, value]) => `${key}=${String(value)}`)
                    .join(" ")}
                </li>
              ))}
            </ul>
          ) : null}

          <div className="card relative overflow-hidden" style={{ height: "68vh" }}>
            {webglOk ? <div className="absolute inset-0" ref={container} /> : null}

            {!webglOk ? (
              <div className="scroll-thin absolute inset-0 overflow-y-auto p-3">
                <p className="mb-2 flex items-center gap-2 text-[12px] text-warn">
                  <MonitorX size={14} strokeWidth={2} aria-hidden />
                  WebGL is unavailable here, so this capture is listed instead of drawn. The same
                  inspection works: {plural(payload.nodes.length, "node")} and{" "}
                  {plural(payload.edges.length, "edge")}.
                </p>
                <div className="grid gap-3 md:grid-cols-2">
                  <div>
                    <span className="label">nodes</span>
                    <ul className="space-y-1">
                      {payload.nodes.map((node) => (
                        <li key={node.id}>
                          <button
                            className={`btn w-full justify-start font-mono text-[11.5px] ${
                              selected?.kind === "node" && selected.id === node.id ? "btn-primary" : ""
                            }`}
                            onClick={() => select({ kind: "node", id: node.id })}
                            type="button"
                          >
                            {node.table ?? "?"} · {nodeLabel(node)}
                          </button>
                        </li>
                      ))}
                    </ul>
                  </div>
                  <div>
                    <span className="label">edges</span>
                    <ul className="space-y-1">
                      {payload.edges.map((edge) => (
                        <li key={edge.id}>
                          <button
                            className={`btn w-full justify-start font-mono text-[11.5px] ${
                              selected?.kind === "edge" && selected.id === edge.id ? "btn-primary" : ""
                            }`}
                            onClick={() => select({ kind: "edge", id: edge.id })}
                            type="button"
                          >
                            {edge.label || edge.table} · {edge.source} → {edge.target}
                          </button>
                        </li>
                      ))}
                    </ul>
                  </div>
                </div>
              </div>
            ) : null}

            {settling ? (
              <div className="pointer-events-none absolute bottom-3 right-3 z-10 flex items-center gap-2 rounded-lg border border-line bg-panel/90 px-2 py-1 text-[11.5px] text-accent">
                <Spinner label="settling layout…" />
              </div>
            ) : null}

            {load.isPending ? (
              <div className="absolute inset-0 flex items-center justify-center bg-void/70">
                <Spinner label="capturing and laying out…" />
              </div>
            ) : null}
            {!payload.nodes.length && !load.isPending ? (
              <div className="absolute inset-0 flex items-center justify-center">
                <Empty>Capture a projection or a statement to draw the graph.</Empty>
              </div>
            ) : null}

            {webglOk && payload.nodes.length ? (
              <>
                <div className="absolute bottom-3 left-3 z-10 flex flex-col overflow-hidden rounded-lg border border-line bg-panel/90">
                  <button
                    aria-label="zoom in"
                    className="px-1.5 py-1 text-muted hover:bg-raised hover:text-fg"
                    onClick={() => zoom("in")}
                    type="button"
                  >
                    <ZoomIn size={14} strokeWidth={2} aria-hidden />
                  </button>
                  <button
                    aria-label="zoom out"
                    className="border-t border-line px-1.5 py-1 text-muted hover:bg-raised hover:text-fg"
                    onClick={() => zoom("out")}
                    type="button"
                  >
                    <ZoomOut size={14} strokeWidth={2} aria-hidden />
                  </button>
                  <button
                    aria-label="fit the whole capture"
                    className="border-t border-line px-1.5 py-1 text-muted hover:bg-raised hover:text-fg"
                    onClick={fit}
                    type="button"
                  >
                    <Maximize size={14} strokeWidth={2} aria-hidden />
                  </button>
                </div>
                <button
                  className="btn absolute bottom-3 right-3 z-10"
                  onClick={rerunLayout}
                  title="Re-run the force layout"
                  type="button"
                >
                  <RotateCcw size={13} strokeWidth={2} aria-hidden />
                  re-run layout
                </button>
                <p className="pointer-events-none absolute bottom-3 left-12 z-10 text-[11px] text-muted">
                  scroll to zoom · drag to pan · drag a node to move it · click a node or an edge to inspect ·
                  double-click to centre
                </p>
              </>
            ) : null}
          </div>

          <div className="flex flex-wrap items-center gap-2 text-[12px] text-muted">
            {Object.entries(model.current?.colors ?? {}).map(([table, color]) => (
              <span className="inline-flex items-center gap-1.5" key={table}>
                <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: color }} />
                {table}
              </span>
            ))}
            <span className="ml-auto">
              {plural(payload.nodes.length, "node")} · {plural(payload.edges.length, "edge")} · loops and parallel
              edges are kept as drawn
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

/** The inspected relationship: what it joins, in which direction, and what it carries. */
function EdgeInspector({
  edge,
  onSelect,
  titleOf,
}: {
  edge: GraphEdge;
  onSelect: (focus: Focus) => void;
  titleOf: (id: string) => string;
}) {
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone="ok">{edge.table ?? edge.label}</Badge>
        {edge.record_id !== null ? <Badge>record {edge.record_id}</Badge> : null}
        {edge.source === edge.target ? <Badge tone="warn">self-loop</Badge> : null}
      </div>
      <div className="space-y-1">
        <span className="label">direction</span>
        <div className="flex flex-wrap items-center gap-1">
          <button
            className="btn font-mono text-[11.5px]"
            onClick={() => onSelect({ kind: "node", id: edge.source })}
            type="button"
          >
            {titleOf(edge.source)}
          </button>
          <span className="font-mono text-[12px] text-accent">→</span>
          <button
            className="btn font-mono text-[11.5px]"
            onClick={() => onSelect({ kind: "node", id: edge.target })}
            type="button"
          >
            {titleOf(edge.target)}
          </button>
        </div>
        <p className="text-[11.5px] text-muted">
          The stroke carries no arrowhead, so direction is stated here: {edge.source} is the source,{" "}
          {edge.target} the target.
        </p>
      </div>
      {Object.keys(edge.properties).length === 0 ? (
        <p className="text-[12px] text-muted">
          no properties: a projection relationship is topology only. Capture in query mode to read what the
          store holds on it.
        </p>
      ) : (
        <JsonBlock maxHeight={200} value={edge.properties} />
      )}
      <p className="font-mono text-[11px] text-muted">physical edge id {edge.id}</p>
    </div>
  );
}

function plural(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

function MultiPick({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: string[];
  value: string[];
  onChange: (next: string[]) => void;
}) {
  return (
    <div>
      <span className="label">{label}</span>
      <div className="flex flex-wrap gap-1">
        {options.length === 0 ? <span className="text-[11.5px] text-muted">the store has none</span> : null}
        {options.map((option) => {
          const on = value.includes(option);
          return (
            <button
              className={`btn ${on ? "btn-primary" : ""}`}
              key={option}
              onClick={() => onChange(on ? value.filter((item) => item !== option) : [...value, option])}
              type="button"
            >
              {option}
            </button>
          );
        })}
      </div>
    </div>
  );
}
