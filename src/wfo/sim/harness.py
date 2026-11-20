"""Simulation harness: runs every strategy through the REAL pipeline (router,
cache, ledger, breakers, token buckets, retries, verifier) on a virtual clock,
over the same seeded synthetic contacts, for several seeds.

All numbers produced here are SIMULATION results on synthetic data.
"""

from __future__ import annotations

import asyncio
import random
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from typing import Any

import numpy as np

from wfo.cache import InMemoryTTLCache
from wfo.clock import VirtualClock
from wfo.ledger import CostLedger
from wfo.models import Segment, Verification
from wfo.pipeline import EnrichmentPipeline, PipelineConfig, build_envelopes
from wfo.providers.simulated import build_simulated_providers
from wfo.routing import CostModel, Router, SequenceRouter, ThompsonRouter, make_router
from wfo.sim.calibration import Calibration
from wfo.sim.calibration import load as load_calibration
from wfo.sim.oracle import best_sequences
from wfo.sim.stats import mean_ci95, percentile
from wfo.sim.world import SyntheticWorld
from wfo.verification import SimulatedVerifier

SIM_STRATEGIES = ("static", "greedy", "thompson-flat", "thompson", "oracle")
VERIFY_COST = 0.003
CURVE_POINTS = 200


@dataclass(slots=True)
class SimConfig:
    n_contacts: int = 20_000
    seeds: tuple[int, ...] = (1, 2, 3, 4, 5)
    strategies: tuple[str, ...] = SIM_STRATEGIES
    value_per_verified: float = 0.50
    max_spend_per_contact: float = 0.30
    budget_usd: float | None = None
    oracle_draws: int = 4000
    fault_scenario: bool = True
    """Also run each strategy with a hard 'apollo' outage over 30-50% of the stream."""
    workers: int = 2
    world_config: str | None = None
    """Path to a calibration JSON (e.g. configs/real_calibrated.json); None = illustrative."""
    hit_multipliers: dict[str, float] = field(default_factory=dict)
    segment_noise: float | None = None
    country_effects: bool = True
    price_set: str = "list"
    """'list' (cheapest self-serve plan as published) or 'annual' (annual-billing prices)."""


def calibration_for(cfg: SimConfig) -> Calibration | None:
    if cfg.world_config is None:
        return None
    cal = load_calibration(cfg.world_config).with_overrides(
        cfg.hit_multipliers, cfg.segment_noise, cfg.country_effects
    )
    if cfg.price_set == "annual":
        prices = {
            n: float(p["price_usd_annual_billing"]["value"])
            for n, p in cal.raw["providers"].items()
        }
        cal = replace(cal, specs=tuple(replace(s, cost_per_call=prices[s.name]) for s in cal.specs))
    elif cfg.price_set != "list":
        raise ValueError(f"unknown price_set {cfg.price_set!r}")
    return cal


def build_world(seed: int, cfg: SimConfig) -> tuple[SyntheticWorld, float]:
    """World for one seed plus the per-check verification cost."""
    cal = calibration_for(cfg)
    if cal is None:
        return SyntheticWorld(seed), VERIFY_COST
    return SyntheticWorld(seed, cal.specs, cal.segment_noise, cal.model), cal.verify_cost


@dataclass(slots=True)
class RunSummary:
    strategy: str
    seed: int
    scenario: str
    n: int
    total_cost: float
    verified: int
    found: int
    true_verified: int
    cost_per_verified: float
    coverage: float
    found_rate: float
    precision: float
    cost_per_contact: float
    p50_latency_s: float
    p95_latency_s: float
    calls_per_contact: float
    provider_calls: dict[str, int]
    breaker_opens: dict[str, int]
    cache_hits: int
    wall_s: float
    first_choice_agreement: float | None = None
    rewards: list[float] = field(default_factory=list)


def _first_choice_agreement(
    router: ThompsonRouter, oracle: dict[str, list[str]], costs: CostModel, names: list[str]
) -> float:
    """Share of segments where the bandit's posterior-mean first pick == oracle's first pick."""
    agree = 0
    for key, seq in oracle.items():
        seg = Segment.from_key(key)
        best: tuple[float, str] | None = None
        for p in names:
            m = router.posterior_mean(seg, p, 0)
            c = router.expected_cost(seg, p, 0)
            if m * router.value < c:
                continue
            if best is None or m / c > best[0]:
                best = (m / c, p)
        pick = None if best is None else best[1]
        agree += int(pick == (seq[0] if seq else None))
    return agree / len(oracle)


