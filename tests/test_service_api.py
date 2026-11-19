from __future__ import annotations

import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from wfo.api import create_app
from wfo.config import Settings
from wfo.service import build_service
from wfo.sim.world import SyntheticWorld
from wfo.webhook import SIGNATURE_HEADER, verify_signature


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "wfo.db"),
        sim_latency_scale=0.0,
        rate_per_s=10_000,
        burst=10_000,
        concurrency=4,
        webhook_secret="whsec",
    )


def test_submit_poll_results_and_webhook(tmp_path: Path) -> None:
    received: list[httpx.Request] = []

    def hook(req: httpx.Request) -> httpx.Response:
        received.append(req)
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(hook))
    service = build_service(_settings(tmp_path), http_client=client)
    contacts = [c.to_dict() for c in SyntheticWorld(11).generate_contacts(40)]
    with TestClient(create_app(service)) as api:
        r = api.post(
            "/v1/batches",
            json={
                "contacts": contacts,
                "strategy": "thompson",
                "webhook_url": "https://hooks.example/done",
            },
            headers={"Idempotency-Key": "crm-export-1"},
        )
        assert r.status_code == 202
        batch_id = r.json()["batch_id"]
        dup = api.post(
            "/v1/batches", json={"contacts": contacts}, headers={"Idempotency-Key": "crm-export-1"}
        )
        assert dup.json() == {**r.json(), "duplicate": True}

        status: dict[str, object] = {}
        for _ in range(200):
            status = api.get(f"/v1/batches/{batch_id}").json()
            if status["status"] == "completed" and status["webhook_status"]:
                break
            time.sleep(0.05)
        assert status["status"] == "completed"
        assert status["finished"] == 40
        assert status["webhook_status"] == "delivered"

        results = api.get(f"/v1/batches/{batch_id}/results").json()["results"]
        assert len(results) == 40
        assert all(r["status"] == "done" for r in results)
        health = api.get("/v1/health").json()
        assert health["ledger"]["spent_usd"] > 0
        assert set(health["providers"]) == {"apollo", "hunter", "prospeo", "dropcontact"}

        assert api.get("/v1/batches/nope").status_code == 404
        assert (
            api.post("/v1/batches", json={"contacts": contacts, "strategy": "yolo"}).status_code
            == 422
        )
        assert api.post("/v1/batches", json={"contacts": []}).status_code == 422

    assert len(received) == 1
    body = received[0].content
    assert verify_signature("whsec", body, received[0].headers[SIGNATURE_HEADER])
    # learned posteriors were persisted on batch completion
    assert service.state_store.load("thompson")


def test_router_state_survives_restart(tmp_path: Path) -> None:
    s1 = build_service(_settings(tmp_path))
    s1.state_store.save("thompson", {"local": {"US|1-50|ic#0#hunter": [5, 1]}})
    s2 = build_service(_settings(tmp_path))
    router = s2.routers["thompson"]
    assert router.state_dict()["local"]["US|1-50|ic#0#hunter"] == [5, 1]
