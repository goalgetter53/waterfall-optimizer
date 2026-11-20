"""Markdown report generated from results.json (numbers are never hand-edited)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from wfo.sim.charts import LABELS

ORDER = ("static", "greedy", "thompson-flat", "thompson", "oracle")


def _ci(m: dict[str, float], fmt: str = "{:.4f}", scale: float = 1.0) -> str:
    return f"{fmt.format(m['mean'] * scale)} ± {fmt.format(m['ci95'] * scale)}"


def _table(table: dict[str, Any]) -> list[str]:
    lines = [
        "| Strategy | Cost / verified (USD) | Verified coverage | Found rate | Precision* "
        "| Spend / contact | Net value / contact | p95 latency (s) | Calls / contact "
        "| Final regret (USD) |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in ORDER:
        if s not in table:
            continue
        t = table[s]
        regret = _ci(t["final_regret"], "{:.1f}") if "final_regret" in t else "-"
        if s == "oracle":
            regret = "0 (reference)"
        lines.append(
            f"| {LABELS[s]} | {_ci(t['cost_per_verified'])} "
            f"| {_ci(t['coverage'], '{:.1f}', 100)}% | {_ci(t['found_rate'], '{:.1f}', 100)}% "
            f"| {_ci(t['precision'], '{:.2f}', 100)}% | {_ci(t['cost_per_contact'])} "
            f"| {_ci(t['net_value_per_contact'])} | {_ci(t['p95_latency_s'], '{:.2f}')} "
            f"| {_ci(t['calls_per_contact'], '{:.2f}')} | {regret} |"
        )
    return lines


def headline(results: dict[str, Any]) -> dict[str, float]:
    """Derived comparisons quoted in the README (all from results.json)."""
    t = results["scenarios"]["baseline"]
    out: dict[str, float] = {}
    if "static" in t and "thompson" in t:
        s, b = t["static"], t["thompson"]
        out["cpv_reduction_vs_static_pct"] = 100 * (
            1 - b["cost_per_verified"]["mean"] / s["cost_per_verified"]["mean"]
        )
        out["coverage_delta_vs_static_pp"] = 100 * (b["coverage"]["mean"] - s["coverage"]["mean"])
        out["spend_reduction_vs_static_pct"] = 100 * (
            1 - b["cost_per_contact"]["mean"] / s["cost_per_contact"]["mean"]
        )
        out["p95_reduction_vs_static_pct"] = 100 * (
            1 - b["p95_latency_s"]["mean"] / s["p95_latency_s"]["mean"]
        )
        sa = {r["seed"]: r for r in b.get("per_seed", [])}
        sb = {r["seed"]: r for r in s.get("per_seed", [])}
        seeds = sorted(set(sa) & set(sb))
        if len(seeds) > 1:
            from wfo.sim.stats import mean_ci95

            red = [
                100 * (1 - sa[k]["cost_per_verified"] / sb[k]["cost_per_verified"]) for k in seeds
            ]
            out["cpv_reduction_vs_static_pct_ci95"] = mean_ci95(red)[1]
    if "thompson" in t and "final_regret" in t["thompson"] and "static" in t:
        out["regret_reduction_vs_static_pct"] = 100 * (
            1 - t["thompson"]["final_regret"]["mean"] / t["static"]["final_regret"]["mean"]
        )
    return out


def _calibration_section(cal: dict[str, Any]) -> list[str]:
    lines = [
        "## Calibration inputs",
        "",
        f"From `{cal['file']}` (sources accessed {cal['as_of']}; full citations in "
        "`docs/real_calibration_sources.md`). Hit rate / accuracy are the world-model "
        "parameters solved from the benchmark's found and valid rates; "
        f"P(email discoverable at all) = {cal['discoverable']:.3f}; verification cost "
        f"${cal['verify_cost_usd']:.3f}; segment noise {cal['segment_noise']}.",
        "",
        "| Provider | USD per email (list) | Charged on miss | Hit rate (model) | Accuracy (model) "
        "| Price source |",
        "|---|---|---|---|---|---|",
    ]
    for name, p in sorted(cal["providers"].items()):
        src = cal["sources"].get(p["price_source"], {}).get("url", p["price_source"])
        lines.append(
            f"| {name} | ${p['price_usd']:.4f} | {'yes' if p['charge_on_miss'] else 'no'} "
            f"| {p['base_hit']:.3f} | {p['accuracy']:.3f} | {src} |"
        )
    return [*lines, ""]


def _sensitivity_section(sweep: dict[str, Any]) -> list[str]:
    c = sweep["config"]
    lines = [
        "## Sensitivity sweep (hit-rate and heterogeneity assumptions)",
        "",
        f"{c['n_contacts']:,} contacts x seeds {c['seeds']} per variant; strategies static, "
        "greedy, Thompson (segment x depth), oracle. Differences are paired per seed "
        "(Thompson minus static), mean ± 95% Student-t interval.",
        "",
        "| Variant | CPV reduction vs static | Coverage change | Net value change / contact "
        "| Greedy CPV reduction | Oracle CPV reduction |",
        "|---|---|---|---|---|---|",
    ]
    for r in sweep["variants"]:
        t, g, o = r["thompson_vs_static"], r["greedy_vs_static"], r["oracle_vs_static"]
        cov, net = t["coverage_delta_pp"], t["net_value_delta_usd"]
        lines.append(
            f"| {r['label']} | {_ci(t['cpv_reduction_pct'], '{:.1f}')}% "
            f"| {cov['mean']:+.2f} ± {cov['ci95']:.2f} pp "
            f"| {net['mean']:+.4f} ± {net['ci95']:.4f} "
            f"| {_ci(g['cpv_reduction_pct'], '{:.1f}')}% "
            f"| {_ci(o['cpv_reduction_pct'], '{:.1f}')}% |"
        )
    return [*lines, "", "![Sensitivity](sensitivity.png)", ""]


def render_report(results: dict[str, Any], out_dir: Path) -> Path:
    cfg = results["config"]
    base = results["scenarios"]["baseline"]
    cal = results.get("calibration")
    if cal is not None:
        title = "# Simulation report: waterfall-optimizer (real-calibrated world)"
        banner = (
            "> **SIMULATION, CALIBRATED WITH REAL PUBLIC INPUTS.** Provider prices are public "
            f"list prices (accessed {cal['as_of']}); hit and accuracy rates are fitted to "
            "published, vendor-run benchmarks. Contacts and provider outcomes are still "
            "simulated, latencies and error rates are illustrative, and no live vendor "
            "traffic was used."
        )
    else:
        title = "# Simulation report: waterfall-optimizer"
        banner = (
            "> **SIMULATION ONLY.** Synthetic contacts with seeded ground truth and simulated "
            "providers whose prices, hit rates, latencies and error rates are illustrative. "
            "These are not production results and not benchmarks of any real vendor."
        )
    lines = [
        title,
        "",
        banner,
        "",
        *(_calibration_section(cal) if cal is not None else []),
        "## Setup",
        "",
        f"- Contacts per run: **{cfg['n_contacts']:,}**; seeds: **{cfg['seeds']}** "
        "(each seed = new world parameters, new contacts, new stream order)",
        f"- Value of a verified email V = **${cfg['value_per_verified']:.2f}**; per-contact "
        f"spend cap **${cfg['max_spend_per_contact']:.2f}**; global budget: "
        f"{'none' if cfg['budget_usd'] is None else '$' + str(cfg['budget_usd'])}",
        "- Every strategy runs through the same production pipeline (cache, ledger, token "
        "buckets, circuit breakers, retries with jitter, verifier) on a virtual clock, and "
        "sees the same contacts and the same provider outcomes (common random numbers).",
        "- Oracle = best fixed provider sequence per segment under the TRUE parameters "
        f"(Monte-Carlo, {cfg['oracle_draws']:,} draws x 65 sequences x 45 segments).",
        "- Intervals are 95% Student-t intervals across seeds. *Precision = share of "
        "'verified' emails that match ground truth.",
        f"- Total harness wall-clock: {results.get('wall_clock_s', 0):.0f}s.",
        "",
        "## Baseline scenario",
        "",
        *_table(base),
        "",
    ]
    h = headline(results)
    if h:
        lines += ["### Headline comparisons (Thompson segment x depth vs static waterfall)", ""]
        if "cpv_reduction_vs_static_pct" in h:
            ci = h.get("cpv_reduction_vs_static_pct_ci95")
            lines.append(
                f"- Cost per verified email: **{h['cpv_reduction_vs_static_pct']:.1f}% lower**"
                + ("" if ci is None else f" (paired per-seed 95% CI ± {ci:.1f} pp)")
            )
            lines.append(
                f"- Verified coverage change: **{h['coverage_delta_vs_static_pp']:+.2f} "
                "percentage points**"
            )
            lines.append(
                f"- Spend per contact: **{h['spend_reduction_vs_static_pct']:.1f}% lower**"
            )
            lines.append(f"- p95 latency: **{h['p95_reduction_vs_static_pct']:.1f}% lower**")
        if "regret_reduction_vs_static_pct" in h:
            lines.append(
                f"- Cumulative regret vs oracle: **{h['regret_reduction_vs_static_pct']:.0f}% "
                "lower** than static (equivalently: that share of the static-to-oracle "
                "net-value gap is closed)"
            )
        if "first_choice_agreement" in base.get("thompson", {}):
            a = base["thompson"]["first_choice_agreement"]
            lines.append(
                f"- Segments where the learned first provider equals the oracle's: "
                f"**{_ci(a, '{:.0f}', 100)}%**"
            )
        lines.append("")
    if "apollo_outage" in results["scenarios"]:
        lines += [
            "## Fault scenario: hard outage of `apollo` for contacts 30%-50% of the stream",
            "",
            "Circuit breakers open, routers skip the provider, half-open probes detect recovery.",
            "",
            *_table(results["scenarios"]["apollo_outage"]),
            "",
        ]
    lines += [
        "## Charts",
        "",
        "![Cost per verified](cost_per_verified.png)",
        "",
        "![Coverage vs cost](coverage_vs_cost.png)",
        "",
        "![Cumulative regret](cumulative_regret.png)",
        "",
        "![Learning curve](learning_curve.png)",
        "",
        "![p95 latency](p95_latency.png)",
        "",
    ]
    sens = out_dir / "sensitivity.json"
    if cal is not None and sens.exists():
        lines += _sensitivity_section(json.loads(sens.read_text()))
    lines += [
        "## Reading these results",
        "",
        "- `thompson` (segment x depth) and `thompson-flat` (segment only) are statistically "
        "indistinguishable here: in this world the depth context did not pay for its "
        "extra arms. It is kept as an ablation, not claimed as a win.",
        "- The objective is **net value = V x verified - spend**, not raw cost per verified. "
        "A router that calls only the single best provider gets a low cost per verified "
        "email by giving up coverage; the table shows both so that trade-off is visible.",
        (
            "- In this calibrated world, segment structure comes only from small per-country "
            "benchmark splits plus assumed noise, and every vendor is pay-on-found. A static "
            "cheapest-first waterfall is then already close to the oracle in net value, so "
            "most of the cost-per-verified gain comes from the stopping rule (skipping the "
            "expensive last call), not from per-segment learning."
            if cal is not None
            else "- The synthetic world was designed with segment-dependent provider strengths. "
            "If real vendors were uniform across segments, a contextual router would have "
            "little to learn and the gap to a static waterfall would shrink."
        ),
        "",
    ]
    path = out_dir / "report.md"
    path.write_text("\n".join(lines))
    return path
