from __future__ import annotations

import pytest

from tests.conftest import FakeProvider, FakeVerifier, make_contact, make_pipeline
from wfo.clock import VirtualClock
from wfo.ledger import CostLedger
from wfo.models import StopReason, Verification
from wfo.pipeline import PipelineConfig


async def test_stops_at_first_verified_email(clock: VirtualClock) -> None:
    a = FakeProvider("a", 0.02, email=None, clock=clock)
    b = FakeProvider("b", 0.03, email="ada@acme0.example", clock=clock)
    c = FakeProvider("c", 0.05, email="other@acme0.example", clock=clock)
    pipe = make_pipeline({"a": a, "b": b, "c": c}, clock)
    out = await pipe.enrich(make_contact(), "batch:t0")
    assert out.verified
    assert out.provider == "b"
    assert out.stop_reason is StopReason.VERIFIED
    assert [x.provider for x in out.calls] == ["a", "b"]
    assert c.calls == 0
    assert out.cost == pytest.approx(0.02 + 0.03 + 0.003)
    assert pipe.ledger.spent_usd == pytest.approx(out.cost)
    assert out.latency_s == pytest.approx(0.2)


async def test_found_is_not_verified_and_risky_continues(clock: VirtualClock) -> None:
    a = FakeProvider("a", 0.02, email="guess@acme0.example", clock=clock)
    b = FakeProvider("b", 0.03, email="ada@acme0.example", clock=clock)
    v = FakeVerifier(
        by_email={
            "guess@acme0.example": Verification.RISKY,
            "ada@acme0.example": Verification.INVALID,
        }
    )
    out = await make_pipeline({"a": a, "b": b}, clock, verifier=v).enrich(make_contact(), "k")
    assert out.found
    assert not out.verified
    assert out.email == "guess@acme0.example"
    assert out.verification is Verification.RISKY
    assert out.stop_reason is StopReason.EXHAUSTED


async def test_catch_all_domain_stops_waterfall(clock: VirtualClock) -> None:
    a = FakeProvider("a", 0.02, clock=clock)
    b = FakeProvider("b", 0.03, clock=clock)
    out = await make_pipeline(
        {"a": a, "b": b}, clock, verifier=FakeVerifier(Verification.CATCH_ALL)
    ).enrich(make_contact(), "k")
    assert out.stop_reason is StopReason.CATCH_ALL
    assert b.calls == 0


async def test_per_contact_cap_stops_spending(clock: VirtualClock) -> None:
    provs = {n: FakeProvider(n, 0.05, email=None, clock=clock) for n in "abcd"}
    pipe = make_pipeline(provs, clock, config=PipelineConfig(max_spend_per_contact=0.12))
    out = await pipe.enrich(make_contact(), "k")
    assert out.stop_reason is StopReason.BUDGET
    assert out.cost <= 0.12
    assert len(out.calls) == 2


async def test_global_budget_is_never_exceeded(clock: VirtualClock) -> None:
    provs = {n: FakeProvider(n, 0.05, email=None, clock=clock) for n in "ab"}
    ledger = CostLedger(budget_usd=0.33)
    pipe = make_pipeline(provs, clock, ledger=ledger)
    outs = [await pipe.enrich(make_contact(i), f"k{i}") for i in range(10)]
    assert ledger.spent_usd <= 0.33
    assert outs[-1].stop_reason is StopReason.BUDGET


async def test_response_cache_avoids_paying_twice(clock: VirtualClock) -> None:
    a = FakeProvider("a", 0.02, clock=clock)
    pipe = make_pipeline({"a": a}, clock)
    first = await pipe.enrich(make_contact(), "b1:t0")
    second = await pipe.enrich(make_contact(), "b2:t0")  # same person, new batch
    assert a.calls == 1
    assert second.calls[0].from_cache
    assert second.calls[0].cost == 0
    assert first.verified
    assert second.verified


async def test_no_providers_available_is_exhausted(clock: VirtualClock) -> None:
    out = await make_pipeline({}, clock).enrich(make_contact(), "k")
    assert out.stop_reason is StopReason.EXHAUSTED
    assert out.email is None
