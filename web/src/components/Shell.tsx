import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Database,
  KeyRound,
  LogOut,
  ScrollText,
  Settings2,
  ShieldCheck,
  ListChecks,
  Check,
  type LucideIcon,
} from "lucide-react";
import { admin, ApiError, dataKey } from "@/api/client";
import { Badge, ErrorBanner } from "@/components/ui";
import { useEffect, useState, type ReactNode } from "react";

/** Lucide, the icon set of the Grafx page: 2px stroke, currentColor, round joins, 14px inline. */
type Icon = LucideIcon;

const NAV: { to: string; label: string; icon: Icon }[] = [
  { to: "/databases", label: "Databases", icon: Database },
  { to: "/keys", label: "API keys", icon: KeyRound },
  { to: "/audit", label: "Audit", icon: ScrollText },
  { to: "/jobs", label: "Jobs", icon: ListChecks },
  { to: "/settings", label: "Settings", icon: Settings2 },
];

export function Shell({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const [keyDraft, setKeyDraft] = useState(dataKey.get());

  const session = useQuery({
    queryKey: ["session"],
    queryFn: admin.session,
    retry: false,
    refetchInterval: 60_000,
  });
  const signedIn = session.data?.signed_in === true;
  // The default posture: no credential exists, so there is nothing for this browser to sign out of.
  const openAccess = session.data?.auth === "open-loopback";
  // The daemon answers `200 {"signed_in": false}` for an anonymous visitor — it keeps 401 for a
  // credential it actively refuses. Either way there is nothing to show but the login screen.
  const refused = session.error instanceof ApiError && session.error.status === 401;
  const anonymous = !signedIn && (session.data !== undefined || refused);

  const status = useQuery({
    queryKey: ["status"],
    queryFn: admin.status,
    refetchInterval: 15_000,
    // An anonymous visitor has no handle table to look at, and asking for one answers 401 before the
    // redirect even lands: the console should not make noise about a session it does not have.
    enabled: signedIn,
  });

  useEffect(() => {
    if (signedIn || location.pathname.startsWith("/login")) return;
    if (anonymous) navigate("/login", { replace: true });
  }, [anonymous, signedIn, navigate, location.pathname]);

  const logout = useMutation({
    mutationFn: admin.logout,
    onSuccess: () => {
      queryClient.clear();
      navigate("/login", { replace: true });
    },
  });

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-56 shrink-0 flex-col border-r border-line bg-rail">
        <div className="border-b border-line px-4 py-3">
          <Link
            className="flex items-center gap-2 text-[15px] font-semibold tracking-tight text-fg"
            to="/databases"
          >
            {/* The Okto Grafx mark: the open octagon with a graph inside it, served from this daemon
                (web/public/okto-grafx-icon.svg) so the CSP never has to allow an external origin. */}
            <img alt="" className="h-[22px] w-[22px]" src="/okto-grafx-icon.svg" />
            Okto Grafx
          </Link>
          <span className="eyebrow mt-1 block">
            {session.data?.server_version ? `server ${session.data.server_version}` : "operator console"}
          </span>
        </div>
        <nav className="flex-1 space-y-0.5 p-2">
          {NAV.map((item) => (
            <NavLink
              className={({ isActive }) =>
                `flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-[13px] transition-colors duration-150 ${
                  isActive
                    ? "bg-[#41d7ee14] text-accent shadow-[inset_2px_0_0_var(--color-accent)]"
                    : "text-muted hover:bg-raised hover:text-fg"
                }`
              }
              key={item.to}
              to={item.to}
            >
              <item.icon size={14} strokeWidth={2} aria-hidden />
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-line p-3 text-[11px] text-muted">
          <div className="mb-2 flex items-center gap-2">
            <Badge tone={openAccess ? "warn" : signedIn ? "ok" : "danger"}>
              {openAccess ? "this machine only" : signedIn ? "admin session" : "signed out"}
            </Badge>
          </div>
          {status.data ? (
            <dl className="space-y-0.5">
              <div className="flex justify-between gap-2">
                <dt>open stores</dt>
                <dd className="font-mono text-cell">{status.data.open_slugs?.length ?? 0}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt>ws sessions</dt>
                <dd className="font-mono text-cell">{status.data.ws_sessions}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt>uptime</dt>
                <dd className="font-mono text-cell">{Math.round(status.data.uptime_seconds)}s</dd>
              </div>
            </dl>
          ) : null}
          {openAccess ? (
            // Signing out of a door that has no lock would log the operator out of nothing, so the
            // rail points at the switch instead.
            <Link className="btn mt-3 w-full justify-center" to="/settings">
              <ShieldCheck size={14} strokeWidth={2} aria-hidden />
              token is off
            </Link>
          ) : (
            <button className="btn mt-3 w-full justify-center" onClick={() => logout.mutate()} type="button">
              <LogOut size={14} strokeWidth={2} aria-hidden />
              Sign out
            </button>
          )}
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex flex-wrap items-center gap-2 border-b border-line bg-raised px-4 py-2">
          <KeyRound size={14} strokeWidth={2} className="text-muted" aria-hidden />
          <span className="eyebrow">data-plane credential</span>
          <input
            className="input max-w-xs font-mono text-[12px]"
            onChange={(event) => setKeyDraft(event.target.value.trim())}
            placeholder="okg_… (blank: use the admin session)"
            value={keyDraft}
          />
          <button
            className="btn"
            onClick={() => {
              dataKey.set(keyDraft);
              queryClient.invalidateQueries();
            }}
            type="button"
          >
            <Check size={14} strokeWidth={2} aria-hidden />
            Use
          </button>
          <Badge tone={dataKey.get() ? "ok" : "warn"}>
            {dataKey.get() ? "querying as a scoped key" : "querying as admin"}
          </Badge>
          <span className="ml-auto flex items-center gap-1.5 text-[11px] text-muted">
            <ShieldCheck size={13} strokeWidth={2} aria-hidden />
            no TLS: reach this console through 127.0.0.1 only
          </span>
        </header>
        <main className="min-w-0 flex-1 space-y-4 p-4">
          {signedIn ? (
            children
          ) : (
            // Nothing here is worth rendering without a session: the pages behind this point ask the
            // admin API for stores, keys and audit rows, and each of those asks would answer 401 on
            // the way to a screen the operator is about to be sent to.
            <p className="text-[12.5px] text-muted">
              {anonymous
                ? "signed out — taking you back to the sign-in screen…"
                : "checking the administrator session…"}
            </p>
          )}
        </main>
        {session.error && session.error instanceof ApiError && session.error.status !== 401 ? (
          <div className="px-4 pb-4">
            <ErrorBanner error={session.error} onRetry={() => session.refetch()} />
          </div>
        ) : null}
      </div>
    </div>
  );
}
