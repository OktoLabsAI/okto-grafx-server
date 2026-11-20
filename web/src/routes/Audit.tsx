import { Fragment, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { admin } from "@/api/client";
import { Badge, Empty, ErrorBanner, Field, JsonBlock, Section, Spinner } from "@/components/ui";
import { formatTime } from "@/lib/format";

export function Audit() {
  const [actor, setActor] = useState("");
  const [database, setDatabase] = useState("");
  const [action, setAction] = useState("");
  const [open, setOpen] = useState<number | null>(null);

  const entries = useQuery({
    queryKey: ["audit", actor, database, action],
    queryFn: () => admin.audit({ actor, database, action, limit: 200 }),
    refetchInterval: 20_000,
  });

  return (
    <Section title="audit log">
      <div className="mb-3 grid gap-3 sm:grid-cols-3">
        <Field hint="admin, or key:<fingerprint>" label="actor">
          <input className="input font-mono" onChange={(event) => setActor(event.target.value.trim())} placeholder="key:78dee893" value={actor} />
        </Field>
        <Field label="database">
          <input className="input font-mono" onChange={(event) => setDatabase(event.target.value.trim())} value={database} />
        </Field>
        <Field hint="e.g. query, ddl, maintenance.verify, database.attach, mcp.write" label="action">
          <input className="input font-mono" onChange={(event) => setAction(event.target.value.trim())} value={action} />
        </Field>
      </div>

      {entries.isLoading ? <Spinner /> : null}
      {entries.error ? <ErrorBanner error={entries.error} onRetry={() => entries.refetch()} /> : null}

      <table className="w-full border-collapse">
        <thead>
          <tr>
            <th className="th">when</th>
            <th className="th">actor</th>
            <th className="th">action</th>
            <th className="th">database</th>
            <th className="th">outcome</th>
            <th className="th">rows</th>
            <th className="th">ms</th>
            <th className="th">params</th>
          </tr>
        </thead>
        <tbody>
          {(entries.data?.entries ?? []).map((entry) => (
            <Fragment key={entry.id}>
              <tr className="hover:bg-raised">
                <td className="td whitespace-nowrap text-muted">{formatTime(entry.at)}</td>
                <td className="td">{entry.actor}</td>
                <td className="td">{entry.action}</td>
                <td className="td">{entry.database ?? "—"}</td>
                <td className="td">
                  <Badge tone={entry.outcome === "ok" ? "ok" : entry.outcome === "not_found" ? "muted" : "danger"}>
                    {entry.outcome}
                  </Badge>
                </td>
                <td className="td">{entry.rows ?? "—"}</td>
                <td className="td">{entry.duration_ms ?? "—"}</td>
                <td className="td">
                  <button className="hover:underline" onClick={() => setOpen(open === entry.id ? null : entry.id)} type="button">
                    {open === entry.id ? "hide" : "view"}
                  </button>
                </td>
              </tr>
              {open === entry.id ? (
                <tr>
                  <td className="td" colSpan={8}>
                    <JsonBlock maxHeight={260} value={entry.request} />
                    <p className="mt-1 text-[11px] text-muted">
                      Query text is kept; parameter values are not. The audit trail is metadata, not a copy of your data.
                    </p>
                  </td>
                </tr>
              ) : null}
            </Fragment>
          ))}
        </tbody>
      </table>
      {(entries.data?.entries ?? []).length === 0 ? <Empty>Nothing matches the filter.</Empty> : null}
    </Section>
  );
}
