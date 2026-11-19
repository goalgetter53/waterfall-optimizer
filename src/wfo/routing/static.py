"""Baseline: classic static waterfall, cheapest provider first, never stops early."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from wfo.models import Segment
from wfo.routing.base import CostModel


class StaticWaterfallRouter:
    name = "static"

    def __init__(self, costs: CostModel) -> None:
        self.costs = costs

    def choose(
        self, segment: Segment, candidates: Sequence[str], depth: int, remaining_usd: float
    ) -> str | None:
        options = self.costs.affordable(candidates, remaining_usd)
        if not options:
            return None
        return min(options, key=lambda p: (self.costs.provider_costs[p], p))

    def observe(
        self, segment: Segment, provider: str, depth: int, success: bool, found: bool = True
    ) -> None:
        return None

    def state_dict(self) -> dict[str, Any]:
        return {}

    def load_state(self, state: Mapping[str, Any]) -> None:
        return None
