"""The local service: loopback-only HTTP over one browser, one resume, one database."""
from __future__ import annotations

import asyncio
import csv
import io
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .. import config
from ..embedding import available as embedding_available, load_embedder
from ..rerank import available as rerank_available, load_reranker
from ..jobs.crawler import WaterlooWorksCrawler, validation_message
from ..llm import DeepSeekClient, DeepSeekError, Extractor, load_api_key, model_name
from ..match import filters
from ..match.pipeline import SemanticRanker, rank_local
from ..resume import build_profile, extract_resume
from ..store import Store
from .middleware import LocalOnlyMiddleware
from .schemas import AIRankOptions, CrawlOptions, ImportJobs, Job, RankOptions

CSV_FIELDS = ["rank", "score", "id", "title", "company", "location", "deadline", "url",
              "eligibility", "must_have_coverage", "must_have_count", "evidence_coverage",
              "retrieval_score", "matched_skills", "missing_skills", "reasons", "warnings",
              "score_breakdown", "ai_status", "criteria", "pairwise_reviews"]


def initial_crawl() -> dict:
    return {"state": "idle", "message": "请先上传简历，再登录 WaterlooWorks 并采集职位。",
            "job_count": 0, "pages": 0, "errors": [], "warnings": [], "complete": False}


def csv_safe(value) -> str:
    if isinstance(value, (list, dict)):
        value = (" | ".join(value) if isinstance(value, list) and all(isinstance(item, str) for item in value)
                 else json.dumps(value, ensure_ascii=False))
    text = str(value if value is not None else "")
    # Job text is untrusted, including when it is later opened in Excel.
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r")) else text


