"""Per-contact enrichment waterfall with the full reliability envelope.

For each step: router picks a provider -> response cache -> ledger reservation
-> circuit breaker -> token bucket -> provider call with retries/backoff/jitter
-> ledger commit (idempotent) -> cache write -> verification -> router feedback.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass, field

from wfo.cache import ResponseCache
from wfo.clock import Clock
from wfo.ledger import CostLedger
from wfo.models import (
    Contact,
    ContactOutcome,
    ProviderCall,
    StopReason,
    Verification,
)
from wfo.providers.base import Provider, ProviderError, ProviderResponse
from wfo.reliability import CircuitBreaker, CircuitOpenError, RetryPolicy, TokenBucket
from wfo.reliability.retry import call_with_retry
from wfo.routing.base import Router
from wfo.verification import Verifier

_RANK = {
    Verification.VALID: 4,
    Verification.CATCH_ALL: 3,
    Verification.RISKY: 2,
    Verification.UNVERIFIED: 1,
    Verification.INVALID: 0,
}


@dataclass(slots=True)
class PipelineConfig:
    value_per_verified: float = 0.50
    max_spend_per_contact: float = 0.30
    cache_ttl_s: float = 30 * 86400.0
    negative_cache_ttl_s: float = 7 * 86400.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)


@dataclass(slots=True)
class ProviderEnvelope:
    provider: Provider
    bucket: TokenBucket
    breaker: CircuitBreaker


def build_envelopes(
    providers: Mapping[str, Provider],
    clock: Clock,
    rate_per_s: float = 10.0,
    burst: float = 20.0,
    failure_threshold: int = 5,
    reset_timeout_s: float = 30.0,
) -> dict[str, ProviderEnvelope]:
    return {
        name: ProviderEnvelope(
            provider=p,
            bucket=TokenBucket(rate_per_s, burst, clock),
            breaker=CircuitBreaker(clock, failure_threshold, reset_timeout_s),
        )
        for name, p in providers.items()
    }


class EnrichmentPipeline:
    def __init__(
        self,
        envelopes: Mapping[str, ProviderEnvelope],
        router: Router,
        verifier: Verifier,
        ledger: CostLedger,
        cache: ResponseCache,
        clock: Clock,
        config: PipelineConfig | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.envelopes = dict(envelopes)
        self.router = router
        self.verifier = verifier
        self.ledger = ledger
        self.cache = cache
        self.clock = clock
        self.config = config or PipelineConfig()
        self.rng = rng or random.Random(0)

    async def enrich(self, contact: Contact, job_key: str) -> ContactOutcome:
        seg = contact.segment
        start = self.clock.now()
        tried: list[str] = []
        calls: list[ProviderCall] = []
        spent = 0.0
        best_email: str | None = None
        best_ver = Verification.UNVERIFIED
        best_provider: str | None = None
        stop = StopReason.EXHAUSTED

        while True:
            candidates = [
                n for n, env in self.envelopes.items() if n not in tried and env.breaker.available()
            ]
            if not candidates:
                stop = StopReason.EXHAUSTED
                break
            remaining = min(self.config.max_spend_per_contact - spent, self.ledger.remaining_usd())
            depth = len(tried)
            name = self.router.choose(seg, candidates, depth, remaining)
            if name is None:
                affordable = any(
                    self.envelopes[n].provider.cost_per_call <= remaining + 1e-12
                    for n in candidates
                )
                stop = StopReason.ROUTER_STOP if affordable else StopReason.BUDGET
                break
            tried.append(name)
            call, response = await self._call_provider(name, contact, job_key)
            calls.append(call)
            spent += call.cost
            if response is None:
                if call.error == "budget":
                    stop = StopReason.BUDGET
                    break
                continue  # provider error: censored observation, try the next one
            if response.email is None:
                self.router.observe(seg, name, depth, False, found=False)
                continue

            verification, vcost = await self._verify(contact, response, job_key, name)
            spent += vcost
            call.verification = verification
            if _RANK[verification] > _RANK[best_ver] or best_email is None:
                best_email, best_ver, best_provider = response.email, verification, name
            if verification is Verification.VALID:
                self.router.observe(seg, name, depth, True)
                stop = StopReason.VERIFIED
                break
            if verification is Verification.CATCH_ALL:
                # Domain accepts everything: no provider can verify. It still counts as
                # a non-verified outcome for learning, because the call cost money.
                self.router.observe(seg, name, depth, False)
                stop = StopReason.CATCH_ALL
                break
            if verification is Verification.UNVERIFIED:
                stop = StopReason.BUDGET
                break
            self.router.observe(seg, name, depth, False)

        return ContactOutcome(
            contact_id=contact.id,
            segment=seg.key,
            email=best_email,
            verification=best_ver,
            provider=best_provider,
            stop_reason=stop,
            cost=round(spent, 6),
            latency_s=self.clock.now() - start,
            calls=calls,
        )

    async def _call_provider(
        self, name: str, contact: Contact, job_key: str
    ) -> tuple[ProviderCall, ProviderResponse | None]:
        env = self.envelopes[name]
        provider = env.provider
        cache_key = f"{name}:{contact.lookup_key}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            resp = ProviderResponse.from_cache(cached)
            return ProviderCall(name, resp.email is not None, 0.0, 0.0, from_cache=True), resp

        reservation = self.ledger.reserve(provider.cost_per_call)
        if reservation is None:
            return ProviderCall(name, False, 0.0, 0.0, error="budget"), None

        t0 = self.clock.now()

        async def attempt() -> ProviderResponse:
            env.breaker.before_call()
            await env.bucket.acquire()
            try:
                result = await provider.find_email(contact)
            except ProviderError:
                env.breaker.on_failure()
                raise
            env.breaker.on_success()
            return result

        try:
            resp, _ = await call_with_retry(
                attempt, self.config.retry, self.clock, self.rng, on_rate_limited=env.bucket.drain
            )
        except (ProviderError, CircuitOpenError) as exc:
            self.ledger.release(reservation)
            return ProviderCall(name, False, 0.0, self.clock.now() - t0, error=str(exc)), None
        except BaseException:  # cancellation / shutdown mid-call: never leak the hold
            self.ledger.release(reservation)
            raise

        ttl = self.config.cache_ttl_s if resp.email else self.config.negative_cache_ttl_s
        self.cache.set(cache_key, resp.to_cache(), ttl)
        charged = self.ledger.commit(reservation, f"{job_key}:{name}", name, resp.charged)
        return ProviderCall(name, resp.email is not None, charged, self.clock.now() - t0), resp

    async def _verify(
        self, contact: Contact, resp: ProviderResponse, job_key: str, provider: str
    ) -> tuple[Verification, float]:
        assert resp.email is not None
        cost = self.verifier.cost_per_check
        if cost <= 0:
            return await self.verifier.verify(contact, resp.email, resp.provider_status), 0.0
        reservation = self.ledger.reserve(cost)
        if reservation is None:
            return Verification.UNVERIFIED, 0.0
        try:
            v = await self.verifier.verify(contact, resp.email, resp.provider_status)
        except Exception:
            self.ledger.release(reservation)
            raise
        charged = self.ledger.commit(reservation, f"{job_key}:verify:{provider}", "verifier", cost)
        return v, charged
