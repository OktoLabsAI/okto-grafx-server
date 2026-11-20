import { useEffect, useMemo, useState } from "react";
import { Network, Play, ScrollText, Trash2 } from "lucide-react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import CodeMirror from "@uiw/react-codemirror";
import { oktoTheme, editorExtensions } from "@/lib/editor";
import { data, type Cell, type QueryResult } from "@/api/client";
import { DataTable, PlanTree } from "@/components/DataTable";
import { Badge, Empty, ErrorBanner, JsonBlock, Section, Spinner } from "@/components/ui";
import { entityIds, formatTime, graphQueryUrl, history } from "@/lib/format";

export function QueryConsole() {
  const { slug = "" } = useParams();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [query, setQuery] = useState(params.get("query") ?? "MATCH (n) RETURN n LIMIT 25");
  const [limit, setLimit] = useState<number | "">(params.get("limit") ? Number(params.get("limit")) : "");
  const [result, setResult] = useState<QueryResult | null>(null);
  const [running, setRunning] = useState<"none" | "query" | "explain">("none");
  const [error, setError] = useState<unknown>(null);
  const [plan, setPlan] = useState<Record<string, unknown> | null>(null);
  const [inspect, setInspect] = useState<{ value: Cell; column: string } | null>(null);
  const [localHistory, setLocalHistory] = useState(() => history.load(slug));

  const schema = useQuery({
    queryKey: ["schema", slug],
    queryFn: () => data.schema(slug),
    staleTime: 60_000,
    retry: false,
  });

  const suggestions = useMemo(() => {
    const tables = [...(schema.data?.tables ?? []), ...(schema.data?.relationships ?? [])];
    const keywords = ["MATCH", "WHERE", "RETURN", "CREATE", "SET", "DELETE", "LIMIT", "ORDER BY", "WITH", "UNWIND"];
    return [
      ...tables.map((table) => table.name),
      ...tables.flatMap((table) => table.columns.map((column) => column.name)),
      ...keywords,
    ];
  }, [schema.data]);

  async function run(kind: "query" | "explain") {
    setRunning(kind);
    setError(null);
    try {
      if (kind === "explain") {
        const explained = await data.explain(slug, query);
        setPlan(
          (explained.plan as unknown as Record<string, unknown>) ??
            (explained as unknown as Record<string, unknown>),
        );
      } else {
        const answered = await data.query(slug, query, limit === "" ? {} : { limit: Number(limit) });
        setResult(answered);
        setPlan(null);
        history.push(slug, query);
        setLocalHistory(history.load(slug));
      }
    } catch (caught) {
      setError(caught);
    } finally {
      setRunning("none");
    }
  }

  // Ctrl/Cmd+Enter runs the statement, from anywhere in the page.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        event.preventDefault();
        void run("query");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const entities = useMemo(() => (result ? entityIds(result.rows) : []), [result]);

  return (
    <div className="space-y-3">
      <header className="flex flex-wrap items-center gap-3">
        <Link className="btn" to={`/databases/${slug}`} aria-label="back to store">
          ← {slug}
        </Link>
        <h1 className="text-[16px] font-semibold tracking-tight">query console</h1>
        <Badge tone={data.usingKey() ? "ok" : "warn"}>
          {data.usingKey() ? "bearer key" : "admin session"}
        </Badge>
        <span className="ml-auto text-[11px] text-muted">
          the read door: statements here cannot write. Writes go through a WebSocket session in mode=write.
        </span>
      </header>

      <Section
        actions={
          <>
            <label className="flex items-center gap-1.5 text-[12px] text-muted">
              rows
              <input
                className="input w-20"
                onChange={(event) => setLimit(event.target.value === "" ? "" : Number(event.target.value))}
                placeholder="cap"
                type="number"
                value={limit}
              />
            </label>
            <button className="btn" disabled={running !== "none"} onClick={() => void run("explain")} type="button">
              <ScrollText size={14} strokeWidth={2} aria-hidden />
              EXPLAIN
            </button>
            <button className="btn btn-primary" disabled={running !== "none" || !query.trim()} onClick={() => void run("query")} type="button">
              <Play size={14} strokeWidth={2} aria-hidden />
              {running === "query" ? "running…" : "Run  ⌘/Ctrl+↵"}
            </button>
          </>
        }
        title={`statement on ${slug}`}
      >
        <div className="h-52 overflow-hidden rounded-md border border-line">
          <CodeMirror
            basicSetup={{ autocompletion: true, highlightActiveLine: true, foldGutter: false }}
            extensions={editorExtensions(suggestions)}
            height="100%"
            onChange={setQuery}
            placeholder="MATCH (d:Doc) RETURN d.id, d.title LIMIT 25"
            theme={oktoTheme}
            value={query}
          />
        </div>
        {error ? <div className="mt-2"><ErrorBanner error={error} onRetry={() => void run("query")} /></div> : null}
      </Section>

      <div className="grid gap-3 xl:grid-cols-[minmax(0,3fr)_minmax(0,1fr)]">
        <Section
          actions={
            <>
              {result ? (
                <span className="text-[11.5px] text-muted">
                  {result.row_count} rows · {result.duration_ms} ms · lsn {result.snapshot_lsn}
                </span>
              ) : null}
              {result && entities.length > 0 ? (
                <button className="btn btn-primary" onClick={() => navigate(graphQueryUrl(slug, query))} type="button">
                  <Network size={14} strokeWidth={2} aria-hidden />
                  view as graph ({entities.length} entities)
                </button>
              ) : null}
            </>
          }
          title="result"
        >
          {running === "query" ? <Spinner label="executing…" /> : null}
          {result && running === "none" ? (
            <>
              {result.truncated ? (
                <p className="mb-2 rounded-md border border-[#e2b13c45] bg-[#1a1508] px-2.5 py-1.5 text-[12px] text-warn">
                  truncated: the server stopped after {result.row_count} rows. The cap is its{" "}
                  <code>max_request_rows</code>, and no continuation token would keep this snapshot — a
                  Cypher <code>LIMIT</code> is the honest way to ask for less.
                </p>
              ) : null}
              {result.warnings.length ? (
                <ul className="mb-2 space-y-1">
                  {result.warnings.map((warning, index) => (
                    <li className="rounded-md border border-[#e2b13c45] bg-[#1a1508] px-2.5 py-1 text-[12px] text-warn" key={index}>
                      <code>{warning.kind}</code> {JSON.stringify(warning).replace(`{"kind":"${warning.kind}",`, "").slice(0, -1)}
                    </li>
                  ))}
                </ul>
              ) : null}
              <DataTable
                columns={result.columns}
                onCellClick={(value, column) => setInspect({ value, column })}
                rows={result.rows}
              />
            </>
          ) : null}
          {!result && running === "none" && !error ? <Empty>Nothing has run yet.</Empty> : null}
        </Section>

        <div className="space-y-3">
          <Section title={plan ? "plan" : "statistics"}>
            {running === "explain" ? <Spinner label="planning…" /> : null}
            {plan ? <PlanTree plan={plan} /> : null}
            {!plan && result ? (
              <dl className="space-y-1 text-[12.5px]">
                {Object.entries(result.statistics).map(([key, value]) => (
                  <div className="flex justify-between gap-3 border-b border-line pb-0.5" key={key}>
                    <dt className="text-muted">{key}</dt>
                    <dd className="font-mono text-[12px] text-cell">{value}</dd>
                  </div>
                ))}
                {Object.keys(result.statistics).length === 0 ? <Empty>The engine reported no counters.</Empty> : null}
              </dl>
            ) : null}
            {!plan && !result && running !== "explain" ? <Empty>Run EXPLAIN to see the operator tree.</Empty> : null}
          </Section>

          {inspect ? (
            <Section title={`cell · ${inspect.column}`}>
              <JsonBlock value={inspect.value} />
            </Section>
          ) : null}

          <Section
            actions={
              <button
                className="btn"
                onClick={() => {
                  history.clear(slug);
                  setLocalHistory([]);
                }}
                type="button"
              >
                <Trash2 size={14} strokeWidth={2} aria-hidden />
                clear
              </button>
            }
            title="history (this browser only)"
          >
            {localHistory.length === 0 ? <Empty>No statements yet.</Empty> : null}
            <ul className="space-y-1">
              {localHistory.map((entry) => (
                <li key={entry.at}>
                  <button
                    className="block w-full truncate rounded border border-line bg-deep px-2 py-1 text-left font-mono text-[11.5px] text-cell hover:border-[#3a4250]"
                    onClick={() => setQuery(entry.query)}
                    title={entry.query}
                    type="button"
                  >
                    {entry.query.replace(/\s+/g, " ")}
                  </button>
                  <span className="text-[10.5px] text-muted">{formatTime(entry.at)}</span>
                </li>
              ))}
            </ul>
          </Section>
        </div>
      </div>
    </div>
  );
}
