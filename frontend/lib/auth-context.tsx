"use client";

// Whole-app admin gate. Every backend route except /api/auth/* and /health is
// now require_admin-gated (see app/api/dependencies.py's own docstring:
// "logged-out or non-admin = 403 (API) / redirect to login (UI)" - the
// redirect is explicitly left as a frontend concern, which is what this
// module does, once, for the entire app, instead of every page repeating its
// own guard).
import { createContext, useContext, useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { me, AdminUser } from "@/lib/admin-api";

type AuthState = {
  admin: AdminUser | null;
  loading: boolean;
};

type AuthContextValue = AuthState & {
  // Lets the login page seed the auth state directly with the AdminUser the
  // login call already returned, instead of relying on the one-time mount
  // effect below to notice. That effect runs ONCE, on AuthProvider's own
  // mount (empty deps) - AuthProvider lives in the root layout and is never
  // remounted by a client-side router.push, so without this, a successful
  // login's state.admin stays stale (still null, from before login) right
  // up until the redirect effect below fires on the new pathname and
  // bounces straight back to /login. Confirmed live, not hypothetical -
  // reproduced by a real user, not just this session's own test tooling.
  setAdmin: (admin: AdminUser) => void;
};

const AuthContext = createContext<AuthContextValue>({ admin: null, loading: true, setAdmin: () => {} });

export function useAuth(): AuthContextValue {
  return useContext(AuthContext);
}

const PUBLIC_PATHS = new Set(["/login"]);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [state, setState] = useState<AuthState>({ admin: null, loading: true });

  useEffect(() => {
    let cancelled = false;
    me()
      .then((user) => {
        if (!cancelled) setState({ admin: user, loading: false });
      })
      .catch(() => {
        if (!cancelled) setState({ admin: null, loading: false });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (state.loading) return;
    if (!state.admin && !PUBLIC_PATHS.has(pathname)) {
      router.replace("/login");
    }
  }, [state, pathname, router]);

  function setAdmin(admin: AdminUser) {
    setState({ admin, loading: false });
  }

  return <AuthContext.Provider value={{ ...state, setAdmin }}>{children}</AuthContext.Provider>;
}

// Wraps every page's content: while the session check is in flight, or while
// a logged-out visitor is about to be redirected, shows a neutral loading
// state instead of flashing the real page. The /login page itself is never
// wrapped in this (see app/layout.tsx) - it has to render for logged-out
// visitors, which is the one case this component would otherwise block.
export function Protected({ children }: { children: React.ReactNode }) {
  const { admin, loading } = useAuth();

  if (loading || !admin) {
    return (
      <div className="flex items-center justify-center py-24 text-slate-500">
        <span className="inline-block h-2 w-2 rounded-full bg-[var(--brand-1)] animate-pulse mr-2" />
        {loading ? "Checking session..." : "Redirecting to login..."}
      </div>
    );
  }

  return <>{children}</>;
}
