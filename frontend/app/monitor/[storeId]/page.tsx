"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { motion, AnimatePresence } from "framer-motion";
import { getLatestReport, rerunStoreAudit, latestReportDownloadUrl, LatestReport, ApiError, listStoreRuns, getStoreRun, AuditRunSummary, AuditRunDetail } from "@/lib/api";
import { fadeUp, staggerContainer, staggerItem } from "@/lib/motion";
import ReportView from "@/components/ReportView";
import MajorOnlyToggle from "@/components/MajorOnlyToggle";

function triggerLabel(trigger: string): string {
  if (trigger.startsWith("policy_change:")) {
    const areas = trigger
      .slice("policy_change:".length)
      .split(",")
      .map((a) => a.replace(/_/g, " "))
      .join(", ");
    return `Policy change (${areas})`;
  }
  if (trigger === "interval") return "Scheduled";
  if (trigger === "on_change") return "Store change detected";
  if (trigger === "manual") return "Manual re-run";
  return trigger;
}

export default function StoreLatestReportPage() {
  const { storeId } = useParams<{ storeId: string }>();
  const router = useRouter();
  const [report, setReport] = useState<LatestReport | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [rerunning, setRerunning] = useState(false);
  const [rerunError, setRerunError] = useState<string | null>(null);
  const [majorOnly, setMajorOnly] = useState(true);

  const [runs, setRuns] = useState<AuditRunSummary[] | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const [selectedRun, setSelectedRun] = useState<AuditRunDetail | null>(null);
  const [selectedRunLoading, setSelectedRunLoading] = useState(false);
  const [selectedRunError, setSelectedRunError] = useState<string | null>(null);
  const [showDelta, setShowDelta] = useState(false);

  useEffect(() => {
    getLatestReport(Number(storeId))
      .then(setReport)
      .catch((err) =>
        setLoadError(
          err instanceof ApiError && err.status === 404
            ? "No completed report yet for this store."
            : "Could not reach the monitoring service."
        )
      );
    listStoreRuns(Number(storeId))
      .then(setRuns)
      .catch(() => setRuns([]));
  }, [storeId]);

  async function handleRerun() {
    setRerunning(true);
    setRerunError(null);
    try {
      const { job_id } = await rerunStoreAudit(Number(storeId));
      router.push(`/report/${job_id}`);
    } catch (err) {
      setRerunError(
        err instanceof ApiError
          ? `${err.message}${err.status === 429 ? " (rate limit)" : ""}`
          : "Could not start the re-run."
      );
      setRerunning(false);
    }
  }

  async function handleSelectRun(runId: number) {
    setSelectedRunId(runId);
    setSelectedRun(null);
    setSelectedRunError(null);
    setShowDelta(false);
    setSelectedRunLoading(true);
    try {
      const run = await getStoreRun(Number(storeId), runId);
      setSelectedRun(run);
    } catch (err) {
      setSelectedRunError(err instanceof ApiError ? err.message : "Could not load this historical run.");
    } finally {
      setSelectedRunLoading(false);
    }
  }

  function backToLatest() {
    setSelectedRunId(null);
    setSelectedRun(null);
    setSelectedRunError(null);
  }

  const activeMarkdown = report
    ? majorOnly
      ? report.report_markdown_major_only ?? report.report_markdown
      : report.report_markdown
    : null;

  const historicalMarkdown = selectedRun
    ? showDelta
      ? selectedRun.delta_markdown_major_only ?? selectedRun.delta_markdown ?? selectedRun.report_markdown
      : majorOnly
        ? selectedRun.report_markdown_major_only ?? selectedRun.report_markdown
        : selectedRun.report_markdown
    : null;

  return (
    <motion.div initial="hidden" animate="show" variants={staggerContainer}>
      <motion.div variants={staggerItem} className="flex items-center justify-between mb-1">
        <h1 className="text-xl font-semibold">{selectedRunId ? "Historical report" : "Latest report"}</h1>
        <motion.button
          onClick={handleRerun}
          disabled={rerunning}
          whileHover={{ scale: rerunning ? 1 : 1.03 }}
          whileTap={{ scale: rerunning ? 1 : 0.97 }}
          className="border border-surface-border rounded-lg px-3 py-1.5 text-sm font-medium hover:bg-brand-1-soft disabled:opacity-50 transition-colors"
        >
          {rerunning ? "Starting audit..." : "Re-run audit now"}
        </motion.button>
      </motion.div>
      {rerunError && <p className="text-sm text-red-600 mb-4">{rerunError}</p>}

      {loadError && (
        <motion.p variants={fadeUp} className="text-slate-600 mt-4">
          {loadError}
        </motion.p>
      )}
      {!loadError && !report && <p className="text-slate-600 mt-4">Loading...</p>}

      {selectedRunId && (
        <motion.div variants={staggerItem} className="glass-card rounded-xl px-4 py-3 mb-4 flex items-center justify-between">
          <p className="text-sm text-slate-600 dark:text-slate-300">
            Showing run #{selectedRunId} - not the latest.
          </p>
          <button onClick={backToLatest} className="text-sm font-medium underline">
            Back to latest report
          </button>
        </motion.div>
      )}

      {!selectedRunId && report && (
        <>
          <motion.p variants={staggerItem} className="text-slate-600 dark:text-slate-300 mb-4">
            {report.run_type} audit, triggered by {triggerLabel(report.trigger)} - started{" "}
            {new Date(report.started_at).toLocaleString()}
            {report.finished_at ? `, finished ${new Date(report.finished_at).toLocaleString()}` : ""}
            {" - "}
            {report.findings_count} finding(s)
          </motion.p>

          <motion.div variants={staggerItem} className="glass-card rounded-xl px-4 py-3 mb-6">
            <MajorOnlyToggle checked={majorOnly} onChange={setMajorOnly} />
          </motion.div>

          <motion.div variants={staggerItem} className="flex flex-wrap gap-3 mb-6">
            <DownloadButton href={latestReportDownloadUrl(Number(storeId), "md", majorOnly)}>Download Markdown</DownloadButton>
            <DownloadButton href={latestReportDownloadUrl(Number(storeId), "docx", majorOnly)}>Download Word (.docx)</DownloadButton>
            <DownloadButton href={latestReportDownloadUrl(Number(storeId), "pdf", majorOnly)}>Download PDF</DownloadButton>
          </motion.div>

          <motion.div variants={staggerItem} className="glass-card rounded-xl p-4">
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
        </>
      )}

      {selectedRunId && (
        <>
          {selectedRunLoading && <p className="text-slate-600">Loading run #{selectedRunId}...</p>}
          {selectedRunError && <p className="text-sm text-red-600">{selectedRunError}</p>}
          {selectedRun && (
            <>
              <motion.p variants={staggerItem} className="text-slate-600 dark:text-slate-300 mb-4">
                {selectedRun.run_type} audit, triggered by {triggerLabel(selectedRun.trigger)} - started{" "}
                {new Date(selectedRun.started_at).toLocaleString()}
                {" - "}
                {selectedRun.findings_count} finding(s)
              </motion.p>

              <motion.div variants={staggerItem} className="glass-card rounded-xl px-4 py-3 mb-6 flex flex-wrap items-center gap-4">
                <MajorOnlyToggle checked={majorOnly} onChange={setMajorOnly} />
                {selectedRun.delta_markdown && (
                  <label className="flex items-center gap-2 text-sm">
                    <input type="checkbox" checked={showDelta} onChange={(e) => setShowDelta(e.target.checked)} />
                    Show delta vs. previous run
                  </label>
                )}
                {!selectedRun.delta_markdown && (
                  <span className="text-sm text-slate-500">No delta available (first run for this store).</span>
                )}
              </motion.div>

              <motion.div variants={staggerItem} className="glass-card rounded-xl p-4">
                <ReportView markdown={historicalMarkdown ?? ""} />
              </motion.div>
            </>
          )}
        </>
      )}

      <motion.div variants={staggerItem} className="mt-10">
        <h2 className="text-lg font-semibold mb-2">Audit history</h2>
        {runs === null && <p className="text-slate-600">Loading history...</p>}
        {runs !== null && runs.length === 0 && <p className="text-slate-600">No runs recorded yet.</p>}
        {runs !== null && runs.length > 0 && (
          <p className="text-xs text-slate-500 mb-2">
            Showing the {runs.length} most recently retained run(s) for this store - older runs are pruned per the retention policy.
          </p>
        )}
        <ul className="flex flex-col gap-2">
          {runs?.map((run) => (
            <li key={run.id}>
              <button
                onClick={() => handleSelectRun(run.id)}
                className={`w-full text-left glass-card rounded-lg px-3 py-2 text-sm transition-colors hover:bg-brand-1-soft ${
                  selectedRunId === run.id ? "ring-2 ring-brand-1" : ""
                }`}
              >
                <span className="font-medium">{new Date(run.started_at).toLocaleString()}</span>
                {" - "}
                {triggerLabel(run.trigger)}
                {" - "}
                {run.findings_count} finding(s)
                {run.critical_count > 0 ? `, ${run.critical_count} critical` : ""}
                {run.suspension_risk_count > 0 ? `, ${run.suspension_risk_count} suspension-risk` : ""}
                {run.has_delta ? " - delta available" : ""}
              </button>
            </li>
          ))}
        </ul>
      </motion.div>
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
