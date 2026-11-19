"""Baseline: greedy expected value. Learns one *global* verified-rate and found-rate
per provider (no segment context) and calls providers in order of
P(verified) / expected cost, stopping when no call has positive expected value."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from wfo.models import Segment
from wfo.routing.base import CostModel


class GreedyEVRouter:
    name = "greedy"

    def __init__(self, costs: CostModel, value_per_verified: float) -> None:
        self.costs = costs
        self.value = value_per_verified
        self.success: dict[str, int] = defaultdict(int)
        self.found: dict[str, int] = defaultdict(int)
        self.trials: dict[str, int] = defaultdict(int)

    def mean(self, provider: str) -> float:
        return (1 + self.success[provider]) / (2 + self.trials[provider])

    def found_rate(self, provider: str) -> float:
        return (1 + self.found[provider]) / (2 + self.trials[provider])

    def choose(
        self, segment: Segment, candidates: Sequence[str], depth: int, remaining_usd: float
    ) -> str | None:
        best: tuple[float, str] | None = None
        for p in sorted(self.costs.affordable(candidates, remaining_usd)):
            m = self.mean(p)
            c = self.costs.expected_step_cost(p, self.found_rate(p))
            if m * self.value < c:
                continue
            score = m / c
            if best is None or score > best[0]:
                best = (score, p)
        return None if best is None else best[1]

    def observe(
        self, segment: Segment, provider: str, depth: int, success: bool, found: bool = True
    ) -> None:
        self.trials[provider] += 1
        self.success[provider] += int(success)
        self.found[provider] += int(found or success)

    def state_dict(self) -> dict[str, Any]:
        return {
            "success": dict(self.success),
            "found": dict(self.found),
            "trials": dict(self.trials),
        }

    def load_state(self, state: Mapping[str, Any]) -> None:
        for name in ("success", "found", "trials"):
            setattr(
                self, name, defaultdict(int, {k: int(v) for k, v in state.get(name, {}).items()})
            )
