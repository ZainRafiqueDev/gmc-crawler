"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import NavLink from "@/components/NavLink";
import ThemeToggle from "@/components/ThemeToggle";
import { useAuth } from "@/lib/auth-context";
import { logout } from "@/lib/admin-api";

export default function SiteHeader() {
  const { admin } = useAuth();
  const router = useRouter();

  async function handleLogout() {
    await logout();
    router.push("/login");
  }

  return (
    <header className="sticky top-0 z-20 border-b border-surface-border/80 bg-background/70 backdrop-blur-md">
      <nav className="max-w-5xl mx-auto px-4 py-3 flex items-center gap-6">
        <Link href="/" className="font-semibold tracking-tight text-lg gradient-text">
          GMC Compliance Checker
        </Link>
        {admin && (
          <>
            <NavLink href="/">Run Audit</NavLink>
            <NavLink href="/monitor">Monitored Stores</NavLink>
            <NavLink href="/first-audit">First Audit</NavLink>
            <span className="flex-1" />
            <ThemeToggle />
            <span className="text-xs text-slate-500 dark:text-slate-400 hidden sm:inline">{admin.email}</span>
            <button onClick={handleLogout} className="text-sm text-slate-500 dark:text-slate-400 hover:text-foreground transition-colors">
              Log out
            </button>
          </>
        )}
      </nav>
    </header>
  );
}
