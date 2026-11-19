"""FastAPI: submit a batch, poll its status, fetch results; webhook on completion."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from wfo.config import Settings
from wfo.models import Contact
from wfo.routing import STRATEGIES
from wfo.service import EnrichmentService, build_service


class ContactIn(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    company_domain: str = Field(min_length=3, max_length=255)
    country: str = "US"
    employees: int = Field(default=100, ge=1)
    seniority: str = "ic"


class BatchIn(BaseModel):
    contacts: list[ContactIn] = Field(min_length=1, max_length=50_000)
    strategy: str = "thompson"
    webhook_url: str | None = None


class BatchAccepted(BaseModel):
    batch_id: str
    duplicate: bool
    status_url: str


def create_app(service: EnrichmentService | None = None, start_workers: bool = True) -> FastAPI:
    holder: dict[str, Any] = {}

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        svc = service or build_service(Settings.from_env())
        holder["svc"] = svc
        stop = asyncio.Event()
        task = asyncio.create_task(svc.worker_pool().run(stop)) if start_workers else None
        try:
            yield
        finally:
            stop.set()
            if task is not None:
                await task
            if service is None:
                await svc.aclose()

    app = FastAPI(title="waterfall-optimizer", version="0.1.0", lifespan=lifespan)

    def svc() -> EnrichmentService:
        s: EnrichmentService = holder["svc"]
        return s

    @app.post("/v1/batches", status_code=202, response_model=BatchAccepted)
    async def submit_batch(
        body: BatchIn, idempotency_key: str | None = Header(default=None)
    ) -> BatchAccepted:
        if body.strategy not in STRATEGIES:
            raise HTTPException(422, f"strategy must be one of {list(STRATEGIES)}")
        contacts = [Contact.from_dict(c.model_dump()) for c in body.contacts]
        batch_id, created = svc().submit(contacts, body.strategy, body.webhook_url, idempotency_key)
        return BatchAccepted(
            batch_id=batch_id, duplicate=not created, status_url=f"/v1/batches/{batch_id}"
        )

    @app.get("/v1/batches/{batch_id}")
    async def batch_status(batch_id: str) -> dict[str, Any]:
        st = svc().queue.batch_status(batch_id)
        if st is None:
            raise HTTPException(404, "batch not found")
        return {
            "batch_id": st.batch_id,
            "status": st.status,
            "strategy": st.strategy,
            "total": st.total,
            "finished": st.finished,
            "counts": st.counts,
            "webhook_status": st.webhook_status,
        }

    @app.get("/v1/batches/{batch_id}/results")
    async def batch_results(batch_id: str) -> dict[str, Any]:
        if svc().queue.batch_status(batch_id) is None:
            raise HTTPException(404, "batch not found")
        return {"batch_id": batch_id, "results": svc().queue.results(batch_id)}

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        return svc().health()

    return app
