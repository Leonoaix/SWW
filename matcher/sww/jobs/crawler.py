"""A serial, manually authenticated WaterlooWorks browser crawler.

Each page's posting details are read before moving to the next result page.
No passwords, imported cookies, hidden APIs, or application actions are used.
Live selectors still need verification in the user's authenticated account.
"""
from __future__ import annotations

import asyncio
import math
import random
import time
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from pydantic import ValidationError

from ..store import detail_is_fresh

from .parser import (
    DASHBOARD_URL, JOBS_URL, ORIGIN, Listing, Target, login_required, parse_detail,
    parse_listing, safe_job_url,
)

Progress = Callable[[dict], Awaitable[None]]

# Only navigations and data calls count against the rate limit; images, CSS and
# fonts are the browser filling out a page we already asked for.
TRACKED_RESOURCES = frozenset({"document", "xhr", "fetch"})
# Above this, the board is taken to be struggling and the interval widens.
SLOW_RESPONSE_SECONDS = 5.0
MAX_PENALTY = 8.0
# The hard floor and the default. Neither is a measured WaterlooWorks limit —
# no such number is published — so the default stays well under one request a
# second and the backoff above does the adapting.
MIN_DELAY_SECONDS = 0.5
DEFAULT_DELAY_SECONDS = 1.0

# Last listing parse, reused while a page settles. One entry: the poll loop
# only ever asks about the page it is currently waiting on.
_LISTING_CACHE: dict = {}
_LOGIN_CACHE: dict = {}


def validation_message(exc: ValidationError, job_id: str = "") -> str:
    """Report field/rule only; exception inputs can contain private page data."""
    fields = {"id", "title", "company", "location", "description", "requirements", "deadline", "url", "status", "is_open", "metadata"}
    issues = []
    for error in exc.errors(include_input=False, include_context=False, include_url=False):
        name = str(error["loc"][0]) if error["loc"] else ""
        name = name if name in fields else "job"
        rule = error["type"]
        issues.append(f"{name}（{rule}）")
    prefix = f"岗位 {job_id}" if job_id.isdigit() else "岗位数据"
    return f"{prefix}校验失败：{'、'.join(dict.fromkeys(issues))}。已保留有效数据。"


def reusable_details(jobs, fallback_time=None, *, now=None) -> dict:
    """Filter postings handed to the crawler down to the still-fresh details.

    The freshness rule itself lives in `store.detail_is_fresh` and is shared:
    the store applies it when offering postings for resumption, and the crawler
    re-applies it to whatever it is given rather than trusting the caller.
    """
    now = now or datetime.now(timezone.utc)
    result = {}
    for job in jobs:
        metadata = job.get("metadata", {})
        stamp = metadata.get("detail_collected_at", fallback_time)
        if not job.get("id") or not detail_is_fresh({**metadata, "detail_collected_at": stamp}, now):
            continue
        cached = deepcopy(job)
        cached["metadata"]["detail_collected_at"] = stamp
        result[job["id"]] = cached
    return result


class CrawlStopped(Exception):
    def __init__(self, state: str, message: str):
        self.state = state
        super().__init__(message)