async def _run_async(strategy: str, seed: int, cfg: SimConfig, scenario: str) -> RunSummary:
    t0 = time.perf_counter()
    world, verify_cost = build_world(seed, cfg)
    contacts = world.generate_contacts(cfg.n_contacts)
    clock = VirtualClock()
    providers = build_simulated_providers(world, clock)
    position = [0]
    if scenario == "apollo_outage":
        lo, hi = int(0.3 * cfg.n_contacts), int(0.5 * cfg.n_contacts)
        providers["apollo"].outage_fn = lambda: lo <= position[0] < hi
    verifier = SimulatedVerifier(world, clock, cost_per_check=verify_cost)
    costs = CostModel(
        {n: p.cost_per_call for n, p in providers.items()},
        verify_cost,
        {n: p.charge_on_miss for n, p in providers.items()},
    )
    oracle_seq: dict[str, list[str]] = {}
    if strategy == "oracle" or strategy.startswith("thompson"):
        oracle_seq, _ = best_sequences(world, cfg.value_per_verified, verify_cost, cfg.oracle_draws)
    router: Router
    if strategy == "oracle":
        router = SequenceRouter(oracle_seq)
    else:
        router = make_router(strategy, costs, cfg.value_per_verified, seed=seed * 101)
    envelopes = build_envelopes(
        providers, clock, rate_per_s=50.0, burst=100.0, failure_threshold=5, reset_timeout_s=30.0
    )
    pipeline = EnrichmentPipeline(
        envelopes,
        router,
        verifier,
        CostLedger(":memory:", cfg.budget_usd),
        InMemoryTTLCache(clock.now),
        clock,
        PipelineConfig(
            value_per_verified=cfg.value_per_verified,
            max_spend_per_contact=cfg.max_spend_per_contact,
        ),
        random.Random(seed),
    )

    costs_arr = np.zeros(cfg.n_contacts)
    verified = np.zeros(cfg.n_contacts, dtype=bool)
    latencies: list[float] = []
    found = true_verified = n_calls = 0
    for i, contact in enumerate(contacts):
        position[0] = i
        out = await pipeline.enrich(contact, f"sim:{seed}:{i}")
        costs_arr[i] = out.cost
        verified[i] = out.verification is Verification.VALID
        latencies.append(out.latency_s)
        n_calls += sum(1 for c in out.calls if not c.from_cache)
        if out.email is not None:
            found += 1
            if verified[i] and out.email == world.truth(contact).true_email:
                true_verified += 1

    n = cfg.n_contacts
    total = float(costs_arr.sum())
    v = int(verified.sum())
    rewards = (verified * cfg.value_per_verified - costs_arr).tolist()
    agreement = None
    if isinstance(router, ThompsonRouter):
        agreement = _first_choice_agreement(router, oracle_seq, costs, sorted(providers))
    return RunSummary(
        strategy=strategy,
        seed=seed,
        scenario=scenario,
        n=n,
        total_cost=total,
        verified=v,
        found=found,
        true_verified=true_verified,
        cost_per_verified=total / v if v else float("inf"),
        coverage=v / n,
        found_rate=found / n,
        precision=true_verified / v if v else 0.0,
        cost_per_contact=total / n,
        p50_latency_s=percentile(latencies, 50),
        p95_latency_s=percentile(latencies, 95),
        calls_per_contact=n_calls / n,
        provider_calls={k: p.calls for k, p in providers.items()},
        breaker_opens={
            k: sum(1 for _, new in e.breaker.transitions if new.value == "open")
            for k, e in envelopes.items()
        },
        cache_hits=0,
        wall_s=time.perf_counter() - t0,
        first_choice_agreement=agreement,
        rewards=rewards,
    )


def run_one(strategy: str, seed: int, cfg: SimConfig, scenario: str = "baseline") -> RunSummary:
    return asyncio.run(_run_async(strategy, seed, cfg, scenario))


def _downsample(arr: np.ndarray, points: int = CURVE_POINTS) -> list[float]:
    idx = np.linspace(0, len(arr) - 1, min(points, len(arr))).astype(int)
    return [float(x) for x in arr[idx]]


