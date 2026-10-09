"use client";

import { usePathname } from "next/navigation";
import { AuthProvider, Protected } from "@/lib/auth-context";
import SiteHeader from "@/components/SiteHeader";
import ThemeToggle from "@/components/ThemeToggle";

// The one place that decides "does this route get the chrome + admin gate".
// /login is the sole exception (see lib/auth-context.tsx's PUBLIC_PATHS) -
// it has to render for a logged-out visitor, which is exactly what Protected
// would otherwise block.
export default function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const isLogin = pathname === "/login";

  return (
    <AuthProvider>
      <div className="bg-grid" aria-hidden="true" />
      <div className="bg-aurora" aria-hidden="true" />
      {!isLogin && <SiteHeader />}
      {isLogin && (
        <div className="fixed top-4 right-4 z-20">
          <ThemeToggle />
        </div>
      )}
      <main className={isLogin ? "flex-1 flex items-center justify-center px-4" : "flex-1 max-w-5xl mx-auto w-full px-4 py-10"}>
        {isLogin ? children : <Protected>{children}</Protected>}
      </main>
      {!isLogin && (
        <footer className="text-center text-xs text-slate-500 py-6">
          Automated GMC policy checks - always confirm critical findings before acting.
        </footer>
      )}
    </AuthProvider>
  );
}
