from __future__ import annotations

import json

import httpx

from wfo.webhook import SIGNATURE_HEADER, WebhookNotifier, sign, verify_signature


async def _no_sleep(_: float) -> None:
    return None


async def test_signed_delivery_with_retry() -> None:
    attempts: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        attempts.append(req)
        return httpx.Response(503 if len(attempts) < 3 else 200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        ok = await WebhookNotifier(c, "s3cret", sleep=_no_sleep).deliver(
            "https://hooks.example/wfo", {"batch_id": "b1"}
        )
    assert ok
    assert len(attempts) == 3
    req = attempts[-1]
    assert verify_signature("s3cret", req.content, req.headers[SIGNATURE_HEADER])
    assert json.loads(req.content) == {"batch_id": "b1"}


async def test_client_error_is_not_retried() -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(410)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert not await WebhookNotifier(c, sleep=_no_sleep).deliver("https://x.example", {})
    assert calls == 1


def test_signature_rejects_tampering() -> None:
    sig = sign("k", b'{"a":1}')
    assert verify_signature("k", b'{"a":1}', sig)
    assert not verify_signature("k", b'{"a":2}', sig)
