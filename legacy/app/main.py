"""FastAPI entry point.

The startup event is deliberately the first thing that runs and the first
thing that logs - `Settings.describe_mode()` prints which parts of the
system are live vs. mocked before anything else happens, so mode is never
ambiguous.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app.auth import require_api_auth
from app.config import load_settings
from app.models.report import ComplianceReport
from app.pipeline import Pipeline, PipelineRunResult, build_pipeline
from app.scheduler import ComplianceScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("gmc_compliance.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    pipeline = build_pipeline(settings)
    scheduler = ComplianceScheduler(pipeline)
    scheduler.start()

    app.state.settings = settings
    app.state.pipeline = pipeline
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        scheduler.shutdown()


app = FastAPI(title="GMC Compliance Bot", lifespan=lifespan)


def _get_pipeline(app_: FastAPI) -> Pipeline:
    return app_.state.pipeline


@app.get("/health")
async def health() -> dict:
    settings = app.state.settings
    return {
        "status": "ok",
        "store_platform": settings.store_platform.value,
        "llm_provider": settings.llm_provider.value,
        "gmc_configured": settings.gmc_is_configured,
    }


@app.post("/scan", response_model=None, dependencies=[Depends(require_api_auth)])
async def trigger_scan() -> PipelineRunResult:
    pipeline = _get_pipeline(app)
    return await pipeline.run_once()


@app.get("/dashboard/alerts", response_model=None, dependencies=[Depends(require_api_auth)])
async def list_alerts() -> list[ComplianceReport]:
    pipeline = _get_pipeline(app)
    return pipeline.dashboard_store.list_alerts()
