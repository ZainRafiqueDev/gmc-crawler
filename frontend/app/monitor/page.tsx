"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { motion, AnimatePresence } from "framer-motion";
import { listStores, removeStore, rerunStoreAudit, MonitoredStore, ApiError } from "@/lib/api";
import { fadeUp, staggerContainer, staggerItem } from "@/lib/motion";
import AnimatedCounter from "@/components/AnimatedCounter";

function handleSpotlight(e: React.MouseEvent<HTMLElement>) {
  const el = e.currentTarget;
  const rect = el.getBoundingClientRect();
  el.style.setProperty("--spot-x", `${e.clientX - rect.left}px`);
  el.style.setProperty("--spot-y", `${e.clientY - rect.top}px`);
}

export default function MonitorPage() {
  const router = useRouter();
  const [stores, setStores] = useState<MonitoredStore[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [removingId, setRemovingId] = useState<number | null>(null);
  const [rerunningId, setRerunningId] = useState<number | null>(null);

  async function load() {
    try {
      const result = await listStores();
      setStores(result);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the monitoring service.");
    }
  }

  useEffect(() => {
    let cancelled = false;
    listStores()
      .then((result) => {
        if (cancelled) return;
        setStores(result);
        setError(null);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : "Could not reach the monitoring service.");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleRemove(id: number) {
    setRemovingId(id);
    try {
      await removeStore(id);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not remove store.");
    } finally {
      setRemovingId(null);
    }
  }

  async function handleRerun(id: number) {
    setRerunningId(id);
    setError(null);
    try {
      const { job_id } = await rerunStoreAudit(id);
      router.push(`/report/${job_id}`);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `${err.message}${err.status === 429 ? " (rate limit)" : ""}`
          : "Could not start the re-run."
      );
      setRerunningId(null);
    }
  }

  return (
    <div>
      <motion.div initial="hidden" animate="show" variants={fadeUp} className="flex items-baseline justify-between mb-4 flex-wrap gap-2">
        <h1 className="text-xl font-semibold">Monitored stores</h1>
        {stores && stores.length > 0 && (
          <span className="text-sm text-slate-500 dark:text-slate-400">
            <AnimatedCounter value={stores.length} className="gradient-text font-semibold" /> tracked
          </span>
        )}
      </motion.div>
      {error && <p className="text-sm text-red-600 dark:text-red-400 mb-4">{error}</p>}
      {!stores && !error && <p className="text-slate-500 dark:text-slate-400">Loading...</p>}
      {stores && stores.length === 0 && (
        <motion.p initial="hidden" animate="show" variants={fadeUp} className="text-slate-500 dark:text-slate-400">
          No stores registered yet. Run an audit from the{" "}
          <Link href="/" className="underline gradient-text font-medium">
            home page
          </Link>{" "}
          and register it for monitoring from the report screen.
        </motion.p>
      )}
      {stores && stores.length > 0 && (
        <motion.div initial="hidden" animate="show" variants={staggerContainer} className="flex flex-col gap-3">
          <AnimatePresence>
            {stores.map((s) => (
              <motion.div
                key={s.id}
                variants={staggerItem}
                exit={{ opacity: 0, x: -12, transition: { duration: 0.2 } }}
                whileHover={{ y: -2 }}
                layout
                onMouseMove={handleSpotlight}
                className="spotlight-card glass-card rounded-xl p-4 flex flex-wrap items-center justify-between gap-3"
              >
                <div className="min-w-0">
                  <div className="font-medium truncate max-w-md">{s.url}</div>
                  <div className="text-xs text-slate-500 dark:text-slate-400 flex flex-wrap gap-x-3 mt-0.5">
                    <span>{s.mode}</span>
                    <span>{s.interval_days ? `every ${s.interval_days}d` : "no interval"}</span>
                    <span>
                      last audit: {s.last_full_audit_at ? new Date(s.last_full_audit_at).toLocaleString() : "never"}
                    </span>
                  </div>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  {s.has_report ? (
                    <Link
                      href={`/monitor/${s.id}`}
                      className="text-sm font-medium rounded-lg px-3 py-1.5 border border-surface-border hover:bg-brand-1-soft transition-colors"
                    >
                      View latest
                    </Link>
                  ) : (
                    <span className="text-xs text-slate-500 dark:text-slate-400 px-1">not yet available</span>
                  )}
                  <motion.button
                    whileHover={{ scale: 1.03 }}
                    whileTap={{ scale: 0.97 }}
                    onClick={() => handleRerun(s.id)}
                    disabled={rerunningId === s.id}
                    className="text-sm font-medium rounded-lg px-3 py-1.5 text-white disabled:opacity-50"
                    style={{ background: "linear-gradient(90deg, var(--brand-1), var(--brand-2))" }}
                  >
                    {rerunningId === s.id ? "Starting..." : "Re-run now"}
                  </motion.button>
                  <button
                    onClick={() => handleRemove(s.id)}
                    disabled={removingId === s.id}
                    className="text-sm text-red-600 dark:text-red-400 hover:underline disabled:opacity-50"
                  >
                    {removingId === s.id ? "Removing..." : "Remove"}
                  </button>
                </div>
              </motion.div>
            ))}
          </AnimatePresence>
        </motion.div>
      )}
    </div>
  );
}
