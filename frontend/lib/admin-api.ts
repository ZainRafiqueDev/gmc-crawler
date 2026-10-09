// Thin client for the admin session (app/api/auth.py) and First Audit
// (app/api/first_audit.py) routes. Every route in this whole app is now
// require_admin-gated on the backend (both the old /api/audits + /api/monitor
// pipeline and First Audit), but lib/api.ts's calls still go through plain
// fetch - the session cookie just has to already be in the browser's jar for
// those to succeed, which only this module's `login()` can put there.
// `credentials: "include"` is required on every call here: the API runs on a
// different origin (NEXT_PUBLIC_API_BASE_URL) than this frontend, so the
// browser won't attach/store the httponly session cookie without it.

import { API_BASE, ApiError, asJson } from "@/lib/api";

export type AdminUser = {
  email: string;
  role: string;
};

export type FirstAuditStatus = {
  run_id: number;
  url: string;
  status: "pending" | "running" | "done" | "error";
  platform: string | null;
  pages_crawled: number | null;
  unreachable_pages_count: number | null;
  snapshot_status: string | null;
  error: string | null;
};

export async function login(email: string, password: string): Promise<AdminUser> {
  const res = await fetch(`${API_BASE}/api/auth/login`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  return asJson(res);
}

export async function logout(): Promise<void> {
  const res = await fetch(`${API_BASE}/api/auth/logout`, { method: "POST", credentials: "include" });
  if (!res.ok && res.status !== 204) {
    throw new ApiError(res.status, res.statusText);
  }
}

export async function me(): Promise<AdminUser> {
  const res = await fetch(`${API_BASE}/api/auth/me`, { credentials: "include" });
  return asJson(res);
}

export async function createFirstAudit(url: string): Promise<{ run_id: number }> {
  const res = await fetch(`${API_BASE}/api/first-audit`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
  return asJson(res);
}

export async function getFirstAuditStatus(runId: number): Promise<FirstAuditStatus> {
  const res = await fetch(`${API_BASE}/api/first-audit/${runId}`, { credentials: "include" });
  return asJson(res);
}

// A plain anchor href, not a fetch: SameSite=Lax still attaches the session
// cookie on a top-level GET navigation (what a download link/click triggers),
// so no blob/fetch workaround is needed - same pattern as lib/api.ts's own
// reportDownloadUrl.
export function firstAuditReportPdfUrl(runId: number): string {
  return `${API_BASE}/api/first-audit/${runId}/report.pdf`;
}
