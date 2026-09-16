"""The local service end to end, with a fake browser and no network."""
import asyncio
import csv
import io
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import RESUME, job, seed_jobs, seed_resume
from sww.api import create_app
from sww.config import MAX_BODY_BYTES as MAX_BODY
from test_docx import docx_bytes
from test_resume import pdf_bytes


class FakeCrawler:
    def __init__(self):
        self.closed = False
        self.started = False
        self.cached = None

    async def open_browser(self):
        self.started = True

    async def close(self):
        self.closed = True

    async def crawl(self, *, on_progress, cancel, **options):
        self.cached = options.get("cached_jobs")
        await on_progress({"state": "running", "pages": 1, "job_count": 1})
        await asyncio.sleep(0)
        await on_progress({"state": "completed", "pages": 1, "job_count": 1, "complete": True})
        return [{"id": "123", "title": "Python Developer", "description": "Python SQL Docker",
                 "url": "https://waterlooworks.uwaterloo.ca/myAccount/co-op/full/jobs.htm?jobId=123",
                 "metadata": {}}]


@pytest.fixture
def local(tmp_path):
    browser = FakeCrawler()
    app = create_app(tmp_path, browser, embedder=None)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-SWW-Client": "1"}) as client:
        yield client, app, browser
    assert browser.closed


def finish(client, app, name="task"):
    async def wait():
        if app.state.matcher[name]:
            await app.state.matcher[name]
    client.portal.call(wait)


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
    assert client.post("/matcher-api/resume", headers={"Content-Length": str(MAX_BODY + 1)},
                       content=b"x").status_code == 413


