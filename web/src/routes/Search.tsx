import { useMemo, useState } from "react";
import { Search as SearchIcon } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { admin, data, type HybridHits, type SearchHit, type TextHits, type VectorHits } from "@/api/client";
import { Badge, Empty, ErrorBanner, Field, JsonBlock, Spinner, Tabs } from "@/components/ui";
import { formatCell } from "@/lib/format";

type Mode = "text" | "vector" | "hybrid";

const MODES = [
  { id: "text" as const, label: "Full-text" },
  { id: "vector" as const, label: "Vector" },
  { id: "hybrid" as const, label: "Hybrid" },
];

/** `[0.1, 0.2]`, `0.1 0.2`, `0.1,0.2` — all mean the same literal here. */
export function parseVector(raw: string): number[] | null {
  const cleaned = raw.trim().replace(/^\[|\]$/g, "");
  if (!cleaned) return null;
  const parts = cleaned.split(/[\s,]+/).filter(Boolean);
  const values = parts.map((part) => Number(part));
  if (values.some((value) => !Number.isFinite(value))) return null;
  return values;
}

export function Search() {
  const { slug = "" } = useParams();
  const [mode, setMode] = useState<Mode>("text");

  const inventory = useQuery({ queryKey: ["inventory", slug], queryFn: () => admin.inventory(slug), retry: false });
  const indexes = inventory.data?.indexes ?? [];
  // The store reports what each index was built to do. Only `text` indexes can answer a full-text
  // query: an `exact` identity index (`pk_…`, `ef_…`) is a real index that would answer with
  // `index_error`, so offering it as the default would make the first search of a fresh store fail.
  const textIndexes = indexes.filter((index) => index.kind === "text");
  const spaces = inventory.data?.schema?.vector_spaces ?? [];
  // A space is "shared" when more than one column writes it. The schema states that directly — a
  // vector column names the space it belongs to — so the warning is not a guess from a name.
  const spaceOwners = useMemo(() => {
    const owners = new Map<string, string[]>();
    for (const table of [...(inventory.data?.schema?.tables ?? []), ...(inventory.data?.schema?.relationships ?? [])]) {
      for (const column of table.columns ?? []) {
        if (!column.vector_space) continue;
        owners.set(column.vector_space, [...(owners.get(column.vector_space) ?? []), `${table.name}.${column.name}`]);
      }
    }
    return owners;
  }, [inventory.data]);
  const tables = [
    ...(inventory.data?.schema?.tables ?? []),
    ...(inventory.data?.schema?.relationships ?? []),
  ].filter(
    (table): table is NonNullable<typeof table> => Boolean(table),
  );

  const [index, setIndex] = useState("");
  const [query, setQuery] = useState("");
  const [k, setK] = useState(10);
  const [prefix, setPrefix] = useState(false);
  const [phrase, setPhrase] = useState(false);

  const [space, setSpace] = useState("");
  const [vector, setVector] = useState("");
  const [table, setTable] = useState("");
  const [tableKind, setTableKind] = useState<"node" | "rel">("node");

  const [candidateK, setCandidateK] = useState(20);
  const [lexicalWeight, setLexicalWeight] = useState(1);
  const [vectorWeight, setVectorWeight] = useState(1);
  const [relations, setRelations] = useState("");
  const [graphWeight, setGraphWeight] = useState(0);
  const [allowPartial, setAllowPartial] = useState(false);

  const vectorValues = useMemo(() => parseVector(vector), [vector]);
  const chosenIndex = index || textIndexes[0]?.name || "";
  const chosenSpace = space || spaces[0]?.name || "";

  const search = useMutation({
    mutationFn: async () => {
      if (mode === "text") {
        return data.search.text(slug, { index: chosenIndex, query, k, prefix, phrase });
      }
      if (mode === "vector") {
        if (!vectorValues) throw new Error("the vector must be numbers, e.g. 0.1, 0.2");
        const shared = (spaceOwners.get(chosenSpace)?.length ?? 0) > 1;
        return data.search.vector(slug, {
          space: chosenSpace,
          vector: vectorValues,
          k,
          table: shared || table ? table : null,
          table_kind: shared || table ? tableKind : null,
        });
      }
      return data.search.hybrid(slug, {
        table,
        index: chosenIndex || null,
        query,
        space: chosenSpace || null,
        vector: vectorValues ?? [],
        k,
        candidate_k: candidateK,
        lexical_weight: lexicalWeight,
        vector_weight: vectorWeight,
        allow_partial: allowPartial,
        graph_relations: relations.split(/[\s,]+/).filter(Boolean),
        graph_weight: graphWeight,
      });
    },
  });

  const hits: SearchHit[] =
    (search.data as TextHits | VectorHits | HybridHits | undefined)?.hits ?? [];
  const meta = search.data as (TextHits & VectorHits & HybridHits) | undefined;

  return (
    <div className="space-y-3">
      <header className="flex flex-wrap items-center gap-3">
        <Link className="btn" to={`/databases/${slug}`}>
          ← {slug}
        </Link>
        <h1 className="text-[16px] font-semibold tracking-tight">search</h1>
        {inventory.error ? <Badge tone="warn">store closed or unreadable</Badge> : null}
      </header>

      <Tabs tabs={MODES} active={mode} onChange={setMode} />

      <div className="card grid gap-4 p-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.6fr)]">
        <div className="space-y-3">
          {mode !== "vector" ? (
            <Field hint="Text indexes are listed by the store itself; the name is what the engine matches." label="index">
              <select className="input" onChange={(event) => setIndex(event.target.value)} value={index}>
                <option value="">{chosenIndex ? `${chosenIndex} (first available)` : "no text index"}</option>
                {textIndexes.map((item) => (
                  <option key={`${item.table}.${item.name}`} value={item.name}>
                    {item.name} · {item.table}
                    {item.stale ? " (stale)" : ""}
                  </option>
                ))}
              </select>
            </Field>
          ) : null}

          <Field hint="Which table owns the space. Needed for a shared space, and for hybrid search." label={mode === "hybrid" ? "table" : "table (optional disambiguation)"}>
            <input
              className="input font-mono"
              onChange={(event) => setTable(event.target.value.trim())}
              placeholder={tables.map((item) => item.name).slice(0, 4).join(", ") || "Doc"}
              value={table}
            />
          </Field>

          {mode !== "vector" ? (
            <Field hint="Boolean terms: `vector AND storage`, `vector OR hnsw`, `vector NOT storage`." label="query">
              <textarea
                className="input h-20 font-mono"
                onChange={(event) => setQuery(event.target.value)}
                value={query}
              />
            </Field>
          ) : null}

          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="k">
              <input className="input" min={1} onChange={(event) => setK(Number(event.target.value))} type="number" value={k} />
            </Field>
            {mode === "text" ? (
              <>
                <Field hint="Trailing-prefix expansion." label="prefix">
                  <select className="input" onChange={(event) => setPrefix(event.target.value === "true")} value={String(prefix)}>
                    <option value="false">off</option>
                    <option value="true">on</option>
                  </select>
                </Field>
                <Field hint="Exact phrase over positions." label="phrase">
                  <select className="input" onChange={(event) => setPhrase(event.target.value === "true")} value={String(phrase)}>
                    <option value="false">off</option>
                    <option value="true">on</option>
                  </select>
                </Field>
              </>
            ) : null}
            {mode === "vector" || mode === "hybrid" ? (
              <Field hint={`the space is ${spaces.find((item) => item.name === chosenSpace)?.dimension ?? "?"} dimensional`} label="table kind">
                <select className="input" onChange={(event) => setTableKind(event.target.value as "node" | "rel")} value={tableKind}>
                  <option value="node">node</option>
                  <option value="rel">rel</option>
                </select>
              </Field>
            ) : null}
          </div>

          {mode === "vector" || mode === "hybrid" ? (
            <>
              <Field hint="Choose a space; the dimension must match exactly." label="space">
                <select className="input" onChange={(event) => setSpace(event.target.value)} value={space}>
                  <option value="">{chosenSpace || "no vector space"}</option>
                  {spaces.map((item) => (
                    <option key={item.name} value={item.name}>
                      {item.name} · {item.dimension}d · {item.metric}
                      {(spaceOwners.get(item.name)?.length ?? 0) > 1 ? ` · ${spaceOwners.get(item.name)?.length} columns` : ""}
                    </option>
                  ))}
                </select>
              </Field>
              <Field hint="Literal vector: 0.1, 0.2, …" label="query vector">
                <textarea className="input h-16 font-mono" onChange={(event) => setVector(event.target.value)} value={vector} />
              </Field>
              {/* The plan asks the search screen to say this out loud: a vector search here is not a
                  semantic search by itself, and the console must not pretend otherwise. */}
              <p className="text-[11.5px] text-muted">
                This server does not generate embeddings. The vector above is yours — compute it with
                whatever model produced the vectors stored in the column, or type one to probe.
              </p>
              {vector && !vectorValues ? <p className="text-[12px] text-danger">that vector does not parse</p> : null}
            </>
          ) : null}

          {mode === "hybrid" ? (
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="candidate_k">
                <input className="input" onChange={(event) => setCandidateK(Number(event.target.value))} type="number" value={candidateK} />
              </Field>
              <Field label="lexical weight">
                <input className="input" onChange={(event) => setLexicalWeight(Number(event.target.value))} step="0.1" type="number" value={lexicalWeight} />
              </Field>
              <Field label="vector weight">
                <input className="input" onChange={(event) => setVectorWeight(Number(event.target.value))} step="0.1" type="number" value={vectorWeight} />
              </Field>
              <Field hint="Relationship types used as evidence in the fusion." label="graph relations">
                <input className="input font-mono" onChange={(event) => setRelations(event.target.value)} placeholder="CITES, TAGGED" value={relations} />
              </Field>
              <Field label="graph weight">
                <input className="input" onChange={(event) => setGraphWeight(Number(event.target.value))} step="0.1" type="number" value={graphWeight} />
              </Field>
              <Field hint="Answer from one signal when the other errors, and say so in the payload." label="allow_partial">
                <select className="input" onChange={(event) => setAllowPartial(event.target.value === "true")} value={String(allowPartial)}>
                  <option value="false">off — refuse if a signal fails</option>
                  <option value="true">on</option>
                </select>
              </Field>
            </div>
          ) : null}

          <button
            className="btn btn-primary"
            disabled={search.isPending || !((mode === "vector" ? chosenSpace : chosenIndex) && (mode === "vector" ? vector : query) || (mode === "hybrid" && table))}
            onClick={() => search.mutate()}
            type="button"
          >
            <SearchIcon size={14} strokeWidth={2} aria-hidden />
            {search.isPending ? "searching…" : "search"}
          </button>
          {search.error ? <ErrorBanner error={search.error} onRetry={() => search.mutate()} /> : null}
        </div>

        <div>
          {search.isPending ? <Spinner label="asking the index…" /> : null}
          {search.data ? (
            <>
              <div className="mb-2 flex flex-wrap items-center gap-1.5">
                <Badge tone="ok">{hits.length} hits</Badge>
                <Badge tone={isExactRegime(meta?.regime) ? "ok" : "warn"}>regime {meta?.regime}</Badge>
                {"statistics_regime" in (meta ?? {}) ? <Badge tone={meta?.statistics_regime === "exact" ? "ok" : "warn"}>statistics {meta?.statistics_regime}</Badge> : null}
                {"candidates" in (meta ?? {}) ? <Badge>{meta?.candidates} candidates</Badge> : null}
                {"achieved_k" in (meta ?? {}) ? <Badge>achieved {meta?.achieved_k}/{meta?.requested_k}</Badge> : null}
                {meta?.fusion ? <Badge>{meta.fusion}</Badge> : null}
                <Badge tone={meta?.lexical_regime === "complete" ? "ok" : "warn"}>lexical {meta?.lexical_regime ?? "—"}</Badge>
                <Badge tone={meta?.vector_regime === "complete" ? "ok" : "warn"}>vector {meta?.vector_regime ?? "—"}</Badge>
                <span className="ml-auto font-mono text-[11px] text-muted">commit {meta?.snapshot_commit}</span>
              </div>
              {meta?.source_errors && Object.keys(meta.source_errors).length ? (
                <p className="mb-2 rounded-md border border-[#e2b13c45] bg-[#1a1508] px-2 py-1 text-[12px] text-warn">
                  source errors: {JSON.stringify(meta.source_errors)}
                </p>
              ) : null}
              <ol className="space-y-1.5">
                {hits.map((hit) => (
                  <li className="rounded-md border border-line bg-deep p-2" key={`${hit.id}-${hit.score}`}>
                    <div className="flex flex-wrap items-center gap-2">
                      <Link
                        className="font-mono text-[12.5px] text-accent hover:underline"
                        title="open this node in the graph view"
                        to={`/databases/${slug}/graph?focus=${encodeURIComponent(hit.id)}`}
                      >
                        {hit.id}
                      </Link>
                      <Badge>score {hit.score.toFixed(4)}</Badge>
                      {"distance" in hit ? <Badge tone="muted">d {(hit.distance as number)?.toFixed?.(4)}</Badge> : null}
                      {"lexical_score" in hit ? <Badge>lex {(hit.lexical_score as number)?.toFixed?.(3)}</Badge> : null}
                      {"vector_score" in hit ? <Badge>vec {(hit.vector_score as number)?.toFixed?.(3)}</Badge> : null}
                    </div>
                    {hit.fields && Object.keys(hit.fields).length ? (
                      <div className="mt-1 space-y-0.5">
                        {Object.entries(hit.fields).slice(0, 4).map(([key, value]) => (
                          <div className="truncate text-[12px] text-cell" key={key}>
                            <span className="text-muted">{key}: </span>
                            {formatCell(value as never)}
                          </div>
                        ))}
                      </div>
                    ) : null}
                  </li>
                ))}
              </ol>
              {hits.length === 0 ? <Empty>No hit. A word that is not in the corpus is a legitimate empty answer.</Empty> : null}
              <details className="mt-2">
                <summary className="cursor-pointer text-[11.5px] text-muted">raw payload</summary>
                <div className="mt-1">
                  <JsonBlock value={search.data} />
                </div>
              </details>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}

/**
 * Whether the engine answered from an exact structure rather than an approximation.
 *
 * The name of the regime depends on the index family — `complete` for hybrid sources, `exact_index`
 * for the text index, `exact` for a vector scan — and all three mean the same promise to the reader:
 * nothing was skipped. Only an approximate regime deserves a warning colour.
 */
function isExactRegime(regime: string | undefined): boolean {
  return regime === "complete" || regime === "exact" || regime === "exact_index";
}
