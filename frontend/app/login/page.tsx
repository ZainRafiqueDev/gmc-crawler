"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { motion } from "framer-motion";
import { login } from "@/lib/admin-api";
import { ApiError } from "@/lib/api";
import { fadeUp } from "@/lib/motion";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await login(email, password);
      router.push("/");
      router.refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the auth service. Is the backend running?");
      setSubmitting(false);
    }
  }

  return (
    <motion.div initial="hidden" animate="show" variants={fadeUp} className="w-full max-w-sm">
      <div className="text-center mb-8">
        <h1 className="text-2xl font-semibold tracking-tight gradient-text mb-1">GMC Compliance Checker</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400">Admin access only</p>
      </div>

      <form onSubmit={handleSubmit} className="gradient-border glass-card rounded-2xl p-6 flex flex-col gap-3">
        <label htmlFor="email" className="text-sm font-medium">
          Email
        </label>
        <input
          id="email"
          type="email"
          required
          autoComplete="username"
          autoFocus
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className="gradient-ring border border-surface-border bg-surface rounded-lg px-3 py-2 focus:outline-none transition-shadow"
        />

        <label htmlFor="password" className="text-sm font-medium mt-2">
          Password
        </label>
        <input
          id="password"
          type="password"
          required
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="gradient-ring border border-surface-border bg-surface rounded-lg px-3 py-2 focus:outline-none transition-shadow"
        />

        <motion.button
          type="submit"
          disabled={submitting}
          whileHover={{ scale: submitting ? 1 : 1.02 }}
          whileTap={{ scale: submitting ? 1 : 0.98 }}
          className="text-white rounded-lg px-4 py-2.5 font-medium disabled:opacity-50 disabled:cursor-not-allowed mt-3"
          style={{ background: "linear-gradient(90deg, var(--brand-1), var(--brand-2))" }}
        >
          {submitting ? "Logging in..." : "Log in"}
        </motion.button>
        {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
      </form>
    </motion.div>
  );
}
