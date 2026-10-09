"use client";

import { useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { motion } from "framer-motion";
import { getFirstAuditStatus, firstAuditReportPdfUrl, FirstAuditStatus } from "@/lib/admin-api";
import { ApiError } from "@/lib/api";
import { fadeUp, staggerContainer, staggerItem } from "@/lib/motion";
import AnimatedCounter from "@/components/AnimatedCounter";

function handleSpotlight(e: React.MouseEvent<HTMLElement>) {
  const el = e.currentTarget;
  const rect = el.getBoundingClientRect();
  el.style.setProperty("--spot-x", `${e.clientX - rect.left}px`);
  el.style.setProperty("--spot-y", `${e.clientY - rect.top}px`);
}

const POLL_INTERVAL_MS = 2000;

const SNAPSHOT_LABEL: Record<string, string> = {
  COMPLIANT: "Compliant",
  ACTION_REQUIRED: "Action required",
  CRITICAL: "Critical",
};

export default function FirstAuditRunPage() {
  const { runId } = useParams<{ runId: string }>();
  const [run, setRun] = useState<FirstAuditStatus | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function poll() {
      try {
        const status = await getFirstAuditStatus(Number(runId));
        if (cancelled) return;
        setRun(status);
        setPollError(null);
        if (status.status === "pending" || status.status === "running") {
          timerRef.current = setTimeout(poll, POLL_INTERVAL_MS);
        }
      } catch (err) {
        if (cancelled) return;
        setPollError(err instanceof ApiError ? err.message : "Lost connection to the first-audit service.");
        timerRef.current = setTimeout(poll, POLL_INTERVAL_MS);
      }
    }

    poll();
    return () => {
      cancelled = true;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [runId]);

  if (pollError && !run) {
    return <p className="text-red-600 dark:text-red-400">{pollError}</p>;
  }
  if (!run) {
    return <p className="text-slate-500 dark:text-slate-400">Loading...</p>;
  }

  if (run.status === "error") {
    return (
      <motion.div initial="hidden" animate="show" variants={fadeUp}>
        <h1 className="text-xl font-semibold mb-2">Audit failed</h1>
        <p className="text-slate-500 dark:text-slate-400 mb-1">{run.url}</p>
        <p className="text-red-600 dark:text-red-400">{run.error}</p>
      </motion.div>
    );
  }

  if (run.status === "pending" || run.status === "running") {
    return (
      <motion.div initial="hidden" animate="show" variants={fadeUp}>
        <h1 className="text-xl font-semibold mb-1">Auditing {run.url}</h1>
        <p className="text-slate-500 dark:text-slate-400 mb-6">
          Discovering pages, extracting facts, and evaluating rules. This can take a few minutes.
        </p>
        <div className="flex items-center gap-3 text-sm">
          <span className="relative h-2.5 w-2.5 rounded-full overflow-hidden">
            <motion.span
              className="absolute inset-0 rounded-full"
              style={{ background: "linear-gradient(90deg, var(--brand-1), var(--brand-2))" }}
              animate={{ scale: [1, 1.35, 1] }}
              transition={{ duration: 1.2, repeat: Infinity, ease: "easeInOut" }}
            />
          </span>
          <span className="text-slate-300">Running...</span>
        </div>
        {pollError && <p className="text-sm text-amber-600 dark:text-amber-400 mt-4">{pollError} - retrying...</p>}
      </motion.div>
    );
  }

  // status === "done"
  const snapshotLabel = run.snapshot_status ? SNAPSHOT_LABEL[run.snapshot_status] ?? run.snapshot_status : "-";

  return (
    <motion.div initial="hidden" animate="show" variants={staggerContainer}>
      <motion.h1 variants={staggerItem} className="text-xl font-semibold mb-1">
        Audit complete
      </motion.h1>
      <motion.p variants={staggerItem} className="text-slate-500 dark:text-slate-400 mb-6">
        {run.url}
      </motion.p>

      <motion.div variants={staggerItem} className="flex flex-wrap gap-4 mb-6 text-sm">
        <Stat
          label="Snapshot status"
          value={snapshotLabel}
          highlight={run.snapshot_status === "CRITICAL"}
          emphasize
        />
        <Stat label="Platform" value={run.platform ?? "unknown"} />
        <Stat
          label="Pages crawled"
          value={run.pages_crawled != null ? <AnimatedCounter value={run.pages_crawled} /> : "-"}
        />
        <Stat
          label="Unreachable pages"
          value={<AnimatedCounter value={run.unreachable_pages_count ?? 0} />}
          highlight={!!run.unreachable_pages_count}
        />
      </motion.div>

      {!!run.unreachable_pages_count && (
        <motion.p variants={staggerItem} className="text-sm text-amber-600 dark:text-amber-400 mb-6">
          {run.unreachable_pages_count} page(s) could not be read (blocked or unreachable) - findings
          about those pages are reported as &quot;could not verify,&quot; never a confirmed pass or fail.
        </motion.p>
      )}

      <motion.a
        variants={staggerItem}
        href={firstAuditReportPdfUrl(Number(runId))}
        whileHover={{ scale: 1.02, y: -1 }}
        whileTap={{ scale: 0.98 }}
        className="inline-block text-white rounded-lg px-4 py-2 font-medium w-fit"
        style={{ background: "linear-gradient(90deg, var(--brand-1), var(--brand-2))" }}
      >
        Download PDF Report
      </motion.a>
    </motion.div>
  );
}

function Stat({
  label, value, highlight, emphasize,
}: { label: string; value: React.ReactNode; highlight?: boolean; emphasize?: boolean }) {
  return (
    <motion.div
      whileHover={{ y: -2 }}
      onMouseMove={handleSpotlight}
      className={`spotlight-card glass-card rounded-lg px-3 py-2 min-w-[7rem] ${emphasize && highlight ? "ring-2 ring-red-400/60" : ""}`}
    >
      <div className="text-slate-500 text-xs">{label}</div>
      <div className={`font-semibold ${highlight ? "text-red-600 dark:text-red-400" : ""} ${emphasize ? "text-lg" : ""}`}>{value}</div>
    </motion.div>
  );
}
