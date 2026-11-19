"""Runs only against a real Postgres: WFO_PG_DSN=postgresql://... pytest -m postgres"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.postgres
DSN = os.environ.get("WFO_PG_DSN")


@pytest.mark.skipif(not DSN, reason="WFO_PG_DSN not set")
def test_skip_locked_lease_roundtrip() -> None:
    from wfo.queue.postgres import PostgresJobQueue

    q = PostgresJobQueue(DSN or "")
    key = uuid.uuid4().hex
    batch, created = q.create_batch([("c1", {"x": 1})], "static", idempotency_key=key)
    assert created
    assert q.create_batch([("c1", {})], "static", idempotency_key=key) == (batch, False)
    (lease,) = q.lease("w1", 30)
    assert q.lease("w2", 30) == [] or all(item.batch_id != batch for item in q.lease("w2", 30))
    assert q.complete(lease, {"ok": True})
    assert q.try_finalize_batch(batch)
