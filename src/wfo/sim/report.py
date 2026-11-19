"""Markdown report generated from results.json (numbers are never hand-edited)."""

from __future__ import annotations

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
    if "thompson" in t and "final_regret" in t["thompson"] and "static" in t:
        out["regret_reduction_vs_static_pct"] = 100 * (
            1 - t["thompson"]["final_regret"]["mean"] / t["static"]["final_regret"]["mean"]
        )
    return out


def render_report(results: dict[str, Any], out_dir: Path) -> Path:
    cfg = results["config"]
    base = results["scenarios"]["baseline"]
    lines = [
        "# Simulation report: waterfall-optimizer",
        "",
        "> **SIMULATION ONLY.** Synthetic contacts with seeded ground truth and simulated "
        "providers whose prices, hit rates, latencies and error rates are illustrative. "
        "These are not production results and not benchmarks of any real vendor.",
        "",
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
            lines.append(
                f"- Cost per verified email: **{h['cpv_reduction_vs_static_pct']:.1f}% lower**"
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
        "## Reading these results",
        "",
        "- `thompson` (segment x depth) and `thompson-flat` (segment only) are statistically "
        "indistinguishable here: in this world the depth context did not pay for its "
        "extra arms. It is kept as an ablation, not claimed as a win.",
        "- The objective is **net value = V x verified - spend**, not raw cost per verified. "
        "A router that calls only the single best provider gets a low cost per verified "
        "email by giving up coverage; the table shows both so that trade-off is visible.",
        "- The synthetic world was designed with segment-dependent provider strengths. If "
        "real vendors were uniform across segments, a contextual router would have little "
        "to learn and the gap to a static waterfall would shrink.",
        "",
    ]
    path = out_dir / "report.md"
    path.write_text("\n".join(lines))
    return path
