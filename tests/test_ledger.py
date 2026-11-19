from __future__ import annotations

import asyncio
import random
import threading
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from wfo.ledger import CostLedger, Reservation


def test_reserve_commit_release_cycle() -> None:
    ledger = CostLedger(budget_usd=1.0)
    r = ledger.reserve(0.4)
    assert r is not None
    assert ledger.reserved_usd == pytest.approx(0.4)
    assert ledger.commit(r, "job1:hunter", "hunter", 0.3) == pytest.approx(0.3)
    assert ledger.reserved_usd == 0
    assert ledger.spent_usd == pytest.approx(0.3)
    r2 = ledger.reserve(0.5)
    assert r2 is not None
    ledger.release(r2)
    assert ledger.remaining_usd() == pytest.approx(0.7)
    assert ledger.spent_by_provider() == {"hunter": pytest.approx(0.3)}


def test_refuses_over_budget() -> None:
    ledger = CostLedger(budget_usd=0.05)
    assert ledger.reserve(0.04) is not None
    assert ledger.reserve(0.02) is None


def test_commit_is_idempotent_per_key() -> None:
    ledger = CostLedger()
    r1, r2 = ledger.reserve(0.1), ledger.reserve(0.1)
    assert r1 is not None
    assert r2 is not None
    assert ledger.commit(r1, "b:c1:apollo", "apollo", 0.1) == pytest.approx(0.1)
    assert ledger.commit(r2, "b:c1:apollo", "apollo", 0.1) == 0.0  # replay after crash
    assert ledger.spent_usd == pytest.approx(0.1)
    assert ledger.reserved_usd == 0


def test_overcharge_is_clamped_to_reservation() -> None:
    ledger = CostLedger(budget_usd=0.1)
    r = ledger.reserve(0.1)
    assert r is not None
    assert ledger.commit(r, "k", "p", 5.0) == pytest.approx(0.1)
    assert ledger.spent_usd <= 0.1


def test_settled_reservation_cannot_commit_twice() -> None:
    ledger = CostLedger()
    r = ledger.reserve(0.1)
    assert r is not None
    ledger.commit(r, "k", "p", 0.1)
    with pytest.raises(KeyError):
        ledger.commit(r, "k2", "p", 0.1)


def test_release_stale_recovers_crashed_holds() -> None:
    ledger = CostLedger(budget_usd=1.0)
    assert ledger.reserve(0.6) is not None
    assert ledger.release_stale(older_than_s=-1) == 1
    assert ledger.reserved_usd == 0


ops = st.lists(
    st.one_of(
        st.tuples(st.just("reserve"), st.floats(0, 0.5)),
        st.tuples(st.just("commit"), st.integers(0, 50), st.floats(0, 1), st.integers(0, 8)),
        st.tuples(st.just("release"), st.integers(0, 50)),
    ),
    max_size=80,
)


@settings(max_examples=150, deadline=None)
@given(budget=st.floats(0.01, 3), ops=ops)
def test_property_ledger_never_exceeds_budget(budget: float, ops: list[tuple]) -> None:  # type: ignore[type-arg]
    """For ANY sequence of reserve/commit/release (incl. replays and overcharges),
    spent + reserved <= budget and spent equals the sum of ledger entries."""
    ledger = CostLedger(budget_usd=budget)
    open_res: list[Reservation] = []
    for op in ops:
        if op[0] == "reserve":
            r = ledger.reserve(op[1])
            if r is not None:
                open_res.append(r)
        elif op[0] == "commit" and open_res:
            r = open_res.pop(op[1] % len(open_res))
            ledger.commit(r, f"key{op[3]}", "p", op[2])
        elif op[0] == "release" and open_res:
            ledger.release(open_res.pop(op[1] % len(open_res)))
        assert ledger.spent_usd + ledger.reserved_usd <= budget + 1e-6
        assert ledger.spent_usd == pytest.approx(ledger.entries_total_usd())


async def test_concurrent_tasks_cannot_overspend() -> None:
    ledger = CostLedger(budget_usd=1.0)
    rng = random.Random(3)

    async def worker(i: int) -> None:
        for j in range(20):
            r = ledger.reserve(rng.uniform(0.01, 0.1))
            if r is None:
                return
            await asyncio.sleep(0)  # interleave with other tasks mid-call
            ledger.commit(r, f"{i}:{j}", "p", r.amount_micros / 1e6)

    await asyncio.gather(*(worker(i) for i in range(25)))
    assert ledger.spent_usd <= 1.0 + 1e-9
    assert ledger.reserved_usd == 0


def test_two_processes_style_connections_share_budget(tmp_path: Path) -> None:
    """Separate connections (as separate worker processes would have) on one WAL file."""
    path = tmp_path / "ledger.db"
    CostLedger(path, budget_usd=2.0)
    errors: list[BaseException] = []

    def hammer(seed: int) -> None:
        try:
            ledger = CostLedger(path)
            rng = random.Random(seed)
            for j in range(60):
                r = ledger.reserve(rng.uniform(0.01, 0.08))
                if r is not None:
                    ledger.commit(r, f"{seed}:{j}", "p", r.amount_micros / 1e6)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(s,)) for s in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    final = CostLedger(path)
    assert final.spent_usd <= 2.0 + 1e-9
    assert final.spent_usd == pytest.approx(final.entries_total_usd())
