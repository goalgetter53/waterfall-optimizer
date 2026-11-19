"""Provider response cache with TTL (positive and negative results).

A cache hit costs nothing, so it is also the mechanism that makes a recovered
job (after a worker crash) avoid paying a provider twice for the same lookup.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from wfo.db import connect


class ResponseCache(Protocol):
    def get(self, key: str) -> dict[str, Any] | None: ...

    def set(self, key: str, value: dict[str, Any], ttl_s: float) -> None: ...


class InMemoryTTLCache:
    def __init__(self, now: Callable[[], float]) -> None:
        self._now = now
        self._data: dict[str, tuple[float, dict[str, Any]]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> dict[str, Any] | None:
        item = self._data.get(key)
        if item is None or item[0] <= self._now():
            if item is not None:
                del self._data[key]
            self.misses += 1
            return None
        self.hits += 1
        return item[1]

    def set(self, key: str, value: dict[str, Any], ttl_s: float) -> None:
        self._data[key] = (self._now() + ttl_s, value)


class SqliteResponseCache:
    def __init__(self, path: str | Path, now: Callable[[], float]) -> None:
        self._conn: sqlite3.Connection = connect(path)
        self._lock = threading.Lock()
        self._now = now
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS response_cache "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL, expires_at REAL NOT NULL)"
        )
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value, expires_at FROM response_cache WHERE key = ?", (key,)
            ).fetchone()
        if row is None or float(row[1]) <= self._now():
            self.misses += 1
            return None
        self.hits += 1
        value: dict[str, Any] = json.loads(row[0])
        return value

    def set(self, key: str, value: dict[str, Any], ttl_s: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO response_cache (key, value, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "expires_at = excluded.expires_at",
                (key, json.dumps(value), self._now() + ttl_s),
            )

    def purge_expired(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM response_cache WHERE expires_at <= ?", (self._now(),)
            )
            return cur.rowcount
