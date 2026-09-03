"""Pydantic request/response models for the frontend API. Deliberately
minimal - matches the frontend's actual four screens, not a general-purpose
API surface (Phase G's dashboard may need more).
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class CreateAuditRequest(BaseModel):
    url: str
    max_pages: int | None = None
    max_depth: int | None = None


class CreateAuditResponse(BaseModel):
    job_id: str


class AuditJobStatus(BaseModel):
    job_id: str
    url: str
    status: str  # "pending" | "running" | "done" | "error"
    phase: str | None = None
    phase_label: str | None = None
    error: str | None = None
    platform: str | None = None
    pages_crawled: int | None = None
    findings_count: int | None = None
    critical_count: int | None = None
    suspension_risk_count: int | None = None
    report_markdown: str | None = None
    report_markdown_major_only: str | None = None
    is_delta: bool = False
    created_at: datetime


class RegisterStoreRequest(BaseModel):
    url: str
    mode: str  # "interval" | "on_change" | "both"
    interval_days: int | None = None
    cheap_check_interval_days: int | None = None
    wc_consumer_key: str | None = None
    wc_consumer_secret: str | None = None
    # Independent of `mode` - combinable with any of interval/on_change/both
    # (audit-history + policy-change-triggered re-audits follow-up, Part 2.2).
    on_policy_change: bool = False


class MonitoredStoreResponse(BaseModel):
    id: int
    url: str
    mode: str
    interval_days: int | None
    cheap_check_interval_days: int | None
    on_policy_change: bool
    created_at: datetime
    last_full_audit_at: datetime | None
    last_cheap_check_at: datetime | None
    has_report: bool


class LatestReportResponse(BaseModel):
    store_id: int
    run_type: str
    trigger: str
    started_at: datetime
    finished_at: datetime | None
    report_markdown: str
    report_markdown_major_only: str | None = None
    findings_count: int


class AuditRunSummary(BaseModel):
    """One row in the audit-history list (Part 2.1) - light enough to list
    every retained run without shipping full report text for each."""
    id: int
    run_type: str
    trigger: str
    started_at: datetime
    finished_at: datetime | None
    change_detected: bool
    findings_count: int
    critical_count: int
    suspension_risk_count: int
    has_delta: bool


class AuditRunDetailResponse(BaseModel):
    """Full report for one specific historical run, plus its delta vs. the
    previous run (None for a store's first run - nothing to diff)."""
    id: int
    store_id: int
    run_type: str
    trigger: str
    started_at: datetime
    finished_at: datetime | None
    report_markdown: str
    report_markdown_major_only: str | None = None
    delta_markdown: str | None = None
    delta_markdown_major_only: str | None = None
    findings_count: int
