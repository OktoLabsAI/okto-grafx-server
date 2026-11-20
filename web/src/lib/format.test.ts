import { describe, expect, it } from "vitest";
import { entityIds, formatCell, graphQueryUrl, history, parseEntityId } from "@/lib/format";
import { parseVector } from "@/routes/Search";
import { stateOf } from "@/routes/Databases";
import type { Cell, DatabaseRecord } from "@/api/client";

describe("typed cell rendering", () => {
  it("renders the tag it was sent instead of guessing", () => {
    expect(formatCell(null)).toBe("∅");
    expect(formatCell({ kind: "vector", values: [1, 0], dtype: "float32" } as Cell)).toBe("⟨1.000, 0.000⟩");
    expect(
      formatCell({ kind: "vector", values: Array.from({ length: 8 }, (_, index) => index / 8), dtype: "float32" } as Cell),
    ).toContain("8d");
    expect(formatCell({ kind: "decimal", value: "12.50", precision: 4, scale: 2 } as Cell)).toBe("12.50");
    expect(formatCell({ kind: "node", table: "Doc", record_id: 7, label: null, properties: {} } as Cell)).toBe("(Doc:7)");
    expect(formatCell({ kind: "relationship", table: "CITES", record_id: 3 } as Cell)).toBe("-[CITES:3]->");
    expect(formatCell({ kind: "date", value: "1970-01-01" } as Cell)).toBe("1970-01-01");
  });

  it("shortens long collections rather than filling the screen", () => {
    const wide = Array.from({ length: 20 }, (_, index) => index) as Cell[];
    expect(formatCell(wide)).toMatch(/\+12\]$/);
    expect(formatCell({ a: 1, b: 2, c: 3, d: 4, e: 5 } as Cell)).toContain("+1");
  });

  it("keeps numbers readable", () => {
    expect(formatCell(3)).toBe("3");
    expect(formatCell(0.5)).toBe("0.5");
    expect(formatCell(true)).toBe("true");
  });
});

describe("entity extraction for “view as graph”", () => {
  it("finds nodes inside rows, lists and paths", () => {
    const rows: Cell[][] = [
      [
        { kind: "node", table: "Doc", record_id: 1, properties: {} },
        [{ kind: "node", table: "Tag", record_id: 2, properties: {} }],
        {
          kind: "path",
          nodes: [
            { kind: "node", table: "Doc", record_id: 3, properties: {} },
            { kind: "node", table: "Doc", record_id: 1, properties: {} },
          ],
          relationships: [{ kind: "relationship", table: "CITES", record_id: 1 }],
        },
        "not an entity",
        7,
      ],
    ];
    expect(entityIds(rows).sort()).toEqual(["Doc:1", "Doc:3", "Tag:2"]);
  });

  it("re-runs the same statement in graph mode rather than inventing a key", () => {
    const url = graphQueryUrl("docs", "MATCH (a)-[r]->(b) RETURN a, r, b");
    expect(url).toBe("/databases/docs/graph?mode=query&query=MATCH%20(a)-%5Br%5D-%3E(b)%20RETURN%20a%2C%20r%2C%20b");
  });

  it("splits a physical id for the neighbours call", () => {
    expect(parseEntityId("Doc:12")).toEqual({ table: "Doc", record_id: 12 });
    expect(parseEntityId("nope")).toBeNull();
  });
});

describe("literal vectors", () => {
  it("accepts the shapes people type", () => {
    expect(parseVector("[0.5, 0.25]")).toEqual([0.5, 0.25]);
    expect(parseVector("1 2 3")).toEqual([1, 2, 3]);
    expect(parseVector("0.1,0.2,0.3")).toEqual([0.1, 0.2, 0.3]);
  });

  it("refuses what is not a vector", () => {
    expect(parseVector("")).toBeNull();
    expect(parseVector("vector")).toBeNull();
    expect(parseVector("[0.1, oops]")).toBeNull();
  });
});

describe("query history", () => {
  it("stays in this browser, newest first, without duplicates", () => {
    history.push("docs", "MATCH (a) RETURN a");
    history.push("docs", "MATCH (b) RETURN b");
    history.push("docs", "MATCH (a) RETURN a");
    const loaded = history.load("docs");
    expect(loaded).toHaveLength(2);
    expect(loaded[0].query).toBe("MATCH (a) RETURN a");
    expect(window.localStorage.getItem("okg.history.docs")).toContain("MATCH (b)");
    history.clear("docs");
    expect(history.load("docs")).toEqual([]);
  });

  it("ignores blank statements", () => {
    history.push("docs", "   ");
    expect(history.load("docs")).toEqual([]);
  });
});

describe("store state badges", () => {
  const record = {
    slug: "docs",
    name: "docs",
    path: "/srv/grafx/docs",
    database_uuid: "u",
    format_version: 2,
    read_only: false,
    allow_writes: false,
    options: {},
    enabled: true,
    open_policy: "lazy",
    note: "",
    created_at: "",
    updated_at: "",
  } satisfies DatabaseRecord;

  it("says recovery before anything else", () => {
    expect(stateOf(record, true, { stale: ["idx"], recovery: true }).label).toBe("recovery required");
  });

  it("says degraded when an index is stale", () => {
    expect(stateOf(record, true, { stale: ["idx"], recovery: false })).toEqual({ label: "degraded", tone: "warn" });
  });

  it("distinguishes closed from open, and disabled from both", () => {
    expect(stateOf(record, false, { stale: [], recovery: false }).label).toBe("closed");
    expect(stateOf(record, true, { stale: [], recovery: false }).label).toBe("open");
    expect(stateOf({ ...record, enabled: false }, true, { stale: [], recovery: false }).label).toBe("disabled");
  });
});
