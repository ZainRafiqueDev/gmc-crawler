"use client";

import { useState } from "react";
import Link from "next/link";
import { motion, AnimatePresence } from "framer-motion";
import { registerStore, ApiError, MonitoredStore } from "@/lib/api";

export default function RegisterMonitoringForm({ defaultUrl }: { defaultUrl: string }) {
  const [mode, setMode] = useState<"interval" | "on_change" | "both">("interval");
  const [intervalDays, setIntervalDays] = useState(7);
  const [onPolicyChange, setOnPolicyChange] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<MonitoredStore | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    setResult(null);
    try {
      const store = await registerStore({
        url: defaultUrl,
        mode,
        interval_days: mode !== "on_change" ? intervalDays : undefined,
        on_policy_change: onPolicyChange,
      });
      setResult(store);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the monitoring service.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <AnimatePresence mode="wait">
      {result ? (
        <motion.div
          key="result"
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          className="bg-green-50 border border-green-200 rounded-xl p-4 text-sm"
        >
          <p className="text-green-800 font-medium mb-1">
            Registered for monitoring (store #{result.id}).
          </p>
          <p className="text-green-700">
            {result.url} will be checked in <strong>{result.mode}</strong> mode
            {result.interval_days ? ` every ${result.interval_days} day(s)` : ""}.
            {result.on_policy_change ? " It will also re-audit whenever a tracked GMC policy page changes." : ""}
          </p>
          <Link href="/monitor" className="text-green-800 underline mt-2 inline-block">
            View monitored stores
          </Link>
        </motion.div>
      ) : (
        <motion.form
          key="form"
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          onSubmit={handleSubmit}
          className="glass-card rounded-xl p-4 flex flex-col gap-3 max-w-sm"
        >
          <div>
            <label className="text-sm font-medium block mb-1">Mode</label>
            <select
              value={mode}
              onChange={(e) => setMode(e.target.value as typeof mode)}
              className="gradient-ring border border-surface-border bg-surface rounded-lg px-3 py-2 w-full focus:outline-none"
            >
              <option value="interval">Interval (full re-audit on a schedule)</option>
              <option value="on_change">On change (cheap check, audit when policy pages change)</option>
              <option value="both">Both</option>
            </select>
          </div>
          {mode !== "on_change" && (
            <div>
              <label className="text-sm font-medium block mb-1">Interval (days)</label>
              <input
                type="number"
                min={1}
                value={intervalDays}
                onChange={(e) => setIntervalDays(Number(e.target.value))}
                className="gradient-ring border border-surface-border bg-surface rounded-lg px-3 py-2 w-full focus:outline-none"
              />
            </div>
          )}
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={onPolicyChange}
              onChange={(e) => setOnPolicyChange(e.target.checked)}
              className="rounded border-surface-border"
            />
            Also re-audit whenever Google updates its official Merchant Center policy pages
          </label>
          <motion.button
            type="submit"
            disabled={submitting}
            whileHover={{ scale: submitting ? 1 : 1.02 }}
            whileTap={{ scale: submitting ? 1 : 0.98 }}
            className="text-white rounded-lg px-4 py-2 font-medium disabled:opacity-50 w-fit shadow-lg shadow-indigo-500/20"
            style={{ background: "linear-gradient(90deg, var(--brand-1), var(--brand-2))" }}
          >
            {submitting ? "Registering..." : "Register for monitoring"}
          </motion.button>
          {error && <p className="text-sm text-red-600">{error}</p>}
        </motion.form>
      )}
    </AnimatePresence>
  );
}
