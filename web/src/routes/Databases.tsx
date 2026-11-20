import { useMemo, useState } from "react";
import { Eject, Link2, Plus, Power, ScanSearch } from "lucide-react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { admin, ApiError, type DatabaseRecord, type InspectResult } from "@/api/client";
import { ConfigForm, toPayload, validateOptions, type OptionValues } from "@/components/ConfigForm";
import { Badge, ConfirmButton, Empty, ErrorBanner, Field, Section, Spinner } from "@/components/ui";

/**
 * A store is "degraded" when indexes are stale or recovery is needed; that comes from the store's
 * own inventory rather than from a guess, because a closed handle cannot say anything about data.
 */
export function stateOf(record: DatabaseRecord, open: boolean, health?: { stale: string[]; recovery: boolean }): { label: string; tone: "ok" | "warn" | "danger" | "muted" } {
  if (!record.enabled) return { label: "disabled", tone: "muted" };
  if (health?.recovery) return { label: "recovery required", tone: "danger" };
  if (health?.stale.length) return { label: "degraded", tone: "warn" };
  if (open) return { label: "open", tone: "ok" };
  return { label: "closed", tone: "muted" };
}

export function Databases() {
  const queryClient = useQueryClient();
  const databases = useQuery({ queryKey: ["databases"], queryFn: admin.databases });
  const status = useQuery({ queryKey: ["status"], queryFn: admin.status, refetchInterval: 15_000 });
  const [adding, setAdding] = useState(false);
  // A payload missing a field must not take the page down: the list renders as empty instead.
  const listed = databases.data?.databases ?? [];

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["databases"] });
    queryClient.invalidateQueries({ queryKey: ["status"] });
  };

  return (
    <>
      <Section
        actions={
          <button className="btn btn-primary" onClick={() => setAdding((value) => !value)} type="button">
            {!adding ? <Plus size={14} strokeWidth={2} aria-hidden /> : null}
            {adding ? "cancel" : "Add existing store"}
          </button>
        }
        title="registered stores"
      >
        {databases.isLoading ? <Spinner /> : null}
        {databases.error ? <ErrorBanner error={databases.error} onRetry={() => databases.refetch()} /> : null}
        {listed.length === 0 ? (
          <Empty>
            No store is registered. Every Grafx store must live inside a configured{" "}
            <code>allowed_roots</code> directory — the server opens stores by reference and never copies them.
          </Empty>
        ) : null}
        {listed.length > 0 ? (
          <div className="scroll-thin overflow-auto">
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  <th className="th">id</th>
                  <th className="th">name</th>
                  <th className="th">state</th>
                  <th className="th">physical uuid</th>
                  <th className="th">door</th>
                  <th className="th">path</th>
                  <th className="th">actions</th>
                </tr>
              </thead>
              <tbody>
                {listed.map((record) => (
                  <DatabaseRow
                    key={record.slug}
                    onInvalidate={invalidate}
                    open={status.data?.open_slugs.includes(record.slug) ?? false}
                    record={record}
                  />
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </Section>
      {adding ? <AttachDialog onDone={() => { setAdding(false); invalidate(); }} /> : null}
    </>
  );
}

function DatabaseRow({
  record,
  open,
  onInvalidate,
}: {
  record: DatabaseRecord;
  open: boolean;
  onInvalidate: () => void;
}) {
  const health = useQuery({
    queryKey: ["inventory", record.slug],
    queryFn: () => admin.inventory(record.slug),
    // A closed store cannot answer; asking would just produce a refusal on every render.
    enabled: open,
    staleTime: 30_000,
    retry: false,
  });
  const openIt = useMutation({ mutationFn: () => admin.openDatabase(record.slug), onSuccess: onInvalidate });
  const eject = useMutation({ mutationFn: () => admin.ejectDatabase(record.slug), onSuccess: onInvalidate });
  const close = useMutation({ mutationFn: () => admin.closeDatabase(record.slug, true), onSuccess: onInvalidate });
  const remove = useMutation({ mutationFn: () => admin.removeDatabase(record.slug), onSuccess: onInvalidate });

  const state = stateOf(record, open, {
    stale: (health.data?.stale as string[] | undefined) ?? [],
    recovery: Boolean((health.data?.maintenance as { recovery_required?: boolean } | undefined)?.recovery_required),
  });

  return (
    <tr className="hover:bg-raised">
      <td className="td">
        <Link className="text-accent hover:underline" to={`/databases/${record.slug}`}>
          {record.slug}
        </Link>
      </td>
      <td className="td">{record.name}</td>
      <td className="td">
        <Badge tone={state.tone}>{state.label}</Badge>
        {record.open_policy === "pinned" ? <Badge> pinned</Badge> : null}
      </td>
      <td className="td text-muted">{record.database_uuid}</td>
      <td className="td">
        {record.read_only ? <Badge tone="warn">read-only</Badge> : record.allow_writes ? <Badge tone="ok">writable</Badge> : <Badge>writes off</Badge>}
      </td>
      <td className="td break-all text-muted">{record.path}</td>
      <td className="td whitespace-nowrap">
        {!open ? (
          <button className="btn" disabled={openIt.isPending} onClick={() => openIt.mutate()} type="button">
            <Power size={14} strokeWidth={2} aria-hidden />
            open
          </button>
        ) : (
          <span className="inline-flex gap-1">
            <button className="btn" disabled={eject.isPending} onClick={() => eject.mutate()} type="button">
              <Eject size={14} strokeWidth={2} aria-hidden />
              eject
            </button>
            <ConfirmButton
              confirmLabel="really close"
              detail="Closing releases the server's handles; live sessions are retired and their transactions rolled back."
              disabled={close.isPending}
              label="close"
              onConfirm={() => close.mutate()}
            />
          </span>
        )}{" "}
        <ConfirmButton
          confirmLabel="remove registration"
          danger
          detail="Removes the registration only. The store directory is never deleted."
          disabled={remove.isPending}
          label="remove"
          onConfirm={() => remove.mutate()}
        />
        {remove.data ? <span className="ml-2 text-[11px] text-muted">directory untouched</span> : null}
      </td>
    </tr>
  );
}

/** Browse the allowed roots, inspect a candidate, then attach with a form generated from the daemon. */
export function AttachDialog({ onDone }: { onDone: () => void }) {
  const navigate = useNavigate();
  const roots = useQuery({ queryKey: ["roots"], queryFn: admin.roots });
  const settings = useQuery({ queryKey: ["settings"], queryFn: admin.settings });
  const [path, setPath] = useState("");
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [readOnly, setReadOnly] = useState(true);
  const [allowWrites, setAllowWrites] = useState(false);
  const [pinned, setPinned] = useState(false);
  const [options, setOptions] = useState<OptionValues>({});

  const inspect = useMutation({
    mutationFn: (candidate: string) => admin.inspect(candidate),
    onSuccess: (result: InspectResult, candidate) => {
      // The slug is what every URL will use, so it is offered from the directory's own name and
      // stays editable: the daemon deliberately does not choose identifiers for the operator.
      const suggested = suggestedSlug(candidate);
      if (!slug) setSlug(suggested);
      if (!name) setName(suggested);
      if (result.already_registered_as) {
        setSlug(`__${suggested.slice(0, 60)}`);
      }
    },
  });
  const inspected = inspect.data as InspectResult | undefined;

  const problems = useMemo(
    () => validateOptions(settings.data?.database_options ?? [], options),
    [settings.data, options],
  );

  const attach = useMutation({
    mutationFn: () =>
      admin.attach({
        path,
        slug,
        name: name || slug,
        read_only: readOnly,
        allow_writes: allowWrites,
        open_policy: pinned ? "pinned" : "lazy",
        options: toPayload(options),
      }),
    onSuccess: (record) => {
      onDone();
      navigate(`/databases/${record.slug}`);
    },
  });

  const error = (inspect.error ?? attach.error) as ApiError | null;
  const blocked = Object.keys(problems).length > 0;

  return (
    <Section title="add an existing store by reference">
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
        <div className="space-y-3">
          <Field hint="Absolute paths only, inside a root the daemon was configured with. Symlinks and ~ are refused." label="store directory">
            <div className="flex gap-2">
              <input
                className="input font-mono"
                onChange={(event) => {
                  setPath(event.target.value.trim());
                  inspect.reset();
                }}
                placeholder="/srv/grafx/stores/events"
                value={path}
              />
              <button className="btn" disabled={!path || inspect.isPending} onClick={() => inspect.mutate(path)} type="button">
                <ScanSearch size={14} strokeWidth={2} aria-hidden />
                {inspect.isPending ? "…" : "inspect"}
              </button>
            </div>
          </Field>

          {roots.data ? (
            <div className="space-y-2">
              {(roots.data.roots ?? []).map((root) => (
                <div className="rounded-md border border-line p-2" key={root.path}>
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-[11.5px] text-cell">{root.path}</span>
                    {!root.exists ? <Badge tone="warn">missing</Badge> : null}
                  </div>
                  {(root.stores ?? []).length ? (
                    <div className="mt-1.5 flex flex-wrap gap-1">
                      {(root.stores ?? []).map((store) => (
                        <button
                          className="btn"
                          key={store.path}
                          onClick={() => {
                            setPath(store.path);
                            inspect.mutate(store.path);
                          }}
                          title={store.path}
                          type="button"
                        >
                          {store.name}
                        </button>
                      ))}
                    </div>
                  ) : (
                    <p className="mt-1 text-[11px] text-muted">
                      {root.exists ? "no store directories here" : "root is missing from this machine"}
                    </p>
                  )}
                </div>
              ))}
              {roots.data.note ? <p className="text-[11px] text-muted">{roots.data.note}</p> : null}
            </div>
          ) : null}

          {inspected ? (
            <div className="rounded-md border border-line bg-deep p-2 text-[12px]">
              <ul className="space-y-0.5 font-mono text-[11.5px] text-cell">
                <li>uuid {inspected.database_uuid}</li>
                <li>
                  format v{inspected.format_version} · page {inspected.page_size} · {inspected.files} files /{" "}
                  {Math.round(inspected.bytes / 1024)} KiB
                </li>
                <li>{(inspected.node_tables ?? []).length} node · {(inspected.relationship_tables ?? []).length} relationship tables</li>
                {(inspected.vector_spaces ?? []).length ? (
                  <li>vector spaces: {inspected.vector_spaces?.join(", ")}</li>
                ) : null}
                {inspected.recovery_required ? <li className="text-warn">recovery required</li> : null}
                {inspected.already_registered_as ? (
                  <li className="text-warn">already registered as {inspected.already_registered_as}</li>
                ) : null}
                {inspected.uuid_registered_elsewhere ? (
                  <li className="text-warn">
                    this uuid is already registered as {inspected.uuid_registered_elsewhere}: the same store
                    under two slugs would be two doors onto one database
                  </li>
                ) : null}
              </ul>
            </div>
          ) : null}

          {error ? <ErrorBanner error={error} /> : null}
        </div>

        <div className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field hint="1–64 characters of a-z, 0-9, _ or -. This is the id used in every URL." label="database id (slug)">
              <input className="input font-mono" onChange={(event) => setSlug(event.target.value.trim())} value={slug} />
            </Field>
            <Field label="display name">
              <input className="input" onChange={(event) => setName(event.target.value)} value={name} />
            </Field>
          </div>
          <div className="grid gap-3 sm:grid-cols-3">
            <Field hint="A read-only registration can only open a store whose WAL is already checkpoint-complete." label="read_only">
              <select className="input" onChange={(event) => {
                const value = event.target.value === "true";
                setReadOnly(value);
                if (value) setAllowWrites(false);
              }} value={String(readOnly)}>
                <option value="true">true — the read door</option>
                <option value="false">false</option>
              </select>
            </Field>
            <Field hint="Writes additionally need a key with allow_writes and a WebSocket session in mode=write." label="allow_writes">
              <select className="input" disabled={readOnly} onChange={(event) => setAllowWrites(event.target.value === "true")} value={String(allowWrites)}>
                <option value="false">false</option>
                <option value="true">true</option>
              </select>
            </Field>
            <Field hint="Pinned stores are opened when the daemon starts, so the first query is not slow." label="open policy">
              <select className="input" onChange={(event) => setPinned(event.target.value === "pinned")} value={pinned ? "pinned" : "lazy"}>
                <option value="lazy">lazy — open on demand</option>
                <option value="pinned">pinned — open at startup</option>
              </select>
            </Field>
          </div>

          {settings.data ? (
            <ConfigForm
              onChange={(name2, value) => setOptions((current) => ({ ...current, [name2]: value }))}
              options={settings.data.database_options}
              problems={problems}
              values={options}
            />
          ) : (
            <Spinner label="loading the DatabaseConfig schema…" />
          )}

          <div className="flex items-center gap-2">
            <button
              className="btn btn-primary"
              disabled={!path || !slug || blocked || !inspected || attach.isPending}
              onClick={() => attach.mutate()}
              type="button"
            >
              <Link2 size={14} strokeWidth={2} aria-hidden />
              {attach.isPending ? "attaching…" : "attach store"}
            </button>
            {blocked ? <span className="text-[12px] text-warn">fix the highlighted option bounds first</span> : null}
            {!inspected ? <span className="text-[12px] text-muted">inspect the directory first</span> : null}
            {inspected?.already_registered_as ? (
              <span className="text-[12px] text-warn">that path is registered already</span>
            ) : null}
          </div>
        </div>
      </div>
    </Section>
  );
}

/**
 * The slug is the identifier every URL will carry, so it is derived from the store directory's own
 * name and then left to the operator: `[a-z0-9_-]`, at most 64 characters, never a server guess.
 */
export function suggestedSlug(path: string): string {
  const leaf = path.replace(/\/+$/, "").split("/").pop() ?? "store";
  const cleaned = leaf
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
  return cleaned || "store";
}
