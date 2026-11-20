import { useMemo, useState } from "react";
import { KeyRound, Network, Save, Search as SearchIcon, TerminalSquare } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  admin,
  jobTime,
  type ColumnInfo,
  type DatabaseRecord,
  type Job,
  type TableInfo,
} from "@/api/client";
import { Badge, ConfirmButton, Empty, ErrorBanner, Field, JsonBlock, Section, Spinner, Tabs } from "@/components/ui";
import { formatTime, relativeTime } from "@/lib/format";

type Tab = "schema" | "maintenance" | "indexes" | "keys" | "overview";

const TABS = [
  { id: "overview" as const, label: "Overview" },
  { id: "schema" as const, label: "Schema" },
  { id: "maintenance" as const, label: "Maintenance" },
  { id: "indexes" as const, label: "Indexes & DDL" },
  { id: "keys" as const, label: "Keys" },
];

export function DatabaseDetail() {
  const { slug = "" } = useParams();
  const [tab, setTab] = useState<Tab>("overview");

  const record = useQuery({ queryKey: ["database", slug], queryFn: () => admin.database(slug) });
  const inventory = useQuery({
    queryKey: ["inventory", slug],
    queryFn: () => admin.inventory(slug),
    retry: false,
  });

  return (
    <div className="space-y-3">
      <header className="flex flex-wrap items-center gap-3">
        <Link className="btn" to="/databases" aria-label="back to databases">
          ← stores
        </Link>
        <h1 className="text-[16px] font-semibold tracking-tight">{slug}</h1>
        {record.data ? (
          <>
            {record.data.read_only ? <Badge tone="warn">read-only</Badge> : record.data.allow_writes ? <Badge tone="ok">writable</Badge> : <Badge>writes off</Badge>}
            <span className="font-mono text-[11.5px] text-muted">{record.data.database_uuid}</span>
          </>
        ) : null}
        <nav className="ml-auto flex gap-2">
          <Link className="btn" to={`/databases/${slug}/query`}>
            <TerminalSquare size={14} strokeWidth={2} aria-hidden />
            Query console
          </Link>
          <Link className="btn" to={`/databases/${slug}/search`}>
            <SearchIcon size={14} strokeWidth={2} aria-hidden />
            Search
          </Link>
          <Link className="btn" to={`/databases/${slug}/graph`}>
            <Network size={14} strokeWidth={2} aria-hidden />
            Graph
          </Link>
        </nav>
      </header>

      <Tabs tabs={TABS} active={tab} onChange={setTab} />

      <div className="card p-3">
        {inventory.error ? (
          <ErrorBanner
            error={
              // The read door's precondition is the commonest reason for this refusal, and the
              // operator needs the remedy, not just the code.
              inventory.error
            }
          />
        ) : null}
        {inventory.isLoading ? <Spinner label="opening the store…" /> : null}
        {inventory.data && record.data ? (
          tab === "overview" ? (
            <Overview slug={slug} />
          ) : tab === "schema" ? (
            <SchemaTab slug={slug} />
          ) : tab === "maintenance" ? (
            <MaintenanceTab slug={slug} />
          ) : tab === "indexes" ? (
            <IndexesTab slug={slug} />
          ) : (
            <KeysTab slug={slug} />
          )
        ) : null}
      </div>

      {record.data ? <RegistrationPanel record={record.data} /> : null}
    </div>
  );
}

/**
 * The registration itself: the doors the daemon opens this store behind.
 *
 * These are the settings an operator has to change after the first look — a freshly created store
 * cannot be opened read-only until recovery and checkpoint have run once, and nothing can be written
 * until the store's own toggle is on. Both are answered here rather than in a text editor, and a
 * change that opens a door asks to be confirmed, because it changes what the server is willing to do.
 */
