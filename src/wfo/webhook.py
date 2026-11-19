"""Signed completion webhooks with retries."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import random
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

SIGNATURE_HEADER = "X-WFO-Signature"


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    return hmac.compare_digest(sign(secret, body), header)


class WebhookNotifier:
    def __init__(
        self,
        client: httpx.AsyncClient,
        secret: str = "",
        max_attempts: int = 4,
        base_delay_s: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.client = client
        self.secret = secret
        self.max_attempts = max_attempts
        self.base_delay_s = base_delay_s
        self._sleep = sleep
        self._rng = random.Random()

    async def deliver(self, url: str, payload: dict[str, Any]) -> bool:
        body = json.dumps(payload, sort_keys=True).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "waterfall-optimizer"}
        if self.secret:
            headers[SIGNATURE_HEADER] = sign(self.secret, body)
        for attempt in range(self.max_attempts):
            try:
                resp = await self.client.post(url, content=body, headers=headers, timeout=10.0)
                if resp.status_code < 300:
                    return True
                if 400 <= resp.status_code < 500 and resp.status_code not in (408, 429):
                    return False  # receiver rejected it; retrying will not help
            except httpx.HTTPError:
                pass
            if attempt + 1 < self.max_attempts:
                cap = self.base_delay_s * 2**attempt
                await self._sleep(self._rng.uniform(cap / 2, cap))
        return False
