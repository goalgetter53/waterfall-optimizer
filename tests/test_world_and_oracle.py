from __future__ import annotations

import asyncio
import dataclasses

from wfo.clock import VirtualClock
from wfo.models import Segment, Verification
from wfo.providers.simulated import build_simulated_providers
from wfo.sim.oracle import best_sequences, evaluate_sequences, ordered_subsets
from wfo.sim.stats import mean_ci95, percentile, t_critical
from wfo.sim.world import SyntheticWorld
from wfo.verification import ProviderStatusVerifier, SimulatedVerifier


def test_world_is_deterministic_per_seed() -> None:
    a, b, c = SyntheticWorld(3), SyntheticWorld(3), SyntheticWorld(4)
    assert a.params == b.params
    assert a.params != c.params
    assert a.generate_contacts(50) == b.generate_contacts(50)
    contact = a.generate_contacts(1)[0]
    assert a.truth(contact) == b.truth(contact)


def test_segment_mix_and_probabilities() -> None:
    w = SyntheticWorld(1)
    contacts = w.generate_contacts(2000)
    assert len({c.segment.key for c in contacts}) > 35
    for key in w.params:
        for p in w.specs:
            assert 0.0 <= w.p_verified(Segment.from_key(key), p) <= 1.0


def test_simulated_providers_use_common_random_numbers() -> None:
    w = SyntheticWorld(5)
    contacts = w.generate_contacts(200)

    async def outcomes() -> list[str | None]:
        clock = VirtualClock()
        prov = build_simulated_providers(w, clock)["hunter"]
        prov.spec = dataclasses.replace(prov.spec, error_rate=0.0, rate_limit_rate=0.0)
        return [(await prov.find_email(c)).email for c in contacts]

    assert asyncio.run(outcomes()) == asyncio.run(outcomes())


async def test_verifier_distinguishes_found_from_verified() -> None:
    w = SyntheticWorld(2)
    clock = VirtualClock()
    v = SimulatedVerifier(w, clock)
    statuses = []
    for c in w.generate_contacts(400):
        truth = w.truth(c)
        statuses.append(await v.verify(c, truth.true_email, None))
        if not truth.catch_all:
            wrong = await v.verify(c, "nobody@" + c.company_domain, None)
            assert wrong is not Verification.CATCH_ALL
    assert Verification.CATCH_ALL in statuses
    assert Verification.VALID in statuses
    assert clock.now() > 0


async def test_provider_status_verifier_maps_vendor_codes() -> None:
    v = ProviderStatusVerifier()
    c = SyntheticWorld(1).generate_contacts(1)[0]
    assert await v.verify(c, "a@b.c", "deliverable") is Verification.VALID
    assert await v.verify(c, "a@b.c", "accept_all") is Verification.CATCH_ALL
    assert await v.verify(c, "a@b.c", None) is Verification.RISKY


def test_oracle_dominates_any_fixed_sequence() -> None:
    assert len(ordered_subsets(["a", "b", "c", "d"])) == 65
    w = SyntheticWorld(9)
    best, value = best_sequences(w, 0.5, 0.003, n_draws=1500)
    static = {k: ["apollo", "hunter", "prospeo", "dropcontact"] for k in best}
    nothing: dict[str, list[str]] = {k: [] for k in best}
    s_val = evaluate_sequences(w, static, 0.5, 0.003, n_draws=1500)
    n_val = evaluate_sequences(w, nothing, 0.5, 0.003, n_draws=1500)
    for k in best:
        assert value[k] >= s_val[k] - 1e-12
        assert value[k] >= n_val[k] == 0.0


def test_stats_helpers() -> None:
    m, hw = mean_ci95([1.0, 2.0, 3.0])
    assert m == 2.0
    assert abs(hw - t_critical(2) * (1 / 3**0.5)) < 1e-9
    assert mean_ci95([5.0]) == (5.0, 0.0)
    assert percentile([1, 2, 3, 4, 5], 50) == 3
    assert percentile(list(range(101)), 95) == 95
