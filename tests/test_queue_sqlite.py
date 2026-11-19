from __future__ import annotations

import threading
from pathlib import Path

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from wfo.queue import Lease, SqliteJobQueue


class Clock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t


def make_queue(
    path: str | Path = ":memory:", max_attempts: int = 3
) -> tuple[SqliteJobQueue, Clock]:
    clock = Clock()
    return SqliteJobQueue(path, now=clock, max_attempts=max_attempts), clock


def test_create_batch_is_idempotent_and_dedupes_jobs() -> None:
    q, _ = make_queue()
    jobs = [("c1", {"x": 1}), ("c2", {"x": 2}), ("c1", {"x": 3})]
    b1, created1 = q.create_batch(jobs, "thompson", idempotency_key="upload-42")
    b2, created2 = q.create_batch(jobs, "thompson", idempotency_key="upload-42")
    assert created1
    assert not created2
    assert b1 == b2
    st_ = q.batch_status(b1)
    assert st_ is not None
    assert st_.total == 2


def test_lease_complete_and_finalize_exactly_once() -> None:
    q, _ = make_queue()
    b, _ = q.create_batch([("c1", {}), ("c2", {})], "static")
    leases = q.lease("w1", 30, limit=5)
    assert len(leases) == 2
    assert q.lease("w2", 30) == []
    assert not q.try_finalize_batch(b)
    for lease in leases:
        assert q.complete(lease, {"ok": True})
    assert q.try_finalize_batch(b)
    assert not q.try_finalize_batch(b)
    st_ = q.batch_status(b)
    assert st_ is not None
    assert st_.status == "completed"
    assert st_.finished == 2


def test_expired_lease_is_fenced() -> None:
    q, clock = make_queue()
    q.create_batch([("c1", {})], "static")
    (old,) = q.lease("crashed-worker", 10)
    clock.t += 11
    (new,) = q.lease("w2", 10)
    assert new.job_id == old.job_id
    assert new.token != old.token
    assert new.attempts == 2
    assert not q.complete(old, {"late": True})  # zombie write rejected
    assert not q.heartbeat(old, 10)
    assert q.complete(new, {"ok": True})


def test_heartbeat_extends_lease() -> None:
    q, clock = make_queue()
    q.create_batch([("c1", {})], "static")
    (lease,) = q.lease("w1", 10)
    clock.t += 8
    assert q.heartbeat(lease, 10)
    clock.t += 8
    assert q.lease("w2", 10) == []  # still owned thanks to heartbeat
    assert q.complete(lease, {})


def test_fail_requeues_then_dead_letters() -> None:
    q, clock = make_queue(max_attempts=2)
    b, _ = q.create_batch([("c1", {})], "static")
    (l1,) = q.lease("w", 10)
    assert q.fail(l1, "boom", retry_delay_s=5)
    assert q.lease("w", 10) == []  # not yet available
    clock.t += 5
    (l2,) = q.lease("w", 10)
    assert q.fail(l2, "boom again")
    assert q.counts() == {"dead": 1}
    assert q.try_finalize_batch(b)


def test_recover_expired_sweeps_and_dead_letters() -> None:
    q, clock = make_queue(max_attempts=1)
    q.create_batch([("c1", {}), ("c2", {})], "static")
    q.lease("w", 10, limit=2)
    clock.t += 20
    q.recover_expired()
    assert q.counts() == {"dead": 2}


def test_concurrent_workers_on_separate_connections_never_share_a_job(tmp_path: Path) -> None:
    path = tmp_path / "q.db"
    q0, _ = make_queue(path)
    q0.create_batch([(f"c{i}", {"i": i}) for i in range(300)], "static")
    seen: dict[str, list[int]] = {}
    lock = threading.Lock()

    def worker(name: str) -> None:
        q = SqliteJobQueue(path)
        mine: list[int] = []
        while True:
            got = q.lease(name, 60, limit=3)
            if not got:
                break
            for lease in got:
                mine.append(lease.job_id)
                assert q.complete(lease, {"by": name})
        with lock:
            seen[name] = mine

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    all_ids = [j for ids in seen.values() for j in ids]
    assert len(all_ids) == 300
    assert len(set(all_ids)) == 300


class LeaseMachine(RuleBasedStateMachine):
    """Stateful property: a job is never leased to a second worker while another
    worker's lease on it is still live, completed jobs are never re-leased, and
    only the current lease holder can complete or heartbeat."""

    def __init__(self) -> None:
        super().__init__()
        self.clock = Clock()
        self.q = SqliteJobQueue(":memory:", now=self.clock, max_attempts=10_000)
        self.q.create_batch([(f"k{i}", {}) for i in range(3)], "static")
        self.held: dict[str, Lease] = {}
        self.expiry: dict[str, float] = {}  # token -> expiry (model)
        self.current: dict[int, str] = {}  # job -> latest token issued
        self.done: set[int] = set()

    def _live(self, lease: Lease) -> bool:
        return (
            self.current.get(lease.job_id) == lease.token
            and self.expiry[lease.token] > self.clock.t
            and lease.job_id not in self.done
        )

    @rule(worker=st.sampled_from("abcd"))
    def lease(self, worker: str) -> None:
        if worker in self.held and self._live(self.held[worker]):
            return
        got = self.q.lease(worker, 10.0)
        if not got:
            return
        new = got[0]
        assert new.job_id not in self.done
        for other in self.held.values():
            if other.job_id == new.job_id:
                assert not self._live(other), "job leased twice concurrently"
        self.held[worker] = new
        self.expiry[new.token] = new.expires_at
        self.current[new.job_id] = new.token

    @rule(worker=st.sampled_from("abcd"))
    def complete(self, worker: str) -> None:
        lease = self.held.get(worker)
        if lease is None:
            return
        expected = self._live(lease)
        assert self.q.complete(lease, {"by": worker}) is expected
        if expected:
            self.done.add(lease.job_id)

    @rule(worker=st.sampled_from("abcd"))
    def heartbeat(self, worker: str) -> None:
        lease = self.held.get(worker)
        if lease is None:
            return
        expected = self._live(lease)
        assert self.q.heartbeat(lease, 10.0) is expected
        if expected:
            self.expiry[lease.token] = self.clock.t + 10.0

    @rule(dt=st.floats(0, 12))
    def advance(self, dt: float) -> None:
        self.clock.t += dt

    @invariant()
    def at_most_one_live_lease_per_job(self) -> None:
        live = [lease.job_id for lease in self.held.values() if self._live(lease)]
        assert len(live) == len(set(live))


TestLeaseMachine = LeaseMachine.TestCase
TestLeaseMachine.settings = settings(max_examples=60, stateful_step_count=40, deadline=None)
