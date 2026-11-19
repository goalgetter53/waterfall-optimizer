"""SQLite (WAL) implementation of the durable lease queue.

Every state transition runs inside ``BEGIN IMMEDIATE``, which takes SQLite's
single write lock up front; combined with a fencing token per lease this gives
"at most one live lease per job" across threads *and* processes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wfo.db import connect, immediate
from wfo.queue.base import BatchStatus, JobStatus, Lease

_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT UNIQUE,
  strategy TEXT NOT NULL,
  status TEXT NOT NULL,
  total INTEGER NOT NULL,
  webhook_url TEXT,
  webhook_status TEXT,
  created_at REAL NOT NULL,
  completed_at REAL
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id TEXT NOT NULL REFERENCES batches(id),
  job_key TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL,
  available_at REAL NOT NULL,
  lease_owner TEXT,
  lease_token TEXT,
  lease_expires_at REAL,
  result TEXT,
  last_error TEXT,
  updated_at REAL NOT NULL,
  UNIQUE (batch_id, job_key)
);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs (status, available_at);
CREATE INDEX IF NOT EXISTS jobs_lease ON jobs (status, lease_expires_at);
CREATE INDEX IF NOT EXISTS jobs_batch ON jobs (batch_id, status);
"""


class SqliteJobQueue:
    def __init__(
        self,
        path: str | Path,
        now: Callable[[], float] = time.time,
        max_attempts: int = 5,
    ) -> None:
        self._conn: sqlite3.Connection = connect(path)
        self._lock = threading.Lock()
        self._now = now
        self.max_attempts = max_attempts
        self._conn.executescript(_SCHEMA)

    # ---- batches ----------------------------------------------------------
    def create_batch(
        self,
        payloads: list[tuple[str, dict[str, Any]]],
        strategy: str,
        webhook_url: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[str, bool]:
        now = self._now()
        with self._lock, immediate(self._conn) as c:
            if idempotency_key is not None:
                row = c.execute(
                    "SELECT id FROM batches WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if row is not None:
                    return str(row[0]), False
            batch_id = uuid.uuid4().hex
            c.execute(
                "INSERT INTO batches (id, idempotency_key, strategy, status, total, webhook_url,"
                " created_at) VALUES (?, ?, ?, 'running', 0, ?, ?)",
                (batch_id, idempotency_key, strategy, webhook_url, now),
            )
            # Duplicate job keys inside one batch are collapsed (idempotent enqueue).
            c.executemany(
                "INSERT OR IGNORE INTO jobs (batch_id, job_key, payload, status, max_attempts,"
                " available_at, updated_at) VALUES (?, ?, ?, 'queued', ?, ?, ?)",
                [(batch_id, k, json.dumps(p), self.max_attempts, now, now) for k, p in payloads],
            )
            total = c.execute(
                "SELECT COUNT(*) FROM jobs WHERE batch_id = ?", (batch_id,)
            ).fetchone()[0]
            c.execute("UPDATE batches SET total = ? WHERE id = ?", (total, batch_id))
            if total == 0:
                c.execute(
                    "UPDATE batches SET status = 'completed', completed_at = ? WHERE id = ?",
                    (now, batch_id),
                )
            return batch_id, True

    # ---- leasing ----------------------------------------------------------
    def lease(self, worker_id: str, visibility_timeout_s: float, limit: int = 1) -> list[Lease]:
        now = self._now()
        out: list[Lease] = []
        with self._lock, immediate(self._conn) as c:
            self._dead_letter_exhausted(c, now)
            rows = c.execute(
                "SELECT id, batch_id, payload, attempts FROM jobs "
                "WHERE (status = 'queued' AND available_at <= ?) "
                "   OR (status = 'leased' AND lease_expires_at <= ?) "
                "ORDER BY id LIMIT ?",
                (now, now, limit),
            ).fetchall()
            for r in rows:
                token = uuid.uuid4().hex
                expires = now + visibility_timeout_s
                c.execute(
                    "UPDATE jobs SET status = 'leased', lease_owner = ?, lease_token = ?, "
                    "lease_expires_at = ?, attempts = attempts + 1, updated_at = ? WHERE id = ?",
                    (worker_id, token, expires, now, r[0]),
                )
                out.append(
                    Lease(int(r[0]), str(r[1]), token, json.loads(r[2]), int(r[3]) + 1, expires)
                )
        return out

    def _dead_letter_exhausted(self, c: sqlite3.Connection, now: float) -> None:
        c.execute(
            "UPDATE jobs SET status = 'dead', last_error = COALESCE(last_error, "
            "'lease expired after max attempts'), lease_token = NULL, updated_at = ? "
            "WHERE status = 'leased' AND lease_expires_at <= ? AND attempts >= max_attempts",
            (now, now),
        )

    def heartbeat(self, lease: Lease, visibility_timeout_s: float) -> bool:
        now = self._now()
        with self._lock, immediate(self._conn) as c:
            cur = c.execute(
                "UPDATE jobs SET lease_expires_at = ?, updated_at = ? "
                "WHERE id = ? AND lease_token = ? AND status = 'leased' AND lease_expires_at > ?",
                (now + visibility_timeout_s, now, lease.job_id, lease.token, now),
            )
            return cur.rowcount == 1

    def complete(self, lease: Lease, result: dict[str, Any]) -> bool:
        """Fenced completion: only the current, unexpired lease holder can complete."""
        now = self._now()
        with self._lock, immediate(self._conn) as c:
            cur = c.execute(
                "UPDATE jobs SET status = 'done', result = ?, lease_token = NULL, updated_at = ? "
                "WHERE id = ? AND lease_token = ? AND status = 'leased' AND lease_expires_at > ?",
                (json.dumps(result), now, lease.job_id, lease.token, now),
            )
            return cur.rowcount == 1

    def fail(self, lease: Lease, error: str, retry_delay_s: float = 0.0) -> bool:
        now = self._now()
        with self._lock, immediate(self._conn) as c:
            row = c.execute(
                "SELECT attempts, max_attempts FROM jobs WHERE id = ? AND lease_token = ? "
                "AND status = 'leased' AND lease_expires_at > ?",
                (lease.job_id, lease.token, now),
            ).fetchone()
            if row is None:
                return False
            status = JobStatus.DEAD if int(row[0]) >= int(row[1]) else JobStatus.QUEUED
            c.execute(
                "UPDATE jobs SET status = ?, last_error = ?, lease_token = NULL, "
                "available_at = ?, updated_at = ? WHERE id = ?",
                (status.value, error[:2000], now + retry_delay_s, now, lease.job_id),
            )
            return True

    def recover_expired(self) -> int:
        """Sweep: requeue expired leases (or dead-letter them if out of attempts)."""
        now = self._now()
        with self._lock, immediate(self._conn) as c:
            self._dead_letter_exhausted(c, now)
            cur = c.execute(
                "UPDATE jobs SET status = 'queued', lease_token = NULL, lease_owner = NULL, "
                "updated_at = ? WHERE status = 'leased' AND lease_expires_at <= ?",
                (now, now),
            )
            return cur.rowcount

    # ---- batch completion -------------------------------------------------
    def try_finalize_batch(self, batch_id: str) -> bool:
        """Mark the batch completed iff all jobs are terminal. True exactly once."""
        now = self._now()
        with self._lock, immediate(self._conn) as c:
            pending = c.execute(
                "SELECT COUNT(*) FROM jobs WHERE batch_id = ? AND status IN ('queued', 'leased')",
                (batch_id,),
            ).fetchone()[0]
            if pending:
                return False
            cur = c.execute(
                "UPDATE batches SET status = 'completed', completed_at = ? "
                "WHERE id = ? AND status = 'running'",
                (now, batch_id),
            )
            return cur.rowcount == 1

    def set_webhook_status(self, batch_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE batches SET webhook_status = ? WHERE id = ?", (status, batch_id)
            )

    # ---- reads ------------------------------------------------------------
    def batch_status(self, batch_id: str) -> BatchStatus | None:
        with self._lock:
            b = self._conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if b is None:
                return None
            counts = {
                str(r[0]): int(r[1])
                for r in self._conn.execute(
                    "SELECT status, COUNT(*) FROM jobs WHERE batch_id = ? GROUP BY status",
                    (batch_id,),
                )
            }
        return BatchStatus(
            batch_id=batch_id,
            status=str(b["status"]),
            strategy=str(b["strategy"]),
            total=int(b["total"]),
            counts=counts,
            webhook_url=b["webhook_url"],
            webhook_status=b["webhook_status"],
            created_at=float(b["created_at"]),
            completed_at=b["completed_at"],
        )

    def batch_strategy(self, batch_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT strategy FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()
        return None if row is None else str(row[0])

    def results(self, batch_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_key, status, result, last_error FROM jobs WHERE batch_id = ? "
                "ORDER BY id",
                (batch_id,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "job_key": r[0],
                    "status": r[1],
                    "result": json.loads(r[2]) if r[2] else None,
                    "error": r[3],
                }
            )
        return out

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {
                str(r[0]): int(r[1])
                for r in self._conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status")
            }