function RegistrationPanel({ record }: { record: DatabaseRecord }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState({
    read_only: record.read_only,
    allow_writes: record.allow_writes,
    enabled: record.enabled,
    open_policy: record.open_policy,
    note: record.note,
  });
  const save = useMutation({
    mutationFn: () => admin.patchDatabase(record.slug, { ...draft }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["database", record.slug] });
      queryClient.invalidateQueries({ queryKey: ["databases"] });
      queryClient.invalidateQueries({ queryKey: ["inventory", record.slug] });
      queryClient.invalidateQueries({ queryKey: ["status"] });
    },
  });

  const changed = JSON.stringify(draft) !== JSON.stringify({
    read_only: record.read_only,
    allow_writes: record.allow_writes,
    enabled: record.enabled,
    open_policy: record.open_policy,
    note: record.note,
  });
  const opensWrites = draft.allow_writes && !record.allow_writes;
  const closesReadDoor = !draft.read_only && record.read_only;
  const warnsAboutReadDoor = draft.read_only && !record.read_only;

  return (
    <Section title="registration">
      <div className="grid gap-3 lg:grid-cols-[repeat(4,minmax(0,1fr))_minmax(0,1.4fr)]">
        <Field hint="A read-only open can only prove a checkpoint-complete database. Clear it once on a new store, run recover and checkpoint, then put it back." label="read_only">
          <select
            className="input"
            onChange={(event) => {
              const value = event.target.value === "true";
              setDraft((current) => ({
                ...current,
                read_only: value,
                allow_writes: value ? false : current.allow_writes,
              }));
            }}
            value={String(draft.read_only)}
          >
            <option value="true">read_only: true — the read door</option>
            <option value="false">read_only: false — the server may open it writable</option>
          </select>
        </Field>
        <Field hint="Writes also need a key with allow_writes and a WebSocket session in mode=write." label="allow_writes">
          <select
            className="input"
            disabled={draft.read_only}
            onChange={(event) => setDraft((current) => ({ ...current, allow_writes: event.target.value === "true" }))}
            value={String(draft.allow_writes)}
          >
            <option value="false">allow_writes: false — writes refused</option>
            <option value="true">allow_writes: true — writes permitted</option>
          </select>
        </Field>
        <Field hint="A disabled store answers 403 everywhere, registration and keys intact." label="enabled">
          <select
            className="input"
            onChange={(event) => setDraft((current) => ({ ...current, enabled: event.target.value === "true" }))}
            value={String(draft.enabled)}
          >
            <option value="true">enabled: true</option>
            <option value="false">enabled: false</option>
          </select>
        </Field>
        <Field hint="Pinned stores are opened when the daemon starts; lazy ones open on the first request and go idle." label="open policy">
          <select
            className="input"
            onChange={(event) =>
              setDraft((current) => ({ ...current, open_policy: event.target.value as DatabaseRecord["open_policy"] }))
            }
            value={draft.open_policy}
          >
            <option value="lazy">open policy: lazy</option>
            <option value="pinned">open policy: pinned</option>
          </select>
        </Field>
        <Field hint="Says what this store is for; shown in the store list." label="note">
          <input
            className="input"
            onChange={(event) => setDraft((current) => ({ ...current, note: event.target.value }))}
            value={draft.note}
          />
        </Field>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        {opensWrites || closesReadDoor ? (
          <ConfirmButton
            confirmLabel={opensWrites ? "allow writes" : "open the write path"}
            detail={
              opensWrites
                ? "From this moment a key with allow_writes can change this store through a write session."
                : "A writable registration can checkpoint and reclaim space; the read-only guarantee ends until you put it back."
            }
            disabled={!changed || save.isPending}
            label="save registration"
            onConfirm={() => save.mutate()}
          />
        ) : (
          <button className="btn" disabled={!changed || save.isPending} onClick={() => save.mutate()} type="button">
            <Save size={14} strokeWidth={2} aria-hidden />
            save registration
          </button>
        )}
        {!changed ? <span className="text-[12px] text-muted">nothing changed</span> : null}
        {warnsAboutReadDoor ? (
          <span className="text-[12px] text-warn">
            the next request opens this store read-only, and will be refused until the WAL is checkpoint-complete
          </span>
        ) : null}
        {save.data ? <Badge tone="ok">saved</Badge> : null}
      </div>
      {save.error ? <ErrorBanner error={save.error} /> : null}
      <p className="mt-2 text-[11.5px] text-muted">
        Changing read_only closes the live handles: the next request reopens the store with the new settings, and
        running sessions are retired rather than silently reconfigured.
      </p>
    </Section>
  );
}

