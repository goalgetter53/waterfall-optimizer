"""Router interface.

The pipeline calls ``choose`` once per step of a contact's waterfall. Because the
waterfall only continues after a miss, a router that depends on (segment, tried
providers) is equivalent to an ordered provider sequence per segment, which is
what the oracle optimises over.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from wfo.models import Segment


class Router(Protocol):
    name: str

    def choose(
        self, segment: Segment, candidates: Sequence[str], depth: int, remaining_usd: float
    ) -> str | None:
        """Next provider to call, or ``None`` to stop the waterfall for this contact."""
        ...

    def observe(
        self, segment: Segment, provider: str, depth: int, success: bool, found: bool = True
    ) -> None:
        """Feedback: did this call yield a *verified* email (and did it return any email)?"""
        ...

    def state_dict(self) -> dict[str, Any]: ...

    def load_state(self, state: Mapping[str, Any]) -> None: ...


class CostModel:
    """Prices one waterfall step.

    ``step_cost`` is the worst case (list price + verification check).
    ``expected_step_cost`` accounts for pay-per-hit vendors and for the fact that a
    verification check is only paid when an email was found.
    """

    def __init__(
        self,
        provider_costs: Mapping[str, float],
        verify_cost: float,
        charge_on_miss: Mapping[str, bool] | None = None,
    ) -> None:
        self.provider_costs = dict(provider_costs)
        self.verify_cost = verify_cost
        self.charge_on_miss = {p: True for p in self.provider_costs}
        self.charge_on_miss.update(charge_on_miss or {})

    def step_cost(self, provider: str) -> float:
        return self.provider_costs[provider] + self.verify_cost

    def expected_step_cost(self, provider: str, found_rate: float) -> float:
        price = self.provider_costs[provider]
        call = price if self.charge_on_miss[provider] else price * found_rate
        return max(1e-6, call + found_rate * self.verify_cost)

    def affordable(self, candidates: Sequence[str], remaining_usd: float) -> list[str]:
        return [p for p in candidates if self.provider_costs[p] <= remaining_usd + 1e-12]
