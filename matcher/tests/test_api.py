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
from sww.store import Store
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


def test_the_resume_summary_carries_the_dates_recency_is_scored_from(local):
    """The panel shows how long ago each experience was, so the dimension that
    moves the ranking is visible rather than invisible arithmetic."""
    client, app, _ = local
    seed_resume(app)
    body = client.get("/matcher-api/resume/profile").json()
    experiences = body["experiences"]
    assert all(item["months_ago"] is not None for item in experiences)
    assert all(0 < item["recency"] <= 1 for item in experiences)
    # Newest first, so a length-capped prompt or query drops the oldest.
    assert experiences == sorted(experiences, key=lambda item: item["months_ago"])


class OpenableCrawler(FakeCrawler):
    """A login browser that records what it was asked to show.

    It mirrors the real crawler's three cases: a posting with an address of its
    own is navigated to, a posting without one is found on the board and
    clicked, and anything that is not a WaterlooWorks posting is refused.
    """

    def __init__(self, is_open=True, on_board=("123",)):
        super().__init__()
        self.is_open = is_open
        self.opened = []        # addresses navigated to
        self.clicked = []       # postings opened by clicking their board row
        self.on_board = set(on_board)

    async def open_posting(self, url, job_id="", max_pages=60):
        from sww.jobs.parser import JOBS_URL, is_placeholder, safe_job_url
        if url and not is_placeholder(url):
            destination = safe_job_url(url)
            if destination is None:
                raise ValueError("Refused a job URL that is not a read-only WaterlooWorks posting.")
            if "?" in destination:
                self.opened.append(destination)
                return {"opened": "page", "url": destination, "pages_searched": 0}
        if not job_id.isdigit():
            raise ValueError("Opening a posting without its own address needs its numeric id.")
        if job_id not in self.on_board:
            raise LookupError(f"Posting {job_id} was not found on the current board.")
        self.clicked.append(job_id)
        return {"opened": "dialog", "url": JOBS_URL, "pages_searched": 1}


def app_with_browser(tmp_path, crawler):
    return create_app(tmp_path, crawler, embedder=None, reranker=None)


def test_apply_opens_the_posting_in_the_logged_in_browser(tmp_path):
    crawler = OpenableCrawler()
    app = app_with_browser(tmp_path, crawler)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-SWW-Client": "1"}) as client:
        seed_jobs(app, [job("123")])
        response = client.post("/matcher-api/jobs/123/open")
        assert response.status_code == 200
        assert crawler.opened == [job("123")["url"]]
        assert "不会替你提交" in response.json()["message"]
        assert client.get("/matcher-api/status").json()["browser"]["open"] is True


def test_apply_needs_the_browser_and_a_known_posting(tmp_path):
    closed = OpenableCrawler(is_open=False)
    app = app_with_browser(tmp_path, closed)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-SWW-Client": "1"}) as client:
        seed_jobs(app, [job("123")])
        assert client.post("/matcher-api/jobs/123/open").status_code == 409
        assert client.get("/matcher-api/status").json()["browser"]["open"] is False
        assert closed.opened == []

    crawler = OpenableCrawler()
    app = app_with_browser(tmp_path, crawler)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-SWW-Client": "1"}) as client:
        seed_jobs(app, [job("123")])
        assert client.post("/matcher-api/jobs/999/open").status_code == 404


def test_apply_refuses_a_url_that_is_not_a_waterlooworks_posting(tmp_path):
    """Job URLs are validated on import, but the browser is the thing being
    pointed somewhere, so the refusal lives there too."""
    crawler = OpenableCrawler()
    app = app_with_browser(tmp_path, crawler)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-SWW-Client": "1"}) as client:
        app.state.store.replace_jobs([{**job("123"), "url": "https://evil.test/apply"}])
        assert client.post("/matcher-api/jobs/123/open").status_code == 422
        assert crawler.opened == []


def test_apply_never_submits_an_application(tmp_path):
    """The service opens the application page and stops there. Nothing in the
    crawler may click Apply, and this is the endpoint most likely to drift."""
    crawler = OpenableCrawler()
    app = app_with_browser(tmp_path, crawler)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-SWW-Client": "1"}) as client:
        seed_jobs(app, [job("123")])
        client.post("/matcher-api/jobs/123/open")
    # The only interaction is a navigation to the read-only posting URL.
    assert crawler.opened and crawler.clicked == []
    assert all(url.startswith("https://waterlooworks.uwaterloo.ca/") for url in crawler.opened)
    assert not hasattr(crawler, "submitted")


