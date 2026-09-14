"""Synthetic browser tests exercise actual clicks, pagination, and modal parsing.

No WaterlooWorks account or real posting is used. The browser routes every
request to an in-memory fixture and never contacts WaterlooWorks.
"""
import asyncio
import os

import pytest

from sww.crawler import WaterlooWorksCrawler
from sww.parser import JOBS_URL


HTML = '''<!doctype html><html><head><title>Fixture</title></head><body>
<main id="board"></main><div id="modal"></div>
<script>
let current = 1;
function showPage(n) {
 current = n;
 const id = n === 1 ? '101' : '102';
 document.querySelector('#board').innerHTML = `<p>2 results ${n} - ${n}</p>
  <table><thead><tr><th></th><th>Job Title</th><th>Organization</th><th>City</th></tr></thead>
  <tbody><tr><td><input name="dataViewerSelection" value="${id}"></td>
  <td><a class="overflow--ellipsis" href="#" onclick="showDetail('${id}'); return false">Engineer ${id}</a></td>
  <td>Example ${n}</td><td>Waterloo</td></tr></tbody></table>
  <a class="pagination__link ${n===1 ? 'active' : ''}" href="#" onclick="showPage(1); return false">1</a>
  <a class="pagination__link ${n===2 ? 'active' : ''}" href="#" onclick="showPage(2); return false">2</a>
  <a class="pagination__link ${n===2 ? 'disabled' : ''}" href="#" onclick="showPage(2); return false">Next</a>`;
}
function showDetail(id) {
 document.querySelector('#modal').innerHTML = `<div class="modal is--visible"><h2>Posting ${id}</h2>
  <div class="is--long-form-reading"><table><tr><td>Job Summary:</td><td>Develop Python services for job ${id}</td></tr>
  <tr><td>Required Skills:</td><td>Python and SQL</td></tr></table></div></div>`;
}
document.addEventListener('keydown', e => { if(e.key === 'Escape') document.querySelector('#modal').innerHTML = ''; });
showPage(1);
</script></body></html>'''


async def run_fixture(tmp_path, html=HTML, **options):
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, channel=os.environ.get("SWW_TEST_BROWSER_CHANNEL") or None)
        context = await browser.new_context()
        await context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
        crawler = WaterlooWorksCrawler(tmp_path)
        crawler._context = context
        crawler._page = await context.new_page()
        context.on("response", crawler._on_response)
        await crawler._page.goto(JOBS_URL)
        progress = []

        async def notify(state):
            progress.append(state)

        # Skip only throttle waits in fixture tests; production clamps >=2s.
        async def fast_pause(seconds=None):
            crawler._check_stop()
            if seconds is not None:
                await asyncio.sleep(min(seconds, 0.001))

        crawler._pause = fast_pause
        jobs = await crawler.crawl(on_progress=notify, **options)
        await browser.close()
        return jobs, progress


def test_real_browser_pagination_then_detail_modal(tmp_path):
    jobs, progress = asyncio.run(run_fixture(tmp_path))
    assert [job["id"] for job in jobs] == ["101", "102"]
    assert all(job["requirements"] == "Python and SQL" for job in jobs)
    assert progress[-1]["state"] == "completed" and progress[-1]["complete"]
    assert progress[-1]["pages"] == 2
    messages = [p["message"] for p in progress]
    assert next(i for i, m in enumerate(messages) if "Collected page 2" in m) < next(i for i, m in enumerate(messages) if "Read 1/2" in m)


def test_limits_return_partial_jobs(tmp_path):
    jobs, progress = asyncio.run(run_fixture(tmp_path, max_pages=1))
    assert len(jobs) == 1
    assert progress[-1]["state"] == "completed"
    assert not progress[-1]["complete"]
    assert any("limit" in warning for warning in progress[-1]["warnings"])


def test_login_requires_user_action(tmp_path):
    jobs, progress = asyncio.run(run_fixture(tmp_path, html='<input type="password">'))
    assert not jobs
    assert progress[-1]["state"] == "login_required"
    assert not progress[-1]["complete"]


def test_empty_results_is_successful(tmp_path):
    jobs, progress = asyncio.run(run_fixture(tmp_path, html='<main>0 results</main>'))
    assert not jobs
    assert progress[-1]["state"] == "completed" and progress[-1]["complete"]


def test_parser_mismatch_is_failure(tmp_path):
    jobs, progress = asyncio.run(run_fixture(tmp_path, html='<main>Unknown redesigned job board</main>'))
    assert not jobs
    assert progress[-1]["state"] == "failed" and not progress[-1]["complete"]


def test_cancel_preserves_partial_state(tmp_path):
    event = asyncio.Event()
    event.set()
    jobs, progress = asyncio.run(run_fixture(tmp_path, cancel=event))
    assert not jobs
    assert progress[-1]["state"] == "cancelled" and not progress[-1]["complete"]


def test_rate_limit_stops_without_retry(tmp_path):
    crawler = WaterlooWorksCrawler(tmp_path)
    response = type("Response", (), {"status": 429, "url": JOBS_URL})()
    crawler._on_response(response)
    with pytest.raises(Exception, match="HTTP 429"):
        crawler._check_stop()


def test_browser_fallback_uses_only_dedicated_profile(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, Mock
    import playwright.async_api

    page = Mock()
    page.goto = AsyncMock()
    context = Mock()
    context.pages = [page]
    context.close = AsyncMock()
    engine = Mock()
    engine.chromium.launch_persistent_context = AsyncMock(side_effect=[
        RuntimeError("Executable doesn't exist at bundled location"), context,
    ])
    engine.stop = AsyncMock()
    manager = Mock()
    manager.start = AsyncMock(return_value=engine)
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: manager)
    monkeypatch.delenv("SWW_BROWSER_CHANNEL", raising=False)

    async def check():
        crawler = WaterlooWorksCrawler(tmp_path)
        await crawler.open_browser()
        await crawler.close()

    asyncio.run(check())
    calls = engine.chromium.launch_persistent_context.call_args_list
    assert len(calls) == 2
    assert "channel" not in calls[0].kwargs
    assert calls[1].kwargs["channel"] == "chrome"
    for call in calls:
        assert call.kwargs["user_data_dir"] == str((tmp_path / "browser").resolve())
        assert call.kwargs["headless"] is False
        assert call.kwargs["accept_downloads"] is False


def test_browser_channel_is_allowlisted(tmp_path, monkeypatch):
    monkeypatch.setenv("SWW_BROWSER_CHANNEL", "custom-profile")
    with pytest.raises(ValueError, match="SWW_BROWSER_CHANNEL"):
        asyncio.run(WaterlooWorksCrawler(tmp_path).open_browser())
