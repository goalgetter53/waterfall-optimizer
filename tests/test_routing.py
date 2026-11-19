from __future__ import annotations

import random

from wfo.models import Segment
from wfo.routing import (
    CostModel,
    GreedyEVRouter,
    SequenceRouter,
    StaticWaterfallRouter,
    ThompsonRouter,
    make_router,
)

SEG = Segment("DE", "1-50", "ic")
COSTS = CostModel({"cheap": 0.02, "mid": 0.04, "pricey": 0.08}, 0.003, {"mid": False})
FLAT = CostModel({"cheap": 0.02, "mid": 0.04, "pricey": 0.08}, 0.003)


def test_static_is_cheapest_first_and_budget_aware() -> None:
    r = StaticWaterfallRouter(COSTS)
    assert r.choose(SEG, ["pricey", "mid", "cheap"], 0, 1.0) == "cheap"
    assert r.choose(SEG, ["pricey", "mid"], 1, 1.0) == "mid"
    assert r.choose(SEG, ["pricey"], 2, 0.05) is None


def test_expected_cost_accounts_for_pay_per_hit() -> None:
    assert COSTS.expected_step_cost("mid", 0.5) == 0.04 * 0.5 + 0.5 * 0.003
    assert COSTS.expected_step_cost("cheap", 0.5) == 0.02 + 0.5 * 0.003


def test_greedy_learns_global_rates_and_stops_on_negative_ev() -> None:
    r = GreedyEVRouter(FLAT, value_per_verified=0.5)
    for _ in range(200):
        r.observe(SEG, "pricey", 0, True)
        r.observe(SEG, "cheap", 0, False, found=False)
        r.observe(SEG, "mid", 0, False, found=False)
    assert r.choose(SEG, ["cheap", "mid", "pricey"], 0, 1.0) == "pricey"
    assert r.choose(SEG, ["cheap"], 1, 1.0) is None  # p~0.005 * 0.5 < 0.02


def _simulate(
    router: ThompsonRouter, rates: dict[str, dict[str, float]], n: int, seed: int = 0
) -> None:
    rng = random.Random(seed)
    segs = [Segment.from_key(k) for k in rates]
    for i in range(n):
        seg = segs[i % len(segs)]
        tried: list[str] = []
        while True:
            cands = [p for p in rates[seg.key] if p not in tried]
            p = router.choose(seg, cands, len(tried), 1.0)
            if p is None:
                break
            ok = rng.random() < rates[seg.key][p]
            router.observe(seg, p, len(tried), ok, found=ok)
            tried.append(p)
            if ok:
                break


def test_thompson_learns_segment_specific_best_provider() -> None:
    rates = {
        "DE|1-50|ic": {"cheap": 0.05, "mid": 0.10, "pricey": 0.70},
        "US|1-50|ic": {"cheap": 0.60, "mid": 0.10, "pricey": 0.30},
    }
    r = ThompsonRouter(FLAT, 0.5, random.Random(1))
    _simulate(r, rates, 3000)
    de, us = Segment.from_key("DE|1-50|ic"), Segment.from_key("US|1-50|ic")
    picks_de = [r.choose(de, ["cheap", "mid", "pricey"], 0, 1.0) for _ in range(200)]
    picks_us = [r.choose(us, ["cheap", "mid", "pricey"], 0, 1.0) for _ in range(200)]
    assert picks_de.count("pricey") > 170
    assert picks_us.count("cheap") > 170


def test_pay_per_hit_vendor_is_priced_by_expected_cost() -> None:
    """A 10%-hit pay-per-hit vendor costs ~$0.004/call in expectation, so it should be
    tried before a $0.08 vendor even though its list price is higher than 'cheap'."""
    rates = {"DE|1-50|ic": {"cheap": 0.05, "mid": 0.10, "pricey": 0.70}}
    r = ThompsonRouter(COSTS, 0.5, random.Random(4))
    _simulate(r, rates, 3000)
    picks = [r.choose(SEG, ["cheap", "mid", "pricey"], 0, 1.0) for _ in range(200)]
    assert picks.count("mid") > 150
    assert r.expected_cost(SEG, "mid", 0) < 0.01


def test_thompson_stops_when_nothing_is_worth_calling() -> None:
    rates = {"DE|1-50|ic": {"cheap": 0.01, "mid": 0.01, "pricey": 0.01}}
    r = ThompsonRouter(FLAT, 0.5, random.Random(2))
    _simulate(r, rates, 2000)
    stops = sum(r.choose(SEG, ["cheap", "mid", "pricey"], 0, 1.0) is None for _ in range(200))
    assert stops > 180


def test_thompson_budget_aware_excludes_unaffordable() -> None:
    r = ThompsonRouter(COSTS, 1000.0, random.Random(0))
    for _ in range(50):
        assert r.choose(SEG, ["pricey", "mid"], 0, remaining_usd=0.05) == "mid"
        assert r.choose(SEG, ["pricey"], 0, remaining_usd=0.05) is None


def test_backoff_prior_shares_strength_across_segments() -> None:
    """Data from DE|1-50|ic informs the never-seen DE|501+|exec through the country prior."""
    r = ThompsonRouter(COSTS, 0.5, random.Random(0))
    for _ in range(300):
        r.observe(SEG, "pricey", 0, True)
        r.observe(Segment("US", "1-50", "ic"), "pricey", 0, False, found=False)
    unseen_de = Segment("DE", "501+", "exec")
    unseen_us = Segment("US", "501+", "exec")
    assert r.posterior_mean(unseen_de, "pricey") > 0.7
    assert r.posterior_mean(unseen_us, "pricey") < 0.3


def test_state_roundtrip() -> None:
    a = ThompsonRouter(COSTS, 0.5, random.Random(0))
    a.observe(SEG, "mid", 0, True)
    b = ThompsonRouter(COSTS, 0.5, random.Random(0))
    b.load_state(a.state_dict())
    assert b.posterior(SEG, "mid", 0) == a.posterior(SEG, "mid", 0)
    g = GreedyEVRouter(COSTS, 0.5)
    g.observe(SEG, "mid", 0, True)
    g2 = GreedyEVRouter(COSTS, 0.5)
    g2.load_state(g.state_dict())
    assert g2.mean("mid") == g.mean("mid")


def test_sequence_router_and_factory() -> None:
    r = SequenceRouter({SEG.key: ["pricey", "cheap"]})
    assert r.choose(SEG, ["cheap", "mid", "pricey"], 0, 1) == "pricey"
    assert r.choose(SEG, ["cheap", "mid"], 1, 1) == "cheap"
    assert r.choose(SEG, ["mid"], 2, 1) is None
    assert make_router("thompson-flat", COSTS, 0.5).name == "thompson-flat"
