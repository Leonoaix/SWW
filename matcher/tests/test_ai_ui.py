"""Built frontend + real local API + fake model: no API key or network needed."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from sww.api import create_app
from sww.config import ROOT
from conftest import RESUME, FakeClient, job, judgement_payload
from test_api import FakeCrawler
from test_resume import pdf_bytes
from test_docx import docx_bytes


@pytest.mark.skipif(not (ROOT / "frontend/dist/waterlooworks.html").exists(), reason="Build frontend to run browser integration")
@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("extension,mime,builder", [
    ("md", "text/markdown", lambda text: ("# Resume\n\n" + text).encode("utf-8")),
    ("pdf", "application/pdf", pdf_bytes),
    ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", docx_bytes),
])
def test_upload_import_ai_evidence_and_csv(tmp_path, width, extension, mime, builder):
    # Model explanations reach the DOM: they must be inserted as text.
    judgements = judgement_payload("direct", "Built asynchronous Python services with durable queues")
    judgements["judgements"][0]["explanation"] = (
        '<img src=x onerror="window.compromised=true">这是文本而非 HTML。')
    app = create_app(tmp_path, FakeCrawler(), ai_factory=lambda: FakeClient(judgements=judgements),
                     embedder=None)
    with TestClient(app, base_url="http://127.0.0.1:8765") as http, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, channel=os.environ.get("SWW_TEST_BROWSER_CHANNEL") or None)
        context = browser.new_context(viewport={"width": width, "height": 1000}, accept_downloads=True)

        def route_local(route):
            request = route.request
            url = urlsplit(request.url)
            if url.hostname != "127.0.0.1":
                route.abort()
                return
            response = http.request(request.method, url.path, headers=request.all_headers(), content=request.post_data_buffer)
            headers = {key: value for key, value in response.headers.items() if key not in {"content-length", "content-encoding", "transfer-encoding"}}
            route.fulfill(status=response.status_code, headers=headers, body=response.content)

        context.route("**/*", route_local)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://127.0.0.1:8765/waterlooworks")
        # An id referenced from TypeScript but missing from the HTML throws
        # during module evaluation and leaves the page frozen at its
        # placeholders. Fail on that here rather than ten confusing
        # assertions later.
        page.wait_for_timeout(200)
        assert errors == []
        expect(page.locator("#ai-config")).to_contain_text("已配置模型")
        assert f".{extension}" in page.locator("#resume-file").get_attribute("accept")
        page.locator("#resume-file").set_input_files({"name": f"synthetic.{extension}", "mimeType": mime, "buffer": builder(RESUME)})
        page.locator("#upload-button").click()
        expect(page.locator("#resume-filename")).to_have_text(f"synthetic.{extension}")
        page.locator(".import-details summary").click()
        posting = job("1", "Backend Intern",
                      "Develop reliable event-driven services using durable message queues.")
        restricted = {**posting, "id": "restricted",
                      "metadata": {"Special Job Requirements": "Funded by SWPP"}}
        page.locator("#jobs-file").set_input_files(
            {"name": "jobs.json", "mimeType": "application/json",
             "buffer": json.dumps({"jobs": [posting, restricted]}).encode()})
        page.locator("#import-button").click()
        expect(page.locator("#total-count")).to_have_text("2")
        expect(page.locator("#exclude-restricted-eligibility")).to_be_checked()
        page.locator("#rank-button").click()
        expect(page.locator(".job-result")).to_have_count(1, timeout=15000)
        # The note must state what was shortlisted and what was not assessed,
        # so a partial sweep is never read as a full one.
        expect(page.locator("#ai-result-note")).to_contain_text("模型已评估 1 个")
        expect(page.locator("#ai-result-note")).to_contain_text("BM25")
        expect(page.locator("#resume-experiences li")).to_have_count(2)
        expect(page.locator("#resume-listed-skills")).to_contain_text("Rust")
        expect(page.locator("#resume-availability")).to_contain_text("4")
        page.locator(".job-detail summary").click()
        expect(page.locator(".evidence-quote")).to_have_count(2)
        assert page.evaluate("window.compromised") is None
        # The apply affordance is on the row, not buried in the disclosure.
        apply_link = page.locator(".job-result .job-actions a").first
        expect(apply_link).to_have_text("去申请 ↗")
        assert apply_link.get_attribute("target") == "_blank"
        assert apply_link.get_attribute("rel") == "noopener noreferrer"
        assert apply_link.get_attribute("href").startswith("https://waterlooworks.uwaterloo.ca/")
        expect(page.locator(".job-applied-state")).to_be_hidden()
        apply_link.click()
        expect(page.locator(".job-applied-state")).to_have_text("已打开过申请页")
        # Detail bodies are built on first open, not for every collapsed row.
        collapsed = page.evaluate(
            "document.querySelectorAll('.job-detail:not([open]) .evidence-item').length")
        assert collapsed == 0
        # Filtering hides existing rows instead of rebuilding them.
        row = page.locator(".job-result").first
        page.locator("#result-search").fill("zzz-no-such-job")
        expect(page.locator(".job-result:not([hidden])")).to_have_count(0)
        expect(page.locator("#results-empty")).to_be_visible()
        page.locator("#result-search").fill("")
        expect(page.locator(".job-result:not([hidden])")).to_have_count(1)
        assert row.evaluate("node => node.isConnected") is True
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        with page.expect_download() as info:
            page.locator("#export-button").click()
        download = info.value
        assert download.suggested_filename == "waterlooworks-top100.csv"
        assert "Built asynchronous Python" in Path(download.path()).read_text(encoding="utf-8-sig")
        page.locator("#ranking-details summary").click()
        expect(page.locator("#excluded-jobs")).to_contain_text("SWPP")
        page.locator("#exclude-restricted-eligibility").uncheck()
        expect(page.locator(".job-result")).to_have_count(0)
        page.locator("#ranking-engine").select_option("local")
        page.locator("#rank-button").click()
        expect(page.locator(".job-result")).to_have_count(2)
        page.locator("#exclude-restricted-eligibility").check()
        page.locator("#rank-button").click()
        expect(page.locator(".job-result")).to_have_count(1)
        assert not errors
        page.screenshot(path=str(tmp_path / f"ai-results-{width}.png"), full_page=True)
        browser.close()
