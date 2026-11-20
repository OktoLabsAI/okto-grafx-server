import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy } from "lucide-react";
import { admin, type DatabaseOption } from "@/api/client";
import { GROUP_TITLES } from "@/components/ConfigForm";
import { formatBytes } from "@/lib/format";
import { Badge, ConfirmButton, ErrorBanner, JsonBlock, Section, Spinner } from "@/components/ui";

export function Settings() {
  const queryClient = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: admin.settings });
  const status = useQuery({ queryKey: ["status"], queryFn: admin.status, refetchInterval: 10_000 });
  const [issued, setIssued] = useState<string | null>(null);

  const refreshAuth = () => {
    queryClient.invalidateQueries({ queryKey: ["session"] });
    queryClient.invalidateQueries({ queryKey: ["settings"] });
  };

  // Rotating kills every session built on the old value, this browser's included. The panel keeps
  // the operator standing where they are by trading the fresh token for a fresh session in the same
  // motion; every other browser is signed out, which is the promise the sentence makes.
  const rotate = useMutation({
    mutationFn: async () => {
      const result = await admin.rotateAdminToken();
      await admin.login(result.admin_token);
      return result;
    },
    onSuccess: (result) => {
      setIssued(result.admin_token);
      refreshAuth();
    },
  });

  const requireToken = useMutation({
    mutationFn: admin.mintAdminToken,
    onSuccess: (result) => {
      setIssued(result.admin_token);
      refreshAuth();
    },
  });

  const stopRequiring = useMutation({
    mutationFn: admin.disableAdminToken,
    onSuccess: () => {
      setIssued(null);
      refreshAuth();
    },
  });

  if (settings.isLoading) return <Spinner label="loading settings…" />;
  if (settings.error) return <ErrorBanner error={settings.error} onRetry={() => settings.refetch()} />;
  const payload = settings.data!;
  const auth = payload.admin_auth;

  return (
    <div className="space-y-3">
      <div className="grid gap-3 lg:grid-cols-2">
        <Section title="daemon">
          <dl className="space-y-1 text-[12.5px]">
            <Line label="version" value={payload.server_version} />
            <Line label="bind" value={`${payload.bind.host}:${payload.bind.port}`} />
            <Line label="state directory" value={payload.state_dir} />
            <Line label="allowed roots" value={payload.allowed_roots.join(", ") || "none configured"} />
            <Line label="tls" value={payload.security.tls ? "on" : "off — loopback only"} />
            <Line label="cors origins" value={payload.security.cors_origins.length ? payload.security.cors_origins.join(", ") : "same origin only"} />
            <Line label="metrics" value={payload.security.metrics_enabled ? "enabled" : "disabled"} />
            <Line
              label="store metrics off-machine"
              value={
                payload.security.allow_remote_store_metrics
                  ? "permitted by configuration"
                  : "refused"
              }
            />
          </dl>
          <p className="mt-2 text-[11.5px] text-muted">{payload.security.note}</p>
        </Section>

        <Section title="limits">
          <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-[12.5px]">
            {Object.entries(payload.limits).map(([key, value]) => (
              <Line key={key} label={key} value={String(value)} />
            ))}
          </dl>
        </Section>
      </div>

      <Section
        actions={
          auth.mode === "open-loopback" ? (
            <ConfirmButton
              confirmLabel="require a token"
              detail="Any process on this machine can create API keys and register stores right now. Turning this on makes every administrator request prove the token — and this browser signs itself in with the value it is about to show you."
              label="require a token"
              onConfirm={() => requireToken.mutate()}
            />
          ) : (
            <div className="flex flex-wrap gap-2">
              <ConfirmButton
                confirmLabel="rotate token"
                danger
                detail="The old token stops working and every other browser is signed out. This one stays in, with the new value shown once."
                label="rotate admin token"
                onConfirm={() => rotate.mutate()}
              />
              <ConfirmButton
                confirmLabel="stop requiring it"
                danger
                detail="Administrator access becomes this machine only: any process that can reach 127.0.0.1 gets in. API keys, stores and sessions already open are untouched."
                label="stop requiring a token"
                onConfirm={() => stopRequiring.mutate()}
              />
            </div>
          )
        }
        title="administrator"
      >
        {rotate.error ? <ErrorBanner error={rotate.error} /> : null}
        {requireToken.error ? <ErrorBanner error={requireToken.error} /> : null}
        {stopRequiring.error ? <ErrorBanner error={stopRequiring.error} /> : null}
        {issued ? (
          <div className="rounded-md border border-[#e2b13c45] bg-[#1a1508] p-2.5">
            <p className="text-[12px] text-warn">
              Store this now: it is shown once, and the server keeps only a hash of it.
            </p>
            <code className="mt-1 block overflow-auto rounded bg-deep px-2 py-1.5 font-mono text-[12.5px] text-warn">
              {issued}
            </code>
            <button
              className="btn mt-2"
              onClick={() => navigator.clipboard?.writeText(issued)}
              type="button"
            >
              <Copy size={14} strokeWidth={2} aria-hidden />
              copy token
            </button>
          </div>
        ) : null}
        <dl className="mt-2 space-y-1 text-[12.5px]">
          <div className="flex items-center gap-2">
            <dt className="eyebrow">access</dt>
            <dd>
              <Badge tone={auth.mode === "open-loopback" ? "warn" : "ok"}>
                {auth.mode === "open-loopback" ? "no token — this machine only" : "token required"}
              </Badge>
            </dd>
          </div>
          {auth.token_rotated_at ? <Line label="token rotated" value={auth.token_rotated_at} /> : null}
          <Line label="from elsewhere" value="refused — the server binds 127.0.0.1 and has no TLS" />
          <Line
            label="browser writes"
            value="a page that is not this page may not change anything (origin checked)"
          />
          <Line
            label="from the shell"
            value={auth.mode === "open-loopback" ? auth.commands.require : auth.commands.stop_requiring}
          />
        </dl>
        <p className="mt-2 text-[11.5px] text-muted">
          {auth.mode === "open-loopback"
            ? "A token is opt-in here. Until one exists the boundary is the machine: this machine in, everyone else out."
            : "The token is stored hashed in the control database. Rotating it does not touch API keys."}
        </p>
      </Section>

      <Section title="live handle table">
        {status.data ? (
          <>
            <div className="mb-2 flex flex-wrap gap-1.5">
              <Badge tone="muted">runner submitted {status.data.runner.submitted}</Badge>
              <Badge tone="muted">completed {status.data.runner.completed}</Badge>
              <Badge tone={status.data.runner.failed ? "danger" : "muted"}>failed {status.data.runner.failed}</Badge>
              <Badge tone="muted">in flight {status.data.runner.in_flight}</Badge>
              <Badge tone="muted">ws sessions {status.data.ws_sessions}</Badge>
            </div>
            {(status.data.handles ?? []).length === 0 ? (
              <p className="text-[12px] text-muted">No store is open: the LRU closes idle handles.</p>
            ) : (
              <>
                <p className="mb-2 text-[12px] text-muted">
                  The engine grants a page-cache budget per handle, so the pool bounds budget times
                  handles as well as handle count, and closes idle stores first.
                </p>
                <table className="w-full border-collapse">
                  <thead>
                    <tr>
                      <th className="th">store</th>
                      <th className="th">readers</th>
                      <th className="th">writer</th>
                      <th className="th">ops</th>
                      <th className="th">tx</th>
                      <th className="th">idle</th>
                      <th className="th">budget</th>
                      <th className="th">path</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(status.data.handles ?? []).map((handle) => (
                      <tr key={handle.slug}>
                        <td className="td">
                          {handle.slug}{" "}
                          {handle.quarantined ? (
                            <Badge tone="danger">recovery required</Badge>
                          ) : null}
                        </td>
                        <td className="td">
                          {handle.readers} (idle {handle.idle_readers})
                        </td>
                        <td className="td">
                          {handle.writer_open ? <Badge tone="warn">open</Badge> : "—"}
                        </td>
                        <td className="td">{handle.active_operations}</td>
                        <td className="td">{handle.open_transactions}</td>
                        <td className="td">{handle.idle_seconds.toFixed(1)}s</td>
                        <td className="td">{formatBytes(handle.buffer_budget_bytes)}</td>
                        <td className="td break-all text-muted">{handle.path}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </>
        ) : (
          <Spinner />
        )}
      </Section>

      <Section title="DatabaseConfig options the console can set">
        <p className="mb-2 text-[12px] text-muted">
          This table is the schema the attach form is generated from: {payload.database_options.length} options, each
          with its type, default and bounds. Anything the library adds appears here without a frontend release.
        </p>
        <details>
          <summary className="cursor-pointer text-[12px] text-muted">show the option schema</summary>
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            {groupBy(payload.database_options).map(([group, items]) => (
              <div className="rounded-md border border-line p-2" key={group}>
                <h3 className="label">{GROUP_TITLES[group] ?? group}</h3>
                <ul className="space-y-1 text-[11.5px]">
                  {items.map((option) => (
                    <li className="flex justify-between gap-2 border-b border-line pb-0.5" key={option.name}>
                      <span className="font-mono text-cell">{option.name}</span>
                      <span className="text-muted">
                        {option.type}
                        {option.minimum !== undefined || option.maximum !== undefined ? ` ${option.minimum ?? ""}…${option.maximum ?? ""}` : ""} = {JSON.stringify(option.default)}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </details>
      </Section>

      <Section title="maintenance action catalogue">
        <table className="w-full border-collapse">
          <thead>
            <tr>
              <th className="th">action</th>
              <th className="th">read-only ok</th>
              <th className="th">quiescence</th>
              <th className="th">confirmation</th>
              <th className="th">arguments</th>
              <th className="th">what it does</th>
            </tr>
          </thead>
          <tbody>
            {payload.maintenance_actions.map((action) => (
              <tr key={action.action}>
                <td className="td">{action.action}</td>
                <td className="td">{action.read_only_ok ? <Badge tone="ok">yes</Badge> : <Badge tone="warn">no</Badge>}</td>
                <td className="td">{action.quiescing ? <Badge tone="warn">required</Badge> : "—"}</td>
                <td className="td">{action.confirm_quiescent ? <Badge tone="danger">operator</Badge> : "—"}</td>
                <td className="td">{action.requires.join(", ") || "—"}</td>
                <td className="td text-muted">{action.description}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="mt-2">
          <details>
            <summary className="cursor-pointer text-[12px] text-muted">server status payload</summary>
            <div className="mt-1">
              <JsonBlock maxHeight={320} value={status.data} />
            </div>
          </details>
        </div>
      </Section>
    </div>
  );
}

function Line({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4 border-b border-line pb-0.5">
      <dt className="text-muted">{label}</dt>
      <dd className="truncate font-mono text-[12px] text-cell">{value}</dd>
    </div>
  );
}

function groupBy(options: DatabaseOption[]): [string, DatabaseOption[]][] {
  const byGroup = new Map<string, DatabaseOption[]>();
  for (const option of options) {
    byGroup.set(option.group, [...(byGroup.get(option.group) ?? []), option]);
  }
  return [...byGroup.entries()].sort(([left], [right]) => left.localeCompare(right));
}
