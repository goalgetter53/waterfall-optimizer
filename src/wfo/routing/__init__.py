"""Routing strategies."""

from __future__ import annotations

import random

from wfo.routing.bandit import ThompsonRouter
from wfo.routing.base import CostModel, Router
from wfo.routing.greedy import GreedyEVRouter
from wfo.routing.oracle import SequenceRouter
from wfo.routing.static import StaticWaterfallRouter

STRATEGIES = ("static", "greedy", "thompson", "thompson-flat")


def make_router(
    strategy: str, costs: CostModel, value_per_verified: float, seed: int = 0
) -> Router:
    if strategy == "static":
        return StaticWaterfallRouter(costs)
    if strategy == "greedy":
        return GreedyEVRouter(costs, value_per_verified)
    if strategy == "thompson":
        return ThompsonRouter(costs, value_per_verified, random.Random(seed))
    if strategy == "thompson-flat":
        return ThompsonRouter(costs, value_per_verified, random.Random(seed), depth_aware=False)
    raise ValueError(f"unknown strategy {strategy!r}; choose from {STRATEGIES}")


__all__ = [
    "STRATEGIES",
    "CostModel",
    "GreedyEVRouter",
    "Router",
    "SequenceRouter",
    "StaticWaterfallRouter",
    "ThompsonRouter",
    "make_router",
]
