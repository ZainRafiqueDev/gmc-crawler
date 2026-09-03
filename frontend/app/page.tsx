"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { motion } from "framer-motion";
import { createAudit, ApiError } from "@/lib/api";
import { fadeUp } from "@/lib/motion";

const DEFAULT_MAX_PAGES = 25;

export default function HomePage() {
  const router = useRouter();
  const [url, setUrl] = useState("");
  const [maxPages, setMaxPages] = useState(DEFAULT_MAX_PAGES);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!url.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      const { job_id } = await createAudit(url.trim(), maxPages || undefined);
      router.push(`/report/${job_id}`);
    } catch (err) {
      const message =
        err instanceof ApiError
          ? `${err.message}${err.status === 429 ? " (rate limit)" : ""}`
          : "Could not reach the audit service. Is the backend running?";
      setError(message);
      setSubmitting(false);
    }
  }

  return (
    <div className="max-w-xl">
      <motion.h1
        initial="hidden"
        animate="show"
        variants={fadeUp}
        className="text-3xl font-semibold tracking-tight mb-2"
      >
        Run a <span className="gradient-text">GMC compliance</span> audit
      </motion.h1>
      <motion.p
        initial="hidden"
        animate="show"
        variants={fadeUp}
        transition={{ delay: 0.05 }}
        className="text-slate-600 dark:text-slate-300 mb-8"
      >
        Paste a live store URL below. We&apos;ll crawl it, run deterministic and AI-graded
        checks against Google Merchant Center policy, and produce a report.
      </motion.p>

      <motion.form
        initial="hidden"
        animate="show"
        variants={fadeUp}
        transition={{ delay: 0.1 }}
        onSubmit={handleSubmit}
        className="glass-card rounded-2xl p-6 flex flex-col gap-3"
      >
        <label htmlFor="url" className="text-sm font-medium">
          Store URL
        </label>
        <input
          id="url"
          type="url"
          required
          placeholder="https://example-store.com"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          className="gradient-ring border border-surface-border bg-surface rounded-lg px-3 py-2 focus:outline-none transition-shadow"
        />

        <label htmlFor="maxPages" className="text-sm font-medium mt-2">
          Max pages to crawl
        </label>
        <input
          id="maxPages"
          type="number"
          min={1}
          max={500}
          value={maxPages}
          onChange={(e) => setMaxPages(Number(e.target.value))}
          className="gradient-ring border border-surface-border bg-surface rounded-lg px-3 py-2 w-32 focus:outline-none transition-shadow"
        />
        <p className="text-xs text-slate-500 -mt-2">
          Bounds crawl time and AI-check cost. Raise it for a deeper audit.
        </p>

        <motion.button
          type="submit"
          disabled={submitting}
          whileHover={{ scale: submitting ? 1 : 1.02 }}
          whileTap={{ scale: submitting ? 1 : 0.98 }}
          className="text-white rounded-lg px-4 py-2 font-medium disabled:opacity-50 disabled:cursor-not-allowed w-fit mt-2 shadow-lg shadow-indigo-500/20"
          style={{ background: "linear-gradient(90deg, var(--brand-1), var(--brand-2))" }}
        >
          {submitting ? "Starting audit..." : "Run Audit"}
        </motion.button>
        {error && (
          <motion.p initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="text-sm text-red-600">
            {error}
          </motion.p>
        )}
      </motion.form>
    </div>
  );
}
