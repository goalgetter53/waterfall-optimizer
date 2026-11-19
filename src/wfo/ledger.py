"""Cost ledger with a hard budget, reservations and idempotent charges.

Invariant: ``spent + reserved <= budget`` at every commit point. Callers reserve
a provider's maximum cost *before* calling it, then commit the actual charge
(<= reservation) or release. Amounts are stored as integer micro-dollars to
avoid floating-point drift over millions of entries.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from wfo.db import connect, immediate

MICROS = 1_000_000
UNLIMITED = 2**62


def to_micros(usd: float) -> int:
    return round(usd * MICROS)


def to_usd(micros: int) -> float:
    return micros / MICROS


class BudgetExceededError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Reservation:
    id: str
    amount_micros: int


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  budget INTEGER NOT NULL, spent INTEGER NOT NULL, reserved INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_reservations (
  id TEXT PRIMARY KEY, amount INTEGER NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_entries (
  idem_key TEXT PRIMARY KEY, provider TEXT NOT NULL, amount INTEGER NOT NULL,
  created_at REAL NOT NULL
);
"""


class CostLedger:
    def __init__(self, path: str | Path = ":memory:", budget_usd: float | None = None) -> None:
        self._conn: sqlite3.Connection = connect(path)
        self._lock = threading.Lock()
        self._conn.executescript(_SCHEMA)
        budget = UNLIMITED if budget_usd is None else to_micros(budget_usd)
        with self._lock, immediate(self._conn) as c:
            c.execute(
                "INSERT OR IGNORE INTO ledger_state (id, budget, spent, reserved) "
                "VALUES (1, ?, 0, 0)",
                (budget,),
            )
            if budget_usd is not None:
                c.execute("UPDATE ledger_state SET budget = ? WHERE id = 1", (budget,))

    def _state(self, c: sqlite3.Connection) -> tuple[int, int, int]:
        row = c.execute("SELECT budget, spent, reserved FROM ledger_state WHERE id = 1").fetchone()
        return int(row[0]), int(row[1]), int(row[2])

    # ---- core operations --------------------------------------------------
    def reserve(self, amount_usd: float) -> Reservation | None:
        """Atomically hold ``amount_usd``; ``None`` if it would exceed the budget."""
        amount = to_micros(amount_usd)
        if amount < 0:
            raise ValueError("amount must be non-negative")
        with self._lock, immediate(self._conn) as c:
            budget, spent, reserved = self._state(c)
            if spent + reserved + amount > budget:
                return None
            rid = uuid.uuid4().hex
            c.execute(
                "INSERT INTO ledger_reservations (id, amount, created_at) VALUES (?, ?, ?)",
                (rid, amount, time.time()),
            )
            c.execute("UPDATE ledger_state SET reserved = reserved + ? WHERE id = 1", (amount,))
            return Reservation(rid, amount)

    def commit(
        self, reservation: Reservation, idem_key: str, provider: str, actual_usd: float
    ) -> float:
        """Convert a reservation into a charge. Idempotent on ``idem_key``.

        Returns the amount recorded (0.0 if ``idem_key`` was already charged).
        A charge above the reservation is clamped so the budget can never be breached.
        """
        actual = max(0, min(to_micros(actual_usd), reservation.amount_micros))
        with self._lock, immediate(self._conn) as c:
            row = c.execute(
                "SELECT amount FROM ledger_reservations WHERE id = ?", (reservation.id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown or settled reservation {reservation.id}")
            held = int(row[0])
            c.execute("DELETE FROM ledger_reservations WHERE id = ?", (reservation.id,))
            c.execute("UPDATE ledger_state SET reserved = reserved - ? WHERE id = 1", (held,))
            if actual == 0:
                return 0.0
            cur = c.execute(
                "INSERT OR IGNORE INTO ledger_entries (idem_key, provider, amount, created_at) "
                "VALUES (?, ?, ?, ?)",
                (idem_key, provider, actual, time.time()),
            )
            if cur.rowcount == 0:
                return 0.0
            c.execute("UPDATE ledger_state SET spent = spent + ? WHERE id = 1", (actual,))
            return to_usd(actual)

    def release(self, reservation: Reservation) -> None:
        with self._lock, immediate(self._conn) as c:
            cur = c.execute("DELETE FROM ledger_reservations WHERE id = ?", (reservation.id,))
            if cur.rowcount:
                c.execute(
                    "UPDATE ledger_state SET reserved = reserved - ? WHERE id = 1",
                    (reservation.amount_micros,),
                )

    def release_stale(self, older_than_s: float) -> int:
        """Crash recovery: free reservations held by workers that died mid-call."""
        cutoff = time.time() - older_than_s
        with self._lock, immediate(self._conn) as c:
            rows = c.execute(
                "SELECT id, amount FROM ledger_reservations WHERE created_at < ?", (cutoff,)
            ).fetchall()
            for r in rows:
                c.execute("DELETE FROM ledger_reservations WHERE id = ?", (r[0],))
                c.execute(
                    "UPDATE ledger_state SET reserved = reserved - ? WHERE id = 1", (int(r[1]),)
                )
            return len(rows)

    # ---- reporting --------------------------------------------------------
    @property
    def budget_usd(self) -> float | None:
        with self._lock:
            b = self._state(self._conn)[0]
        return None if b >= UNLIMITED else to_usd(b)

    @property
    def spent_usd(self) -> float:
        with self._lock:
            return to_usd(self._state(self._conn)[1])

    @property
    def reserved_usd(self) -> float:
        with self._lock:
            return to_usd(self._state(self._conn)[2])

    def remaining_usd(self) -> float:
        with self._lock:
            b, s, r = self._state(self._conn)
        return float("inf") if b >= UNLIMITED else to_usd(b - s - r)

    def spent_by_provider(self) -> dict[str, float]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT provider, SUM(amount) FROM ledger_entries GROUP BY provider"
            ).fetchall()
        return {str(r[0]): to_usd(int(r[1])) for r in rows}

    def entries_total_usd(self) -> float:
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(SUM(amount), 0) FROM ledger_entries")
            return to_usd(int(row.fetchone()[0]))
