import type { Cell } from "@/api/client";

/**
 * The wire format is tagged: `{kind:"vector"}`, `{kind:"decimal"}`, `{kind:"node"}` … so the console
 * renders the type it was told about instead of guessing. Anything unrecognised is shown as its
 * JSON text rather than silently flattened.
 */

export function isTagged(value: unknown): value is { kind: string } & Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) && typeof (value as { kind?: unknown }).kind === "string";
}

export function formatCell(value: Cell | undefined, depth = 0): string {
  if (value === null || value === undefined) return "∅";
  if (typeof value === "string") return value;
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(6).replace(/0+$/, "");
  if (typeof value === "boolean") return value ? "true" : "false";
  if (Array.isArray(value)) {
    if (value.length > 8) return `[${value.slice(0, 8).map((v) => formatCell(v, depth + 1)).join(", ")}, +${value.length - 8}]`;
    return `[${value.map((v) => formatCell(v, depth + 1)).join(", ")}]`;
  }
  if (isTagged(value)) {
    switch (value.kind) {
      case "vector": {
        const values = (value.values ?? []) as number[];
        const head = values.slice(0, 4).map((n) => n.toFixed(3)).join(", ");
        return `⟨${head}${values.length > 4 ? ` … ${values.length}d` : ""}⟩`;
      }
      case "decimal":
        return String(value.value ?? "");
      case "node":
        return `(${value.table ?? "?"}:${value.record_id ?? "?"})`;
      case "relationship":
        return `-[${value.table ?? "?"}:${value.record_id ?? "?"}]->`;
      case "path":
        return `path(${(value.nodes as unknown[])?.length ?? 0})`;
      case "date":
      case "datetime":
      case "local_datetime":
      case "timestamp":
      case "time":
      case "local_time":
        return String(value.value ?? "");
      case "duration":
        return String(value.iso ?? value.value ?? "");
      case "map":
        return formatCell((value.values as Cell[]) ?? null, depth + 1);
      default:
        return JSON.stringify(value);
    }
  }
  const entries = Object.entries(value as Record<string, unknown>);
  const body = entries.slice(0, 4).map(([key, item]) => `${key}: ${formatCell(item as Cell, depth + 1)}`);
  return `{${body.join(", ")}${entries.length > 4 ? `, +${entries.length - 4}` : ""}}`;
}

/** Entities survive as tagged objects; "ver como grafo" is only offered when they are there. */
export function entityIds(rows: Cell[][]): string[] {
  const ids = new Set<string>();
  const walk = (value: unknown): void => {
    if (Array.isArray(value)) return value.forEach(walk);
    if (!value || typeof value !== "object") return;
    if (isTagged(value)) {
      if (value.kind === "node" && value.table !== undefined && value.record_id !== undefined) {
        ids.add(`${value.table}:${value.record_id}`);
      }
      if (value.kind === "path") {
        for (const node of (value.nodes as Cell[]) ?? []) walk(node);
        for (const rel of (value.relationships as Cell[]) ?? []) walk(rel);
      }
    }
    for (const item of Object.values(value as Record<string, unknown>)) walk(item);
  };
  rows.forEach(walk);
  return [...ids];
}

export function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / 1024 / 1024).toFixed(1)} MiB`;
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, { hour12: false });
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86_400)}d ago`;
}

/** Query history and editor drafts live in localStorage: nothing about them leaves the browser. */
export const history = {
  key: (slug: string) => `okg.history.${slug}`,
  load(slug: string, limit = 20): { query: string; at: string }[] {
    try {
      const raw = window.localStorage.getItem(history.key(slug));
      const parsed = raw ? (JSON.parse(raw) as { query: string; at: string }[]) : [];
      return parsed.filter((entry) => typeof entry?.query === "string").slice(0, limit);
    } catch {
      return [];
    }
  },
  push(slug: string, query: string): void {
    if (!query.trim()) return;
    const entries = history.load(slug, 20).filter((entry) => entry.query !== query);
    window.localStorage.setItem(
      history.key(slug),
      JSON.stringify([{ query, at: new Date().toISOString() }, ...entries].slice(0, 20)),
    );
  },
  clear(slug: string): void {
    window.localStorage.removeItem(history.key(slug));
  },
};

/**
 * "View as graph" re-runs *the same statement* in graph mode, which extracts the entities the rows
 * already carry. Synthesising a WHERE on a guessed primary key would be a different question.
 */
export function graphQueryUrl(slug: string, query: string): string {
  const path = `/databases/${encodeURIComponent(slug)}/graph`;
  return `${path}?mode=query&query=${encodeURIComponent(query)}`;
}

/** Split a physical id back into the pair /neighbors needs. */
export function parseEntityId(id: string): { table: string; record_id: number } | null {
  const match = /^([^:]+):(\d+)$/.exec(id);
  return match ? { table: match[1], record_id: Number(match[2]) } : null;
}
