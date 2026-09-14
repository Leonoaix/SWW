"""A serial, manually authenticated WaterlooWorks browser crawler.

The first pass discovers all result pages; a second pass reads posting details.
No passwords, imported cookies, hidden APIs, or application actions are used.
Live selectors still need verification in the user's authenticated account.
"""
from __future__ import annotations

import asyncio
import math
import os
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .parser import (
    DASHBOARD_URL, JOBS_URL, Listing, Target, login_required, parse_detail,
    parse_listing, safe_job_url,
)

Progress = Callable[[dict], Awaitable[None]]


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
        self._delay = 2.0

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
            self._context.on("response", self._on_response)
            self._context.set_default_timeout(15000)
            self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
            await self._page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            await self.close()
            raise RuntimeError("Unable to open the crawler browser. Run: python -m playwright install chromium; then retry. Close another matcher browser using the same profile if necessary.") from None

    def _on_response(self, response) -> None:
        if response.status == 429 and response.url.startswith("https://waterlooworks.uwaterloo.ca/"):
            self._rate_limited = True

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

    async def _pause(self, seconds: Optional[float] = None) -> None:
        self._check_stop()
        try:
            await asyncio.wait_for(self._cancel.wait(), timeout=self._delay if seconds is None else seconds)
        except asyncio.TimeoutError:
            pass
        self._check_stop()

    async def _content(self, page=None) -> str:
        self._check_stop()
        page = page or self._page
        if page is None or page.is_closed():
            raise CrawlStopped("failed", "Crawler browser was closed. Open it again and retry.")
        html = await page.content()
        if login_required(html, page.url):
            raise CrawlStopped("login_required", "Log in manually in the crawler browser, complete Duo if requested, open Co-op Full-Cycle jobs, then start the crawl again.")
        if "too many requests" in html.lower() or "rate limit exceeded" in html.lower():
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
        last = None
        for _ in range(60):
            last = parse_listing(await self._content(), self._page.url)
            ids = tuple(job["id"] for job in last.jobs)
            if last.empty or (last.jobs and (previous_ids is None or ids != previous_ids)):
                return last
            await self._pause(0.25)
        if previous_ids is not None:
            raise CrawlStopped("failed", "Pagination did not produce a new result page. Partial results were retained.")
        raise CrawlStopped("failed", "No recognizable job results appeared. Open the Full-Cycle board and use table view; the live page layout may require a parser update.")

    async def _navigate(self, target: Target, previous_ids: Optional[tuple] = None) -> Listing:
        await self._pause()
        if target.url:
            await self._goto(self._page, target.url)
        else:
            await self._page.locator(target.selector).click()
        return await self._wait_listing(previous_ids)

    async def _start_listing(self) -> Listing:
        await self._goto(self._page, JOBS_URL)
        listing = await self._wait_listing()
        if listing.current_page and listing.current_page > 1:
            if listing.first_page is None:
                raise CrawlStopped("failed", "The results are not on page 1 and no first-page control was recognized.")
            listing = await self._navigate(listing.first_page, tuple(j["id"] for j in listing.jobs))
        return listing

    async def _close_modal(self) -> None:
        modal = self._page.locator(".modal.is--visible, [role='dialog'][aria-hidden='false']")
        if await modal.count():
            await self._page.keyboard.press("Escape")
            for _ in range(20):
                if not await modal.count():
                    return
                await self._pause(0.15)
            # Recognized close controls only; never click Apply or form submit.
            close = modal.locator("button[aria-label='Close'], button.modal__close, .modal__close")
            if await close.count() == 1:
                await close.click()
                if not await modal.count():
                    return
            raise CrawlStopped("failed", "The job preview did not close; close it manually before retrying.")

    async def _read_detail(self, target: Target, job_id: str) -> Optional[dict]:
        await self._pause()
        detail_page = None
        before_pages = set(self._context.pages)
        try:
            if target.url:
                detail_page = await self._context.new_page()
                await self._goto(detail_page, target.url)
            else:
                await self._page.locator(target.selector).click()
            for _ in range(60):
                if detail_page is None:
                    popups = [p for p in self._context.pages if p not in before_pages]
                    if popups:
                        detail_page = popups[0]
                        if detail_page.url == "about:blank":
                            await self._pause(0.25)
                            continue
                        if safe_job_url(detail_page.url) is None:
                            raise CrawlStopped("failed", "Posting opened an unrecognized destination; crawl stopped.")
                html = await self._content(detail_page or self._page)
                detail = parse_detail(html, job_id)
                if detail:
                    return detail
                await self._pause(0.25)
            return None
        finally:
            for popup in list(self._context.pages):
                if popup not in before_pages:
                    await popup.close()
            if not self._page.is_closed():
                await self._close_modal()

    async def crawl(
        self, *, max_pages: int = 100, max_jobs: int = 3000,
        delay_seconds: float = 2, include_details: bool = True,
        on_progress: Optional[Progress] = None, cancel: Optional[asyncio.Event] = None,
    ) -> list[dict]:
        if self._busy:
            raise RuntimeError("A crawl is already running.")
        if not 1 <= max_pages <= 1000 or not 1 <= max_jobs <= 20000:
            raise ValueError("max_pages must be 1–1000 and max_jobs must be 1–20000.")
        if not math.isfinite(float(delay_seconds)):
            raise ValueError("delay_seconds must be finite.")
        self._busy = True
        self._cancel = cancel if cancel is not None else asyncio.Event()
        self._delay = max(2.0, float(delay_seconds))
        self._rate_limited = False
        jobs, snapshots, warnings, errors = {}, [], [], []
        list_complete = False
        expected_total = None

        async def emit(state: str, message: str, complete: bool = False) -> None:
            if on_progress:
                await on_progress({
                    "state": state, "message": message, "pages": len(snapshots),
                    "job_count": len(jobs), "errors": errors[:],
                    "warnings": list(dict.fromkeys(warnings)), "complete": complete,
                    "expected_total": expected_total,
                })

        try:
            await emit("running", "Opening the Full-Cycle job board. Results use the board's current saved filters.")
            await self.open_browser()
            listing = await self._start_listing()
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
                    jobs[job["id"]] = job
                    page_ids.append(job["id"])
                snapshots.append({"ids": fingerprint, "selected": page_ids})
                warnings.extend(listing.warnings)
                await emit("running", f"Collected page {len(snapshots)}: {len(jobs)} unique jobs. Details will be read after pagination.")
                if any(job["id"] not in jobs for job in listing.jobs):
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

            if include_details and jobs:
                await emit("running", "Reading job descriptions serially; each posting request is at least two seconds apart.")
                await self._pause()
                listing = await self._start_listing()
                consecutive_detail_failures = 0
                for index, snapshot in enumerate(snapshots):
                    fingerprint = tuple(job["id"] for job in listing.jobs)
                    if fingerprint != snapshot["ids"]:
                        raise CrawlStopped("failed", "Job ordering changed between passes. Partial results are saved; crawl again for consistent details.")
                    for job_id in snapshot["selected"]:
                        self._check_stop()
                        target = listing.targets.get(job_id)
                        if not target:
                            warnings.append(f"Job {job_id}: detail navigation unavailable.")
                            continue
                        try:
                            detail = await self._read_detail(target, job_id)
                            if detail:
                                consecutive_detail_failures = 0
                                metadata = {**jobs[job_id]["metadata"], **detail.pop("metadata", {})}
                                jobs[job_id].update({key: value for key, value in detail.items() if value})
                                jobs[job_id]["metadata"] = metadata
                            else:
                                consecutive_detail_failures += 1
                                jobs[job_id]["metadata"]["detail_status"] = "failed"
                                warnings.append(f"Job {job_id}: expected description did not load or its layout was not recognized.")
                        except CrawlStopped:
                            raise
                        except Exception as exc:
                            self._check_stop()
                            # Do not put browser exception payloads (URLs/session
                            # arguments) into persistent progress records.
                            jobs[job_id]["metadata"]["detail_status"] = "failed"
                            consecutive_detail_failures += 1
                            warnings.append(f"Job {job_id}: detail read failed ({type(exc).__name__}).")
                        if consecutive_detail_failures >= 3:
                            raise CrawlStopped("failed", "Three consecutive posting details failed. Crawl stopped; the live detail layout may require a parser update.")
                        completed = sum(j["metadata"].get("detail_status") == "complete" for j in jobs.values())
                        await emit("running", f"Read {completed}/{len(jobs)} job descriptions.")
                    if index + 1 < len(snapshots):
                        if not listing.next_page:
                            raise CrawlStopped("failed", "The next page disappeared during the detail pass.")
                        listing = await self._navigate(listing.next_page, fingerprint)
            elif jobs:
                warnings.append("Job descriptions were not requested; ranking has only listing fields.")
                for job in jobs.values():
                    job["metadata"]["detail_status"] = "skipped"
            details_complete = all(j["metadata"].get("detail_status") == "complete" for j in jobs.values()) if include_details else True
            complete = list_complete and details_complete and not warnings
            await emit("completed", "Crawl completed." if complete else "Crawl finished with partial or unverified results; review the warnings.", complete)
        except CrawlStopped as exc:
            if exc.state == "failed":
                errors.append(str(exc))
            await emit(exc.state, str(exc))
        except asyncio.CancelledError:
            await emit("cancelled", "Crawl cancelled; collected jobs are retained as a partial result.")
        except Exception as exc:
            errors.append(f"Crawl failed ({type(exc).__name__}). Open the crawler browser and verify the jobs board is accessible before retrying.")
            await emit("failed", errors[-1])
        finally:
            self._busy = False
        return list(jobs.values())
