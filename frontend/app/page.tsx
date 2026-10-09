"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { motion } from "framer-motion";
import { createAudit, listStores, ApiError } from "@/lib/api";
import { fadeUp, staggerContainer, staggerItem } from "@/lib/motion";
import ScrollReveal from "@/components/ScrollReveal";
import SpotlightCard from "@/components/SpotlightCard";
import AnimatedCounter from "@/components/AnimatedCounter";

const DEFAULT_MAX_PAGES = 25;

const FEATURES = [
  {
    title: "Ad-hoc audit",
    desc: "Crawl any store once, run deterministic + AI-graded checks against GMC policy, get a report.",
    href: null,
  },
  {
    title: "Monitored stores",
    desc: "Register a store for recurring or on-change re-audits, with full run history and delta reports.",
    href: "/monitor",
  },
  {
    title: "First Audit",
    desc: "A deeper, rule-engine-driven compliance snapshot: fact extraction, contradiction detection, a signed-off PDF.",
    href: "/first-audit",
  },
];

export default function HomePage() {
  const router = useRouter();
  const [url, setUrl] = useState("");
  const [maxPages, setMaxPages] = useState(DEFAULT_MAX_PAGES);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [storeCount, setStoreCount] = useState<number | null>(null);

  useEffect(() => {
    listStores()
      .then((stores) => setStoreCount(stores.length))
      .catch(() => setStoreCount(null));
  }, []);

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
    <div className="flex flex-col gap-16">
      {/* Hero */}
      <section className="text-center max-w-2xl mx-auto pt-6">
        <motion.h1
          initial="hidden"
          animate="show"
          variants={fadeUp}
          className="text-4xl sm:text-5xl font-semibold tracking-tight mb-4"
        >
          Keep every store <span className="gradient-text">GMC-compliant</span>
        </motion.h1>
        <motion.p
          initial="hidden"
          animate="show"
          variants={fadeUp}
          transition={{ delay: 0.05 }}
          className="text-slate-500 dark:text-slate-400 text-lg"
        >
          Crawl, extract facts, detect contradictions, and grade against real Google Merchant
          Center policy - one-off, monitored, or a full rule-engine compliance snapshot.
        </motion.p>

        <motion.div
          initial="hidden"
          animate="show"
          variants={staggerContainer}
          className="flex justify-center gap-6 mt-8 text-sm"
        >
          <motion.div variants={staggerItem} className="flex flex-col items-center">
            <span className="text-2xl font-semibold gradient-text">
              {storeCount !== null ? <AnimatedCounter value={storeCount} /> : "-"}
            </span>
            <span className="text-slate-500">stores monitored</span>
          </motion.div>
          <motion.div variants={staggerItem} className="flex flex-col items-center">
            <span className="text-2xl font-semibold gradient-text">3</span>
            <span className="text-slate-500">audit modes</span>
          </motion.div>
        </motion.div>
      </section>

      {/* Feature cards */}
      <ScrollReveal>
        <div className="grid sm:grid-cols-3 gap-4">
          {FEATURES.map((f) => {
            const card = (
              <SpotlightCard className="p-5 h-full">
                <h3 className="font-semibold mb-1.5">{f.title}</h3>
                <p className="text-sm text-slate-500 dark:text-slate-400">{f.desc}</p>
              </SpotlightCard>
            );
            return f.href ? (
              <Link key={f.title} href={f.href as "/monitor" | "/first-audit"} className="block h-full">
                {card}
              </Link>
            ) : (
              <div key={f.title}>{card}</div>
            );
          })}
        </div>
      </ScrollReveal>

      {/* Audit trigger form */}
      <ScrollReveal delay={0.05}>
        <div className="max-w-xl mx-auto w-full">
          <h2 className="text-xl font-semibold mb-4 text-center">Run an ad-hoc audit</h2>
          <form onSubmit={handleSubmit} className="gradient-border glass-card rounded-2xl p-6 flex flex-col gap-3">
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
              className="text-white rounded-lg px-4 py-2 font-medium disabled:opacity-50 disabled:cursor-not-allowed w-fit mt-2"
              style={{ background: "linear-gradient(90deg, var(--brand-1), var(--brand-2))" }}
            >
              {submitting ? "Starting audit..." : "Run Audit"}
            </motion.button>
            {error && (
              <motion.p initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="text-sm text-red-600 dark:text-red-400">
                {error}
              </motion.p>
            )}
          </form>
        </div>
      </ScrollReveal>
    </div>
  );
}