def create_app(data_dir: Optional[Path] = None, crawler=None, ai_factory=None,
               embedder=..., reranker=...):
    directory = Path(data_dir) if data_dir is not None else config.DATA_DIR
    store = Store(directory)
    state = {
        "resume": None, "ranking": None, "crawl": initial_crawl(),
        "task": None, "cancel": asyncio.Event(), "browser_lock": asyncio.Lock(),
        "source": store.get_meta("source"), "collected_at": store.get_meta("collected_at"),
        "ai_task": None, "embedder": None, "reranker": None,
        "ai": {"state": "idle", "completed": 0, "total": 0, "message": "", "run_id": 0},
    }
    browser = crawler if crawler is not None else WaterlooWorksCrawler(directory)

    def get_embedder():
        """Loaded once, on first use: importing the runtime costs a second or
        two and most requests never need it."""
        if embedder is not ...:
            return embedder
        if state["embedder"] is None:
            state["embedder"] = load_embedder(store) or False
        return state["embedder"] or None

    def get_reranker():
        if reranker is not ...:
            return reranker
        if state["reranker"] is None:
            state["reranker"] = load_reranker() or False
        return state["reranker"] or None

    @asynccontextmanager
    async def lifespan(_app):
        if store.job_count():
            state["crawl"] = {**initial_crawl(), "job_count": store.job_count(),
                              "message": "已加载本机职位缓存；请核对采集时间，必要时重新采集。"}
        yield
        state["cancel"].set()
        for name in ("task", "ai_task"):
            task = state[name]
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await browser.close()
        store.close()

    app = FastAPI(title="SWW local matcher", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(LocalOnlyMiddleware)
    app.state.matcher = state
    app.state.store = store

    def busy() -> bool:
        return any(state[name] is not None and not state[name].done() for name in ("task", "ai_task"))

    def ai_config() -> dict:
        try:
            if ai_factory is None:
                load_api_key()
            return {"configured": True, "model": model_name(), "message": ""}
        except DeepSeekError as exc:
            return {"configured": False, "model": "", "message": str(exc)}

    def make_extractor(client) -> Extractor:
        from ..match.assess import VERSION as MATCH_VERSION
        return Extractor(client, store, MATCH_VERSION)

    def resume_summary() -> Optional[dict]:
        analysis = state["resume"]
        if analysis is None:
            return None
        profile = analysis.profile
        return {
            "filename": analysis.filename,
            "warnings": analysis.warnings,
            "source": profile.source,
            "headline": profile.headline,
            "domains": profile.domains,
            "skills": profile.demonstrated_skills,
            "listed_only_skills": profile.listed_skills,
            "experience_count": len(profile.experiences),
            "total_experience_months": profile.total_experience_months,
            "availability": profile.availability.model_dump(),
            "blocks": [{"id": block.id, "kind": block.kind, "heading": block.heading,
                        "chars": len(block.text)} for block in analysis.blocks],
            "experiences": [{"kind": item.kind, "title": item.title, "organization": item.organization,
                             "start": item.start, "end": item.end, "months": item.months,
                             "months_ago": item.months_ago, "recency": item.recency,
                             "technologies": item.technologies} for item in profile.experiences],
        }

    @app.get("/matcher-api/status")
    async def status():
        return {"crawl": state["crawl"], "job_count": store.job_count(),
                "has_resume": state["resume"] is not None, "resume": resume_summary(),
                "source": state["source"], "collected_at": state["collected_at"],
                "ai": state["ai"], "ai_config": ai_config(),
                "embeddings": {"available": embedding_available(), "model": config.EMBEDDING_MODEL},
                "rerank": {"available": rerank_available(), "model": config.RERANK_MODEL,
                           "pool": config.RERANK_POOL},
                "cache": store.cache_counts()}

    @app.post("/matcher-api/resume")
    async def upload_resume(file: UploadFile = File(...)):
        if busy():
            await file.close()
            raise HTTPException(409, "请先等待或停止当前采集/AI 评估，再更新简历。")
        try:
            data = await file.read(config.MAX_RESUME_BYTES + 1)
        finally:
            await file.close()
        if len(data) > config.MAX_RESUME_BYTES:
            raise HTTPException(413, "PDF / DOCX / MD 简历最大为 10 MiB。")
        try:
            document = await asyncio.to_thread(extract_resume, data, file.filename or "")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if busy():
            raise HTTPException(409, "处理简历期间另一个任务已开始，请等它完成后重新上传。")

        extractor = None
        if ai_factory is not None or ai_config()["configured"]:
            try:
                client = ai_factory() if ai_factory else DeepSeekClient(load_api_key(), model_name())
                extractor = make_extractor(client)
            except DeepSeekError:
                extractor = None
        try:
            analysis = await build_profile(document.text, extractor)
        except DeepSeekError as exc:
            # A model failure must not block ranking: fall back to the local
            # split and say so, rather than refusing the upload.
            analysis = await build_profile(document.text, None)
            analysis.warnings.append("模型拆分简历失败（%s），已改用本地拆分。" % exc)
        finally:
            if extractor is not None:
                await extractor.client.close()

        default_name = "resume.docx" if data.startswith(b"PK") else "resume.pdf"
        analysis.filename = Path((file.filename or default_name).replace("\\", "/")).name[:255]
        analysis.warnings = list(dict.fromkeys(document.warnings + analysis.warnings))
        state["resume"], state["ranking"] = analysis, None
        state["ai"]["state"] = "idle"
        return resume_summary()

    @app.get("/matcher-api/resume/profile")
    async def resume_profile():
        """The parsed resume, so the split can be checked before it is trusted."""
        if state["resume"] is None:
            raise HTTPException(409, "请先上传简历。")
        return {**resume_summary(), "profile": state["resume"].profile.model_dump()}

    @app.post("/matcher-api/browser")
    async def open_browser():
        if busy() or state["browser_lock"].locked():
            raise HTTPException(409, "浏览器正在工作，请等待当前操作完成。")
        async with state["browser_lock"]:
            try:
                await browser.open_browser()
            except Exception as exc:
                raise HTTPException(503, "无法打开采集浏览器。请运行 .venv/bin/python -m playwright install chromium 后重试。") from exc
        state["crawl"].update(state="login_required",
                              message="请在打开的浏览器完成学校登录和双重验证，进入 Co-op Jobs 职位列表。")
        return {"message": state["crawl"]["message"]}

    async def run_crawl(options: CrawlOptions):
        async def progress(update):
            state["crawl"].update(update)

        async def checkpoint(jobs):
            # One upsert per posting instead of rewriting the entire job file,
            # which the previous version did after every single detail read.
            validated = [Job(**job).model_dump() for job in jobs]
            await store.aupsert_jobs(validated)
            state["source"] = "waterlooworks"
            state["collected_at"] = datetime.now(timezone.utc).isoformat()
            store.set_meta("source", state["source"])
            store.set_meta("collected_at", state["collected_at"])

        try:
            reusable = await asyncio.to_thread(store.reusable_details)
            async with state["browser_lock"]:
                jobs = await browser.crawl(**options.model_dump(), on_progress=progress,
                                           cancel=state["cancel"], cached_jobs=list(reusable.values()),
                                           on_checkpoint=checkpoint,
                                           validate_job=lambda job: Job(**job).model_dump())
            if jobs:
                await checkpoint(jobs)
                if state["crawl"].get("complete"):
                    # Only a verified full sweep may retire postings that are no
                    # longer on the board; a partial crawl must not hide jobs.
                    await asyncio.to_thread(store.mark_off_board, [job["id"] for job in jobs])
                state["ranking"] = None
            elif store.job_count():
                state["crawl"].setdefault("warnings", []).append(
                    "本次未获得职位；保留上次缓存，排名将使用上次采集的数据。")
        except asyncio.CancelledError:
            state["crawl"].update(state="cancelled", complete=False, message="采集已停止。")
            raise
        except ValidationError as exc:
            issue = validation_message(exc)
            state["crawl"].update(state="failed", complete=False, message=issue)
            state["crawl"].setdefault("errors", []).append(issue)
        except Exception as exc:
            state["crawl"].update(state="failed", complete=False,
                                  message=f"采集处理失败（{type(exc).__name__}）。已有进度仍保留。")

    @app.post("/matcher-api/crawl")
    async def start_crawl(options: CrawlOptions):
        if busy() or state["browser_lock"].locked():
            raise HTTPException(409, "已有采集或浏览器操作正在进行。")
        state["cancel"] = asyncio.Event()
        state["ranking"] = None
        state["ai"]["state"] = "idle"
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
        count = await asyncio.to_thread(store.replace_jobs, [job.model_dump() for job in payload.jobs])
        state["ranking"] = None
        state["ai"]["state"] = "idle"
        state["source"] = "import"
        state["collected_at"] = datetime.now(timezone.utc).isoformat()
        store.set_meta("source", state["source"])
        store.set_meta("collected_at", state["collected_at"])
        state["crawl"] = {**initial_crawl(), "job_count": count,
                          "message": "已导入职位；导入数据不代表完整的 WaterlooWorks 职位列表。",
                          "warnings": ["导入数据的完整性、采集时间和可申请状态需要自行核对。"]}
        return {"job_count": count}

    def require_inputs():
        if busy():
            raise HTTPException(409, "请等待采集结束或停止采集后再排序。")
        if state["resume"] is None:
            raise HTTPException(409, "请先上传 PDF、DOCX 或 MD 简历。")
        if not store.job_count():
            raise HTTPException(409, "请先采集或导入职位。")

    @app.post("/matcher-api/rank")
    async def rank(options: RankOptions):
        require_inputs()
        jobs = await store.ajobs()
        result = await asyncio.to_thread(rank_local, state["resume"], jobs,
                                         options.preferences.model_dump(), options.limit,
                                         get_embedder(), get_reranker(), store)
        result.update(source=state["source"], collected_at=state["collected_at"], crawl=state["crawl"])
        state["ranking"] = result
        state["ai"]["state"] = "idle"
        return result

    async def run_ai(options: AIRankOptions, client):
        async def progress(update):
            state["ai"].update(update)
            state["ai"]["message"] = (
                "正在双向比较分数接近的岗位…" if update["stage"] == "pairwise"
                else f"已评估 {update['completed']}/{update['total']} 个职位，失败 {update['failed']} 个。")
        try:
            ranker = SemanticRanker(make_extractor(client), get_embedder(), get_reranker(),
                                    options.concurrency)
            jobs = await store.ajobs()
            result = await ranker.rank(state["resume"], jobs, options.preferences.model_dump(),
                                       options.limit, options.shortlist, options.refine_pairs, progress)
            result.update(source=state["source"], collected_at=state["collected_at"], crawl=state["crawl"])
            state["ranking"] = result
            state["ai"].update(state="completed", complete=result["complete"], usage=result["usage"],
                               message=f"语义排序完成：成功评估 {result['assessed_jobs']} 个职位。")
        except asyncio.CancelledError:
            state["ai"].update(state="cancelled", message="AI 评估已停止。已完成的评估已缓存，可重试复用。")
            raise
        except DeepSeekError as exc:
            state["ai"].update(state="failed", message=str(exc))
        except Exception:
            state["ai"].update(state="failed", message="AI 评估发生内部错误；已停止，未把本地排名冒充 AI 结果。")
        finally:
            await client.close()

    @app.post("/matcher-api/ai/rank")
    async def start_ai(options: AIRankOptions):
        require_inputs()
        jobs = await store.ajobs()
        eligible, _ = filters.eligible(jobs, options.preferences.model_dump(),
                                       profile=state["resume"].profile)
        try:
            client = ai_factory() if ai_factory else DeepSeekClient(load_api_key(), model_name())
        except DeepSeekError as exc:
            raise HTTPException(503, str(exc)) from None
        total = len(eligible) if options.shortlist <= 0 else min(len(eligible), options.shortlist)
        state["ranking"] = None
        state["ai"] = {"state": "running", "stage": "criteria", "completed": 0, "total": total,
                       "message": "正在抽取岗位要求并逐条核对简历证据…",
                       "run_id": state["ai"]["run_id"] + 1}
        state["ai_task"] = asyncio.create_task(run_ai(options, client))
        return {"message": "语义评估已开始。", "run_id": state["ai"]["run_id"],
                "shortlisted": total, "eligible": len(eligible)}

    @app.post("/matcher-api/ai/cancel")
    async def cancel_ai():
        task = state["ai_task"]
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return {"message": "已停止 AI 评估。"}

    @app.get("/matcher-api/ai/result")
    async def ai_result():
        if (state["ai"]["state"] != "completed" or state["ranking"] is None
                or state["ranking"].get("engine") != "deepseek"):
            raise HTTPException(409, "尚无已完成的 AI 排名。")
        return state["ranking"]

    @app.post("/matcher-api/cache/clear")
    async def clear_cache(kind: Optional[str] = None):
        if busy():
            raise HTTPException(409, "请先停止当前任务。")
        removed = await asyncio.to_thread(store.clear_cache, kind)
        return {"removed": removed, "cache": store.cache_counts()}

    @app.get("/matcher-api/export.csv")
    async def export_csv():
        if state["ranking"] is None:
            raise HTTPException(409, "请先生成排序。")
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for job in state["ranking"]["jobs"]:
            writer.writerow({key: csv_safe(job.get(key, "")) for key in CSV_FIELDS})
        return Response("﻿" + stream.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="waterlooworks-top100.csv"'})

    dist = config.ROOT / "frontend" / "dist"

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
