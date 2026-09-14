"""Loopback-only API, in-memory resume, and a single user-controlled browser."""
import asyncio
import csv
import io
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .crawler import WaterlooWorksCrawler
from .ranking import rank_jobs
from .resume import extract_resume

ROOT = Path(__file__).resolve().parents[2]
MAX_BODY = 12 * 1024 * 1024
MAX_PDF = 10 * 1024 * 1024
ALLOWED_HOSTS = {"127.0.0.1:8765", "localhost:8765", "127.0.0.1:5173", "localhost:5173"}
ALLOWED_ORIGINS = {"http://" + host for host in ALLOWED_HOSTS}


class BodyTooLarge(Exception):
    pass


class LocalOnlyMiddleware:
    """Reject drive-by writes and DNS rebinding; cap streamed request bodies too."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        host = headers.get(b"host", b"").decode().lower()
        origin = headers.get(b"origin", b"").decode()
        mutation = scope["method"] not in {"GET", "HEAD", "OPTIONS"}
        if host not in ALLOWED_HOSTS or (origin and origin not in ALLOWED_ORIGINS):
            return await JSONResponse({"detail": "Only the local SWW page can access this service."}, 403)(scope, receive, send)
        if mutation and headers.get(b"x-sww-client") != b"1":
            return await JSONResponse({"detail": "Missing X-SWW-Client header."}, 403)(scope, receive, send)
        try:
            if int(headers.get(b"content-length", b"0")) > MAX_BODY:
                raise BodyTooLarge
        except (ValueError, BodyTooLarge):
            return await JSONResponse({"detail": "Request exceeds 12 MiB."}, 413)(scope, receive, send)
        size = 0
        too_large = False
        sent_error = False

        async def limited_receive():
            nonlocal size, too_large
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > MAX_BODY:
                    too_large = True
                    raise BodyTooLarge
            return message

        async def guarded_send(message):
            nonlocal sent_error
            if too_large:
                if not sent_error:
                    sent_error = True
                    await JSONResponse({"detail": "Request exceeds 12 MiB."}, 413)(scope, receive, send)
                return
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend([
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                ])
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except BodyTooLarge:
            if not sent_error:
                await JSONResponse({"detail": "Request exceeds 12 MiB."}, 413)(scope, receive, send)


class CrawlOptions(BaseModel):
    max_pages: int = Field(default=100, ge=1, le=1000)
    max_jobs: int = Field(default=3000, ge=1, le=10000)
    delay_seconds: float = Field(default=2, ge=2, le=60)
    include_details: bool = True


class Preferences(BaseModel):
    target_roles: list[str] = Field(default_factory=list, max_length=20)
    locations: list[str] = Field(default_factory=list, max_length=20)
    exclude_keywords: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("target_roles", "locations", "exclude_keywords")
    @classmethod
    def clean_terms(cls, terms):
        if any(len(term) > 200 for term in terms):
            raise ValueError("Each preference must be at most 200 characters.")
        return [term.strip() for term in terms if term.strip()]


class RankOptions(BaseModel):
    limit: int = Field(default=100, ge=1, le=100)
    preferences: Preferences = Field(default_factory=Preferences)


class Job(BaseModel):
    id: str = Field(default="", max_length=200)
    title: str = Field(min_length=1, max_length=1000)
    company: str = Field(default="", max_length=1000)
    location: str = Field(default="", max_length=1000)
    description: str = Field(default="", max_length=100000)
    requirements: str = Field(default="", max_length=50000)
    deadline: str = Field(default="", max_length=200)
    url: str = Field(default="", max_length=4000)
    status: str = Field(default="", max_length=200)
    is_open: Optional[bool] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def valid_job_url(cls, value):
        if value:
            parts = urlsplit(value)
            if (parts.scheme != "https" or parts.hostname != "waterlooworks.uwaterloo.ca"
                    or parts.username or parts.password or parts.port not in (None, 443)):
                raise ValueError("Job URL must be an HTTPS WaterlooWorks URL.")
        return value


class ImportJobs(BaseModel):
    jobs: list[Job] = Field(min_length=1, max_length=10000)


def initial_crawl():
    return {"state": "idle", "message": "请先上传简历，再登录 WaterlooWorks 并采集职位。",
            "job_count": 0, "pages": 0, "errors": [], "warnings": [], "complete": False}


def write_private_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
    temporary.replace(path)
    path.chmod(0o600)


def csv_safe(value):
    if isinstance(value, (list, dict)):
        value = " | ".join(map(str, value)) if isinstance(value, list) else json.dumps(value, ensure_ascii=False)
    text = str(value if value is not None else "")
    # Job text is untrusted, including when subsequently opened in Excel.
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r")) else text


def create_app(data_dir: Optional[Path] = None, crawler=None):
    directory = data_dir or ROOT / ".sww"
    state = {"resume": None, "jobs": [], "ranking": None, "crawl": initial_crawl(),
             "task": None, "cancel": asyncio.Event(), "browser_lock": asyncio.Lock(),
             "source": None, "collected_at": None}
    browser = crawler if crawler is not None else WaterlooWorksCrawler(directory)

    @asynccontextmanager
    async def lifespan(_app):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        cache = directory / "jobs.json"
        if cache.exists():
            try:
                saved = json.loads(cache.read_text(encoding="utf-8"))
                validated = ImportJobs(jobs=saved["jobs"])
                state["jobs"] = [job.model_dump() for job in validated.jobs]
                state["source"] = saved.get("source", "cache")
                state["collected_at"] = saved.get("collected_at")
                state["crawl"] = {**initial_crawl(), **saved.get("crawl", {}),
                                  "state": "idle", "job_count": len(state["jobs"]),
                                  "message": "已加载本机职位缓存；请核对采集时间，必要时重新采集。"}
            except (ValueError, KeyError, TypeError):
                state["crawl"]["warnings"] = ["本机职位缓存无法读取，请重新采集。"]
        yield
        state["cancel"].set()
        if state["task"] and not state["task"].done():
            state["task"].cancel()
            await asyncio.gather(state["task"], return_exceptions=True)
        await browser.close()

    app = FastAPI(title="SWW local matcher", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(LocalOnlyMiddleware)
    app.state.matcher = state

    def busy():
        return state["task"] is not None and not state["task"].done()

    def save_jobs():
        write_private_json(directory / "jobs.json", {key: state[key] for key in
                           ("jobs", "crawl", "source", "collected_at")})

    @app.get("/matcher-api/status")
    async def status():
        resume = state["resume"]
        return {"crawl": state["crawl"], "job_count": len(state["jobs"]), "has_resume": resume is not None,
                "resume": {key: resume[key] for key in ("filename", "skills", "warnings")} if resume else None,
                "source": state["source"], "collected_at": state["collected_at"]}

    @app.post("/matcher-api/resume")
    async def upload_resume(file: UploadFile = File(...)):
        try:
            data = await file.read(MAX_PDF + 1)
        finally:
            await file.close()
        if len(data) > MAX_PDF:
            raise HTTPException(413, "PDF 最大为 10 MiB。")
        try:
            resume = await asyncio.to_thread(extract_resume, data)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        resume["filename"] = Path((file.filename or "resume.pdf").replace("\\", "/")).name[:255]
        state["resume"], state["ranking"] = resume, None
        return {key: resume[key] for key in ("filename", "skills", "warnings")}

    @app.post("/matcher-api/browser")
    async def open_browser():
        if busy() or state["browser_lock"].locked():
            raise HTTPException(409, "浏览器正在工作，请等待当前操作完成。")
        async with state["browser_lock"]:
            try:
                await browser.open_browser()
            except Exception as exc:
                raise HTTPException(503, "无法打开采集浏览器。请运行 .venv/bin/python -m playwright install chromium 后重试。") from exc
        state["crawl"].update(state="login_required", message="请在打开的浏览器完成学校登录和双重验证，然后返回点击开始采集。")
        return {"message": state["crawl"]["message"]}

    async def run_crawl(options):
        async def progress(update):
            state["crawl"].update(update)
        try:
            async with state["browser_lock"]:
                jobs = await browser.crawl(**options.model_dump(), on_progress=progress, cancel=state["cancel"])
            # Never replace a useful cache with a login failure or empty parser result.
            if jobs:
                state["jobs"] = [Job(**job).model_dump() for job in jobs]
                state["ranking"] = None
                state["source"] = "waterlooworks"
                state["collected_at"] = datetime.now(timezone.utc).isoformat()
                save_jobs()
            elif state["crawl"].get("complete"):
                state["jobs"], state["ranking"] = [], None
                state["source"] = "waterlooworks"
                state["collected_at"] = datetime.now(timezone.utc).isoformat()
                save_jobs()
            elif state["jobs"]:
                state["crawl"].setdefault("warnings", []).append("本次未获得职位；保留上次缓存，排名将使用上次采集的数据。")
        except asyncio.CancelledError:
            state["crawl"].update(state="cancelled", complete=False, message="采集已停止。")
            raise
        except Exception:
            state["crawl"].update(state="failed", complete=False, message="采集失败。请确认浏览器仍打开、登录有效且职位列表可见后重试。")

    @app.post("/matcher-api/crawl")
    async def start_crawl(options: CrawlOptions):
        if busy() or state["browser_lock"].locked():
            raise HTTPException(409, "已有采集或浏览器操作正在进行。")
        state["cancel"] = asyncio.Event()
        state["ranking"] = None
        state["crawl"] = {**initial_crawl(), "state": "running", "message": "正在检查登录并采集职位…"}
        state["task"] = asyncio.create_task(run_crawl(options))
        return {"message": "采集已开始。"}

    @app.post("/matcher-api/crawl/cancel")
    async def cancel_crawl():
        state["cancel"].set()
        return {"message": "已请求停止；当前页面操作完成后会保存已采集的职位。"}

    @app.post("/matcher-api/jobs/import")
    async def import_jobs(payload: ImportJobs):
        if busy():
            raise HTTPException(409, "请先停止当前采集。")
        state["jobs"] = [job.model_dump() for job in payload.jobs]
        state["ranking"] = None
        state["source"] = "import"
        state["collected_at"] = datetime.now(timezone.utc).isoformat()
        state["crawl"] = {**initial_crawl(), "job_count": len(state["jobs"]),
                          "message": "已导入职位；导入数据不代表完整的 WaterlooWorks 职位列表。",
                          "warnings": ["导入数据的完整性、采集时间和可申请状态需要自行核对。"]}
        save_jobs()
        return {"job_count": len(state["jobs"])}

    @app.post("/matcher-api/rank")
    async def rank(options: RankOptions):
        if busy():
            raise HTTPException(409, "请等待采集结束或停止采集后再排序。")
        if state["resume"] is None:
            raise HTTPException(409, "请先上传 PDF 简历。")
        if not state["jobs"]:
            raise HTTPException(409, "请先采集或导入职位。")
        result = rank_jobs(state["resume"]["text"], state["jobs"], options.limit, options.preferences.model_dump())
        result.update(source=state["source"], collected_at=state["collected_at"], crawl=state["crawl"])
        state["ranking"] = result
        return result

    @app.get("/matcher-api/export.csv")
    async def export_csv():
        if state["ranking"] is None:
            raise HTTPException(409, "请先生成排序。")
        fields = ["rank", "score", "id", "title", "company", "location", "deadline", "url",
                  "matched_skills", "missing_skills", "reasons", "warnings", "score_breakdown"]
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for job in state["ranking"]["jobs"]:
            writer.writerow({key: csv_safe(job.get(key, "")) for key in fields})
        return Response("\ufeff" + stream.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="waterlooworks-top100.csv"'})

    dist = ROOT / "frontend" / "dist"

    @app.get("/")
    @app.get("/waterlooworks")
    async def matcher_page():
        page = dist / "waterlooworks.html"
        if not page.exists():
            return JSONResponse({"detail": "Run npm --prefix frontend ci && npm --prefix frontend run build first."}, 503)
        return FileResponse(page)

    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    return app


app = create_app()
