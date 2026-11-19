"""Matplotlib charts for simulation results (PNG). Every chart is titled SIMULATION."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Fixed entity -> color mapping (validated categorical palette, slots 1-4);
# the oracle is a reference, drawn in neutral ink with a dashed line.
COLORS = {
    "thompson": "#2a78d6",
    "thompson-flat": "#eb6834",
    "greedy": "#1baf7a",
    "static": "#eda100",
    "oracle": "#52514e",
}
LABELS = {
    "thompson": "Thompson (segment x depth)",
    "thompson-flat": "Thompson (segment only)",
    "greedy": "Greedy EV (global)",
    "static": "Static waterfall (cheapest first)",
    "oracle": "Oracle (true params)",
}
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
SUBTITLE = "SIMULATION - synthetic contacts, simulated providers"


def _style(ax: Any, title: str) -> None:
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", fontsize=12, color=INK, pad=22)
    ax.text(0, 1.02, SUBTITLE, transform=ax.transAxes, fontsize=8, color=INK_2)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=9)
    ax.grid(axis="both", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def _fig() -> tuple[Any, Any]:
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    return fig, ax


def _order(table: dict[str, Any]) -> list[str]:
    return [s for s in ("static", "greedy", "thompson-flat", "thompson", "oracle") if s in table]


def bar_metric(
    table: dict[str, Any], metric: str, title: str, xlabel: str, path: Path, fmt: str = "{:.4f}"
) -> None:
    fig, ax = _fig()
    names = _order(table)
    means = [table[s][metric]["mean"] for s in names]
    errs = [table[s][metric]["ci95"] for s in names]
    y = list(range(len(names)))
    ax.barh(
        y,
        means,
        xerr=errs,
        color=[COLORS[s] for s in names],
        height=0.55,
        edgecolor=SURFACE,
        linewidth=2,
        error_kw={"ecolor": INK_2, "capsize": 3, "lw": 1},
    )
    for yi, m, e in zip(y, means, errs, strict=True):
        ax.text(m + e + max(means) * 0.01, yi, fmt.format(m), va="center", fontsize=9, color=INK)
    ax.set_yticks(y, [LABELS[s] for s in names])
    ax.set_xlabel(xlabel, color=INK_2)
    ax.set_xlim(0, max(m + e for m, e in zip(means, errs, strict=True)) * 1.18)
    _style(ax, title)
    ax.grid(axis="y", visible=False)
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def coverage_vs_cost(table: dict[str, Any], path: Path) -> None:
    fig, ax = _fig()
    for s in _order(table):
        x = table[s]["cost_per_contact"]
        y = table[s]["coverage"]
        ax.errorbar(
            x["mean"],
            y["mean"] * 100,
            xerr=x["ci95"],
            yerr=y["ci95"] * 100,
            fmt="o",
            ms=8,
            color=COLORS[s],
            ecolor=INK_2,
            elinewidth=1,
            capsize=3,
            markeredgecolor=SURFACE,
            markeredgewidth=2,
            label=LABELS[s],
        )
    ax.set_xlabel("Spend per contact (USD, simulated)", color=INK_2)
    ax.set_ylabel("Verified coverage (%)", color=INK_2)
    ax.legend(fontsize=8, frameon=False, loc="lower right")
    _style(ax, "Coverage vs spend: up and to the left is better")
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def regret_curves(table: dict[str, Any], path: Path) -> None:
    fig, ax = _fig()
    for s in _order(table):
        if s == "oracle" or "regret_curve" not in table[s]:
            continue
        c = table[s]["regret_curve"]
        lo = [m - h for m, h in zip(c["mean"], c["ci95"], strict=True)]
        hi = [m + h for m, h in zip(c["mean"], c["ci95"], strict=True)]
        ax.plot(c["x"], c["mean"], color=COLORS[s], lw=2, label=LABELS[s])
        ax.fill_between(c["x"], lo, hi, color=COLORS[s], alpha=0.15, lw=0)
    ax.axhline(0, color=INK_2, lw=1, ls="--")
    ax.set_xlabel("Contacts processed", color=INK_2)
    ax.set_ylabel("Cumulative regret vs oracle (USD)", color=INK_2)
    ax.legend(fontsize=8, frameon=False, loc="upper left")
    _style(ax, "Cumulative regret (mean, 95% CI band across seeds)")
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def learning_curves(table: dict[str, Any], path: Path) -> None:
    """Rolling per-contact regret (oracle net value - strategy net value).

    Strategies share contacts and provider outcomes with the oracle (common random
    numbers), so subtracting removes most of the stream-level noise."""
    fig, ax = _fig()
    if "oracle" not in table or "rolling_net_value" not in table["oracle"]:
        plt.close(fig)
        return
    oracle = table["oracle"]["rolling_net_value"]
    top = 0.0
    for s in _order(table):
        if s == "oracle" or "rolling_net_value" not in table[s]:
            continue
        c = table[s]["rolling_net_value"]
        gap = [o - m for o, m in zip(oracle["mean"], c["mean"], strict=True)]
        top = max(top, max(gap))
        ax.plot(c["x"], gap, color=COLORS[s], lw=2, label=LABELS[s])
    ax.axhline(0, color=INK_2, lw=1, ls="--")
    ax.set_xlabel("Contacts processed", color=INK_2)
    ax.set_ylabel("Rolling regret per contact (USD)", color=INK_2)
    ax.set_ylim(top=top * 1.4)
    ax.legend(fontsize=8, frameon=False, loc="upper center", ncol=2)
    _style(ax, "Learning curve: per-contact regret vs oracle (lower is better)")
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def render_all(results: dict[str, Any], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    table = results["scenarios"]["baseline"]
    paths = {
        "cost_per_verified": out_dir / "cost_per_verified.png",
        "coverage_vs_cost": out_dir / "coverage_vs_cost.png",
        "cumulative_regret": out_dir / "cumulative_regret.png",
        "p95_latency": out_dir / "p95_latency.png",
        "learning_curve": out_dir / "learning_curve.png",
    }
    bar_metric(
        table,
        "cost_per_verified",
        "Cost per verified email (lower is better)",
        "USD per verified email (simulated prices)",
        paths["cost_per_verified"],
    )
    coverage_vs_cost(table, paths["coverage_vs_cost"])
    regret_curves(table, paths["cumulative_regret"])
    bar_metric(
        table,
        "p95_latency_s",
        "p95 enrichment latency per contact",
        "seconds (simulated provider latency incl. retries)",
        paths["p95_latency"],
        fmt="{:.2f}s",
    )
    learning_curves(table, paths["learning_curve"])
    return list(paths.values())
