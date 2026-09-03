"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

// Nav link with an animated gradient underline that grows in on hover and
// stays lit when it's the active route.
export default function NavLink({ href, children }: { href: string; children: React.ReactNode }) {
  const pathname = usePathname();
  const active = pathname === href;

  return (
    <Link href={href} className="relative text-sm text-slate-600 dark:text-slate-300 hover:text-foreground py-1 group">
      {children}
      <span
        className="absolute left-0 -bottom-0.5 h-0.5 w-full origin-left rounded-full transition-transform duration-300 ease-out"
        style={{
          background: "linear-gradient(90deg, var(--brand-1), var(--brand-2))",
          transform: active ? "scaleX(1)" : "scaleX(0)",
        }}
      />
      <span
        className="absolute left-0 -bottom-0.5 h-0.5 w-full origin-left scale-x-0 rounded-full transition-transform duration-300 ease-out group-hover:scale-x-100"
        style={{ background: "linear-gradient(90deg, var(--brand-1), var(--brand-2))", opacity: active ? 0 : 1 }}
      />
    </Link>
  );
}
