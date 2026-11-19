"""Optional Postgres backend using ``FOR UPDATE SKIP LOCKED``.

Same interface and semantics as ``SqliteJobQueue`` for multi-host deployments.
Requires ``pip install waterfall-optimizer[postgres]``. Exercised by
``tests/test_postgres_queue.py`` only when ``WFO_PG_DSN`` is set; not run in
default CI.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from wfo.queue.base import BatchStatus, Lease

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wfo_batches (
  id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, strategy TEXT NOT NULL,
  status TEXT NOT NULL, total INTEGER NOT NULL, webhook_url TEXT, webhook_status TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), completed_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS wfo_jobs (
  id BIGSERIAL PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES wfo_batches(id),
  job_key TEXT NOT NULL, payload JSONB NOT NULL, status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL,
  available_at TIMESTAMPTZ NOT NULL DEFAULT now(), lease_owner TEXT, lease_token TEXT,
  lease_expires_at TIMESTAMPTZ, result JSONB, last_error TEXT,
  UNIQUE (batch_id, job_key)
);
CREATE INDEX IF NOT EXISTS wfo_jobs_ready ON wfo_jobs (status, available_at);
"""

_LEASE_SQL = """
WITH picked AS (
  SELECT id FROM wfo_jobs
  WHERE (status = 'queued' AND available_at <= now())
     OR (status = 'leased' AND lease_expires_at <= now() AND attempts < max_attempts)
  ORDER BY id
  FOR UPDATE SKIP LOCKED
  LIMIT %(limit)s
)
UPDATE wfo_jobs j
SET status = 'leased', lease_owner = %(worker)s, lease_token = md5(random()::text || j.id),
    lease_expires_at = now() + make_interval(secs => %(vt)s), attempts = j.attempts + 1
FROM picked WHERE j.id = picked.id
RETURNING j.id, j.batch_id, j.lease_token, j.payload, j.attempts,
          extract(epoch FROM j.lease_expires_at)
"""


class PostgresJobQueue:
    def __init__(self, dsn: str, max_attempts: int = 5) -> None:
        import psycopg

        self._conn = psycopg.connect(dsn, autocommit=True)
        self.max_attempts = max_attempts
        with self._conn.cursor() as cur:
            cur.execute(_SCHEMA)

    def create_batch(
        self,
        payloads: list[tuple[str, dict[str, Any]]],
        strategy: str,
        webhook_url: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[str, bool]:
        with self._conn.transaction(), self._conn.cursor() as cur:
            if idempotency_key is not None:
                cur.execute(
                    "SELECT id FROM wfo_batches WHERE idempotency_key = %s", (idempotency_key,)
                )
                row = cur.fetchone()
                if row:
                    return str(row[0]), False
            batch_id = uuid.uuid4().hex
            cur.execute(
                "INSERT INTO wfo_batches (id, idempotency_key, strategy, status, total, "
                "webhook_url) VALUES (%s, %s, %s, 'running', %s, %s)",
                (batch_id, idempotency_key, strategy, len(payloads), webhook_url),
            )
            cur.executemany(
                "INSERT INTO wfo_jobs (batch_id, job_key, payload, status, max_attempts) "
                "VALUES (%s, %s, %s, 'queued', %s) ON CONFLICT DO NOTHING",
                [(batch_id, k, json.dumps(p), self.max_attempts) for k, p in payloads],
            )
            return batch_id, True

    def lease(self, worker_id: str, visibility_timeout_s: float, limit: int = 1) -> list[Lease]:
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(
                _LEASE_SQL, {"limit": limit, "worker": worker_id, "vt": visibility_timeout_s}
            )
            return [
                Lease(int(r[0]), str(r[1]), str(r[2]), dict(r[3]), int(r[4]), float(r[5]))
                for r in cur.fetchall()
            ]

    def heartbeat(self, lease: Lease, visibility_timeout_s: float) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE wfo_jobs SET lease_expires_at = now() + make_interval(secs => %s) "
                "WHERE id = %s AND lease_token = %s AND status = 'leased' "
                "AND lease_expires_at > now()",
                (visibility_timeout_s, lease.job_id, lease.token),
            )
            return bool(cur.rowcount == 1)

    def complete(self, lease: Lease, result: dict[str, Any]) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE wfo_jobs SET status = 'done', result = %s, lease_token = NULL "
                "WHERE id = %s AND lease_token = %s AND status = 'leased' "
                "AND lease_expires_at > now()",
                (json.dumps(result), lease.job_id, lease.token),
            )
            return bool(cur.rowcount == 1)

    def fail(self, lease: Lease, error: str, retry_delay_s: float = 0.0) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE wfo_jobs SET status = CASE WHEN attempts >= max_attempts THEN 'dead' "
                "ELSE 'queued' END, last_error = %s, lease_token = NULL, "
                "available_at = now() + make_interval(secs => %s) "
                "WHERE id = %s AND lease_token = %s AND status = 'leased'",
                (error[:2000], retry_delay_s, lease.job_id, lease.token),
            )
            return bool(cur.rowcount == 1)

    def recover_expired(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE wfo_jobs SET status = CASE WHEN attempts >= max_attempts THEN 'dead' "
                "ELSE 'queued' END, lease_token = NULL "
                "WHERE status = 'leased' AND lease_expires_at <= now()"
            )
            return int(cur.rowcount)

    def try_finalize_batch(self, batch_id: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE wfo_batches SET status = 'completed', completed_at = now() "
                "WHERE id = %s AND status = 'running' AND NOT EXISTS (SELECT 1 FROM wfo_jobs "
                "WHERE batch_id = %s AND status IN ('queued', 'leased'))",
                (batch_id, batch_id),
            )
            return bool(cur.rowcount == 1)

    def set_webhook_status(self, batch_id: str, status: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE wfo_batches SET webhook_status = %s WHERE id = %s", (status, batch_id)
            )

    def batch_status(self, batch_id: str) -> BatchStatus | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT status, strategy, total, webhook_url, webhook_status, "
                "extract(epoch FROM created_at), extract(epoch FROM completed_at) "
                "FROM wfo_batches WHERE id = %s",
                (batch_id,),
            )
            b = cur.fetchone()
            if b is None:
                return None
            cur.execute(
                "SELECT status, COUNT(*) FROM wfo_jobs WHERE batch_id = %s GROUP BY status",
                (batch_id,),
            )
            counts = {str(r[0]): int(r[1]) for r in cur.fetchall()}
        return BatchStatus(
            batch_id,
            str(b[0]),
            str(b[1]),
            int(b[2]),
            counts,
            b[3],
            b[4],
            float(b[5]),
            None if b[6] is None else float(b[6]),
        )

    def batch_strategy(self, batch_id: str) -> str | None:
        st = self.batch_status(batch_id)
        return None if st is None else st.strategy

    def results(self, batch_id: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT job_key, status, result, last_error FROM wfo_jobs WHERE batch_id = %s "
                "ORDER BY id",
                (batch_id,),
            )
            return [
                {"job_key": r[0], "status": r[1], "result": r[2], "error": r[3]}
                for r in cur.fetchall()
            ]
