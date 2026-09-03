// Thin client for the FastAPI backend (app/api/main.py). Types mirror
// app/api/schemas.py - kept minimal and hand-written since the backend
// surface is small and stable rather than pulling in an OpenAPI codegen step.

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8010";

export type AuditJobStatus = {
  job_id: string;
  url: string;
  status: "pending" | "running" | "done" | "error";
  phase: string | null;
  phase_label: string | null;
  error: string | null;
  platform: string | null;
  pages_crawled: number | null;
  findings_count: number | null;
  critical_count: number | null;
  suspension_risk_count: number | null;
  report_markdown: string | null;
  report_markdown_major_only: string | null;
  is_delta: boolean;
  created_at: string;
};

export type MonitoredStore = {
  id: number;
  url: string;
  mode: "interval" | "on_change" | "both";
  interval_days: number | null;
  cheap_check_interval_days: number | null;
  on_policy_change: boolean;
  created_at: string;
  last_full_audit_at: string | null;
  last_cheap_check_at: string | null;
  has_report: boolean;
};

export type LatestReport = {
  store_id: number;
  run_type: string;
  trigger: string;
  started_at: string;
  finished_at: string | null;
  report_markdown: string;
  report_markdown_major_only: string | null;
  findings_count: number;
};

// Audit-history follow-up (Part 2.1)
export type AuditRunSummary = {
  id: number;
  run_type: string;
  trigger: string;
  started_at: string;
  finished_at: string | null;
  change_detected: boolean;
  findings_count: number;
  critical_count: number;
  suspension_risk_count: number;
  has_delta: boolean;
};

export type AuditRunDetail = {
  id: number;
  store_id: number;
  run_type: string;
  trigger: string;
  started_at: string;
  finished_at: string | null;
  report_markdown: string;
  report_markdown_major_only: string | null;
  delta_markdown: string | null;
  delta_markdown_major_only: string | null;
  findings_count: number;
};

async function asJson<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {
      // response body wasn't JSON - fall back to statusText
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function createAudit(url: string, maxPages?: number): Promise<{ job_id: string }> {
  const res = await fetch(`${API_BASE}/api/audits`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url, max_pages: maxPages }),
  });
  return asJson(res);
}

export async function getAuditStatus(jobId: string): Promise<AuditJobStatus> {
  const res = await fetch(`${API_BASE}/api/audits/${jobId}`);
  return asJson(res);
}

export function reportDownloadUrl(jobId: string, format: "md" | "docx" | "pdf", majorOnly = false): string {
  return `${API_BASE}/api/audits/${jobId}/report.${format}${majorOnly ? "?major_only=true" : ""}`;
}

export function latestReportDownloadUrl(storeId: number, format: "md" | "docx" | "pdf", majorOnly = false): string {
  return `${API_BASE}/api/monitor/stores/${storeId}/latest-report.${format}${majorOnly ? "?major_only=true" : ""}`;
}

export async function registerStore(params: {
  url: string;
  mode: "interval" | "on_change" | "both";
  interval_days?: number;
  cheap_check_interval_days?: number;
  on_policy_change?: boolean;
}): Promise<MonitoredStore> {
  const res = await fetch(`${API_BASE}/api/monitor/stores`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  return asJson(res);
}

export async function listStores(): Promise<MonitoredStore[]> {
  const res = await fetch(`${API_BASE}/api/monitor/stores`);
  return asJson(res);
}

export async function removeStore(storeId: number): Promise<void> {
  const res = await fetch(`${API_BASE}/api/monitor/stores/${storeId}`, { method: "DELETE" });
  if (!res.ok && res.status !== 204) {
    throw new ApiError(res.status, res.statusText);
  }
}

export async function getLatestReport(storeId: number): Promise<LatestReport> {
  const res = await fetch(`${API_BASE}/api/monitor/stores/${storeId}/latest-report`);
  return asJson(res);
}

export async function rerunStoreAudit(storeId: number): Promise<{ job_id: string }> {
  const res = await fetch(`${API_BASE}/api/monitor/stores/${storeId}/rerun`, { method: "POST" });
  return asJson(res);
}

export async function listStoreRuns(storeId: number): Promise<AuditRunSummary[]> {
  const res = await fetch(`${API_BASE}/api/monitor/stores/${storeId}/runs`);
  return asJson(res);
}

export async function getStoreRun(storeId: number, runId: number): Promise<AuditRunDetail> {
  const res = await fetch(`${API_BASE}/api/monitor/stores/${storeId}/runs/${runId}`);
  return asJson(res);
}
