"""Every tunable in one place.

Limits that used to be scattered across modules as bare literals live here, so
changing "how much resume text may reach the API" is one edit with one name,
not a grep. Nothing here reads user input; values are constants or environment
overrides validated at import time.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / ".sww"

# ---- Service ---------------------------------------------------------------
HOST = "127.0.0.1"
PORT = 8765
ALLOWED_HOSTS = frozenset({f"{HOST}:{PORT}", f"localhost:{PORT}", f"{HOST}:5173", "localhost:5173"})
ALLOWED_ORIGINS = frozenset("http://" + host for host in ALLOWED_HOSTS)
MAX_BODY_BYTES = 12 * 1024 * 1024

# ---- Resume ingestion ------------------------------------------------------
MAX_RESUME_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 20
MAX_RESUME_CHARACTERS = 200_000
# What may leave the machine. Lower than the parse limit on purpose: a 200k
# character resume is a parsing artefact, not something worth paying to send.
MAX_RESUME_API_CHARACTERS = 40_000
MAX_JOB_API_CHARACTERS = 60_000

# ---- Crawl -----------------------------------------------------------------
MIN_CRAWL_DELAY_SECONDS = 0.5
DEFAULT_CRAWL_DELAY_SECONDS = 1.0
DETAIL_REUSE_SECONDS = 24 * 3600
MAX_CRAWL_PAGES = 1000
MAX_CRAWL_JOBS = 10_000

# ---- Retrieval -------------------------------------------------------------
BM25_K1 = 1.5
BM25_B = 0.75
# A skills-list mention is a claim; a project bullet is evidence. Retrieval
# keeps both but stops a keyword list from outweighing demonstrated work.
LISTED_SKILL_WEIGHT = 0.3
EMBEDDING_MODEL = os.environ.get("SWW_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
EMBEDDING_CACHE_DIR = DATA_DIR / "embeddings"
# Requirement-level late interaction: how many resume chunks a single job
# requirement is compared against before taking the maximum.
MAX_RESUME_CHUNKS = 200
MAX_JOB_CHUNKS = 60
# Fixed calibration from raw similarity to a 0-1 relevance component, measured
# on bge-small-en-v1.5, whose cosines for related technical prose sit high and
# in a narrow band. Fixed, not batch-relative: a score must mean the same thing
# this week as last week, and adding one strong posting must not move every
# other posting's number.
SEMANTIC_FLOOR = 0.35
SEMANTIC_CEILING = 0.80
# BM25 has no upper bound, so it is squashed by x/(x+k) rather than divided by
# the best value in the batch, for the same reason.
BM25_SATURATION = 12.0
SEMANTIC_BLEND = 0.7

# ---- Scoring ---------------------------------------------------------------
CATEGORY_WEIGHTS = {"technical": 35, "experience": 40, "domain": 25}
IMPORTANCE_WEIGHTS = {"must": 2.0, "preferred": 1.0}
# How much credit evidence earns. Transferable is deliberately well below
# direct: related-but-different experience is worth real points, not most of
# the points.
STATUS_CREDIT = {"direct": 1.0, "transferable": 0.6, "missing": 0.0, "unknown": 0.0, "conflict": 0.0}
PREFERENCE_POINTS = 10
CONFLICT_SCORE_CAP = 20

# ---- Cascade ---------------------------------------------------------------
# Retrieval is free and runs over everything; the cross-encoder costs about
# 38 ms per posting on a laptop CPU, so it reads the pool retrieval liked
# rather than the whole board (150 postings is ~6 seconds). The model stage is
# the expensive one, so it sees only what survives the rerank.
RERANK_MODEL = os.environ.get("SWW_RERANK_MODEL", "Xenova/ms-marco-MiniLM-L-6-v2")
RERANK_POOL = 150
# Cross-encoders truncate long inputs; keep both sides inside the window.
RERANK_QUERY_CHARS = 1200
RERANK_DOCUMENT_CHARS = 1800

# ---- Semantic ranking ------------------------------------------------------
DEFAULT_SHORTLIST = 50
DEFAULT_CONCURRENCY = 4
MAX_CRITERIA = 24


def _validated(name: str, default: str, pattern: str) -> str:
    value = os.environ.get(name, default).strip() or default
    if not re.fullmatch(pattern, value):
        raise ValueError(f"{name} is not a valid value.")
    return value


API_ORIGIN = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"


def model_name() -> str:
    return _validated("DEEPSEEK_MODEL", DEFAULT_MODEL, r"[A-Za-z0-9_.-]{1,100}")