def test_streamed_body_limit_without_content_length(local):
    client, _, _ = local
    response = client.post("/matcher-api/jobs/import", content=iter([b"x" * (MAX_BODY + 1)]),
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 413


def test_rejects_unsafe_links_and_bad_options(local):
    client, _, _ = local
    for url in ["javascript:alert(1)", "http://127.0.0.1/private",
                "https://waterlooworks.uwaterloo.ca@evil.test/jobs",
                "https://waterlooworks.uwaterloo.ca:444/jobs"]:
        assert client.post("/matcher-api/jobs/import",
                           json={"jobs": [{"title": "Developer", "url": url}]}).status_code == 422
    assert client.post("/matcher-api/crawl", json={"delay_seconds": 0}).status_code == 422
    assert client.post("/matcher-api/rank", json={"limit": 101}).status_code == 422


def test_resume_upload_returns_a_summary_and_never_the_text(local):
    client, app, _ = local
    app.state.matcher["ranking"] = {"jobs": []}
    response = client.post("/matcher-api/resume",
                           files={"file": ("../../resume.md", RESUME.encode("utf-8"), "text/markdown")})
    assert response.status_code == 200
    body = response.json()
    assert body["filename"] == "resume.md"
    assert "text" not in body
    # The split is surfaced so it can be checked before it is trusted.
    assert body["experience_count"] == 2
    assert "Python" in body["skills"] and "Rust" in body["listed_only_skills"]
    assert app.state.matcher["resume"].text.startswith("Example Student")
    assert app.state.matcher["ranking"] is None
    assert "text" not in client.get("/matcher-api/status").json()["resume"]


def test_parsed_profile_is_inspectable(local):
    client, app, _ = local
    seed_resume(app)
    body = client.get("/matcher-api/resume/profile").json()
    assert [item["kind"] for item in body["profile"]["experiences"]] == ["work", "project"]
    assert body["profile"]["availability"]["months"] == [4]


def test_ranking_export_and_csv_injection_guard(local, tmp_path):
    client, app, _ = local
    seed_resume(app)
    jobs = [{"id": "1", "title": "Python Developer", "company": "=HYPERLINK(1)",
             "description": "Build asynchronous Python services with durable queues.",
             "url": "https://waterlooworks.uwaterloo.ca/myAccount/co-op/full/jobs.htm?jobId=1"}]
    assert client.post("/matcher-api/jobs/import", json={"jobs": jobs}).status_code == 200
    ranked = client.post("/matcher-api/rank", json={})
    assert ranked.status_code == 200
    assert ranked.json()["jobs"][0]["rank"] == 1
    assert ranked.json()["source"] == "import"
    exported = client.get("/matcher-api/export.csv")
    assert "'=HYPERLINK(1)" in exported.text
    database = tmp_path / "sww.db"
    assert "private.pdf" not in database.read_bytes().decode("utf-8", "ignore")
    assert database.stat().st_mode & 0o777 == 0o600
    # New inputs invalidate old downloadable results.
    client.post("/matcher-api/jobs/import", json={"jobs": jobs})
    assert client.get("/matcher-api/export.csv").status_code == 409


def test_eligibility_filter_defaults_on_and_can_be_disabled(local):
    client, app, _ = local
    seed_resume(app)
    jobs = [{"id": "restricted", "title": "Developer", "metadata": {"detail_text": "Funded by SWPP"}},
            {"id": "open", "title": "Developer"}]
    assert client.post("/matcher-api/jobs/import", json={"jobs": jobs}).status_code == 200
    result = client.post("/matcher-api/rank", json={}).json()
    assert [item["id"] for item in result["jobs"]] == ["open"]
    assert "SWPP" in result["excluded_jobs"][0]["reason"]
    result = client.post("/matcher-api/rank",
                         json={"preferences": {"exclude_restricted_eligibility": False}}).json()
    assert result["eligible_jobs"] == 2


def test_import_preserves_an_explicit_closed_marker(local):
    client, app, _ = local
    seed_resume(app)
    client.post("/matcher-api/jobs/import",
                json={"jobs": [{"id": "3", "title": "Python Developer", "is_open": False}]})
    result = client.post("/matcher-api/rank", json={}).json()
    assert result["jobs"] == []
    assert result["excluded_jobs"][0]["reason"] == "职位已关闭。"


def test_crawl_checkpoints_each_page_into_the_database(tmp_path):
    class CheckpointCrawler(FakeCrawler):
        async def crawl(self, *, on_progress, on_checkpoint, cancel, cached_jobs, **options):
            self.cached = cached_jobs
            await on_checkpoint([job("1"), job("2")])
            await on_progress({"state": "running", "complete": False})
            self.saved.set()
            await asyncio.Event().wait()

    import threading
    browser = CheckpointCrawler()
    browser.saved = threading.Event()
    app = create_app(tmp_path, browser, embedder=None)
    with TestClient(app, base_url="http://localhost:8765", headers={"X-SWW-Client": "1"}) as client:
        assert client.post("/matcher-api/crawl", json={"delay_seconds": 7}).status_code == 200
        assert browser.saved.wait(5)
        # Written incrementally, before the crawl finished, and readable at once.
        assert client.get("/matcher-api/status").json()["job_count"] == 2
        client.post("/matcher-api/crawl/cancel")


def test_only_recent_complete_details_are_offered_for_resumption(tmp_path):
    browser = FakeCrawler()
    app = create_app(tmp_path, browser, embedder=None)
    now = datetime.now(timezone.utc)
    stale = (now - timedelta(hours=30)).isoformat()
    app.state.store.upsert_jobs([
        job("fresh", metadata={"detail_status": "complete", "detail_collected_at": now.isoformat()}),
        job("stale", metadata={"detail_status": "complete", "detail_collected_at": stale}),
        job("pending", metadata={"detail_status": "pending"}),
    ])
    with TestClient(app, base_url="http://localhost:8765", headers={"X-SWW-Client": "1"}) as client:
        client.post("/matcher-api/crawl", json={})
        finish(client, app)
        assert {item["id"] for item in browser.cached} == {"fresh"}


def test_a_validation_failure_reports_the_field_without_leaking_page_text(local):
    client, app, browser = local
    seed_jobs(app, [job("1", title="Previously saved")])

    async def invalid_crawl(**options):
        return [{"id": "2", "title": "PRIVATE" * 200}]

    browser.crawl = invalid_crawl
    client.post("/matcher-api/crawl", json={})
    finish(client, app)
    status = client.get("/matcher-api/status").json()
    assert status["crawl"]["state"] == "failed"
    assert "title" in status["crawl"]["message"] and "string_too_long" in status["crawl"]["message"]
    assert "PRIVATE" not in str(status)
    assert status["job_count"] == 1


def test_jobs_survive_a_restart_without_a_resume(tmp_path):
    app = create_app(tmp_path, FakeCrawler(), embedder=None)
    app.state.store.upsert_jobs([job("22", title="Engineer")])
    app.state.store.close()
    with TestClient(create_app(tmp_path, FakeCrawler(), embedder=None),
                    base_url="http://localhost:8765") as client:
        status = client.get("/matcher-api/status").json()
        assert status["job_count"] == 1
        assert status["has_resume"] is False


@pytest.mark.parametrize("extension,mime,builder", [
    ("md", "text/markdown", lambda text: ("# Resume\n\n" + text).encode("utf-8")),
    ("markdown", "text/plain", lambda text: text.encode("utf-8-sig")),
    ("pdf", "application/pdf", pdf_bytes),
    ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", docx_bytes),
])
def test_resume_to_top_100_and_csv(local, extension, mime, builder):
    client, _, _ = local
    resume = builder("WORK EXPERIENCE\nDeveloper, Example  May 2025 - Aug 2025\n"
                     "- Built Python services and SQL data pipelines using Docker.\n")
    assert client.post("/matcher-api/resume",
                       files={"file": (f"resume.{extension}", resume, mime)}).status_code == 200
    jobs = [{"id": str(index), "title": "Python Developer",
             "description": "Python SQL Docker services"} for index in range(105)]
    assert client.post("/matcher-api/jobs/import", json={"jobs": jobs}).status_code == 200
    result = client.post("/matcher-api/rank", json={}).json()
    assert result["total_jobs"] == 105
    assert len(result["jobs"]) == 100
    assert [item["rank"] for item in result["jobs"]] == list(range(1, 101))
    assert result["jobs"][0]["matched_skills"] == ["Docker", "Python", "SQL"]
    exported = client.get("/matcher-api/export.csv")
    rows = list(csv.DictReader(io.StringIO(exported.text.lstrip("﻿"))))
    assert len(rows) == 100
