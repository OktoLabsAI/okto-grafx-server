import { useState } from "react";
import { Ban, CircleCheck, Copy, KeyRound } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { admin, dataKey, type KeyRecord } from "@/api/client";
import { Badge, ConfirmButton, Empty, ErrorBanner, Field, Section, Spinner } from "@/components/ui";
import { formatTime, relativeTime } from "@/lib/format";

export function Keys() {
  const queryClient = useQueryClient();
  const keys = useQuery({ queryKey: ["keys"], queryFn: admin.keys });
  const databases = useQuery({ queryKey: ["databases"], queryFn: admin.databases });

  const [name, setName] = useState("");
  const [scope, setScope] = useState<string[]>([]);
  const [allowWrites, setAllowWrites] = useState(false);
  const [secret, setSecret] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () =>
      admin.createKey({
        name,
        databases: scope,
        allow_writes: allowWrites,
      }),
    onSuccess: (result) => {
      setSecret(result.key);
      setName("");
      queryClient.invalidateQueries({ queryKey: ["keys"] });
    },
  });
  const revoke = useMutation({
    mutationFn: (id: number) => admin.revokeKey(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["keys"] }),
  });
  const remove = useMutation({
    mutationFn: (id: number) => admin.deleteKey(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["keys"] }),
  });
  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) => admin.patchKey(id, { enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["keys"] }),
  });

  return (
    <div className="space-y-3">
      <Section title="create a key">
        <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
          <div className="space-y-3">
            <Field hint="A name for the audit log; it is not secret." label="name">
              <input className="input" onChange={(event) => setName(event.target.value.trim())} placeholder="reporting-service" value={name} />
            </Field>
            <Field hint="Writes still need the store's own write toggle, and a WebSocket session in mode=write." label="allow_writes">
              <select className="input" onChange={(event) => setAllowWrites(event.target.value === "true")} value={String(allowWrites)}>
                <option value="false">false — read-only key</option>
                <option value="true">true</option>
              </select>
            </Field>
          </div>
          <div className="space-y-3">
            <div>
              <span className="label">databases this key may reach</span>
              <div className="flex flex-wrap gap-1">
                {(databases.data?.databases ?? []).map((record) => {
                  const on = scope.includes(record.slug);
                  return (
                    <button
                      className={`btn ${on ? "btn-primary" : ""}`}
                      key={record.slug}
                      onClick={() => setScope(on ? scope.filter((item) => item !== record.slug) : [...scope, record.slug])}
                      type="button"
                    >
                      {record.slug}
                    </button>
                  );
                })}
                {(databases.data?.databases ?? []).length === 0 ? <span className="text-[11.5px] text-muted">no store registered</span> : null}
              </div>
            </div>
            <ConfirmButton
              confirmLabel="issue key"
              detail={`scope: ${scope.length ? scope.join(", ") : "no database (the key will reach nothing)"} · writes ${allowWrites ? "allowed" : "refused"}`}
              disabled={!name || scope.length === 0}
              label="issue key"
              onConfirm={() => create.mutate()}
            />
            {create.error ? <ErrorBanner error={create.error} /> : null}
          </div>
        </div>

        {secret ? (
          <div className="mt-3 rounded-md border border-[#44d2e552] bg-[#07262c] p-3">
            <p className="text-[12px] text-[#8fe6f1]">
              This is the only time the secret is shown. Hashes are stored, not the key itself.
            </p>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <code className="flex-1 overflow-auto rounded bg-deep px-2 py-1.5 font-mono text-[12.5px] text-[#9fe9f8]">{secret}</code>
              <button
                className="btn"
                onClick={() => {
                  dataKey.set(secret);
                  queryClient.invalidateQueries();
                }}
                type="button"
              >
                <KeyRound size={14} strokeWidth={2} aria-hidden />
                use here
              </button>
              <button className="btn" onClick={() => navigator.clipboard?.writeText(secret)} type="button">
                <Copy size={14} strokeWidth={2} aria-hidden />
                copy
              </button>
            </div>
            <p className="mt-2 font-mono text-[11.5px] text-muted">
              curl -H "Authorization: Bearer …" http://127.0.0.1:8737/api/v1/databases
            </p>
          </div>
        ) : null}
      </Section>

      <Section title="keys">
        {keys.isLoading ? <Spinner /> : null}
        {keys.error ? <ErrorBanner error={keys.error} onRetry={() => keys.refetch()} /> : null}
        <table className="w-full border-collapse">
          <thead>
            <tr>
              <th className="th">name</th>
              <th className="th">prefix</th>
              <th className="th">scope</th>
              <th className="th">writes</th>
              <th className="th">state</th>
              <th className="th">created</th>
              <th className="th">last used</th>
              <th className="th">actions</th>
            </tr>
          </thead>
          <tbody>
            {(keys.data?.keys ?? []).map((key: KeyRecord) => (
              <tr className="hover:bg-raised" key={key.id}>
                <td className="td">{key.name}</td>
                <td className="td">{key.prefix}</td>
                <td className="td">{key.databases.length ? key.databases.join(", ") : <span className="text-muted">none</span>}</td>
                <td className="td">{key.allow_writes ? <Badge tone="warn">allowed</Badge> : <Badge>read-only</Badge>}</td>
                <td className="td">
                  {key.revoked_at ? (
                    <Badge tone="danger">revoked {relativeTime(key.revoked_at)}</Badge>
                  ) : key.enabled ? (
                    <Badge tone="ok">enabled</Badge>
                  ) : (
                    <Badge>disabled</Badge>
                  )}
                </td>
                <td className="td text-muted">{formatTime(key.created_at)}</td>
                <td className="td text-muted">{relativeTime(key.last_used_at)}</td>
                <td className="td whitespace-nowrap">
                  {!key.revoked_at ? (
                    <>
                      <button className="btn" onClick={() => toggle.mutate({ id: key.id, enabled: !key.enabled })} type="button">
                        {key.enabled ? <Ban size={14} strokeWidth={2} aria-hidden /> : <CircleCheck size={14} strokeWidth={2} aria-hidden />}
                        {key.enabled ? "disable" : "enable"}
                      </button>{" "}
                      <ConfirmButton
                        confirmLabel="revoke"
                        danger
                        detail="Revoking is immediate: requests carrying this key start failing with 401."
                        label="revoke"
                        onConfirm={() => revoke.mutate(key.id)}
                      />{" "}
                    </>
                  ) : null}
                  <ConfirmButton
                    confirmLabel="delete row"
                    detail="Deletes the record. Prefer revoke: the audit log keeps naming the fingerprint."
                    label="delete"
                    onConfirm={() => remove.mutate(key.id)}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {(keys.data?.keys ?? []).length === 0 ? <Empty>No key yet.</Empty> : null}
      </Section>
    </div>
  );
}
