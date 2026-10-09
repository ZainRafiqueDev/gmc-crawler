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

const AuthContext = createContext<AuthState>({ admin: null, loading: true });

export function useAuth(): AuthState {
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

  return <AuthContext.Provider value={state}>{children}</AuthContext.Provider>;
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
