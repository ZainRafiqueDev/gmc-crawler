"use client";

import { motion } from "framer-motion";

// Client-facing severity filter: ON shows only Critical/High (real GMC
// suspension-risk) findings, OFF shows the full report. Shared by the
// ad-hoc Report screen and the per-store Latest Report screen so both
// toggles look and behave identically.
export default function MajorOnlyToggle({
  checked,
  onChange,
  hiddenCount,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  hiddenCount?: number | null;
}) {
  return (
    <div className="flex items-center gap-3">
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className="gradient-ring relative h-7 w-12 shrink-0 rounded-full transition-colors duration-300"
        style={{
          background: checked
            ? "linear-gradient(90deg, var(--brand-1), var(--brand-2))"
            : "var(--surface-border)",
        }}
      >
        <motion.span
          layout
          transition={{ type: "spring", stiffness: 500, damping: 32 }}
          className="absolute top-0.5 h-6 w-6 rounded-full bg-white shadow"
          style={{ left: checked ? "calc(100% - 1.5rem - 2px)" : "2px" }}
        />
      </button>
      <div className="text-sm leading-tight">
        <div className="font-medium">Major issues only</div>
        <div className="text-slate-500 dark:text-slate-400 text-xs">
          {checked
            ? `Showing GMC suspension-risk findings${hiddenCount ? ` - ${hiddenCount} minor hidden` : ""}`
            : "Showing every finding, including minor ones"}
        </div>
      </div>
    </div>
  );
}
