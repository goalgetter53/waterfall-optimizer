# ADR 0003: Reliability envelope and cost-safety invariants

- Status: accepted
- Date: 2026-11-19

## Context

Enrichment vendors fail in boring ways: 429 storms, 5xx outages, slow async jobs.
The expensive failure mode is not "the job failed". It is "the job silently spent
twice the budget". Spend safety therefore gets the same rigour as correctness.

## Decision

Every provider call goes through, in order:

1. **Response cache** (SQLite, TTL; 30 days for hits, 7 days for misses). A hit is free.
2. **Ledger reservation.** The provider's maximum per-call cost is reserved under a
   `BEGIN IMMEDIATE` transaction that checks `spent + reserved + amount <= budget`.
   Amounts are integer micro-dollars, so there is no float drift.
3. **Circuit breaker** per provider. It opens after N consecutive failures (429s count:
   a vendor that is throttling us is unavailable), fails fast while open, and sends a
   single half-open probe after a cool-down. The router only sees providers whose
   breaker is available, so an outage turns into routing rather than errors.
4. **Token bucket** per provider. It works on an injected clock, so the same code
   paces real HTTP and virtual-time simulations. A 429 drains the bucket.
5. **Retries** with capped exponential backoff and *full jitter*, never shorter than
   the vendor's `Retry-After`. Non-retryable errors (401/403/4xx) fail immediately.
6. **Commit**: the actual charge (<= reservation; pay-per-hit misses cost 0) is
   recorded under an **idempotency key** `batch:contact:provider`. A replayed commit
   records nothing. The response is cached *before* the commit.
7. On any error or cancellation the reservation is released (`try/except
   BaseException`). Reservations left by a process that crashed hard are reclaimed by
   `release_stale()`.

## Invariants (tested)

- `spent + reserved <= budget` after any sequence of reserve/commit/release,
  including replays and over-charges. This is a Hypothesis property test.
- Concurrent tasks and separate connections (simulating separate processes) cannot
  overspend the shared budget.
- A worker that crashes after paying but before acking does not cause a second paid
  call or a second ledger entry. Fault-injection test: the re-leasing worker is served
  from the shared cache.
- An outage costs at most `failure_threshold` failed calls before the breaker opens.
  Failed calls are never charged.

## Known gap

If a process dies after the vendor charged us but *before* the response was cached,
the recovered job calls the vendor again. That is one extra real charge, which the
ledger records once, so the ledger under-reports by one call. Closing the gap needs
the cache write and the ledger commit in one transaction on the same connection, or
vendor-side idempotency keys (most enrichment APIs do not offer them). The window is
the few microseconds between the HTTP response and a local SQLite write.

A hypothesis property test also found a real bug during development: float rounding
after a computed wait could make the token bucket re-wait for a 1e-17 s interval
forever on a virtual clock. Fixed with an epsilon. There is a regression test.
