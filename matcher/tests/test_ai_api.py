"""The semantic ranking endpoints, driven by a scripted model."""
import asyncio
import csv
import io
import json

from fastapi.testclient import TestClient

from conftest import RESUME, FakeClient, job, judgement_payload, seed_jobs, seed_resume
from sww.api import create_app
from test_api import FakeCrawler

POSTING = job("1", "Backend Intern",
              "Develop reliable event-driven services using durable message queues.")
EVIDENCE = "Built asynchronous Python services with durable queues"


def app_with(tmp_path, client):
    return create_app(tmp_path, FakeCrawler(), ai_factory=lambda: client, embedder=None)


def settle(http, limit=200):
    for _ in range(limit):
        status = http.get("/matcher-api/status").json()
        if status["ai"]["state"] != "running":
            return status
    raise AssertionError("AI task did not finish")


def test_async_rank_result_and_export(tmp_path):
    client = FakeClient(judgements=judgement_payload("direct", EVIDENCE))
    app = app_with(tmp_path, client)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-SWW-Client": "1"}) as http:
        seed_resume(app, RESUME)
        seed_jobs(app, [POSTING])
        started = http.post("/matcher-api/ai/rank", json={"refine_pairs": 0})
        assert started.status_code == 200 and started.json()["shortlisted"] == 1
        status = settle(http)
        assert status["ai"]["state"] == "completed"
        assert client.closed
        result = http.get("/matcher-api/ai/result").json()
        assert result["engine"] == "deepseek"
        assert result["jobs"][0]["criteria"][0]["resume_quote"] in RESUME
        assert result["jobs"][0]["must_have_coverage"] == 1.0
        exported = http.get("/matcher-api/export.csv")
        row = next(csv.DictReader(io.StringIO(exported.text.lstrip("﻿"))))
        assert json.loads(row["criteria"])[0]["resume_quote"] in RESUME
        assert "must_have_coverage" in row
        assert "text" not in status["resume"]


def test_requirements_are_extracted_once_per_posting_not_once_per_resume(tmp_path):
    """The split that makes a resume edit cheap: the postings' requirements are
    cached against the postings, so only the judgement stage re-runs."""
    client = FakeClient(judgements=judgement_payload("direct", EVIDENCE))
    app = app_with(tmp_path, client)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-SWW-Client": "1"}) as http:
        seed_resume(app, RESUME)
        seed_jobs(app, [POSTING])
        http.post("/matcher-api/ai/rank", json={"refine_pairs": 0})
        settle(http)
        extraction_calls = sum(1 for kind, _ in client.prompts if kind == "requirements")
        assert extraction_calls == 1

        # A different resume: the postings have not changed.
        seed_resume(app, RESUME + "\n- Also wrote Kafka consumers.\n")
        http.post("/matcher-api/ai/rank", json={"refine_pairs": 0})
        settle(http)
        assert sum(1 for kind, _ in client.prompts if kind == "requirements") == extraction_calls


def test_cancel_and_mutations_cannot_race_the_ai_task(tmp_path):
    class SlowClient(FakeClient):
        async def json(self, system, payload):
            await asyncio.Event().wait()

    client = SlowClient()
    app = app_with(tmp_path, client)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-SWW-Client": "1"}) as http:
        seed_resume(app, RESUME)
        seed_jobs(app, [POSTING])
        assert http.post("/matcher-api/ai/rank", json={}).status_code == 200
        assert http.post("/matcher-api/ai/rank", json={}).status_code == 409
        assert http.post("/matcher-api/jobs/import", json={"jobs": [POSTING]}).status_code == 409
        assert http.post("/matcher-api/resume", files={"file": ("r.pdf", b"invalid")}).status_code == 409
        assert http.post("/matcher-api/crawl", json={}).status_code == 409
        assert http.post("/matcher-api/rank", json={}).status_code == 409
        assert http.get("/matcher-api/ai/result").status_code == 409
        assert http.post("/matcher-api/ai/cancel").status_code == 200
        assert http.get("/matcher-api/status").json()["ai"]["state"] == "cancelled"
        assert client.closed


def test_no_client_is_created_before_the_inputs_are_present(tmp_path):
    def forbidden_factory():
        raise AssertionError("API client must not be created")

    app = create_app(tmp_path, FakeCrawler(), ai_factory=forbidden_factory, embedder=None)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-SWW-Client": "1"}) as http:
        assert http.post("/matcher-api/ai/rank", json={}).status_code == 409
        seed_resume(app, RESUME)
        assert http.post("/matcher-api/ai/rank", json={}).status_code == 409


def test_the_shortlist_size_is_reported_before_any_spend(tmp_path):
    client = FakeClient(judgements=judgement_payload("direct", EVIDENCE))
    app = app_with(tmp_path, client)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-SWW-Client": "1"}) as http:
        seed_resume(app, RESUME)
        seed_jobs(app, [{**POSTING, "id": str(index)} for index in range(5)])
        started = http.post("/matcher-api/ai/rank", json={"shortlist": 2, "refine_pairs": 0}).json()
        assert started["eligible"] == 5 and started["shortlisted"] == 2
        settle(http)
        result = http.get("/matcher-api/ai/result").json()
        assert result["assessed_jobs"] == 2 and result["not_assessed_jobs"] == 3


def test_cache_can_be_cleared(tmp_path):
    client = FakeClient(judgements=judgement_payload("direct", EVIDENCE))
    app = app_with(tmp_path, client)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-SWW-Client": "1"}) as http:
        seed_resume(app, RESUME)
        seed_jobs(app, [POSTING])
        http.post("/matcher-api/ai/rank", json={"refine_pairs": 0})
        settle(http)
        assert http.get("/matcher-api/status").json()["cache"]
        assert http.post("/matcher-api/cache/clear").json()["cache"] == {}
