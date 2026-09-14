import asyncio
import csv
import io
import json

from fastapi.testclient import TestClient

from sww.api import create_app
from test_api import FakeCrawler
from test_ai_ranking import FakeClient, JOB, RESUME


def test_async_ai_result_export_and_cache_without_real_network(tmp_path):
    client = FakeClient()
    app = create_app(tmp_path, FakeCrawler(), ai_factory=lambda: client)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-SWW-Client": "1"}) as http:
        app.state.matcher["resume"] = {"text": RESUME, "filename": "test.pdf", "skills": [], "warnings": []}
        app.state.matcher["jobs"] = [JOB]
        response = http.post("/matcher-api/ai/rank", json={"refine_pairs": 0})
        assert response.status_code == 200
        for _ in range(100):
            status = http.get("/matcher-api/status").json()
            if status["ai"]["state"] != "running":
                break
        assert status["ai"]["state"] == "completed"
        assert client.closed
        result = http.get("/matcher-api/ai/result")
        assert result.status_code == 200
        assert result.json()["engine"] == "deepseek"
        assert result.json()["jobs"][0]["criteria"][0]["resume_quote"] in RESUME
        exported = http.get("/matcher-api/export.csv")
        assert "criteria" in exported.text and "Built asynchronous Python" in exported.text
        row = next(csv.DictReader(io.StringIO(exported.text.lstrip("\ufeff"))))
        assert json.loads(row["criteria"])[0]["resume_quote"] in RESUME
        assert "text" not in status["resume"]


def test_cancel_and_mutations_cannot_race_ai_snapshot(tmp_path):
    class SlowClient(FakeClient):
        async def json(self, system, payload):
            await asyncio.Event().wait()
    client = SlowClient()
    app = create_app(tmp_path, FakeCrawler(), ai_factory=lambda: client)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-SWW-Client": "1"}) as http:
        app.state.matcher["resume"] = {"text": RESUME, "filename": "test.pdf", "skills": [], "warnings": []}
        app.state.matcher["jobs"] = [JOB]
        assert http.post("/matcher-api/ai/rank", json={}).status_code == 200
        assert http.post("/matcher-api/ai/rank", json={}).status_code == 409
        assert http.post("/matcher-api/jobs/import", json={"jobs": [JOB]}).status_code == 409
        assert http.post("/matcher-api/resume", files={"file": ("r.pdf", b"invalid")}).status_code == 409
        assert http.post("/matcher-api/crawl", json={}).status_code == 409
        assert http.post("/matcher-api/rank", json={}).status_code == 409
        assert http.get("/matcher-api/ai/result").status_code == 409
        assert http.post("/matcher-api/ai/cancel").status_code == 200
        assert http.get("/matcher-api/status").json()["ai"]["state"] == "cancelled"
        assert client.closed


def test_ai_budget_validation_happens_before_client_creation(tmp_path):
    def forbidden_factory():
        raise AssertionError("API client must not be created")
    app = create_app(tmp_path, FakeCrawler(), ai_factory=forbidden_factory)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-SWW-Client": "1"}) as http:
        app.state.matcher["resume"] = {"text": RESUME}
        app.state.matcher["jobs"] = [JOB, {**JOB, "id": "2"}]
        assert http.post("/matcher-api/ai/rank", json={"max_jobs": 1}).status_code == 422
