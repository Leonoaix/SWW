"""Synthetic browser tests exercise actual clicks, pagination, and modal parsing.

No WaterlooWorks account or real posting is used. The browser routes every
request to an in-memory fixture and never contacts WaterlooWorks.
"""
import asyncio
import os

import pytest

from sww.jobs.crawler import WaterlooWorksCrawler
from sww.jobs.parser import JOBS_URL


HTML = '''<!doctype html><html><head><title>Fixture</title></head><body>
<button aria-pressed="true">My Program</button>
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


async def run_fixture(tmp_path, html=HTML, read_ids=None, **options):
    from sww.api.schemas import Job
    options.setdefault("validate_job", lambda job: Job(**job).model_dump())
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
        if read_ids is not None:
            read_detail = crawler._read_detail

            async def track_read(target, job_id):
                read_ids.append(job_id)
                return await read_detail(target, job_id)

            crawler._read_detail = track_read
        jobs = await crawler.crawl(on_progress=notify, **options)
        await browser.close()
        return jobs, progress


def test_real_browser_reads_page_details_before_pagination(tmp_path):
    jobs, progress = asyncio.run(run_fixture(tmp_path))
    assert [job["id"] for job in jobs] == ["101", "102"]
    assert all(job["requirements"] == "Python and SQL" for job in jobs)
    assert progress[-1]["state"] == "completed" and progress[-1]["complete"]
    assert progress[-1]["pages"] == 2
    messages = [p["message"] for p in progress]
    assert next(i for i, m in enumerate(messages) if "Read 1/2" in m) < next(i for i, m in enumerate(messages) if "Collected page 2" in m)


def test_limits_return_partial_jobs(tmp_path):
    jobs, progress = asyncio.run(run_fixture(tmp_path, max_pages=1))
    assert len(jobs) == 1
    assert progress[-1]["state"] == "completed"
    assert not progress[-1]["complete"]
    assert any("limit" in warning for warning in progress[-1]["warnings"])
    assert jobs[0]["metadata"]["detail_status"] == "complete"


@pytest.mark.parametrize("start_page", [1, 17])
def test_seventeen_pages_with_collapsed_first_page_control(tmp_path, start_page):
    html = HTML.replace("const id = n === 1 ? '101' : '102';", "const id = String(100 + n);")
    html = html.replace('2 results ${n}', '17 results ${n}')
    start = html.index('  <a class="pagination__link')
    end = html.index('</a>`;', start) + len('</a>')
    html = html[:start] + '''
      <button class="pagination__link" aria-label="Previous page" ${n===1 ? 'disabled' : ''}
        onclick="showPage(${n-1})">chevron_left</button>
      <a class="pagination__link active" href="#">${n}</a>
      <button class="pagination__link" aria-label="Next page" ${n===17 ? 'disabled' : ''}
        onclick="showPage(${n+1})">chevron_right</button>''' + html[end:]
    html = html.replace('showPage(1);\n</script>', f'showPage({start_page});\n</script>')
    jobs, progress = asyncio.run(run_fixture(tmp_path, html=html))
    assert [job["id"] for job in jobs] == [str(100 + n) for n in range(1, 18)]
    assert all(job["metadata"]["detail_status"] == "complete" for job in jobs)
    assert progress[-1]["pages"] == 17
    assert progress[-1]["complete"], progress[-1]


def test_missing_all_backward_controls_reports_actionable_error(tmp_path):
    html = HTML.replace('<a class="pagination__link ${n===1 ? \'active\' : \'\'}" href="#" onclick="showPage(1); return false">1</a>', '')
    html = html.replace('showPage(1);\n</script>', 'showPage(2);\n</script>')
    jobs, progress = asyncio.run(run_fixture(tmp_path, html=html))
    assert not jobs and progress[-1]["state"] == "failed"
    assert "手动回到第一页" in progress[-1]["message"]


def test_login_requires_user_action(tmp_path):
    jobs, progress = asyncio.run(run_fixture(tmp_path, html='<input type="password">'))
    assert not jobs
    assert progress[-1]["state"] == "login_required"
    assert not progress[-1]["complete"]


def test_empty_results_is_successful(tmp_path):
    jobs, progress = asyncio.run(run_fixture(tmp_path, html='<button aria-pressed="true">My Program</button><main>0 results</main>'))
    assert not jobs
    assert progress[-1]["state"] == "completed" and progress[-1]["complete"]


def test_unfiltered_empty_landing_is_not_success(tmp_path):
    jobs, progress = asyncio.run(run_fixture(tmp_path, html='<main>0 results</main>'))
    assert not jobs
    assert progress[-1]["state"] == "failed" and not progress[-1]["complete"]
    assert "My Program" in progress[-1]["message"]


def test_enables_my_program_once_and_preserves_filter_throughout_crawl(tmp_path):
    # A reload resets this synthetic filter. Reading by raw href instead of
    # clicking the JS pagination control would also reset to the landing page.
    html = HTML.replace('<button aria-pressed="true">My Program</button>', '''
      <button id="program" onclick="toggleProgram()">My Program <span>toggle_off</span></button>''')
    html = html.replace('let current = 1;', '''let current = 1; let program = false;
      function toggleProgram() {
        program = !program;
        document.querySelector('#program span').textContent = program ? 'toggle_on' : 'toggle_off';
        document.querySelector('#board').setAttribute('aria-busy', 'true');
        setTimeout(() => { if(program) showPage(1); document.querySelector('#board').removeAttribute('aria-busy'); }, 30);
      }''')
    html = html.replace("showPage(1);\n</script>", "document.querySelector('#board').textContent = '0 results';\n</script>")
    html = html.replace('href="#" onclick="showPage', 'href="?page=1" onclick="showPage')
    jobs, progress = asyncio.run(run_fixture(tmp_path, html=html))
    assert [job["id"] for job in jobs] == ["101", "102"]
    assert all(job["metadata"]["detail_status"] == "complete" for job in jobs)
    assert progress[-1]["complete"]


def test_filter_loss_during_pagination_stops_before_collecting_other_programs(tmp_path):
    html = HTML.replace("current = n;", "current = n; if(n === 2) document.querySelector('button').setAttribute('aria-pressed', 'false');")
    jobs, progress = asyncio.run(run_fixture(tmp_path, html=html))
    assert [job["id"] for job in jobs] == ["101"]
    assert progress[-1]["state"] == "failed"
    assert "My Program" in progress[-1]["message"]


def test_stacked_overview_fields_inside_modal(tmp_path):
    html = HTML.replace('''<table><tr><td>Job Summary:</td><td>Develop Python services for job ${id}</td></tr>
  <tr><td>Required Skills:</td><td>Python and SQL</td></tr></table>''', '''
      <h3>JOB POSTING INFORMATION</h3>
      <div><strong>Work Term:</strong><div>2027 - Winter</div></div>
      <div><strong>Job Title:</strong><div>Engineer ${id}</div></div>
      <div><strong>Job Summary:</strong><div>Develop Python services for job ${id}</div></div>
      <div><strong>Required Skills:</strong><ul><li>Python</li><li>SQL</li></ul></div>''')
    jobs, progress = asyncio.run(run_fixture(tmp_path, html=html))
    assert progress[-1]["complete"]
    assert all(job["requirements"] == "Python\nSQL" for job in jobs)
    assert all(job["metadata"]["Work Term"] == "2027 - Winter" for job in jobs)


def test_hidden_siblings_do_not_shift_title_or_pagination_selectors(tmp_path):
    html = HTML.replace('<main id="board">', '<main hidden>0 results</main><main id="board">')
    html = html.replace('<a class="pagination__link ${n===1', '<a class="pagination__link" hidden href="#">99</a><a class="pagination__link ${n===1')
    jobs, progress = asyncio.run(run_fixture(tmp_path, html=html))
    assert [job["id"] for job in jobs] == ["101", "102"]
    assert progress[-1]["complete"]


@pytest.mark.parametrize("mode", ["same_tab", "popup"])
def test_title_click_opens_detail_page_and_restores_filtered_pagination(tmp_path, mode):
    html = HTML.replace('href="#" onclick="showDetail(\'${id}\'); return false"',
                        'href="?action=displayJob&amp;jobId=${id}"' + (' target="_blank"' if mode == "popup" else ''))
    html = html.replace("current = n;", "current = n; sessionStorage.setItem('page', String(n));")
    html = html.replace("showPage(1);\n</script>", '''
      const posting = new URLSearchParams(location.search).get('jobId');
      if (posting) {
        document.body.innerHTML = `<main><header><h2>Posting ${posting}</h2></header>
          <h3>JOB POSTING INFORMATION</h3>
          <section><strong>Job Title:</strong><p>Engineer ${posting}</p></section>
          <section><strong>Job Summary:</strong><p>Develop services for ${posting}</p></section>
          <section><strong>Required Skills:</strong><p>Python and SQL</p></section></main>`;
      } else { showPage(Number(sessionStorage.getItem('page') || 1)); }
      </script>''')
    jobs, progress = asyncio.run(run_fixture(tmp_path, html=html))
    assert [job["id"] for job in jobs] == ["101", "102"]
    assert progress[-1]["complete"], progress[-1]
    assert all(job["description"] == f"Develop services for {job['id']}" for job in jobs)
    assert all(job["requirements"] == "Python and SQL" for job in jobs)


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


def test_cancel_then_change_interval_reuses_completed_details(tmp_path):
    from copy import deepcopy

    event = asyncio.Event()
    checkpoints = []

    async def save(jobs):
        checkpoints.append(deepcopy(jobs))
        if any(j["metadata"].get("detail_status") == "complete" for j in jobs):
            event.set()

    jobs, progress = asyncio.run(run_fixture(tmp_path, cancel=event, on_checkpoint=save))
    assert progress[-1]["state"] == "cancelled"
    assert jobs[0]["metadata"]["detail_status"] == "complete"
    stamp = jobs[0]["metadata"]["detail_collected_at"]
    reads = []
    resumed, progress = asyncio.run(run_fixture(tmp_path, cached_jobs=checkpoints[-1], delay_seconds=7, read_ids=reads))
    assert reads == ["102"]
    assert progress[-1]["complete"] and progress[-1]["reused_details"] == 1
    assert resumed[0]["metadata"]["detail_collected_at"] == stamp


def test_resume_only_reuses_current_jobs_and_refreshes_listing_fields(tmp_path):
    jobs, _ = asyncio.run(run_fixture(tmp_path))
    reads = []
    html = HTML.replace("Example ${n}", "Updated ${n}").replace("'102'", "'103'")
    resumed, progress = asyncio.run(run_fixture(tmp_path, html=html, cached_jobs=jobs, read_ids=reads))
    assert [j["id"] for j in resumed] == ["101", "103"]
    assert resumed[0]["company"] == "Updated 1"
    assert resumed[0]["requirements"] == "Python and SQL"
    assert reads == ["103"] and progress[-1]["complete"]


def test_expired_details_are_read_again(tmp_path):
    jobs, _ = asyncio.run(run_fixture(tmp_path))
    for job in jobs:
        job["metadata"]["detail_collected_at"] = "2000-01-01T00:00:00+00:00"
    reads = []
    _, progress = asyncio.run(run_fixture(tmp_path, cached_jobs=jobs, read_ids=reads))
    assert reads == ["101", "102"]
    assert progress[-1]["complete"] and progress[-1]["reused_details"] == 0


def test_fully_cached_crawl_opens_no_details_and_failed_details_are_retried(tmp_path):
    jobs, _ = asyncio.run(run_fixture(tmp_path))
    reads = []
    _, progress = asyncio.run(run_fixture(tmp_path, cached_jobs=jobs, read_ids=reads))
    assert reads == [] and progress[-1]["complete"]
    assert progress[-1]["reused_details"] == 2
    jobs[1]["metadata"]["detail_status"] = "failed"
    _, progress = asyncio.run(run_fixture(tmp_path, cached_jobs=jobs, read_ids=reads))
    assert reads == ["102"] and progress[-1]["complete"]


def test_invalid_detail_does_not_poison_checkpoints_or_stop_remaining_jobs(tmp_path):
    from copy import deepcopy
    from sww.api.schemas import Job

    html = HTML.replace('<tr><td>Required Skills:</td>', '''<tr><td>Job Title:</td><td>${id === '101' ? 'PRIVATE'.repeat(200) : 'Engineer 102'}</td></tr>
  <tr><td>Required Skills:</td>''')
    checkpoints = []
    async def save(jobs):
        checkpoints.append([Job(**deepcopy(job)).model_dump() for job in jobs])
    jobs, progress = asyncio.run(run_fixture(tmp_path, html=html, on_checkpoint=save))
    assert [j["id"] for j in jobs] == ["101", "102"]
    assert jobs[0]["title"] == "Engineer 101"
    assert jobs[0]["metadata"]["detail_status"] == "failed"
    assert jobs[1]["metadata"]["detail_status"] == "complete"
    assert checkpoints[-1] == jobs
    assert progress[-1]["state"] == "completed" and not progress[-1]["complete"]
    issue = progress[-1]["warnings"][0]
    assert "101" in issue and "title" in issue and "string_too_long" in issue
    assert "PRIVATE" not in str(progress)
    reads = []
    jobs, progress = asyncio.run(run_fixture(tmp_path, cached_jobs=jobs, read_ids=reads))
    assert reads == ["101"] and progress[-1]["complete"]


def test_invalid_listing_row_does_not_stop_next_page(tmp_path):
    html = HTML.replace('Engineer ${id}</a>', "${n === 1 ? 'X'.repeat(1001) : 'Engineer 102'}</a>")
    jobs, progress = asyncio.run(run_fixture(tmp_path, html=html))
    assert [j["id"] for j in jobs] == ["102"]
    assert jobs[0]["metadata"]["detail_status"] == "complete"
    assert progress[-1]["state"] == "completed" and not progress[-1]["complete"]
    assert any("title" in warning for warning in progress[-1]["warnings"])


def test_detail_validation_is_retried_immediately_and_can_recover(tmp_path):
    from unittest.mock import AsyncMock
    from sww.api.schemas import Job
    crawler = WaterlooWorksCrawler(tmp_path)
    crawler._read_detail = AsyncMock(side_effect=[
        {"title": "X" * 1001, "metadata": {"detail_status": "complete"}},
        {"description": "Recovered description", "metadata": {"detail_status": "complete"}},
    ])
    notify = AsyncMock()
    job = Job(id="123", title="Engineer").model_dump()
    result, issue = asyncio.run(crawler._collect_detail(None, job, lambda j: Job(**j).model_dump(), notify))
    assert result["description"] == "Recovered description" and not issue
    assert crawler._read_detail.await_count == 2 and notify.await_count == 1
    assert "自动重试" in notify.call_args.args[0]


def test_bad_location_retries_then_uses_valid_listing_location(tmp_path):
    from unittest.mock import AsyncMock
    from sww.api.schemas import Job
    crawler = WaterlooWorksCrawler(tmp_path)
    crawler._read_detail = AsyncMock(return_value={"location": "X" * 1200, "description": "Good description",
                                                  "metadata": {"detail_status": "complete"}})
    job = Job(id="123", title="Engineer", location="Toronto").model_dump()
    result, issue = asyncio.run(crawler._collect_detail(None, job, lambda j: Job(**j).model_dump(), AsyncMock()))
    assert crawler._read_detail.await_count == 3
    assert result["location"] == "Toronto" and result["description"] == "Good description"
    assert result["metadata"]["detail_status"] == "complete"
    assert result["metadata"]["location_source"] == "listing"
    assert "继续采集" in issue


def test_read_failures_are_bounded_and_do_not_retry_login_or_cancel(tmp_path):
    from unittest.mock import AsyncMock
    from sww.api.schemas import Job
    from sww.jobs.crawler import CrawlStopped
    crawler = WaterlooWorksCrawler(tmp_path)
    job = Job(id="123", title="Engineer").model_dump()
    crawler._read_detail = AsyncMock(side_effect=RuntimeError("private page data"))
    result, issue = asyncio.run(crawler._collect_detail(None, job, None, AsyncMock()))
    assert result is None and crawler._read_detail.await_count == 3
    assert "继续采集" in issue and "private page data" not in issue
    for state in ["login_required", "cancelled", "failed"]:
        crawler._read_detail = AsyncMock(side_effect=CrawlStopped(state, "Stop"))
        with pytest.raises(CrawlStopped):
            asyncio.run(crawler._collect_detail(None, job, None, AsyncMock()))
        assert crawler._read_detail.await_count == 1


@pytest.mark.parametrize("age,eligible", [(0, True), (86399, True), (86400, False), (86401, False), (-1, False)])
def test_resume_expiry_boundary(age, eligible):
    from datetime import datetime, timedelta, timezone
    from sww.jobs.crawler import reusable_details

    now = datetime.now(timezone.utc)
    stamp = (now - timedelta(seconds=age)).isoformat()
    job = {"id": "1", "metadata": {"detail_status": "complete", "detail_collected_at": stamp}}
    assert bool(reusable_details([job], now=now)) == eligible
    # Recent overall crawl time must not renew an expired individual detail.
    assert bool(reusable_details([job], now.isoformat(), now=now)) == eligible


@pytest.mark.parametrize("stamp", [None, "bad", "2026-01-01T00:00:00", 123])
def test_invalid_resume_dates_are_ignored(stamp):
    from sww.jobs.crawler import reusable_details
    assert not reusable_details([{"id": "1", "metadata": {"detail_status": "complete", "detail_collected_at": stamp}}])
