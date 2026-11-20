import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { admin, jobTime } from "@/api/client";
import { Badge, Empty, ErrorBanner, Field, JsonBlock, Section, Spinner } from "@/components/ui";
import { formatTime, relativeTime } from "@/lib/format";

export function Jobs() {
  const [database, setDatabase] = useState("");
  const databases = useQuery({ queryKey: ["databases"], queryFn: admin.databases });
  const jobs = useQuery({
    queryKey: ["jobs", "all", database],
    queryFn: () => admin.jobs(database, 100),
    refetchInterval: 3_000,
  });

  return (
    <Section
      actions={
        <div className="w-56">
          <Field label="">
            <select className="input" onChange={(event) => setDatabase(event.target.value)} value={database}>
              <option value="">all databases</option>
              {(databases.data?.databases ?? []).map((record) => (
                <option key={record.slug} value={record.slug}>
                  {record.slug}
                </option>
              ))}
            </select>
          </Field>
        </div>
      }
      title="maintenance jobs"
    >
      <p className="mb-2 text-[12px] text-muted">
        One job per store at a time; a second request is refused with <code>job_active</code> rather than queued
        invisibly. Jobs that need quiescence close the server's handles and leave the store closed.
      </p>
      {jobs.isLoading ? <Spinner /> : null}
      {jobs.error ? <ErrorBanner error={jobs.error} onRetry={() => jobs.refetch()} /> : null}
      <table className="w-full border-collapse">
        <thead>
          <tr>
            <th className="th">store</th>
            <th className="th">action</th>
            <th className="th">state</th>
            <th className="th">updated</th>
            <th className="th">result</th>
          </tr>
        </thead>
        <tbody>
          {(jobs.data?.jobs ?? []).map((job) => {
            const slug = String(job.detail?.slug ?? "");
            return (
              <tr className="hover:bg-raised" key={job.id}>
                <td className="td">
                  {slug ? <Link className="text-accent hover:underline" to={`/databases/${slug}`}>{slug}</Link> : `#${job.database_id}`}
                </td>
                <td className="td">{job.kind}</td>
                <td className="td">
                  <Badge tone={job.state === "succeeded" ? "ok" : job.state === "failed" ? "danger" : "warn"}>{job.state}</Badge>
                </td>
                <td className="td text-muted">
                  {relativeTime(jobTime(job))} · {formatTime(job.created_at)}
                </td>
                <td className="td">
                  {job.state === "failed" ? (
                    <span className="text-danger">
                      {String(job.detail?.code ?? "")} {String(job.detail?.message ?? "").slice(0, 160)}
                    </span>
                  ) : job.state === "succeeded" ? (
                    <details>
                      <summary className="cursor-pointer text-[11.5px] text-muted">report</summary>
                      <div className="mt-1">
                        <JsonBlock maxHeight={280} value={job.detail?.report ?? job.detail} />
                      </div>
                    </details>
                  ) : (
                    <span className="text-muted">job {job.id.slice(0, 8)}…</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {(jobs.data?.jobs ?? []).length === 0 ? <Empty>No job has run on this daemon yet.</Empty> : null}
    </Section>
  );
}
