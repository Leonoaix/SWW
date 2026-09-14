"""Built frontend + real local API + fake model: no API key or network needed."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from sww.api import ROOT, create_app
from test_ai_ranking import FakeClient, JOB, RESUME, assessment
from test_api import FakeCrawler
from test_resume import pdf_bytes


@pytest.mark.skipif(not (ROOT / "frontend/dist/waterlooworks.html").exists(), reason="Build frontend to run browser integration")
@pytest.mark.parametrize("width", [1440, 390])
def test_upload_import_ai_evidence_and_csv(tmp_path, width):
    raw = assessment()
    raw["criteria"][0]["explanation"] = '<img src=x onerror="window.compromised=true">这是文本而非 HTML。'
    app = create_app(tmp_path, FakeCrawler(), ai_factory=lambda: FakeClient(raw))
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
        expect(page.locator("#ai-config")).to_contain_text("已配置模型")
        page.locator("#resume-file").set_input_files({"name": "synthetic.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes(RESUME)})
        page.locator("#upload-button").click()
        expect(page.locator("#resume-filename")).to_have_text("synthetic.pdf")
        page.locator(".import-details summary").click()
        page.locator("#jobs-file").set_input_files({"name": "jobs.json", "mimeType": "application/json", "buffer": json.dumps({"jobs": [JOB]}).encode()})
        page.locator("#import-button").click()
        expect(page.locator("#total-count")).to_have_text("1")
        page.locator("#rank-button").click()
        expect(page.locator(".job-result")).to_have_count(1, timeout=15000)
        expect(page.locator("#ai-result-note")).to_contain_text("1/1")
        page.locator(".job-detail summary").click()
        expect(page.locator(".evidence-quote")).to_have_count(2)
        assert page.evaluate("window.compromised") is None
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        with page.expect_download() as info:
            page.locator("#export-button").click()
        download = info.value
        assert download.suggested_filename == "waterlooworks-top100.csv"
        assert "Built asynchronous Python" in Path(download.path()).read_text(encoding="utf-8-sig")
        assert not errors
        page.screenshot(path=str(tmp_path / f"ai-results-{width}.png"), full_page=True)
        browser.close()
