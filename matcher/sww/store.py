"""SQLite persistence for crawled jobs and cached model calls.

Replaces three JSON blobs (`jobs.json`, `crawl-resume.json`, and one file per
cached assessment under `ai-cache/`). The blobs were not just untidy: the crawl
checkpoint rewrote the *entire* job list after every single posting, so a 3,000
posting crawl performed 3,000 serialisations of a growing multi-megabyte file.
Here a checkpoint is one `INSERT ... ON CONFLICT DO UPDATE`.

Everything is created 0600/0700 under `.sww/`; the file holds the user's job
search and never leaves the machine.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                  TEXT PRIMARY KEY,
    title               TEXT NOT NULL DEFAULT '',
    company             TEXT NOT NULL DEFAULT '',
    location            TEXT NOT NULL DEFAULT '',
    description         TEXT NOT NULL DEFAULT '',
    requirements        TEXT NOT NULL DEFAULT '',
    deadline            TEXT NOT NULL DEFAULT '',
    url                 TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT '',
    is_open             INTEGER,
    metadata            TEXT NOT NULL DEFAULT '{}',
    detail_status       TEXT NOT NULL DEFAULT 'pending',
    detail_collected_at TEXT,
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL,
    on_board            INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS jobs_detail ON jobs(detail_status, detail_collected_at);
CREATE INDEX IF NOT EXISTS jobs_board  ON jobs(on_board);

CREATE TABLE IF NOT EXISTS llm_cache (
    key        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS llm_cache_kind ON llm_cache(kind);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

JOB_COLUMNS = ("id", "title", "company", "location", "description", "requirements",
               "deadline", "url", "status", "is_open", "metadata")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()



def detail_is_fresh(metadata: dict, now: datetime,
                    max_age_seconds: int = config.DETAIL_REUSE_SECONDS) -> bool:
    """Whether a posting's detail was read recently enough to skip re-reading.

    Age is measured from the posting's own read time, never from a shared
    "collected_at" for the whole crawl, so reusing a detail cannot extend its
    freshness. A timestamp in the future is rejected rather than trusted.
    """
    if metadata.get("detail_status") != "complete":
        return False
    stamp = metadata.get("detail_collected_at")
    try:
        read_at = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return False
    if read_at.tzinfo is None:
        # A stamp without an offset could be off by a day either way. Re-read
        # the posting rather than guess which day it was written.
        return False
    age = (now - read_at).total_seconds()
    return 0 <= age < max_age_seconds


class Store:
    """Thread-safe SQLite wrapper. Call the `a*` methods from async code."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.path = self.directory / "sww.db"
        new = not self.path.exists()
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        # WAL keeps the crawler's per-posting checkpoint from blocking a read
        # by the ranking pipeline running in the same process.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._db.commit()
        if new:
            os.chmod(self.path, 0o600)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- jobs --------------------------------------------------------------


    def _metadata_for(self, ids: set[str]) -> dict[str, dict]:
        """Stored metadata for the given postings, so an upsert can merge."""
        ids = [value for value in ids if value]
        found: dict[str, dict] = {}
        if not ids:
            return found
        with self._lock:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                placeholders = ",".join("?" * len(chunk))
                for row in self._db.execute(
                        f"SELECT id, metadata, detail_status, detail_collected_at FROM jobs"
                        f" WHERE id IN ({placeholders})", chunk):
                    try:
                        metadata = json.loads(row["metadata"])
                    except (ValueError, TypeError):
                        metadata = {}
                    if not isinstance(metadata, dict):
                        metadata = {}
                    metadata["detail_status"] = row["detail_status"]
                    if row["detail_collected_at"]:
                        metadata["detail_collected_at"] = row["detail_collected_at"]
                    found[row["id"]] = metadata
        return found

    def upsert_jobs(self, jobs: Iterable[dict], *, on_board: bool = True) -> int:
        """Insert or update postings, preserving `first_seen` and any detail
        already read.

        Listing fields refresh, but a blank listing value never overwrites a
        populated detail value, and metadata is *merged* rather than replaced:
        a later listing row carries none of the detail-only fields (work term,
        targeted disciplines, the long-form text), so overwriting the blob
        would silently discard the very data the crawler spent a page visit
        collecting.
        """
        jobs = list(jobs)
        now = _now()
        rows = 0
        existing = self._metadata_for({str(job.get("id") or "") for job in jobs})
        with self._lock:
            for job in jobs:
                incoming = job.get("metadata") or {}
                previous = existing.get(str(job.get("id") or ""), {})
                metadata = {**previous, **{key: value for key, value in incoming.items()
                                           if value not in ("", None)}}
                # A posting only becomes 'complete'; a refresh never demotes it.
                if previous.get("detail_status") == "complete":
                    metadata["detail_status"] = "complete"
                    metadata["detail_collected_at"] = previous.get(
                        "detail_collected_at", metadata.get("detail_collected_at"))
                self._db.execute(
                    """
                    INSERT INTO jobs (id, title, company, location, description, requirements,
                                      deadline, url, status, is_open, metadata, detail_status,
                                      detail_collected_at, first_seen, last_seen, on_board)
                    VALUES (:id, :title, :company, :location, :description, :requirements,
                            :deadline, :url, :status, :is_open, :metadata, :detail_status,
                            :detail_collected_at, :now, :now, :on_board)
                    ON CONFLICT(id) DO UPDATE SET
                        title        = CASE WHEN excluded.title        != '' THEN excluded.title        ELSE jobs.title        END,
                        company      = CASE WHEN excluded.company      != '' THEN excluded.company      ELSE jobs.company      END,
                        location     = CASE WHEN excluded.location     != '' THEN excluded.location     ELSE jobs.location     END,
                        description  = CASE WHEN excluded.description  != '' THEN excluded.description  ELSE jobs.description  END,
                        requirements = CASE WHEN excluded.requirements != '' THEN excluded.requirements ELSE jobs.requirements END,
                        deadline     = CASE WHEN excluded.deadline     != '' THEN excluded.deadline     ELSE jobs.deadline     END,
                        url          = CASE WHEN excluded.url          != '' THEN excluded.url          ELSE jobs.url          END,
                        status       = CASE WHEN excluded.status       != '' THEN excluded.status       ELSE jobs.status       END,
                        is_open      = COALESCE(excluded.is_open, jobs.is_open),
                        metadata     = excluded.metadata,
                        detail_status       = CASE WHEN excluded.detail_status = 'complete'
                                                   THEN 'complete' ELSE jobs.detail_status END,
                        detail_collected_at = COALESCE(excluded.detail_collected_at, jobs.detail_collected_at),
                        last_seen    = excluded.last_seen,
                        on_board     = excluded.on_board
                    """,
                    {
                        "id": str(job.get("id") or ""),
                        "title": job.get("title") or "",
                        "company": job.get("company") or "",
                        "location": job.get("location") or "",
                        "description": job.get("description") or "",
                        "requirements": job.get("requirements") or "",
                        "deadline": job.get("deadline") or "",
                        "url": job.get("url") or "",
                        "status": job.get("status") or "",
                        "is_open": None if job.get("is_open") is None else int(bool(job["is_open"])),
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                        "detail_status": metadata.get("detail_status", "pending"),
                        "detail_collected_at": metadata.get("detail_collected_at"),
                        "now": now,
                        "on_board": int(on_board),
                    },
                )
                rows += 1
            self._db.commit()
        return rows

    def _row_to_job(self, row: sqlite3.Row) -> dict:
        try:
            metadata = json.loads(row["metadata"])
        except (ValueError, TypeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        # The dedicated columns are authoritative: the blob is user-shaped data
        # that a stale write could otherwise contradict.
        metadata["detail_status"] = row["detail_status"]
        if row["detail_collected_at"]:
            metadata["detail_collected_at"] = row["detail_collected_at"]
        else:
            metadata.pop("detail_collected_at", None)
        return {
            "id": row["id"], "title": row["title"], "company": row["company"],
            "location": row["location"], "description": row["description"],
            "requirements": row["requirements"], "deadline": row["deadline"],
            "url": row["url"], "status": row["status"],
            "is_open": None if row["is_open"] is None else bool(row["is_open"]),
            "metadata": metadata,
        }

    def jobs(self, *, on_board_only: bool = True) -> list[dict]:
        clause = "WHERE on_board = 1" if on_board_only else ""
        with self._lock:
            rows = self._db.execute(f"SELECT * FROM jobs {clause} ORDER BY id").fetchall()
        return [self._row_to_job(row) for row in rows]

    def job_count(self, *, on_board_only: bool = True) -> int:
        clause = "WHERE on_board = 1" if on_board_only else ""
        with self._lock:
            return self._db.execute(f"SELECT COUNT(*) FROM jobs {clause}").fetchone()[0]

    def reusable_details(self, *, max_age_seconds: int = config.DETAIL_REUSE_SECONDS,
                         now: Optional[datetime] = None) -> dict[str, dict]:
        """Postings whose detail was read recently enough to skip re-reading.

        Age is measured from the posting's own read time, so reusing a detail
        never extends its freshness — the bug a single `collected_at` for the
        whole file invited.
        """
        now = now or datetime.now(timezone.utc)
        floor = (now - timedelta(seconds=max_age_seconds)).isoformat()
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM jobs WHERE detail_status = 'complete' AND detail_collected_at >= ?"
                " AND detail_collected_at <= ?", (floor, now.isoformat()),
            ).fetchall()
        # The SQL narrows the scan; `detail_is_fresh` is still the rule, so an
        # unparseable timestamp cannot slip through on string comparison alone.
        jobs = (self._row_to_job(row) for row in rows)
        return {job["id"]: job for job in jobs
                if detail_is_fresh(job["metadata"], now, max_age_seconds)}

    def mark_off_board(self, keep_ids: Iterable[str]) -> int:
        """Flag postings absent from the current filtered board.

        Rows are kept, not deleted: their details stay reusable if the posting
        reappears, and the user can still see what vanished. Only `on_board`
        rows are ranked.
        """
        keep = {str(value) for value in keep_ids}
        with self._lock:
            rows = self._db.execute("SELECT id FROM jobs WHERE on_board = 1").fetchall()
            stale = [(row["id"],) for row in rows if row["id"] not in keep]
            self._db.executemany("UPDATE jobs SET on_board = 0 WHERE id = ?", stale)
            self._db.commit()
        return len(stale)

    def replace_jobs(self, jobs: Iterable[dict]) -> int:
        """Import path: the given list becomes the whole board."""
        with self._lock:
            self._db.execute("DELETE FROM jobs")
            self._db.commit()
        return self.upsert_jobs(jobs)

    def set_detail_failure(self, job_id: str, reason: str) -> None:
        with self._lock:
            row = self._db.execute("SELECT metadata FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return
            try:
                metadata = json.loads(row["metadata"])
            except (ValueError, TypeError):
                metadata = {}
            metadata.update(detail_status="failed", detail_error=reason)
            self._db.execute("UPDATE jobs SET detail_status = 'failed', metadata = ? WHERE id = ?",
                             (json.dumps(metadata, ensure_ascii=False), job_id))
            self._db.commit()

    # ---- model call cache --------------------------------------------------

    def cached(self, key: str) -> Optional[Any]:
        with self._lock:
            row = self._db.execute("SELECT payload FROM llm_cache WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["payload"])
        except (ValueError, TypeError):
            return None

    def put_cached(self, key: str, kind: str, payload: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO llm_cache (key, kind, payload, created_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET payload = excluded.payload, created_at = excluded.created_at",
                (key, kind, json.dumps(payload, ensure_ascii=False), _now()),
            )
            self._db.commit()

    def cache_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT kind, COUNT(*) AS n FROM llm_cache GROUP BY kind").fetchall()
        return {row["kind"]: row["n"] for row in rows}

    def clear_cache(self, kind: Optional[str] = None) -> int:
        with self._lock:
            cursor = (self._db.execute("DELETE FROM llm_cache WHERE kind = ?", (kind,)) if kind
                      else self._db.execute("DELETE FROM llm_cache"))
            self._db.commit()
            return cursor.rowcount

    # ---- meta --------------------------------------------------------------

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO meta (key, value) VALUES (?, ?)"
                             " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
            self._db.commit()

    # ---- async wrappers ----------------------------------------------------

    async def aupsert_jobs(self, jobs, **kwargs) -> int:
        return await asyncio.to_thread(self.upsert_jobs, list(jobs), **kwargs)

    async def ajobs(self, **kwargs) -> list[dict]:
        return await asyncio.to_thread(self.jobs, **kwargs)

    async def acached(self, key: str):
        return await asyncio.to_thread(self.cached, key)

    async def aput_cached(self, key: str, kind: str, payload) -> None:
        await asyncio.to_thread(self.put_cached, key, kind, payload)

    # ---- embedding cache ---------------------------------------------------

    def ensure_embedding_table(self) -> None:
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS embeddings ("
                             "key TEXT PRIMARY KEY, vector BLOB NOT NULL, created_at TEXT NOT NULL)")
            self._db.commit()

    def embeddings(self, keys: Iterable[str]) -> dict[str, bytes]:
        keys = list(keys)
        found: dict[str, bytes] = {}
        with self._lock:
            for start in range(0, len(keys), 500):
                chunk = keys[start:start + 500]
                placeholders = ",".join("?" * len(chunk))
                for row in self._db.execute(
                        f"SELECT key, vector FROM embeddings WHERE key IN ({placeholders})", chunk):
                    found[row["key"]] = row["vector"]
        return found

    def put_embeddings(self, vectors: dict[str, bytes]) -> None:
        now = _now()
        with self._lock:
            self._db.executemany(
                "INSERT INTO embeddings (key, vector, created_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET vector = excluded.vector",
                [(key, value, now) for key, value in vectors.items()])
            self._db.commit()
