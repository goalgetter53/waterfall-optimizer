"""Sensitivity sweep for the real-calibrated world.

The prices are public list prices, but the hit rates come from two vendor-run
benchmarks with modest per-country samples, and the size/seniority structure is
an assumption. Each variant re-runs static / greedy / Thompson / oracle on the
same seeds and reports the Thompson-vs-static difference as a paired (per-seed)
mean with a 95% Student-t interval.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any

from wfo.sim.harness import SimConfig, simulate
from wfo.sim.stats import mean_ci95

SWEEP_STRATEGIES = ("static", "greedy", "thompson", "oracle")


@dataclass(frozen=True, slots=True)
class Variant:
    key: str
    label: str
    hit_multipliers: dict[str, float] = field(default_factory=dict)
    segment_noise: float | None = None
    country_effects: bool = True
    price_set: str = "list"


def default_variants(providers: list[str], scale: float = 0.30) -> list[Variant]:
    lo, hi = round(1 - scale, 2), round(1 + scale, 2)
    out = [
        Variant("reference", "Calibrated (reference)"),
        Variant("all_low", f"All hit rates x{lo}", dict.fromkeys(providers, lo)),
        Variant("all_high", f"All hit rates x{hi}", dict.fromkeys(providers, hi)),
    ]
    for p in providers:
        out.append(Variant(f"{p}_low", f"{p} hit rate x{lo}", {p: lo}))
        out.append(Variant(f"{p}_high", f"{p} hit rate x{hi}", {p: hi}))
    out += [
        Variant("no_country", "No country effects", country_effects=False),
        Variant("noise_0", "No segment heterogeneity (noise 0)", segment_noise=0.0),
        Variant("noise_07", "Double segment heterogeneity (noise 0.7)", segment_noise=0.7),
        Variant("annual_prices", "Annual-billing prices", price_set="annual"),
    ]
    return out


def _paired(table: dict[str, Any], a: str, b: str, value: float) -> dict[str, dict[str, float]]:
    """Per-seed paired differences of strategy ``a`` vs baseline ``b``."""
    sa = {r["seed"]: r for r in table[a]["per_seed"]}
    sb = {r["seed"]: r for r in table[b]["per_seed"]}
    seeds = sorted(set(sa) & set(sb))
    cpv = [100 * (1 - sa[s]["cost_per_verified"] / sb[s]["cost_per_verified"]) for s in seeds]
    cov = [100 * (sa[s]["coverage"] - sb[s]["coverage"]) for s in seeds]
    spend = [sa[s]["cost_per_contact"] - sb[s]["cost_per_contact"] for s in seeds]
    net = [
        (sa[s]["coverage"] - sb[s]["coverage"]) * value
        - (sa[s]["cost_per_contact"] - sb[s]["cost_per_contact"])
        for s in seeds
    ]
    out: dict[str, dict[str, float]] = {}
    for k, xs in (
        ("cpv_reduction_pct", cpv),
        ("coverage_delta_pp", cov),
        ("spend_delta_usd", spend),
        ("net_value_delta_usd", net),
    ):
        m, hw = mean_ci95(xs)
        out[k] = {"mean": m, "ci95": hw}
    return out


def paired_vs_static(results: dict[str, Any], strategy: str = "thompson") -> dict[str, Any]:
    table = results["scenarios"]["baseline"]
    return _paired(table, strategy, "static", float(results["config"]["value_per_verified"]))


def run_sweep(base: SimConfig, variants: list[Variant]) -> dict[str, Any]:
    t0 = time.perf_counter()
    rows: list[dict[str, Any]] = []
    for v in variants:
        cfg = replace(
            base,
            strategies=SWEEP_STRATEGIES,
            fault_scenario=False,
            hit_multipliers=v.hit_multipliers,
            segment_noise=v.segment_noise,
            country_effects=v.country_effects,
            price_set=v.price_set,
        )
        res = simulate(cfg)
        table = res["scenarios"]["baseline"]
        rows.append(
            {
                "key": v.key,
                "label": v.label,
                "hit_multipliers": v.hit_multipliers,
                "segment_noise": v.segment_noise,
                "country_effects": v.country_effects,
                "price_set": v.price_set,
                "thompson_vs_static": paired_vs_static(res, "thompson"),
                "greedy_vs_static": paired_vs_static(res, "greedy"),
                "oracle_vs_static": paired_vs_static(res, "oracle"),
                "cost_per_verified": {
                    s: table[s]["cost_per_verified"] for s in SWEEP_STRATEGIES if s in table
                },
                "coverage": {s: table[s]["coverage"] for s in SWEEP_STRATEGIES if s in table},
                "regret": {
                    s: table[s]["final_regret"]
                    for s in SWEEP_STRATEGIES
                    if s in table and "final_regret" in table[s]
                },
            }
        )
        print(f"  sweep {v.key}: done", flush=True)
    return {
        "label": "SENSITIVITY SWEEP (SIMULATION, real-calibrated world)",
        "config": {
            "n_contacts": base.n_contacts,
            "seeds": list(base.seeds),
            "value_per_verified": base.value_per_verified,
            "world_config": base.world_config,
        },
        "variants": rows,
        "wall_clock_s": time.perf_counter() - t0,
    }
