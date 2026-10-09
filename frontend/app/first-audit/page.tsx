"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { motion } from "framer-motion";
import { createFirstAudit } from "@/lib/admin-api";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { fadeUp } from "@/lib/motion";
import ScrollReveal from "@/components/ScrollReveal";
import SpotlightCard from "@/components/SpotlightCard";

export default function FirstAuditHomePage() {
  const router = useRouter();
  const { admin } = useAuth();
  const [url, setUrl] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!url.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      const { run_id } = await createFirstAudit(url.trim());
      router.push(`/first-audit/${run_id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the first-audit service. Is the backend running?");
      setSubmitting(false);
    }
  }

  return (
    <div className="max-w-xl">
      <motion.h1 initial="hidden" animate="show" variants={fadeUp} className="text-2xl font-semibold tracking-tight mb-1">
        Run a <span className="gradient-text">First Audit</span>
      </motion.h1>
      <motion.p
        initial="hidden"
        animate="show"
        variants={fadeUp}
        transition={{ delay: 0.05 }}
        className="text-slate-500 dark:text-slate-400 mb-6 text-sm"
      >
        Signed in as {admin?.email}. Discovery, fact extraction, rule evaluation, and a compliance
        snapshot PDF - website-only (no checkout/order data).
      </motion.p>

      <ScrollReveal delay={0.1}>
        <SpotlightCard className="p-6">
          <form onSubmit={handleSubmit} className="flex flex-col gap-3">
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

            <motion.button
              type="submit"
              disabled={submitting}
              whileHover={{ scale: submitting ? 1 : 1.02 }}
              whileTap={{ scale: submitting ? 1 : 0.98 }}
              className="text-white rounded-lg px-4 py-2 font-medium disabled:opacity-50 disabled:cursor-not-allowed w-fit mt-2"
              style={{ background: "linear-gradient(90deg, var(--brand-1), var(--brand-2))" }}
            >
              {submitting ? "Starting audit..." : "Run First Audit"}
            </motion.button>
            {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
          </form>
        </SpotlightCard>
      </ScrollReveal>
    </div>
  );
}
