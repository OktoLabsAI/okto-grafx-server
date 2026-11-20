import { useEffect, useRef, useState, type ReactNode } from "react";
import { ChevronDown, ChevronRight, LoaderCircle, RotateCw, ShieldAlert, TriangleAlert } from "lucide-react";
import { ApiError } from "@/api/client";

export function Badge({ tone = "muted", children }: { tone?: "muted" | "ok" | "warn" | "danger"; children: ReactNode }) {
  const className =
    tone === "ok" ? "badge badge-ok" : tone === "warn" ? "badge badge-warn" : tone === "danger" ? "badge badge-danger" : "badge";
  return <span className={className}>{children}</span>;
}

export function Section({ title, actions, children }: { title: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="card">
      <header className="flex items-center justify-between gap-3 border-b border-line px-3 py-2">
        <h2 className="eyebrow">{title}</h2>
        <div className="flex items-center gap-2">{actions}</div>
      </header>
      <div className="p-3">{children}</div>
    </section>
  );
}

export function Spinner({ label = "working…" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-[12px] text-muted">
      {/* Lucide's spinner rather than a pulsing dot: the site animates its icons, not its colours. */}
      <LoaderCircle size={14} strokeWidth={2} className="animate-spin text-accent" aria-hidden />
      {label}
    </div>
  );
}

/**
 * A refusal is rendered as the server wrote it: the code, the sentence, whether retrying can help,
 * and the request id for the audit log. Nothing is smoothed over into "something went wrong".
 */
export function ErrorBanner({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  if (!error) return null;
  const api = error instanceof ApiError ? error : null;
  const message = api?.message ?? (error instanceof Error ? error.message : String(error));
  const [open, setOpen] = useState(false);
  const details = api && Object.keys(api.details ?? {}).length > 0 ? api.details : null;
  return (
    <div
      className="rounded-xl border border-[#f0938c45] bg-[#170d10] px-3 py-2 text-[12.5px]"
      role="alert"
    >
      <div className="flex flex-wrap items-center gap-2">
        <TriangleAlert size={14} strokeWidth={2} className="text-danger" aria-hidden />
        <Badge tone="danger">{api?.code ?? "error"}</Badge>
        {api?.retryable ? <Badge tone="warn">retryable</Badge> : null}
        {api?.field ? <Badge tone="muted">field: {api.field}</Badge> : null}
        <span className="text-[#f6c9c5]">{message}</span>
        <span className="ml-auto flex items-center gap-2">
          {onRetry ? (
            <button className="btn" onClick={onRetry} type="button">
              <RotateCw size={14} strokeWidth={2} aria-hidden />
              Retry
            </button>
          ) : null}
          {details ? (
            <button className="btn" onClick={() => setOpen((value) => !value)} type="button">
              {open ? <ChevronDown size={14} strokeWidth={2} aria-hidden /> : <ChevronRight size={14} strokeWidth={2} aria-hidden />}
              {open ? "hide details" : "details"}
            </button>
          ) : null}
        </span>
      </div>
      {api?.requestId ? (
        <div className="mt-1 font-mono text-[11px] text-muted">request id {api.requestId}</div>
      ) : null}
      {open && details ? (
        <pre className="scroll-thin mt-2 max-h-52 overflow-auto rounded-lg border border-[#f0938c26] bg-[#12090b] p-2 font-mono text-[11px] text-[#f0bdb9]">
          {JSON.stringify(details, null, 2)}
        </pre>
      ) : null}
    </div>
  );
}

export function Tabs<T extends string>({
  tabs,
  active,
  onChange,
}: {
  tabs: readonly { id: T; label: string; badge?: string | number }[];
  active: T;
  onChange: (id: T) => void;
}) {
  return (
    <div className="flex flex-wrap items-end gap-1">
      {tabs.map((tab) => (
        <button
          className={`tab ${tab.id === active ? "tab-active" : ""}`}
          key={tab.id}
          onClick={() => onChange(tab.id)}
          type="button"
        >
          {tab.label}
          {tab.badge !== undefined && tab.badge !== 0 ? <span className="ml-1 text-[11px] opacity-70">{tab.badge}</span> : null}
        </button>
      ))}
    </div>
  );
}

/**
 * Writes are never one click deep: the operator has to see the thing being confirmed, and the
 * button only arms for a moment so a stray second click cannot fire it.
 */
export function ConfirmButton({
  label,
  confirmLabel,
  detail,
  danger,
  disabled,
  pending,
  onConfirm,
}: {
  label: string;
  confirmLabel: string;
  detail?: ReactNode;
  danger?: boolean;
  disabled?: boolean;
  pending?: boolean;
  onConfirm: () => void;
}) {
  const [armed, setArmed] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <button
        className={`btn ${danger ? "btn-danger" : ""}`}
        disabled={disabled || pending}
        onClick={() => {
          if (armed) {
            setArmed(false);
            onConfirm();
          } else {
            setArmed(true);
            timer.current = window.setTimeout(() => setArmed(false), 6000);
          }
        }}
        type="button"
      >
        {pending ? "…" : armed ? confirmLabel : label}
      </button>
      {armed ? (
        <span className="flex items-center gap-1.5 text-[12px] text-warn">
          <ShieldAlert size={13} strokeWidth={2} aria-hidden />
          click again to confirm
        </span>
      ) : null}
      {armed && detail ? <span className="w-full font-mono text-[11.5px] text-muted">{detail}</span> : null}
    </span>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="label">{label}</span>
      {children}
      {hint ? <span className="mt-1 block text-[11px] text-muted">{hint}</span> : null}
    </label>
  );
}

export function JsonBlock({ value, maxHeight = 320 }: { value: unknown; maxHeight?: number }) {
  return (
    <pre
      className="scroll-thin overflow-auto rounded-lg border border-line bg-deep p-2 font-mono text-[11.5px] leading-relaxed text-cell"
      style={{ maxHeight }}
    >
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-6 text-center text-[12.5px] text-muted">{children}</p>;
}