class WaterlooWorksCrawler:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self._playwright = None
        self._context = None
        self._page = None
        self._busy = False
        self._rate_limited = False
        self._cancel = asyncio.Event()
        self._delay = DEFAULT_DELAY_SECONDS
        # When the site was last asked for anything. The throttle is a rate
        # limit measured from here, not a fixed sleep before each action.
        self._last_request = 0.0
        self._request_count = 0
        self._pending_since = 0.0
        # Multiplier the site can raise on us. The configured interval is a
        # floor we promise to respect; this is what happens when the board
        # tells us, by getting slow or erroring, that it wants more room.
        self._penalty = 1.0

    async def _program_control(self):
        """Read the visible My Program toggle, including its material icon.

        The board uses `My Program toggle_off/on`; accessible switches and
        checkboxes are also supported. Never infer enabled from result rows.
        """
        name = re.compile(r"^(?:For\s+)?My Program(?:\s|$)", re.I)
        for role in ("button", "switch", "checkbox"):
            controls = self._page.get_by_role(role, name=name)
            for index in range(await controls.count()):
                control = controls.nth(index)
                if not await control.is_visible():
                    continue
                state = await control.evaluate("""el => {
                    if (el.matches('input[type=checkbox]')) return el.checked;
                    const input = el.querySelector('input[type=checkbox]');
                    if (input) return input.checked;
                    for (const attr of ['aria-checked', 'aria-pressed']) {
                        if (el.getAttribute(attr) === 'true') return true;
                        if (el.getAttribute(attr) === 'false') return false;
                    }
                    const text = el.textContent || '';
                    if (/\\btoggle_on\\b/.test(text)) return true;
                    if (/\\btoggle_off\\b/.test(text)) return false;
                    return null;
                }""")
                return control, state
        return None, None

    async def _ensure_my_program(self) -> None:
        clicked = False
        for wait in self._poll_intervals():
            await self._content()
            control, enabled = await self._program_control()
            if enabled is True:
                return
            if enabled is False and not clicked:
                await self._throttle()
                await control.click()
                clicked = True
                # Let the filter request replace the previous rows/empty state.
                await self._throttle()
            else:
                await self._settle(wait)
        raise CrawlStopped("failed", "未能确认 My Program 已开启。请在采集浏览器进入 Co-op 职位列表，打开 My Program（toggle_on），再重试；入口页的空结果不会算作采集完成。")

    @property
    def is_open(self) -> bool:
        """Whether the dedicated login browser is up and usable."""
        return self._context is not None

    async def open_posting(self, url: str) -> str:
        """Show one posting in the already-logged-in browser, in a new tab.

        A new tab rather than the current one: the listing page holds the
        user's filters and, during a crawl, the crawler's place in them.

        This opens the application page. It does not apply. Submitting on
        someone's behalf would pick a resume package, skip employer questions
        and be irreversible — and bulk automated applying is exactly what gets
        a student's account flagged. The same reason `_close_modal` refuses to
        touch an Apply button.
        """
        destination = safe_job_url(url)
        if destination is None:
            raise ValueError("Refused a job URL that is not a read-only WaterlooWorks posting.")
        if self._context is None:
            raise RuntimeError("The login browser is not open.")
        page = await self._context.new_page()
        await page.goto(destination, wait_until="domcontentloaded", timeout=45000)
        await page.bring_to_front()
        return destination

    async def open_browser(self) -> None:
        """Open a dedicated persistent profile for the user's manual login.

        SWW_BROWSER_CHANNEL may select installed Chrome or Edge. If bundled
        Chromium is missing, try installed Chrome. Every launch still uses
        data_dir/browser; no existing Chrome/Edge profile or cookies are read.
        """
        if self._context is not None:
            try:
                if self._page is None or self._page.is_closed():
                    self._page = await self._context.new_page()
                    await self._page.goto(DASHBOARD_URL, wait_until="domcontentloaded")
                await self._page.bring_to_front()
                return
            except Exception:
                await self.close()
        from playwright.async_api import async_playwright

        channel = os.environ.get("SWW_BROWSER_CHANNEL", "").strip().lower()
        if channel not in {"", "chrome", "msedge"}:
            raise ValueError("SWW_BROWSER_CHANNEL must be chrome or msedge, or unset for bundled Chromium.")
        profile = self.data_dir / "browser"
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        profile.chmod(0o700)
        self._playwright = await async_playwright().start()
        try:
            launch_options = {
                "user_data_dir": str(profile.resolve()), "headless": False,
                "accept_downloads": False, "viewport": {"width": 1440, "height": 1000},
            }
            if channel:
                launch_options["channel"] = channel
            try:
                self._context = await self._playwright.chromium.launch_persistent_context(**launch_options)
            except Exception as exc:
                if channel or "executable doesn't exist" not in str(exc).lower():
                    raise
                self._context = await self._playwright.chromium.launch_persistent_context(**launch_options, channel="chrome")
            self._context.on("request", self._on_request)
            self._context.on("response", self._on_response)
            self._context.set_default_timeout(15000)
            self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
            await self._page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            await self.close()
            raise RuntimeError("Unable to open the crawler browser. Run: python -m playwright install chromium; then retry. Close another matcher browser using the same profile if necessary.") from None

    def _on_request(self, request) -> None:
        if request.url.startswith(ORIGIN) and request.resource_type in TRACKED_RESOURCES:
            self._last_request = self._pending_since = time.monotonic()
            self._request_count += 1

    def _on_response(self, response) -> None:
        """Watch how the board is holding up and widen the interval if it isn't.

        A rising response time is the warning that comes *before* a 429. Backing
        off on it means the crawl slows itself down while the site is under
        load, instead of discovering the limit by hitting it.
        """
        if not response.url.startswith(ORIGIN):
            return
        # Count the reply too: a slow response means the site was busy with us
        # for that whole time, which the next interval should respect.
        self._last_request = time.monotonic()
        if response.status == 429:
            self._rate_limited = True
            return
        if response.status >= 500:
            self._penalty = min(MAX_PENALTY, self._penalty * 2)
            return
        if not self._pending_since:
            return
        latency = self._last_request - self._pending_since
        self._pending_since = 0.0
        if latency > SLOW_RESPONSE_SECONDS:
            self._penalty = min(MAX_PENALTY, self._penalty * 1.5)
        elif latency < SLOW_RESPONSE_SECONDS / 2:
            # Recover slowly. Backing off fast and returning slowly is the
            # right asymmetry when the cost of being wrong is someone's account.
            self._penalty = max(1.0, self._penalty * 0.9)

    async def close(self) -> None:
        context, playwright = self._context, self._playwright
        self._context = self._page = self._playwright = None
        try:
            if context:
                await context.close()
        finally:
            if playwright:
                await playwright.stop()

    def _check_stop(self) -> None:
        if self._cancel.is_set():
            raise CrawlStopped("cancelled", "Crawl cancelled; collected jobs are retained as a partial result.")
        if self._rate_limited:
            raise CrawlStopped("failed", "WaterlooWorks returned HTTP 429. Crawl stopped; wait before retrying.")

    async def _settle(self, seconds: float) -> None:
        """Wait locally, and stop waiting the moment a cancel arrives."""
        self._check_stop()
        if seconds > 0:
            try:
                await asyncio.wait_for(self._cancel.wait(), timeout=seconds)
            except asyncio.TimeoutError:
                pass
        self._check_stop()

    async def _throttle(self) -> None:
        """Hold the request rate to at most one every `delay` seconds.

        This used to be a flat sleep before every action, which charged the
        delay *on top of* however long the previous page load took: a two second
        interval plus a two second detail load meant one request every four
        seconds, not every two. Measuring from the last request instead means
        the interval is what the site actually experiences, so the crawl is
        faster while asking for no more than it did before — and slower
        responses automatically widen the gap rather than narrowing it.
        """
        interval = self._delay * self._penalty
        remaining = interval - (time.monotonic() - self._last_request)
        if remaining <= 0:
            return
        # A little jitter: a perfectly constant interval is both more obviously
        # robotic and more likely to land in step with a fixed rate window.
        await self._settle(min(remaining * random.uniform(0.85, 1.15), interval))

    async def _pause(self, seconds: Optional[float] = None) -> None:
        """Backwards-compatible shim: no argument throttles, a number waits."""
        await (self._throttle() if seconds is None else self._settle(seconds))

    def _poll_intervals(self, budget: float = 15.0, start: float = 0.05, cap: float = 0.4):
        """Poll waits that start tight and back off, within a total budget.

        Polling reads the page over the debug protocol and never touches
        WaterlooWorks, so the old fixed 250ms granularity bought nothing and
        cost up to a quarter second after the content was already there.
        """
        spent, wait = 0.0, start
        while spent < budget:
            step = min(wait, budget - spent)
            yield step
            spent += step
            wait = min(cap, wait * 1.6)

    @staticmethod
    def _parse_listing(html: str, url: str) -> Listing:
        """Parse a result page, reusing the last parse of identical HTML.

        `_wait_listing` polls up to sixty times while a page settles, and every
        poll used to run a full BeautifulSoup parse of the entire document —
        even though the HTML is byte-identical once the page has finished
        loading. On a board page that is by far the most expensive thing the
        crawler does, and it happens on every pagination step.
        """
        key = (hash(html), len(html), url)
        if _LISTING_CACHE.get("key") == key:
            return _LISTING_CACHE["value"]
        listing = parse_listing(html, url)
        _LISTING_CACHE.update(key=key, value=listing)
        return listing

    async def _signature(self, page=None) -> tuple:
        """A cheap in-page fingerprint of the result list.

        Pulling `page.content()` serialises the whole DOM across the wire. While
        waiting for a page to settle, ask the page itself for a few numbers
        instead, and only fetch the real HTML once those stop changing.
        """
        page = page or self._page
        if page is None or page.is_closed():
            raise CrawlStopped("failed", "Crawler browser was closed. Open it again and retry.")
        try:
            return tuple(await page.evaluate("""() => {
                const rows = document.querySelectorAll('table tr, [data-job-id], [data-posting-id]');
                const first = rows[0] ? (rows[0].textContent || '').slice(0, 120) : '';
                const last = rows.length ? (rows[rows.length - 1].textContent || '').slice(0, 120) : '';
                return [rows.length, first, last,
                        document.querySelectorAll('[aria-busy="true"]').length,
                        (document.body ? document.body.textContent.length : 0)];
            }"""))
        except Exception:
            # Any evaluation failure just means falling back to the full read.
            return ()

    async def _content(self, page=None) -> str:
        self._check_stop()
        page = page or self._page
        if page is None or page.is_closed():
            raise CrawlStopped("failed", "Crawler browser was closed. Open it again and retry.")
        html = await page.content()
        # `login_required` parses the document too, so an unchanged page would
        # otherwise be parsed twice per poll — once here, once for the listing.
        key = (hash(html), len(html), page.url)
        if _LOGIN_CACHE.get("key") != key:
            _LOGIN_CACHE.update(key=key, value=login_required(html, page.url))
        if _LOGIN_CACHE["value"]:
            raise CrawlStopped("login_required", "Log in manually in the crawler browser, complete Duo if requested, open Co-op Full-Cycle jobs, then start the crawl again.")
        lowered = html.lower()
        if "too many requests" in lowered or "rate limit exceeded" in lowered:
            raise CrawlStopped("failed", "WaterlooWorks rate limit detected. Crawl stopped; wait before retrying.")
        return html

    async def _goto(self, page, url: str) -> None:
        self._check_stop()
        if safe_job_url(url) is None:
            raise CrawlStopped("failed", "Refused an unrecognized job navigation URL.")
        response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if response is not None and response.status >= 400:
            state = "login_required" if response.status in (401, 403) else "failed"
            raise CrawlStopped(state, f"WaterlooWorks returned HTTP {response.status}; crawl stopped.")

    async def _wait_listing(self, previous_ids: Optional[tuple] = None) -> Listing:
        stable, prior = 0, None
        fingerprint = None
        first = True
        for wait in self._poll_intervals():
            # Ask the page for a few numbers before pulling and parsing its
            # whole DOM: while the fingerprint is still moving the page is
            # mid-update and any parse of it would be thrown away.
            current = await self._signature()
            if not first and current and current != fingerprint:
                fingerprint = current
                await self._settle(wait)
                continue
            fingerprint, first = current, False
            last = self._parse_listing(await self._content(), self._page.url)
            ids = tuple(job["id"] for job in last.jobs)
            signature = (ids, last.expected_total, last.current_page, last.empty)
            busy = await self._page.locator('[aria-busy="true"]:visible').count()
            ready = not busy and (last.empty or (last.jobs and (previous_ids is None or ids != previous_ids)))
            stable = stable + 1 if ready and signature == prior else 0
            prior = signature
            if stable >= (7 if last.empty else 2):
                _, enabled = await self._program_control()
                if enabled is not True:
                    raise CrawlStopped("failed", "My Program 筛选已关闭或无法确认，已停止采集，避免混入其他专业的岗位。")
                return last
            await self._settle(wait)
        if previous_ids is not None:
            raise CrawlStopped("failed", "Pagination did not produce a new result page. Partial results were retained.")
        raise CrawlStopped("failed", "No recognizable job results appeared. Open the Full-Cycle board and use table view; the live page layout may require a parser update.")

    async def _navigate(self, target: Target, previous_ids: Optional[tuple] = None) -> Listing:
        await self._throttle()
        # Click the real control: navigating its href can discard JS filters.
        await self._page.locator(target.selector).click()
        return await self._wait_listing(previous_ids)

    async def _start_listing(self) -> Listing:
        # Preserve the user's open board and its filters, including a board
        # opened in a second tab of the dedicated login browser.
        original_page = self._page
        for page in [self._page] + list(reversed(self._context.pages)):
            if page is not None and not page.is_closed() and safe_job_url(page.url):
                self._page = page
                control, _ = await self._program_control()
                if control is not None:
                    break
        else:
            self._page = original_page
        if not safe_job_url(self._page.url):
            await self._goto(self._page, JOBS_URL)
        await self._close_modal()
        await self._ensure_my_program()
        listing = await self._wait_listing()
        visited = set()
        while not listing.start_confirmed and (listing.current_page or listing.previous_page or listing.first_page):
            ids = tuple(job["id"] for job in listing.jobs)
            if ids in visited or len(visited) >= 1000:
                raise CrawlStopped("failed", "返回首页时分页重复或超过范围，已停止。请手动回到第一页后重试。")
            visited.add(ids)
            target = listing.first_page or listing.previous_page
            if target is None:
                raise CrawlStopped("failed", "当前不在第一页，且无法识别首页或上一页控件。请在采集浏览器手动回到第一页后重试，已有缓存仍保留。")
            old_page = listing.current_page
            listing = await self._navigate(target, ids)
            if old_page and listing.current_page and listing.current_page >= old_page:
                raise CrawlStopped("failed", "返回首页时页码未减小，已停止。请手动回到第一页后重试。")
        return listing

    async def _close_modal(self) -> None:
        modal = self._page.locator(".modal.is--visible:visible, [role='dialog']:visible")
        if await modal.count():
            await self._page.keyboard.press("Escape")
            for wait in self._poll_intervals(budget=3.0):
                if not await modal.count():
                    return
                await self._settle(wait)
            # Recognized close controls only; never click Apply or form submit.
            close = modal.locator("button[aria-label='Close'], button.modal__close, .modal__close, button[data-dismiss='modal']")
            if await close.count() == 1:
                await close.click()
                if not await modal.count():
                    return
            raise CrawlStopped("failed", "The job preview did not close; close it manually before retrying.")

    async def _read_detail(self, target: Target, job_id: str) -> Optional[dict]:
        await self._throttle()
        detail_page = None
        before_pages = set(self._context.pages)
        before_url = self._page.url
        before_ids = tuple(job["id"] for job in self._parse_listing(await self._content(), before_url).jobs)
        try:
            await self._page.locator(target.selector).click()
            for wait in self._poll_intervals():
                if detail_page is None:
                    popups = [p for p in self._context.pages if p not in before_pages]
                    if popups:
                        detail_page = popups[0]
                        if detail_page.url == "about:blank":
                            await self._settle(wait)
                            continue
                        if safe_job_url(detail_page.url) is None:
                            raise CrawlStopped("failed", "Posting opened an unrecognized destination; crawl stopped.")
                html = await self._content(detail_page or self._page)
                detail = parse_detail(html, job_id)
                if detail:
                    # When the posting opened at an address of its own — a
                    # popup, a new tab, or a same-tab navigation — that address
                    # is a real deep link, observed rather than guessed. Most
                    # postings open in a modal and have none, which is why the
                    # listing falls back to a board URL with a fragment.
                    source = (detail_page or self._page).url
                    if source != before_url and safe_job_url(source):
                        detail = {**detail, "url": safe_job_url(source)}
                    return detail
                await self._settle(wait)
            return None
        finally:
            for popup in list(self._context.pages):
                if popup not in before_pages:
                    await popup.close()
            if not self._page.is_closed():
                await self._close_modal()
                # A title may open a full page instead of a modal or popup.
                # Return through browser history so the filtered list survives.
                current = self._parse_listing(await self._content(), self._page.url)
                if self._page.url != before_url or not current.jobs:
                    await self._throttle()
                    await self._page.go_back(wait_until="domcontentloaded", timeout=45000)
                restored = await self._wait_listing()
                if tuple(job["id"] for job in restored.jobs) != before_ids:
                    raise CrawlStopped("failed", "返回列表后岗位顺序发生变化，已保留部分结果；请重新采集，避免详情对应到错误岗位。")

    async def _collect_detail(self, target, job, validate_job, on_retry):
        """Read up to three times; a bad posting never stops other postings."""
        issue = ""
        for attempt in range(3):
            self._check_stop()
            if attempt:
                await on_retry(f"岗位 {job['id']} 详情读取或校验未通过，正在自动重试（{attempt}/2）；其余进度已保存。")
            try:
                detail = await self._read_detail(target, job["id"])
                if not detail:
                    issue = f"岗位 {job['id']} 的详情未加载或未能识别。"
                    continue
                metadata = {**job["metadata"], **detail.get("metadata", {})}
                metadata["detail_collected_at"] = datetime.now(timezone.utc).isoformat()
                metadata.pop("detail_error", None)
                candidate = {**job, **{key: value for key, value in detail.items() if value}, "metadata": metadata}
                return (validate_job(candidate) if validate_job else candidate), ""
            except CrawlStopped:
                # Cancellation, expired login, rate limiting or lost list
                # identity require stopping, not repeatedly clicking the site.
                raise
            except ValidationError as exc:
                issue = validation_message(exc, job["id"])
                invalid_fields = {tuple(error["loc"]) for error in exc.errors(include_input=False)}
                if attempt == 2 and invalid_fields == {("location",)} and job.get("location"):
                    # The listing location is already validated. An overlong
                    # free-form Location section must not discard good detail.
                    candidate["location"] = job["location"]
                    candidate["metadata"]["location_source"] = "listing"
                    return validate_job(candidate), f"岗位 {job['id']} 的详情地点解析异常；已自动重试 2 次并使用列表地点，其余详情已保存，继续采集。"
            except Exception as exc:
                self._check_stop()
                issue = f"岗位 {job['id']} 详情读取失败（{type(exc).__name__}）。"
        return None, f"{issue}已自动重试 2 次，仍未成功；已跳过该详情并继续采集后续岗位。"

    async def crawl(
        self, *, max_pages: int = 100, max_jobs: int = 3000,
        delay_seconds: float = 2, include_details: bool = True,
        on_progress: Optional[Progress] = None, cancel: Optional[asyncio.Event] = None,
        cached_jobs: Optional[list[dict]] = None,
        on_checkpoint: Optional[Callable[[list[dict]], Awaitable[None]]] = None,
        validate_job: Optional[Callable[[dict], dict]] = None,
    ) -> list[dict]:
        if self._busy:
            raise RuntimeError("A crawl is already running.")
        if not 1 <= max_pages <= 1000 or not 1 <= max_jobs <= 20000:
            raise ValueError("max_pages must be 1–1000 and max_jobs must be 1–20000.")
        self._penalty = 1.0
        if not math.isfinite(float(delay_seconds)):
            raise ValueError("delay_seconds must be finite.")
        self._busy = True
        self._cancel = cancel if cancel is not None else asyncio.Event()
        self._delay = max(MIN_DELAY_SECONDS, float(delay_seconds))
        self._rate_limited = False
        jobs, snapshots, warnings, errors = {}, [], [], []
        list_complete = False
        expected_total = None
        cached = reusable_details(cached_jobs or [])
        reused = 0
        rejected_ids = set()

        async def emit(state: str, message: str, complete: bool = False) -> None:
            if on_progress:
                await on_progress({
                    "state": state, "message": message, "pages": len(snapshots),
                    "job_count": len(jobs), "errors": errors[:],
                    "warnings": list(dict.fromkeys(warnings)), "complete": complete,
                    "expected_total": expected_total,
                    "reused_details": reused,
                })
            if on_checkpoint and jobs:
                await on_checkpoint(list(jobs.values()))

        try:
            await emit("running", f"正在检查职位列表；一天内已完成的 {len(cached)} 条详情可复用，仅补抓剩余详情。")
            await self.open_browser()
            listing = await self._start_listing()
            await emit("running", "My Program 已开启，正在遍历本专业岗位列表。其他已有筛选条件会保留。")
            expected_total = listing.expected_total
            fingerprints = set()
            while True:
                self._check_stop()
                fingerprint = tuple(job["id"] for job in listing.jobs)
                if fingerprint in fingerprints:
                    raise CrawlStopped("failed", "Repeated result page detected; crawl stopped to avoid a pagination loop.")
                fingerprints.add(fingerprint)
                if listing.empty:
                    list_complete = not jobs
                    if jobs:
                        warnings.append("The result set changed while crawling; some pages may be missing.")
                    break
                if listing.expected_total is not None and expected_total != listing.expected_total:
                    warnings.append("The result count changed during the crawl; run again for a consistent snapshot.")
                page_ids = []
                for job in listing.jobs:
                    if job["id"] in jobs:
                        continue
                    if len(jobs) >= max_jobs:
                        break
                    if validate_job:
                        try:
                            job = validate_job(job)
                        except ValidationError as exc:
                            rejected_ids.add(job["id"])
                            warnings.append(validation_message(exc, job["id"]))
                            continue
                    jobs[job["id"]] = job
                    previous = cached.get(job["id"])
                    if include_details and previous and reusable_details([previous]):
                        # Only reuse IDs present on the current filtered board.
                        # Refresh listing fields while retaining detail-only data.
                        jobs[job["id"]] = {**previous, **{k: v for k, v in job.items() if v not in ("", None, {})},
                            "metadata": {**previous["metadata"], **job["metadata"],
                                         "detail_status": "complete",
                                         "detail_collected_at": previous["metadata"]["detail_collected_at"]}}
                        reused += 1
                    page_ids.append(job["id"])
                snapshots.append({"ids": fingerprint, "selected": page_ids})
                warnings.extend(listing.warnings)
                await emit("running", f"Collected page {len(snapshots)}: {len(jobs)} unique jobs. 已复用 {reused} 条详情，正在补抓剩余岗位。")
                if include_details:
                    for job_id in page_ids:
                        self._check_stop()
                        if jobs[job_id]["metadata"].get("detail_status") == "complete":
                            continue
                        target = listing.targets.get(job_id)
                        if not target:
                            warnings.append(f"Job {job_id}: detail navigation unavailable.")
                            continue
                        async def retry_progress(message):
                            await emit("running", message)
                        candidate, issue = await self._collect_detail(target, jobs[job_id], validate_job, retry_progress)
                        if candidate:
                            jobs[job_id] = candidate
                        else:
                            jobs[job_id]["metadata"]["detail_status"] = "failed"
                            jobs[job_id]["metadata"]["detail_error"] = issue
                        if issue:
                            warnings.append(issue)
                        completed = sum(j["metadata"].get("detail_status") == "complete" for j in jobs.values())
                        await emit("running", f"Read {completed}/{expected_total if expected_total is not None else len(jobs)} job descriptions. 已复用 {reused} 条详情。")
                else:
                    warnings.append("Job descriptions were not requested; ranking has only listing fields.")
                    for job_id in page_ids:
                        jobs[job_id]["metadata"]["detail_status"] = "skipped"
                if any(job["id"] not in jobs and job["id"] not in rejected_ids for job in listing.jobs):
                    warnings.append("Configured job limit reached inside a page; increase the limit to collect the remaining jobs.")
                    break
                if listing.next_page is None:
                    list_complete = listing.end_confirmed or (expected_total is not None and len(jobs) >= expected_total)
                    if not list_complete:
                        warnings.append("No next-page control was recognized and the total could not be verified; results may be incomplete.")
                    break
                if len(snapshots) >= max_pages or len(jobs) >= max_jobs:
                    warnings.append("Configured page/job limit reached; increase the limits to collect the remaining jobs.")
                    break
                listing = await self._navigate(listing.next_page, fingerprint)
            if expected_total is not None and len(jobs) != expected_total:
                list_complete = False
                warnings.append(f"Board reported {expected_total} jobs; collected {len(jobs)} unique jobs.")

            details_complete = all(j["metadata"].get("detail_status") == "complete" for j in jobs.values()) if include_details else True
            complete = list_complete and details_complete and not warnings
            await emit("completed", "Crawl completed." if complete else "Crawl finished with partial or unverified results; review the warnings.", complete)
        except CrawlStopped as exc:
            if exc.state == "failed":
                errors.append(str(exc))
            await emit(exc.state, str(exc))
        except asyncio.CancelledError:
            await emit("cancelled", "Crawl cancelled; collected jobs are retained as a partial result.")
        except ValidationError as exc:
            errors.append(validation_message(exc))
            await emit("failed", errors[-1])
        except Exception as exc:
            errors.append(f"采集处理失败（{type(exc).__name__}）。已保存的进度可在重试时继续；请检查具体错误或本地服务日志。")
            await emit("failed", errors[-1])
        finally:
            self._busy = False
        return list(jobs.values())