def test_a_posting_with_no_address_is_opened_by_clicking_its_board_row(tmp_path):
    """The bug this guards: `jobs.htm#job-123` is a valid WaterlooWorks URL
    that lands on the board, so following it sent people to the wrong page.
    The detail view is a dialog, so the only way in is to click the row."""
    from sww.jobs.parser import placeholder_url
    crawler = OpenableCrawler()
    app = app_with_browser(tmp_path, crawler)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-SWW-Client": "1"}) as client:
        app.state.store.replace_jobs([{**job("123"), "url": placeholder_url("123")}])
        body = client.post("/matcher-api/jobs/123/open").json()
        assert body["opened"] == "dialog"
        assert crawler.clicked == ["123"]
        assert crawler.opened == []                # never navigated to the board


def test_a_posting_missing_from_the_board_says_so_rather_than_opening_the_board(tmp_path):
    from sww.jobs.parser import placeholder_url
    crawler = OpenableCrawler(on_board=())
    app = app_with_browser(tmp_path, crawler)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-SWW-Client": "1"}) as client:
        app.state.store.replace_jobs([{**job("123"), "url": placeholder_url("123")}])
        response = client.post("/matcher-api/jobs/123/open")
        assert response.status_code == 404
        assert "123" in response.json()["detail"]
        assert crawler.opened == []


def test_a_posting_with_a_real_address_opens_that_address(tmp_path):
    crawler = OpenableCrawler()
    app = app_with_browser(tmp_path, crawler)
    real = "https://waterlooworks.uwaterloo.ca/myAccount/co-op/full/jobs.htm?action=display&jobId=654321"
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-SWW-Client": "1"}) as client:
        app.state.store.replace_jobs([{**job("654321"), "url": real}])
        body = client.post("/matcher-api/jobs/654321/open").json()
        assert body["opened"] == "page"
        assert crawler.opened == [real]
        assert crawler.clicked == []


def restart(tmp_path):
    """A second service over the same data directory is what a restart is."""
    return create_app(tmp_path, FakeCrawler(), embedder=None, reranker=None)


def test_a_resume_and_its_ranking_survive_a_restart(tmp_path):
    """Both used to live only in the process, so every restart silently threw
    away an upload and an evaluation that had cost real model calls."""
    app = restart(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-SWW-Client": "1"}) as client:
        seed_jobs(app, [job("123"), job("456", "Data Engineer", "Python pipelines and SQL")])
        uploaded = client.post("/matcher-api/resume",
                               files={"file": ("private.md", RESUME.encode(), "text/markdown")})
        assert uploaded.status_code == 200
        ranked = client.post("/matcher-api/rank", json={}).json()
        assert ranked["jobs"]

    with TestClient(restart(tmp_path), base_url="http://127.0.0.1:8765",
                    headers={"X-SWW-Client": "1"}) as client:
        status = client.get("/matcher-api/status").json()
        assert status["has_resume"] is True
        assert status["resume"]["filename"] == "private.md"
        assert status["resume"]["skills"] == uploaded.json()["skills"]
        assert status["ranking"] == {"available": True, "engine": ranked["engine"]}
        restored = client.get("/matcher-api/ranking").json()
        assert [entry["id"] for entry in restored["jobs"]] == [entry["id"] for entry in ranked["jobs"]]
        # Export reads the same ranking, so it works without ranking again.
        assert client.get("/matcher-api/export.csv").status_code == 200


def test_uploading_a_new_resume_drops_the_ranking_made_from_the_old_one(tmp_path):
    app = restart(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-SWW-Client": "1"}) as client:
        seed_jobs(app, [job("123")])
        client.post("/matcher-api/resume", files={"file": ("a.md", RESUME.encode(), "text/markdown")})
        assert client.post("/matcher-api/rank", json={}).status_code == 200
        client.post("/matcher-api/resume", files={"file": ("b.md", RESUME.encode(), "text/markdown")})
        assert client.get("/matcher-api/status").json()["ranking"]["available"] is False

    with TestClient(restart(tmp_path), base_url="http://127.0.0.1:8765",
                    headers={"X-SWW-Client": "1"}) as client:
        assert client.get("/matcher-api/status").json()["resume"]["filename"] == "b.md"
        assert client.get("/matcher-api/ranking").status_code == 409


def test_state_written_by_an_incompatible_version_is_discarded_not_fatal(tmp_path):
    """Losing a stored blob is exactly as bad as never having stored it. A
    service that will not start is much worse."""
    app = restart(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-SWW-Client": "1"}) as client:
        seed_jobs(app, [job("123")])
        client.post("/matcher-api/resume", files={"file": ("a.md", RESUME.encode(), "text/markdown")})
    store = Store(tmp_path)
    store.set_meta("state:resume", '{"text": 5, "unknown_field": true}')
    store.set_meta("state:ranking", "not json at all")
    store.close()

    with TestClient(restart(tmp_path), base_url="http://127.0.0.1:8765",
                    headers={"X-SWW-Client": "1"}) as client:
        status = client.get("/matcher-api/status").json()
        assert status["has_resume"] is False
        assert status["ranking"]["available"] is False
        # The unreadable blobs are cleared rather than re-read on every start.
        assert Store(tmp_path).get_meta("state:resume") == ""