function Overview({ slug }: { slug: string }) {
  const inventory = useQuery({ queryKey: ["inventory", slug], queryFn: () => admin.inventory(slug), retry: false });
  const status = useQuery({ queryKey: ["status"], queryFn: admin.status });
  const handle = status.data?.handles.find((entry) => entry.slug === slug);
  // A closed store answers with an envelope that carries no schema; both cases mean the same thing
  // to the operator, and neither is a reason for this page to fall over.
  if (!inventory.data?.schema) {
    return <Empty>Store is closed. Open it to see its schema.</Empty>;
  }
  const schema = inventory.data.schema;
  const maintenance = inventory.data.maintenance;
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <dl className="space-y-1 text-[12.5px]">
        <Row label="tables" value={`${schema.tables?.length ?? 0} node · ${schema.relationships?.length ?? 0} relationship`} />
        <Row label="vector spaces" value={String(schema.vector_spaces?.length ?? 0)} />
        <Row label="indexes" value={String(inventory.data.indexes?.length ?? 0)} />
        <Row label="stale indexes" value={(inventory.data.stale ?? []).length ? (inventory.data.stale ?? []).join(", ") : "none"} />
        <Row label="physical uuid" value={String(schema.database_uuid ?? "—")} />
        <Row label="wal bytes" value={String(maintenance?.wal_bytes ?? "—")} />
        <Row label="checkpoint lag" value={String(maintenance?.checkpoint_lag_lsn ?? "—")} />
        <Row label="recovery required" value={String(maintenance?.recovery_required ?? "unknown")} />
      </dl>
      <div>
        <h3 className="label">server handles</h3>
        {handle ? (
          <dl className="space-y-1 text-[12.5px]">
            <Row label="readers" value={`${handle.readers} (idle ${handle.idle_readers})`} />
            <Row label="writer open" value={String(handle.writer_open)} />
            <Row label="active operations" value={String(handle.active_operations)} />
            <Row label="open transactions" value={String(handle.open_transactions)} />
            <Row label="idle" value={`${handle.idle_seconds.toFixed(1)}s`} />
            <Row label="path" value={handle.path} />
          </dl>
        ) : (
          <Empty>Closed: no handle is held. Any request opens it again.</Empty>
        )}
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4 border-b border-line pb-0.5">
      <dt className="text-muted">{label}</dt>
      <dd className="truncate font-mono text-[12px] text-cell">{value}</dd>
    </div>
  );
}

