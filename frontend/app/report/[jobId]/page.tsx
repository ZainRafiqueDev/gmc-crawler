"use client";

import { useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { motion, AnimatePresence } from "framer-motion";
import { getAuditStatus, reportDownloadUrl, AuditJobStatus, ApiError } from "@/lib/api";
import { fadeUp, staggerContainer, staggerItem } from "@/lib/motion";
import ReportView from "@/components/ReportView";
import RegisterMonitoringForm from "@/components/RegisterMonitoringForm";
import MajorOnlyToggle from "@/components/MajorOnlyToggle";

const PHASES = [
  ["detect_platform", "Detecting platform"],
  ["crawl_and_classify", "Crawling and classifying pages"],
  ["deterministic_checks", "Running deterministic checks"],
  ["llm_grading", "Grading with LLM"],
  ["compile_report", "Compiling report"],
] as const;

const POLL_INTERVAL_MS = 2000;

export default function ReportPage() {
  const { jobId } = useParams<{ jobId: string }>();
  const [job, setJob] = useState<AuditJobStatus | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [majorOnly, setMajorOnly] = useState(true);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function poll() {
      try {
        const status = await getAuditStatus(jobId);
        if (cancelled) return;
        setJob(status);
        setPollError(null);
        if (status.status === "pending" || status.status === "running") {
          timerRef.current = setTimeout(poll, POLL_INTERVAL_MS);
        }
      } catch (err) {
        if (cancelled) return;
        setPollError(err instanceof ApiError ? err.message : "Lost connection to the audit service.");
        timerRef.current = setTimeout(poll, POLL_INTERVAL_MS);
      }
    }

    poll();
    return () => {
      cancelled = true;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [jobId]);

  if (pollError && !job) {
    return <p className="text-red-600">{pollError}</p>;
  }
  if (!job) {
    return <p className="text-slate-600">Loading...</p>;
  }

  if (job.status === "error") {
    return (
      <motion.div initial="hidden" animate="show" variants={fadeUp}>
        <h1 className="text-xl font-semibold mb-2">Audit failed</h1>
        <p className="text-slate-600 mb-1">{job.url}</p>
        <p className="text-red-600">{job.error}</p>
      </motion.div>
    );
  }

  if (job.status === "pending" || job.status === "running") {
    const currentIndex = PHASES.findIndex(([key]) => key === job.phase);
    return (
      <motion.div initial="hidden" animate="show" variants={fadeUp}>
        <h1 className="text-xl font-semibold mb-1">Auditing {job.url}</h1>
        <p className="text-slate-600 dark:text-slate-300 mb-6">This can take a minute or two for larger sites.</p>
        <ol className="flex flex-col gap-2">
          {PHASES.map(([key, label], i) => {
            const done = currentIndex > i;
            const active = key === job.phase;
            return (
              <li key={key} className="flex items-center gap-3 text-sm">
                <span className="relative h-2.5 w-2.5 rounded-full bg-slate-200 dark:bg-slate-700 overflow-hidden">
                  {(active || done) && (
                    <motion.span
                      layoutId={`phase-dot-${i}`}
                      className="absolute inset-0 rounded-full"
                      style={{ background: done ? "#22c55e" : "linear-gradient(90deg, var(--brand-1), var(--brand-2))" }}
                      animate={active ? { scale: [1, 1.35, 1] } : { scale: 1 }}
                      transition={active ? { duration: 1.2, repeat: Infinity, ease: "easeInOut" } : { duration: 0.3 }}
                    />
                  )}
                </span>
                <span className={active ? "font-medium" : "text-slate-500"}>{label}</span>
              </li>
            );
          })}
        </ol>
        {pollError && <p className="text-sm text-amber-600 mt-4">{pollError} - retrying...</p>}
      </motion.div>
    );
  }

  // status === "done"
  const activeMarkdown = majorOnly ? job.report_markdown_major_only ?? job.report_markdown : job.report_markdown;

  return (
    <motion.div initial="hidden" animate="show" variants={staggerContainer}>
      <motion.div variants={staggerItem} className="flex items-center gap-2 mb-1">
        <h1 className="text-xl font-semibold">
          {job.is_delta ? "Delta report" : "Audit report"}
        </h1>
        {job.is_delta && (
          <span className="text-xs font-medium bg-blue-100 text-blue-800 rounded-full px-2.5 py-0.5">
            changes since last run
          </span>
        )}
      </motion.div>
      <motion.p variants={staggerItem} className="text-slate-600 dark:text-slate-300 mb-4">
        {job.url}
      </motion.p>

      <motion.div variants={staggerItem} className="flex flex-wrap gap-4 mb-6 text-sm">
        <Stat
          label="GMC suspension risk"
          value={job.suspension_risk_count ?? "-"}
          highlight={!!job.suspension_risk_count}
          emphasize
        />
        <Stat label="Platform" value={job.platform ?? "unknown"} />
        <Stat label="Pages crawled" value={job.pages_crawled ?? "-"} />
        <Stat label="Findings" value={job.findings_count ?? "-"} />
        <Stat label="Critical" value={job.critical_count ?? "-"} highlight={!!job.critical_count} />
      </motion.div>

      <motion.div variants={staggerItem} className="glass-card rounded-xl px-4 py-3 mb-6">
        <MajorOnlyToggle checked={majorOnly} onChange={setMajorOnly} />
      </motion.div>

      <motion.div variants={staggerItem} className="flex flex-wrap gap-3 mb-8">
        <DownloadButton href={reportDownloadUrl(jobId, "md", majorOnly)}>Download Markdown</DownloadButton>
        <DownloadButton href={reportDownloadUrl(jobId, "docx", majorOnly)}>Download Word (.docx)</DownloadButton>
        <DownloadButton href={reportDownloadUrl(jobId, "pdf", majorOnly)}>Download PDF</DownloadButton>
      </motion.div>

      <motion.div variants={staggerItem} className="glass-card rounded-xl p-4 mb-8 max-h-[600px] overflow-y-auto">
        <AnimatePresence mode="wait">
          <motion.div
            key={majorOnly ? "major" : "full"}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
          >
            <ReportView markdown={activeMarkdown ?? ""} />
          </motion.div>
        </AnimatePresence>
      </motion.div>

      {!job.is_delta && (
        <motion.div variants={staggerItem} className="border-t border-surface-border pt-6">
          <h2 className="text-lg font-semibold mb-3">Register this store for monitoring</h2>
          <RegisterMonitoringForm defaultUrl={job.url} />
        </motion.div>
      )}
    </motion.div>
  );
}

function Stat({
  label, value, highlight, emphasize,
}: { label: string; value: string | number; highlight?: boolean; emphasize?: boolean }) {
  return (
    <motion.div
      whileHover={{ y: -2 }}
      className={`glass-card rounded-lg px-3 py-2 min-w-[7rem] ${emphasize && highlight ? "ring-2 ring-red-400/60" : ""}`}
    >
      <div className="text-slate-500 dark:text-slate-400 text-xs">{label}</div>
      <div className={`font-semibold ${highlight ? "text-red-600" : ""} ${emphasize ? "text-lg" : ""}`}>{value}</div>
    </motion.div>
  );
}

function DownloadButton({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <motion.a
      href={href}
      whileHover={{ scale: 1.03, y: -1 }}
      whileTap={{ scale: 0.97 }}
      className="border border-surface-border rounded-lg px-3 py-1.5 text-sm font-medium hover:bg-brand-1-soft transition-colors"
    >
      {children}
    </motion.a>
  );
}