def aggregate(runs: list[RunSummary], cfg: SimConfig) -> dict[str, Any]:
    """Seed-level aggregation with 95% CIs, regret and learning curves."""
    out: dict[str, Any] = {"config": asdict(cfg), "scenarios": {}}
    out["config"]["seeds"] = list(cfg.seeds)
    for scenario in sorted({r.scenario for r in runs}):
        sruns = [r for r in runs if r.scenario == scenario]
        oracle = {r.seed: np.array(r.rewards) for r in sruns if r.strategy == "oracle"}
        table: dict[str, Any] = {}
        for strategy in cfg.strategies:
            rs = sorted((r for r in sruns if r.strategy == strategy), key=lambda r: r.seed)
            if not rs:
                continue
            metrics: dict[str, Any] = {}
            for m in (
                "cost_per_verified",
                "coverage",
                "found_rate",
                "precision",
                "cost_per_contact",
                "p50_latency_s",
                "p95_latency_s",
                "calls_per_contact",
            ):
                mean, hw = mean_ci95([float(getattr(r, m)) for r in rs])
                metrics[m] = {"mean": mean, "ci95": hw}
            net = [float(np.mean(r.rewards)) for r in rs]
            mean, hw = mean_ci95(net)
            metrics["net_value_per_contact"] = {"mean": mean, "ci95": hw}
            agreements = [
                r.first_choice_agreement for r in rs if r.first_choice_agreement is not None
            ]
            if agreements:
                mean, hw = mean_ci95(agreements)
                metrics["first_choice_agreement"] = {"mean": mean, "ci95": hw}
            curves = []
            rolling = []
            for r in rs:
                rew = np.array(r.rewards)
                if r.seed in oracle:
                    curves.append(np.cumsum(oracle[r.seed] - rew))
                window = max(1, len(rew) // 20)
                kernel = np.ones(window) / window
                rolling.append(np.convolve(rew, kernel, mode="valid"))
            if curves:
                stack = np.vstack(curves)
                final_mean, final_hw = mean_ci95([float(c[-1]) for c in curves])
                metrics["final_regret"] = {"mean": final_mean, "ci95": final_hw}
                mean_curve = stack.mean(axis=0)
                hw_curve = np.array(
                    [
                        mean_ci95(stack[:, j].tolist())[1]
                        for j in np.linspace(0, stack.shape[1] - 1, CURVE_POINTS).astype(int)
                    ]
                )
                metrics["regret_curve"] = {
                    "x": _downsample(np.arange(1, stack.shape[1] + 1)),
                    "mean": _downsample(mean_curve),
                    "ci95": hw_curve.tolist(),
                }
            if rolling:
                rstack = np.vstack(rolling)
                metrics["rolling_net_value"] = {
                    "x": _downsample(
                        np.arange(rstack.shape[1]) + len(rs[0].rewards) - rstack.shape[1] + 1
                    ),
                    "mean": _downsample(rstack.mean(axis=0)),
                }
            metrics["per_seed"] = [
                {k: v for k, v in asdict(r).items() if k != "rewards"} for r in rs
            ]
            table[strategy] = metrics
        out["scenarios"][scenario] = table
    return out


def simulate(cfg: SimConfig) -> dict[str, Any]:
    scenarios = ["baseline"] + (["apollo_outage"] if cfg.fault_scenario else [])
    jobs = [(s, seed, sc) for sc in scenarios for seed in cfg.seeds for s in cfg.strategies]
    t0 = time.perf_counter()
    if cfg.workers > 1:
        with ProcessPoolExecutor(max_workers=cfg.workers) as pool:
            futures = [pool.submit(run_one, s, seed, cfg, sc) for s, seed, sc in jobs]
            runs = [f.result() for f in futures]
    else:
        runs = [run_one(s, seed, cfg, sc) for s, seed, sc in jobs]
    result = aggregate(runs, cfg)
    result["wall_clock_s"] = time.perf_counter() - t0
    result["label"] = "SIMULATION - synthetic contacts and simulated providers; not production data"
    cal = calibration_for(cfg)
    if cal is not None:
        result["label"] = "SIMULATION (real-calibrated) - " + str(cal.raw["label"])
        result["calibration"] = {
            "file": cfg.world_config,
            "as_of": cal.as_of,
            "verify_cost_usd": cal.verify_cost,
            "segment_noise": cal.segment_noise,
            "discoverable": cal.model.discoverable_base,
            "providers": {
                s.name: {
                    "price_usd": s.cost_per_call,
                    "charge_on_miss": s.charge_on_miss,
                    "base_hit": s.base_hit,
                    "accuracy": s.accuracy,
                    "hit_multiplier": s.hit_multiplier,
                    "price_source": cal.raw["providers"][s.name]["price_usd"]["source"],
                }
                for s in cal.specs
            },
            "sources": cal.raw["sources"],
        }
    return result
