import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from sww.api import MAX_BODY, create_app
from test_resume import pdf_bytes


class FakeCrawler:
    def __init__(self):
        self.closed = False
        self.started = False

    async def open_browser(self):
        self.started = True

    async def close(self):
        self.closed = True

    async def crawl(self, *, on_progress, cancel, **options):
        await on_progress({"state": "running", "pages": 1, "job_count": 1})
        await asyncio.sleep(0)
        await on_progress({"state": "completed", "pages": 1, "job_count": 1, "complete": True})
        return [{"id": "123", "title": "Python Developer", "description": "Python SQL Docker", "url": "https://waterlooworks.uwaterloo.ca/myAccount/co-op/full/jobs.htm?jobId=123"}]


@pytest.fixture
def local(tmp_path):
    browser = FakeCrawler()
    app = create_app(tmp_path, browser)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-SWW-Client": "1"}) as client:
        yield client, app, browser
    assert browser.closed


def test_local_host_origin_and_mutation_header(local):
    client, _, _ = local
    assert client.get("/matcher-api/status").status_code == 200
    assert client.get("/matcher-api/status", headers={"Host": "attacker.example:8765"}).status_code == 403
    assert client.post("/matcher-api/browser", headers={"Origin": "https://attacker.example"}).status_code == 403
    assert client.post("/matcher-api/browser", headers={"X-SWW-Client": ""}).status_code == 403
    assert client.post("/matcher-api/browser", headers={"Origin": "http://localhost:5173"}).status_code == 200


def test_resume_missing_invalid_and_too_large(local):
    client, _, _ = local
    assert client.post("/matcher-api/rank", json={}).status_code == 409
    assert client.post("/matcher-api/resume", files={"file": ("resume.pdf", b"not a PDF")}).status_code == 422
    assert client.post("/matcher-api/resume", headers={"Content-Length": str(MAX_BODY + 1)}, content=b"x").status_code == 413


def test_streamed_body_limit_without_content_length(local):
    client, _, _ = local
    response = client.post("/matcher-api/jobs/import", content=iter([b"x" * (MAX_BODY + 1)]), headers={"Content-Type": "application/json"})
    assert response.status_code == 413


def test_ranking_export_and_cache_privacy(local, tmp_path):
    client, app, _ = local
    app.state.matcher["resume"] = {"filename": "private.pdf", "text": "Python SQL Docker software developer", "skills": ["Python"], "warnings": []}
    jobs = [{"id": "1", "title": "Python Developer", "company": "=HYPERLINK(1)",
             "description": "Python SQL Docker", "url": "https://waterlooworks.uwaterloo.ca/myAccount/co-op/full/jobs.htm?jobId=1"}]
    assert client.post("/matcher-api/jobs/import", json={"jobs": jobs}).status_code == 200
    ranked = client.post("/matcher-api/rank", json={})
    assert ranked.status_code == 200
    assert ranked.json()["jobs"][0]["rank"] == 1
    assert ranked.json()["source"] == "import"
    assert ranked.json()["crawl"]["complete"] is False
    exported = client.get("/matcher-api/export.csv")
    assert exported.status_code == 200
    assert "'=HYPERLINK(1)" in exported.text
    cache = tmp_path / "jobs.json"
    assert "private.pdf" not in cache.read_text()
    assert cache.stat().st_mode & 0o777 == 0o600
    status = client.get("/matcher-api/status").json()
    assert "text" not in status["resume"]
    # New inputs invalidate old downloadable results.
    client.post("/matcher-api/jobs/import", json={"jobs": jobs})
    assert client.get("/matcher-api/export.csv").status_code == 409


def test_rejects_unsafe_links_and_bad_crawl_options(local):
    client, _, _ = local
    for url in ["javascript:alert(1)", "http://127.0.0.1/private", "https://waterlooworks.uwaterloo.ca@evil.test/jobs", "https://waterlooworks.uwaterloo.ca:444/jobs"]:
        assert client.post("/matcher-api/jobs/import", json={"jobs": [{"title": "Developer", "url": url}]}).status_code == 422
    assert client.post("/matcher-api/crawl", json={"delay_seconds": 0}).status_code == 422
    assert client.post("/matcher-api/rank", json={"limit": 101}).status_code == 422


def test_import_preserves_explicit_closed_marker(local):
    client, app, _ = local
    app.state.matcher["resume"] = {"filename": "test.pdf", "text": "Python Developer", "skills": ["Python"], "warnings": []}
    client.post("/matcher-api/jobs/import", json={"jobs": [{"id": "3", "title": "Python Developer", "is_open": False}]})
    result = client.post("/matcher-api/rank", json={}).json()
    assert result["jobs"] == []
    assert result["excluded_jobs"][0]["reason"] == "Posting is explicitly closed."


def test_resume_upload_clears_export_and_only_returns_summary(local, monkeypatch):
    client, app, _ = local
    monkeypatch.setattr("sww.api.extract_resume", lambda data: {"text": "PRIVATE TEXT", "skills": ["Python"], "warnings": []})
    app.state.matcher["ranking"] = {"jobs": []}
    response = client.post("/matcher-api/resume", files={"file": ("../../resume.pdf", b"%PDF-test", "application/pdf")})
    assert response.status_code == 200
    assert response.json() == {"filename": "resume.pdf", "skills": ["Python"], "warnings": []}
    assert app.state.matcher["resume"]["text"] == "PRIVATE TEXT"
    assert app.state.matcher["ranking"] is None


def test_cache_survives_restart_without_resume(tmp_path):
    cache = {"jobs": [{"id": "22", "title": "Engineer"}], "source": "waterlooworks",
             "collected_at": "2026-09-14T00:00:00Z", "crawl": {"complete": False, "pages": 2}}
    (tmp_path / "jobs.json").write_text(json.dumps(cache))
    with TestClient(create_app(tmp_path, FakeCrawler()), base_url="http://localhost:8765") as client:
        status = client.get("/matcher-api/status").json()
        assert status["job_count"] == 1
        assert status["has_resume"] is False
        assert status["crawl"]["complete"] is False


def test_real_pdf_to_top100_and_csv(local):
    client, _, _ = local
    resume = pdf_bytes("Software Developer. Python SQL Docker. Built Python services and SQL data pipelines using Docker.")
    assert client.post("/matcher-api/resume", files={"file": ("resume.pdf", resume, "application/pdf")}).status_code == 200
    jobs = [{"id": str(index), "title": "Python Developer", "description": "Python SQL Docker services"} for index in range(105)]
    assert client.post("/matcher-api/jobs/import", json={"jobs": jobs}).status_code == 200
    result = client.post("/matcher-api/rank", json={}).json()
    assert result["total_jobs"] == 105
    assert len(result["jobs"]) == 100
    assert [job["rank"] for job in result["jobs"]] == list(range(1, 101))
    assert result["jobs"][0]["matched_skills"] == ["Docker", "Python", "SQL"]
    exported = client.get("/matcher-api/export.csv")
    assert len(exported.text.splitlines()) == 101
