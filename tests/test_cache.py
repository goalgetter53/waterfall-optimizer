from __future__ import annotations

from pathlib import Path

from wfo.cache import InMemoryTTLCache, SqliteResponseCache
from wfo.clock import VirtualClock


def test_in_memory_ttl(clock: VirtualClock) -> None:
    cache = InMemoryTTLCache(clock.now)
    cache.set("k", {"email": "a@b.c"}, ttl_s=10)
    assert cache.get("k") == {"email": "a@b.c"}
    clock.advance(10)
    assert cache.get("k") is None
    assert (cache.hits, cache.misses) == (1, 1)


def test_sqlite_ttl_overwrite_and_purge(tmp_path: Path) -> None:
    now = [100.0]
    cache = SqliteResponseCache(tmp_path / "c.db", now=lambda: now[0])
    cache.set("k", {"email": None}, ttl_s=5)
    cache.set("k", {"email": "x@y.z"}, ttl_s=50)
    assert cache.get("k") == {"email": "x@y.z"}
    now[0] += 60
    assert cache.get("k") is None
    assert cache.purge_expired() == 1


def test_sqlite_cache_is_shared_across_connections(tmp_path: Path) -> None:
    a = SqliteResponseCache(tmp_path / "c.db", now=lambda: 0.0)
    b = SqliteResponseCache(tmp_path / "c.db", now=lambda: 0.0)
    a.set("hunter:ada|l|x.com", {"email": "ada@x.com"}, ttl_s=10)
    assert b.get("hunter:ada|l|x.com") == {"email": "ada@x.com"}
