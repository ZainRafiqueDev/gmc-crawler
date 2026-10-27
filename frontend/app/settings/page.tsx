"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { motion } from "framer-motion";
import {
  requestPasswordChangeOtp, confirmPasswordChange,
  requestEmailChangeOtp, confirmEmailChange,
} from "@/lib/admin-api";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { fadeUp, staggerContainer, staggerItem } from "@/lib/motion";
import ScrollReveal from "@/components/ScrollReveal";
import SpotlightCard from "@/components/SpotlightCard";

const inputClass = "gradient-ring border border-surface-border bg-surface rounded-lg px-3 py-2 focus:outline-none transition-shadow";
const buttonClass = "text-white rounded-lg px-4 py-2 font-medium disabled:opacity-50 disabled:cursor-not-allowed w-fit mt-2";
const buttonStyle = { background: "linear-gradient(90deg, var(--brand-1), var(--brand-2))" };

function PasswordChangeCard() {
  const router = useRouter();
  const [step, setStep] = useState<"request" | "confirm">("request");
  const [currentPassword, setCurrentPassword] = useState("");
  const [otp, setOtp] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleRequest(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await requestPasswordChangeOtp(currentPassword);
      setStep("confirm");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the server.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleConfirm(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await confirmPasswordChange(currentPassword, otp, newPassword);
      // The backend invalidates every session for this account on a
      // successful change (including this one) - go straight to /login
      // rather than waiting for the next auth check to notice.
      router.push("/login");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the server.");
      setSubmitting(false);
    }
  }

  return (
    <SpotlightCard className="p-6">
      <h2 className="text-lg font-semibold mb-1">Change password</h2>
      <p className="text-sm text-slate-500 dark:text-slate-400 mb-4">
        A one-time code will be emailed to your registered address.
      </p>

      {step === "request" ? (
        <form onSubmit={handleRequest} className="flex flex-col gap-3">
          <label className="text-sm font-medium">Current password</label>
          <input
            type="password" required autoComplete="current-password"
            value={currentPassword} onChange={(e) => setCurrentPassword(e.target.value)}
            className={inputClass}
          />
          <motion.button
            type="submit" disabled={submitting}
            whileHover={{ scale: submitting ? 1 : 1.02 }} whileTap={{ scale: submitting ? 1 : 0.98 }}
            className={buttonClass} style={buttonStyle}
          >
            {submitting ? "Sending code..." : "Send code"}
          </motion.button>
        </form>
      ) : (
        <form onSubmit={handleConfirm} className="flex flex-col gap-3">
          <p className="text-sm text-green-700 dark:text-green-400">Code sent - check your registered email.</p>
          <label className="text-sm font-medium">6-digit code</label>
          <input
            type="text" required inputMode="numeric" pattern="[0-9]{6}" maxLength={6}
            value={otp} onChange={(e) => setOtp(e.target.value)}
            className={inputClass}
          />
          <label className="text-sm font-medium mt-2">New password</label>
          <input
            type="password" required minLength={8} autoComplete="new-password"
            value={newPassword} onChange={(e) => setNewPassword(e.target.value)}
            className={inputClass}
          />
          <motion.button
            type="submit" disabled={submitting}
            whileHover={{ scale: submitting ? 1 : 1.02 }} whileTap={{ scale: submitting ? 1 : 0.98 }}
            className={buttonClass} style={buttonStyle}
          >
            {submitting ? "Updating..." : "Update password"}
          </motion.button>
          <button type="button" onClick={() => setStep("request")} className="text-sm text-slate-500 dark:text-slate-400 underline w-fit">
            Start over
          </button>
        </form>
      )}
      {error && <p className="text-sm text-red-600 dark:text-red-400 mt-3">{error}</p>}
    </SpotlightCard>
  );
}

function EmailChangeCard() {
  const router = useRouter();
  const [step, setStep] = useState<"request" | "confirm">("request");
  const [currentPassword, setCurrentPassword] = useState("");
  const [newEmail, setNewEmail] = useState("");
  const [otp, setOtp] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleRequest(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await requestEmailChangeOtp(currentPassword, newEmail);
      setStep("confirm");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the server.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleConfirm(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await confirmEmailChange(currentPassword, otp);
      router.push("/login");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the server.");
      setSubmitting(false);
    }
  }

  return (
    <SpotlightCard className="p-6">
      <h2 className="text-lg font-semibold mb-1">Change email</h2>
      <p className="text-sm text-slate-500 dark:text-slate-400 mb-4">
        A one-time code will be emailed to your CURRENT address, not the new one, to confirm the change.
      </p>

      {step === "request" ? (
        <form onSubmit={handleRequest} className="flex flex-col gap-3">
          <label className="text-sm font-medium">Current password</label>
          <input
            type="password" required autoComplete="current-password"
            value={currentPassword} onChange={(e) => setCurrentPassword(e.target.value)}
            className={inputClass}
          />
          <label className="text-sm font-medium mt-2">New email</label>
          <input
            type="email" required
            value={newEmail} onChange={(e) => setNewEmail(e.target.value)}
            className={inputClass}
          />
          <motion.button
            type="submit" disabled={submitting}
            whileHover={{ scale: submitting ? 1 : 1.02 }} whileTap={{ scale: submitting ? 1 : 0.98 }}
            className={buttonClass} style={buttonStyle}
          >
            {submitting ? "Sending code..." : "Send code"}
          </motion.button>
        </form>
      ) : (
        <form onSubmit={handleConfirm} className="flex flex-col gap-3">
          <p className="text-sm text-green-700 dark:text-green-400">Code sent to your current email address.</p>
          <label className="text-sm font-medium">6-digit code</label>
          <input
            type="text" required inputMode="numeric" pattern="[0-9]{6}" maxLength={6}
            value={otp} onChange={(e) => setOtp(e.target.value)}
            className={inputClass}
          />
          <motion.button
            type="submit" disabled={submitting}
            whileHover={{ scale: submitting ? 1 : 1.02 }} whileTap={{ scale: submitting ? 1 : 0.98 }}
            className={buttonClass} style={buttonStyle}
          >
            {submitting ? "Updating..." : "Update email"}
          </motion.button>
          <button type="button" onClick={() => setStep("request")} className="text-sm text-slate-500 dark:text-slate-400 underline w-fit">
            Start over
          </button>
        </form>
      )}
      {error && <p className="text-sm text-red-600 dark:text-red-400 mt-3">{error}</p>}
    </SpotlightCard>
  );
}

export default function SettingsPage() {
  const { admin } = useAuth();

  return (
    <div className="max-w-2xl">
      <motion.h1 initial="hidden" animate="show" variants={fadeUp} className="text-2xl font-semibold tracking-tight mb-1">
        Account <span className="gradient-text">settings</span>
      </motion.h1>
      <motion.p initial="hidden" animate="show" variants={fadeUp} transition={{ delay: 0.05 }} className="text-slate-500 dark:text-slate-400 mb-6 text-sm">
        Signed in as {admin?.email}. Both changes below need your current password and a one-time code,
        and sign you out everywhere once applied.
      </motion.p>

      <ScrollReveal>
        <motion.div initial="hidden" animate="show" variants={staggerContainer} className="grid sm:grid-cols-2 gap-4">
          <motion.div variants={staggerItem}>
            <PasswordChangeCard />
          </motion.div>
          <motion.div variants={staggerItem}>
            <EmailChangeCard />
          </motion.div>
        </motion.div>
      </ScrollReveal>
    </div>
  );
}
