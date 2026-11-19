# ADR 0001: Durable job queue on SQLite WAL with leases and fencing tokens

- Status: accepted
- Date: 2026-11-19

## Context

Enrichment calls cost money and take seconds. A batch of 50k contacts must survive
worker crashes and restarts without losing jobs and without paying twice for the
same lookup. The target user is a small GTM team that wants to self-host, ideally
as one container with one volume, with no Redis/RabbitMQ to operate.

## Decision

Use a single SQLite database in WAL mode as the job queue, with:

- **Leases with visibility timeouts.** `lease()` moves a job to `leased`, sets
  `lease_expires_at = now + visibility_timeout` and increments `attempts`. A
  worker that dies just stops heartbeating; when the lease expires the job is
  leased again.
- **Fencing tokens.** Every lease gets a fresh random token. `complete`, `fail`
  and `heartbeat` are conditional updates on `(id, token, status='leased',
  lease_expires_at > now)`, so a "zombie" worker that wakes up after its lease
  expired cannot overwrite the new owner's result.
- **`BEGIN IMMEDIATE` for every transition.** It takes SQLite's single write lock
  up front, making read-check-write atomic across threads and processes.
- **Dead-lettering** after `max_attempts`; **idempotent batch creation** by
  `Idempotency-Key`; duplicate job keys inside a batch collapse (`UNIQUE
  (batch_id, job_key)`); `try_finalize_batch` returns true exactly once, which is
  what fires the completion webhook.
- A **Postgres backend** behind the same interface (`FOR UPDATE SKIP LOCKED`) for
  multi-host deployments. It is optional and only tested when `WFO_PG_DSN` is set.

## Consequences

- Delivery is at-least-once. Exactly-once *effects* come from idempotency keys in
  the cost ledger and from the response cache (ADR 0003), not from the queue.
- One writer at a time. Queue transitions are sub-millisecond, while provider
  calls take hundreds of milliseconds, so the write lock is not the bottleneck at
  the target scale (tens of workers, one host). Beyond one host, switch to Postgres.
- Queue calls are synchronous inside async code. They are short local disk
  operations; moving them to a thread pool would add complexity for little gain at
  this scale.
- Verified by a Hypothesis state machine (`tests/test_queue_sqlite.py`): over random
  interleavings of lease/complete/heartbeat/clock-advance across four workers, a job
  is never leased while another lease on it is live, completed jobs are never
  re-leased, and only the current holder can complete. There is also a 6-thread test
  where each worker has its own connection.
