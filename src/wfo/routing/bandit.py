"""Contextual Thompson-sampling router.

Arms are (segment, depth bucket, provider) where segment = country x company
size x seniority and depth = how many providers already missed for this contact.
Depth matters because provider misses are correlated: if three vendors cannot
find someone, the fourth's success rate *for that contact* is lower than its
marginal rate, and the stopping rule should know that.

Each arm keeps two Beta posteriors:

* ``theta``  = P(call yields a *verified* email)  -> Thompson-sampled
* ``phi``    = P(call returns any email)          -> posterior mean, used to price
  the call: pay-per-hit vendors and the verification check only cost money when
  something is found, so the expected step cost is
  ``c = price * (1 if charged on miss else phi) + phi * verify_cost``.

Rare segments (e.g. FR x 501+ x exec is ~0.3% of traffic) would take forever to
learn on their own, so each arm's Beta prior is an empirical-Bayes *log-linear
backoff*: the provider's pooled posteriors along each context dimension
(country, size, seniority - at the same depth) are combined additively in logit
space (each dimension's rate first shrunk toward the global rate),

    logit(m0) = sum_dim logit(m_dim) - (n_dims - 1) * logit(m_global)

and turned into a Beta prior with pseudo-count min(n_global, ``prior_strength``). The arm's own
data dominates as it accumulates.

Decision per step:
  1. sample theta_i ~ Beta(alpha_i, beta_i) for every available, affordable provider
  2. drop providers whose sampled net value theta_i * V - c_i is negative
     (budget-aware stop: providers costing more than what is left of the
     per-contact cap or the global ledger budget are never candidates)
  3. call argmax theta_i / c_i (the classic optimal index for sequential search), or stop
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from wfo.models import Segment
from wfo.routing.base import CostModel

Table = dict[str, list[int]]


def depth_bucket(depth: int) -> int:
    return min(depth, 2)


def _new_table() -> Table:
    return defaultdict(lambda: [0, 0])


def _logit(m: float) -> float:
    m = min(max(m, 1e-9), 1 - 1e-9)
    return math.log(m / (1 - m))


class ThompsonRouter:
    def __init__(
        self,
        costs: CostModel,
        value_per_verified: float,
        rng: random.Random | None = None,
        depth_aware: bool = True,
        prior_strength: float = 20.0,
        name: str | None = None,
    ) -> None:
        self.costs = costs
        self.value = value_per_verified
        self.rng = rng or random.Random(0)
        self.depth_aware = depth_aware
        self.prior_strength = prior_strength
        self.name = name or ("thompson" if depth_aware else "thompson-flat")
        self.tables: dict[str, Table] = {
            "local": _new_table(),
            "pooled": _new_table(),
            "local_found": _new_table(),
            "pooled_found": _new_table(),
        }

    # ---- keys ---------------------------------------------------------------
    def _depth(self, depth: int) -> int:
        return depth_bucket(depth) if self.depth_aware else 0

    def _local_key(self, segment: Segment, provider: str, depth: int) -> str:
        return f"{segment.key}#{self._depth(depth)}#{provider}"

    def _pooled_keys(self, segment: Segment, provider: str, depth: int) -> list[str]:
        d = self._depth(depth)
        return [
            f"*#{d}#{provider}",
            f"c={segment.country}#{d}#{provider}",
            f"s={segment.size}#{d}#{provider}",
            f"r={segment.seniority}#{d}#{provider}",
        ]

    # ---- posteriors ---------------------------------------------------------
    def _beta(self, which: str, segment: Segment, provider: str, depth: int) -> tuple[float, float]:
        local = self.tables["local" + which]
        pooled = self.tables["pooled" + which]
        glob, *dims = self._pooled_keys(segment, provider, depth)
        gs, gf = pooled.get(glob, (0, 0))
        m_g = (1 + gs) / (2 + gs + gf)
        z = -(len(dims) - 1) * _logit(m_g)
        for key in dims:
            ds, df = pooled.get(key, (0, 0))
            # each dimension's rate is shrunk toward the provider's global rate
            z += _logit((ds + 2.0 * m_g) / (ds + df + 2.0))
        m0 = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
        k = min(float(gs + gf), self.prior_strength)
        s, f = local.get(self._local_key(segment, provider, depth), (0, 0))
        return 1.0 + k * m0 + s, 1.0 + k * (1.0 - m0) + f

    def posterior(self, segment: Segment, provider: str, depth: int) -> tuple[float, float]:
        """Beta parameters of P(verified) for this arm."""
        return self._beta("", segment, provider, depth)

    def posterior_mean(self, segment: Segment, provider: str, depth: int = 0) -> float:
        a, b = self.posterior(segment, provider, depth)
        return a / (a + b)

    def found_rate(self, segment: Segment, provider: str, depth: int = 0) -> float:
        a, b = self._beta("_found", segment, provider, depth)
        return a / (a + b)

    def expected_cost(self, segment: Segment, provider: str, depth: int) -> float:
        return self.costs.expected_step_cost(provider, self.found_rate(segment, provider, depth))

    # ---- policy -------------------------------------------------------------
    def choose(
        self, segment: Segment, candidates: Sequence[str], depth: int, remaining_usd: float
    ) -> str | None:
        best: tuple[float, str] | None = None
        for p in sorted(self.costs.affordable(candidates, remaining_usd)):
            a, b = self.posterior(segment, p, depth)
            theta = self.rng.betavariate(a, b)
            c = self.expected_cost(segment, p, depth)
            if theta * self.value - c < 0:
                continue
            score = theta / c
            if best is None or score > best[0]:
                best = (score, p)
        return None if best is None else best[1]

    def observe(
        self, segment: Segment, provider: str, depth: int, success: bool, found: bool = True
    ) -> None:
        lk = self._local_key(segment, provider, depth)
        pks = self._pooled_keys(segment, provider, depth)
        for which, flag in (("", success), ("_found", found or success)):
            idx = 0 if flag else 1
            self.tables["local" + which][lk][idx] += 1
            for key in pks:
                self.tables["pooled" + which][key][idx] += 1

    # ---- persistence --------------------------------------------------------
    def state_dict(self) -> dict[str, Any]:
        return {name: dict(t) for name, t in self.tables.items()}

    def load_state(self, state: Mapping[str, Any]) -> None:
        for name in self.tables:
            table = _new_table()
            for k, v in state.get(name, {}).items():
                table[k] = [int(v[0]), int(v[1])]
            self.tables[name] = table