function SchemaTab({ slug }: { slug: string }) {
  const inventory = useQuery({ queryKey: ["inventory", slug], queryFn: () => admin.inventory(slug), retry: false });
  const schema = inventory.data?.schema;
  if (!schema) return <Empty>Store is closed.</Empty>;
  const indexes = inventory.data?.indexes ?? [];
  return (
    <div className="space-y-4">
      {[...(schema.tables ?? []), ...(schema.relationships ?? [])].map((table: TableInfo) => (
        <div className="rounded-md border border-line" key={`${table.kind}-${table.name}`}>
          <header className="flex items-center gap-2 border-b border-line px-2.5 py-1.5">
            <span className="font-mono text-[13px] text-fg">{table.name}</span>
            <Badge tone={table.kind === "NODE" ? "ok" : "muted"}>{table.kind === "NODE" ? "node" : "relationship"}</Badge>
            {table.from_table && table.to_table ? (
              <span className="font-mono text-[11.5px] text-muted">
                {table.from_table} → {table.to_table}
              </span>
            ) : null}
            {indexes
              .filter((index) => index.table === table.name)
              .map((index) => (
                <Badge key={index.name} tone={index.stale ? "warn" : "muted"}>
                  {index.kind}:{index.name}
                  {index.stale ? " stale" : ""}
                </Badge>
              ))}
          </header>
          <table className="w-full border-collapse">
            <tbody>
              {table.columns.map((column: ColumnInfo) => (
                <tr key={column.name}>
                  <td className="td w-56">{column.name}</td>
                  <td className="td w-40 text-azure">{column.type}</td>
                  <td className="td text-muted">{column.stored_type}</td>
                  <td className="td w-40">
                    {table.primary_key === column.name ? <Badge tone="ok">primary key</Badge> : null}
                    {column.vector_space ? <Badge tone="ok">vector: {column.vector_space}</Badge> : null}
                    {column.nullable ? <Badge>null</Badge> : <Badge tone="warn">not null</Badge>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}

      {(schema.vector_spaces?.length ?? 0) > 0 ? (
        <div className="rounded-md border border-line">
          <header className="border-b border-line px-2.5 py-1.5">
            <span className="text-[12px] font-semibold uppercase tracking-wide text-muted">vector spaces</span>
          </header>
          <table className="w-full border-collapse">
            <tbody>
              {schema.vector_spaces.map((space) => (
                <tr key={space.name}>
                  <td className="td w-56">{space.name}</td>
                  <td className="td w-24">{space.dimension}d</td>
                  <td className="td w-24">{space.metric}</td>
                  <td className="td w-24 text-muted">{space.storage_dtype}</td>
                  <td className="td w-24 text-muted">{space.state}</td>
                  <td className="td">
                    {space.normalized ? <Badge>normalised on write</Badge> : null}
                    {/* The columns that write this space are named by the table listing above: a vector
                        column carries the space it belongs to, so ownership is read from the schema
                        instead of being reconstructed here. */}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}

function MaintenanceTab({ slug }: { slug: string }) {
  const queryClient = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: admin.settings });
  const record = useQuery({ queryKey: ["database", slug], queryFn: () => admin.database(slug) });
  const jobs = useQuery({
    queryKey: ["jobs", slug],
    queryFn: () => admin.jobs(slug, 20),
    refetchInterval: 2_000,
  });
  const [arguments_, setArguments] = useState<Record<string, Record<string, string | boolean>>>({});

  const run = useMutation({
    mutationFn: ({ action, args }: { action: string; args: Record<string, unknown> }) => admin.action(slug, action, args),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["jobs", slug] });
      queryClient.invalidateQueries({ queryKey: ["status"] });
    },
  });

  const actions = settings.data?.maintenance_actions ?? [];
  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
      <div className="space-y-2">
        <p className="text-[12px] text-muted">
          Maintenance needs local quiescence: no session of this server may hold a transaction on the
          store. Actions that reclaim space also need you to confirm that <em>no other Grafx process</em>{" "}
          has the store open — the server cannot see those.
        </p>
        {actions.map((action) => {
          const args = arguments_[action.action] ?? {};
          const needsWritable = !action.read_only_ok && record.data?.read_only;
          const missing = action.requires.filter((name) => !args[name]);
          const needsConfirm = action.confirm_quiescent && !args.confirm_quiescent;
          return (
            <div className="rounded-md border border-line p-2.5" data-action={action.action} key={action.action}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-[13px] text-fg">{action.action}</span>
                {action.quiescing ? <Badge tone="warn">quiescence</Badge> : null}
                {action.confirm_quiescent ? <Badge tone="danger">confirmation</Badge> : null}
                {needsWritable ? <Badge tone="danger">needs writable registration</Badge> : null}
              </div>
              <p className="mt-1 text-[12px] text-muted">{action.description}</p>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                {action.requires.map((name) => (
                  <input
                    className="input w-40"
                    key={name}
                    onChange={(event) =>
                      setArguments((current) => ({
                        ...current,
                        [action.action]: { ...args, [name]: event.target.value },
                      }))
                    }
                    placeholder={name}
                    value={String(args[name] ?? "")}
                  />
                ))}
                {action.confirm_quiescent ? (
                  <label className="flex items-center gap-1.5 text-[12px] text-muted">
                    <input
                      checked={Boolean(args.confirm_quiescent)}
                      onChange={(event) =>
                        setArguments((current) => ({
                          ...current,
                          [action.action]: { ...args, confirm_quiescent: event.target.checked },
                        }))
                      }
                      type="checkbox"
                    />
                    no other process has this store open
                  </label>
                ) : null}
                <ConfirmButton
                  confirmLabel={`run ${action.action}`}
                  detail={
                    <>
                      action {action.action} on {slug}
                      {action.confirm_quiescent ? " · quiescence confirmed by you" : ""}
                    </>
                  }
                  disabled={Boolean(needsWritable) || missing.length > 0 || needsConfirm}
                  label="run"
                  onConfirm={() =>
                    run.mutate({
                      action: action.action,
                      args: Object.fromEntries(
                        Object.entries(args).filter(([, value]) => value !== "" && value !== false),
                      ),
                    })
                  }
                />
              </div>
            </div>
          );
        })}
        {run.error ? <ErrorBanner error={run.error} /> : null}
      </div>

      <div>
        <h3 className="label">job history</h3>
        {jobs.isLoading ? <Spinner /> : null}
        <div className="space-y-2">
          {(jobs.data?.jobs ?? []).map((job: Job) => (
            <div className="rounded-md border border-line p-2" key={job.id}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-[12.5px] text-fg">{job.kind}</span>
                <Badge tone={job.state === "succeeded" ? "ok" : job.state === "failed" ? "danger" : "warn"}>{job.state}</Badge>
                <span className="ml-auto text-[11px] text-muted">{relativeTime(jobTime(job))}</span>
              </div>
              {job.state === "failed" ? (
                <p className="mt-1 text-[12px] text-danger">
                  {String(job.detail?.code ?? "failed")} — {String(job.detail?.message ?? "")}
                </p>
              ) : null}
              {job.state === "succeeded" ? (
                <details className="mt-1">
                  <summary className="cursor-pointer text-[11.5px] text-muted">report</summary>
                  <div className="mt-1">
                    <JsonBlock maxHeight={260} value={job.detail?.report ?? job.detail} />
                  </div>
                </details>
              ) : null}
            </div>
          ))}
          {(jobs.data?.jobs ?? []).length === 0 ? <Empty>No jobs yet.</Empty> : null}
        </div>
      </div>
    </div>
  );
}

function IndexesTab({ slug }: { slug: string }) {
  const queryClient = useQueryClient();
  const inventory = useQuery({ queryKey: ["inventory", slug], queryFn: () => admin.inventory(slug), retry: false });
  const [table, setTable] = useState("");
  const [column, setColumn] = useState("");
  const [space, setSpace] = useState("");
  const [dimension, setDimension] = useState("384");
  const [vectorColumn, setVectorColumn] = useState("");
  const [last, setLast] = useState<string[]>([]);

  const schema = inventory.data?.schema;
  const nodeTables = schema?.tables ?? [];
  const statements = useMemo(() => {
    if (table && column) return [`CREATE INDEX idx_${table.toLowerCase()}_${column.toLowerCase()} ON \`${table}\`(${column})`];
    return [];
  }, [table, column]);

  const preview = useMemo(() => {
    const out: string[] = [...statements];
    if (space && vectorColumn && dimension) {
      out.push(`CREATE VECTOR SPACE ${space} {dimension: ${dimension}, metric: 'cosine'}`);
      out.push(
        `MATCH (n:\`${table || "<table>"}\`) SET n.${vectorColumn} = vector_of(n.${column || "<source column>"})`,
      );
    }
    return out;
  }, [statements, space, vectorColumn, dimension, table, column]);

  const run = useMutation({
    mutationFn: (items: string[]) => admin.ddl(slug, items),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["inventory", slug] });
      queryClient.invalidateQueries({ queryKey: ["schema", slug] });
    },
  });
  const identity = useMutation({
    mutationFn: () => admin.ensureIdentityIndexes(slug),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["inventory", slug] });
      queryClient.invalidateQueries({ queryKey: ["schema", slug] });
    },
  });

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <div className="space-y-3">
        <div className="rounded-md border border-line p-2.5">
          <h3 className="label">exact index (text and equality lookups)</h3>
          <div className="grid gap-2 sm:grid-cols-2">
            <Field label="table">
              <select className="input" onChange={(event) => setTable(event.target.value)} value={table}>
                <option value="">select…</option>
                {nodeTables.map((item) => (
                  <option key={item.name} value={item.name}>
                    {item.name}
                  </option>
                ))}
              </select>
            </Field>
            <Field hint="Hash indexes answer exact equality; full-text search needs a text index." label="column">
              <input className="input font-mono" onChange={(event) => setColumn(event.target.value.trim())} value={column} />
            </Field>
          </div>
        </div>

        <div className="rounded-md border border-line p-2.5">
          <h3 className="label">vector space</h3>
          <div className="grid gap-2 sm:grid-cols-2">
            <Field label="space name">
              <input className="input font-mono" onChange={(event) => setSpace(event.target.value.trim())} value={space} />
            </Field>
            <Field label="dimension">
              <input className="input" onChange={(event) => setDimension(event.target.value)} value={dimension} />
            </Field>
            <Field label="node table">
              <input className="input font-mono" onChange={(event) => setTable(event.target.value.trim())} value={table} />
            </Field>
            <Field label="vector column">
              <input className="input font-mono" onChange={(event) => setVectorColumn(event.target.value.trim())} value={vectorColumn} />
            </Field>
          </div>
        </div>

        <div>
          <h3 className="label">exact DDL that will run</h3>
          <pre className="scroll-thin overflow-auto rounded-md border border-line bg-deep p-2 font-mono text-[12px] text-cell">
            {preview.length ? preview.join("\n") : "-- fill the assistants above"}
          </pre>
          <div className="mt-2">
            <ConfirmButton
              confirmLabel="execute DDL"
              detail={preview.join(" · ")}
              disabled={preview.length === 0}
              label="execute"
              onConfirm={() => {
                setLast(preview);
                run.mutate(preview);
              }}
            />
            {run.error ? <ErrorBanner error={run.error} /> : null}
            {run.data ? (
              <p className="mt-2 text-[12px] text-muted">
                executed {run.data.statements?.length ?? 0} statement(s)
                {run.data.statements[0]?.statistics
                  ? ` · ${JSON.stringify(run.data.statements[0].statistics)}`
                  : ""}
              </p>
            ) : null}
            {last.length ? <p className="mt-1 text-[11px] text-muted">nothing runs twice by accident: press execute again to repeat.</p> : null}
          </div>
        </div>

        <div className="rounded-md border border-line p-2.5" data-action="ensure_identity_indexes">
          <h3 className="label">identity access paths</h3>
          <p className="text-[12px] text-muted">
            <code>ensure_identity_indexes</code> is the library&apos;s one-way catalog door: it publishes the exact
            access paths its endpoint identities need. It is refused while the store is registered read-only, and
            once the generations are active it writes nothing — pressing it twice is safe, and the answer says
            which indexes appeared.
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <ConfirmButton
              confirmLabel="activate identity indexes"
              detail="Publishes the library's identity access paths. One-way: the catalog door does not close again."
              label="activate"
              onConfirm={() => identity.mutate()}
            />
            {identity.data ? (
              <span className="text-[12px] text-muted">
                {identity.data.activated.length
                  ? `${identity.data.activated.length} index(es) appeared: ${identity.data.activated.slice(0, 4).join(", ")}`
                  : `already active (${identity.data.already_present.length} present) — no durable write`}
              </span>
            ) : null}
          </div>
          {identity.error ? <ErrorBanner error={identity.error} /> : null}
        </div>
      </div>

      <div>
        <h3 className="label">indexes</h3>
        <table className="w-full border-collapse">
          <thead>
            <tr>
              <th className="th">name</th>
              <th className="th">table</th>
              <th className="th">columns</th>
              <th className="th">kind</th>
              <th className="th">visibility</th>
            </tr>
          </thead>
          <tbody>
            {(inventory.data?.indexes ?? []).map((index) => (
              <tr key={`${index.table}.${index.name}`}>
                <td className="td">{index.name}</td>
                <td className="td">{index.table}</td>
                <td className="td">{index.columns.join(", ")}</td>
                <td className="td">{index.kind}</td>
                <td className="td">
                  <Badge tone={index.stale ? "warn" : "ok"}>{index.stale ? index.stale_reason ?? "stale" : index.visibility}</Badge>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {(inventory.data?.stale?.length ?? 0) > 0 ? (
          <p className="mt-2 text-[12px] text-warn">
            stale: {inventory.data?.stale?.join(", ")} — rebuild from the Maintenance tab once the store is quiesced.
          </p>
        ) : null}
      </div>
    </div>
  );
}

function KeysTab({ slug }: { slug: string }) {
  const keys = useQuery({ queryKey: ["keys"], queryFn: admin.keys });
  const scoped = (keys.data?.keys ?? []).filter((key) => key.databases.includes(slug));
  return (
    <div className="space-y-2">
      <p className="text-[12px] text-muted">
        These are the credentials that can reach <code>{slug}</code>. A key with{" "}
        <code>allow_writes</code> still needs the store's own write toggle.
      </p>
      <table className="w-full border-collapse">
        <thead>
          <tr>
            <th className="th">name</th>
            <th className="th">prefix</th>
            <th className="th">writes</th>
            <th className="th">status</th>
            <th className="th">last used</th>
          </tr>
        </thead>
        <tbody>
          {scoped.map((key) => (
            <tr key={key.id}>
              <td className="td">{key.name}</td>
              <td className="td">{key.prefix}</td>
              <td className="td">{key.allow_writes ? <Badge tone="warn">allowed</Badge> : <Badge>read-only</Badge>}</td>
              <td className="td">
                {key.revoked_at ? <Badge tone="danger">revoked</Badge> : key.enabled ? <Badge tone="ok">enabled</Badge> : <Badge>disabled</Badge>}
              </td>
              <td className="td text-muted">
                {relativeTime(key.last_used_at)}
                {key.last_used_at ? ` (${formatTime(key.last_used_at)})` : ""}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {scoped.length === 0 ? <Empty>No key sees this store yet.</Empty> : null}
      <div className="pt-2">
        <Link className="btn" to="/keys">
          <KeyRound size={14} strokeWidth={2} aria-hidden />
          manage keys
        </Link>{" "}
        <Link className="btn" to={`/databases/${slug}/query`}>
          query this store
        </Link>
      </div>
    </div>
  );
}
