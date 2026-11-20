import { useState } from "react";
import { LogIn } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { admin, ApiError } from "@/api/client";
import { ErrorBanner } from "@/components/ui";

export function Login() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [token, setToken] = useState("");
  const login = useMutation({
    mutationFn: (value: string) => admin.login(value),
    onSuccess: (result) => {
      // The shell asked before this visit and was told `signed_in: false`. Unless that answer is
      // replaced with the fresh one, the shell would bounce a successfully signed-in operator
      // straight back here, and the reason would look like a bad token.
      queryClient.setQueryData(["session"], {
        signed_in: result.signed_in,
        token_configured: true,
      });
      queryClient.invalidateQueries({ queryKey: ["session"] });
      queryClient.invalidateQueries({ queryKey: ["status"] });
      navigate("/databases", { replace: true });
    },
  });

  const error = login.error as ApiError | null;

  return (
    <div className="flex min-h-screen items-center justify-center p-6">
      <form
        className="card w-full max-w-md space-y-4 p-5"
        onSubmit={(event) => {
          event.preventDefault();
          login.mutate(token.trim());
        }}
      >
        <div>
          {/* The product mark, decorative here: the name is written next to it. */}
          <img alt="" className="h-10 w-10" src="/okto-grafx-icon.svg" />
          <h1 className="mt-2 text-[17px] font-semibold tracking-tight">Okto Grafx</h1>
          <p className="mt-1 text-[12.5px] text-muted">
            Sign in with the administrator token written by <code className="text-cell">oktografx-server init</code>.
            The session is an httpOnly, SameSite=Strict cookie: the token is not kept in this page.
          </p>
        </div>
        <label className="block">
          <span className="label">admin token</span>
          <input
            autoFocus
            className="input font-mono"
            onChange={(event) => setToken(event.target.value.trim())}
            placeholder="ogkadm_…"
            type="password"
            value={token}
          />
        </label>
        {error ? (
          <ErrorBanner
            error={
              error.status === 401
                ? new ApiError(401, {
                    code: error.code,
                    message:
                      error.message +
                      " — the token is the one printed by `oktografx-server init`, or written to the state directory. Rotate it from Settings if it is lost.",
                    retryable: false,
                    details: error.details,
                  })
                : error
            }
          />
        ) : null}
        <button className="btn btn-primary w-full justify-center" disabled={!token || login.isPending} type="submit">
          <LogIn size={14} strokeWidth={2} aria-hidden />
          {login.isPending ? "signing in…" : "Sign in"}
        </button>
        <p className="text-[11px] text-muted">
          Data-plane work here uses an API key when you paste one, so what you see is what a scoped
          credential can see. A local process with filesystem access can open these stores directly:
          this server is logical access control, not an operating-system sandbox.
        </p>
      </form>
    </div>
  );
}
