"""Wires settings -> providers, reliability envelope, routers, queue, ledger, cache."""

from __future__ import annotations

import json
import random
import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from wfo.cache import SqliteResponseCache
from wfo.clock import Clock, RealClock
from wfo.config import Settings
from wfo.db import connect
from wfo.ledger import CostLedger
from wfo.models import Contact
from wfo.pipeline import EnrichmentPipeline, PipelineConfig, build_envelopes
from wfo.providers.base import Provider
from wfo.providers.catalog import build_live_providers
from wfo.providers.simulated import build_simulated_providers
from wfo.queue.sqlite import SqliteJobQueue
from wfo.routing import STRATEGIES, CostModel, Router, make_router
from wfo.sim.world import SyntheticWorld
from wfo.verification import ProviderStatusVerifier, SimulatedVerifier, Verifier
from wfo.webhook import WebhookNotifier
from wfo.worker import WorkerPool


class RouterStateStore:
    """Persists learned posteriors so the bandit keeps its knowledge across restarts."""

    def __init__(self, path: str | Path) -> None:
        self._conn: sqlite3.Connection = connect(path)
        self._lock = threading.Lock()
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS router_state (strategy TEXT PRIMARY KEY, state TEXT)"
        )

    def load(self, strategy: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM router_state WHERE strategy = ?", (strategy,)
            ).fetchone()
        if row is None:
            return None
        state: dict[str, Any] = json.loads(row[0])
        return state

    def save(self, strategy: str, state: Mapping[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO router_state (strategy, state) VALUES (?, ?) ON CONFLICT(strategy) "
                "DO UPDATE SET state = excluded.state",
                (strategy, json.dumps(state)),
            )


class EnrichmentService:
    def __init__(
        self,
        settings: Settings,
        queue: SqliteJobQueue,
        ledger: CostLedger,
        pipelines: Mapping[str, EnrichmentPipeline],
        state_store: RouterStateStore,
        http_client: httpx.AsyncClient,
    ) -> None:
        self.settings = settings
        self.queue = queue
        self.ledger = ledger
        self.pipelines = dict(pipelines)
        self.state_store = state_store
        self.http_client = http_client
        self.webhook = WebhookNotifier(http_client, settings.webhook_secret)

    @property
    def routers(self) -> dict[str, Router]:
        return {k: p.router for k, p in self.pipelines.items()}

    def submit(
        self,
        contacts: Sequence[Contact],
        strategy: str = "thompson",
        webhook_url: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[str, bool]:
        if strategy not in self.pipelines:
            raise ValueError(f"unknown strategy {strategy!r}")
        payloads = [(c.id, {"contact": c.to_dict(), "strategy": strategy}) for c in contacts]
        return self.queue.create_batch(payloads, strategy, webhook_url, idempotency_key)

    async def save_router_state(self, batch_id: str) -> None:
        strategy = self.queue.batch_strategy(batch_id)
        if strategy and strategy in self.pipelines:
            self.state_store.save(strategy, self.pipelines[strategy].router.state_dict())

    def worker_pool(self, concurrency: int | None = None) -> WorkerPool:
        return WorkerPool(
            self.queue,
            self.pipelines,
            concurrency=concurrency or self.settings.concurrency,
            visibility_timeout_s=self.settings.visibility_timeout_s,
            webhook=self.webhook,
            on_batch_complete=self.save_router_state,
        )

    def health(self) -> dict[str, Any]:
        any_pipeline = next(iter(self.pipelines.values()))
        return {
            "mode": self.settings.mode,
            "providers": {
                n: {"breaker": env.breaker.state.value, "tokens": round(env.bucket.tokens, 2)}
                for n, env in any_pipeline.envelopes.items()
            },
            "ledger": {
                "spent_usd": self.ledger.spent_usd,
                "reserved_usd": self.ledger.reserved_usd,
                "budget_usd": self.ledger.budget_usd,
                "by_provider": self.ledger.spent_by_provider(),
            },
            "queue": self.queue.counts(),
        }

    async def aclose(self) -> None:
        await self.http_client.aclose()


def build_service(
    settings: Settings,
    http_client: httpx.AsyncClient | None = None,
    providers: Mapping[str, Provider] | None = None,
    verifier: Verifier | None = None,
    clock: Clock | None = None,
) -> EnrichmentService:
    clock = clock or RealClock()
    client = http_client or httpx.AsyncClient(timeout=20.0)
    if providers is None:
        if settings.mode == "live":
            providers = build_live_providers(client)
            if not providers:
                raise RuntimeError("live mode: set at least one *_API_KEY (see .env.example)")
            verifier = verifier or ProviderStatusVerifier()
        else:
            world = SyntheticWorld(settings.sim_seed)
            providers = build_simulated_providers(world, clock, settings.sim_latency_scale)
            verifier = verifier or SimulatedVerifier(
                world, clock, latency_scale=settings.sim_latency_scale
            )
    if verifier is None:
        verifier = ProviderStatusVerifier()

    db = settings.db_path
    ledger = CostLedger(db, settings.budget_usd)
    cache = SqliteResponseCache(db, now=time.time)
    queue = SqliteJobQueue(db)
    store = RouterStateStore(db)
    envelopes = build_envelopes(providers, clock, settings.rate_per_s, settings.burst)
    costs = CostModel(
        {n: p.cost_per_call for n, p in providers.items()},
        verifier.cost_per_check,
        {n: p.charge_on_miss for n, p in providers.items()},
    )
    cfg = PipelineConfig(
        value_per_verified=settings.value_per_verified,
        max_spend_per_contact=settings.max_spend_per_contact,
    )
    pipelines: dict[str, EnrichmentPipeline] = {}
    for i, strategy in enumerate(STRATEGIES):
        router = make_router(strategy, costs, settings.value_per_verified, seed=i)
        saved = store.load(strategy)
        if saved:
            router.load_state(saved)
        pipelines[strategy] = EnrichmentPipeline(
            envelopes, router, verifier, ledger, cache, clock, cfg, random.Random(i)
        )
    return EnrichmentService(settings, queue, ledger, pipelines, store, client)
