"""Rank a labelled synthetic fixture with each engine and report NDCG.

Run without `--live` to measure the local pipeline only: no API key, no network
and no cost. That path is worth measuring on its own, because it is what the
matcher falls back to and what decides the shortlist the model ever sees.

`--live` additionally runs the semantic pipeline against the real API using a
separate cache directory. Fixtures are synthetic and their relevance labels are
hand-written expectations, not measured hiring outcomes.
"""
import argparse
import asyncio
import json
import math
from pathlib import Path

from sww.embedding import load_embedder
from sww.llm import DeepSeekClient, DeepSeekError, Extractor, load_api_key, model_name
from sww.match.assess import VERSION as MATCH_VERSION
from sww.match.pipeline import SemanticRanker, rank_local
from sww.resume import build_profile
from sww.store import Store

ROOT = Path(__file__).resolve().parents[1]


def ndcg(ids, labels):
    def dcg(values):
        return sum((2 ** value - 1) / math.log2(index + 2) for index, value in enumerate(values))
    ideal = dcg(sorted(labels.values(), reverse=True))
    return dcg([labels[identifier] for identifier in ids]) / ideal if ideal else 0.0


def check(fixture, by_id):
    results = []
    for item in fixture.get("checks", []):
        job = by_id.get(item["id"])
        passed = job is not None
        if job is not None:
            if "max_score" in item:
                passed &= job["score"] <= item["max_score"]
            if "min_score" in item:
                passed &= job["score"] >= item["min_score"]
            if "eligibility" in item:
                passed &= job["eligibility"] == item["eligibility"]
            if "not_eligibility" in item:
                passed &= job["eligibility"] != item["not_eligibility"]
            if "above" in item:
                passed &= item["above"] in by_id and job["rank"] < by_id[item["above"]]["rank"]
        results.append({**item, "passed": bool(passed)})
    return results


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="Also run the semantic engine against the real API (costs money).")
    parser.add_argument("--fixture", type=Path,
                        default=ROOT / "matcher/tests/fixtures/semantic_cases.json")
    parser.add_argument("--output", type=Path, help="Save the synthetic evaluation report as JSON.")
    parser.add_argument("--shortlist", type=int, default=0,
                        help="0 assesses every eligible posting (the default for evaluation).")
    args = parser.parse_args()

    fixture = json.loads(args.fixture.read_text())
    labels = {job["id"]: job["relevance"] for job in fixture["jobs"]}
    jobs = [{key: value for key, value in job.items() if key != "relevance"} for job in fixture["jobs"]]
    preferences = fixture.get("preferences", {})
    analysis = await build_profile(fixture["resume"])
    embedder = load_embedder()

    lexical = rank_local(analysis, jobs, preferences, embedder=None)
    local = rank_local(analysis, jobs, preferences, embedder=embedder)
    report = {
        "note": fixture["note"],
        "fixture": args.fixture.name,
        "semantic_retrieval_available": embedder is not None,
        "resume_split": {"source": analysis.profile.source,
                         "experiences": len(analysis.profile.experiences),
                         "demonstrated_skills": analysis.profile.demonstrated_skills,
                         "listed_only_skills": analysis.profile.listed_skills,
                         "availability_months": analysis.profile.availability.months},
        "lexical_only": {"order": [job["id"] for job in lexical["jobs"]],
                         "ndcg": round(ndcg([job["id"] for job in lexical["jobs"]], labels), 4)},
        "local": {"order": [job["id"] for job in local["jobs"]],
                  "scores": {job["id"]: job["score"] for job in local["jobs"]},
                  "ndcg": round(ndcg([job["id"] for job in local["jobs"]], labels), 4)},
    }
    report["passed"] = True

    if args.live:
        store = Store(ROOT / ".sww/evaluation-cache")
        client = DeepSeekClient(load_api_key(ROOT), model_name())
        try:
            async def progress(state):
                print(json.dumps({"stage": state["stage"], "completed": state["completed"],
                                  "total": state["total"]}), flush=True)
            ranker = SemanticRanker(Extractor(client, store, MATCH_VERSION), embedder)
            result = await ranker.rank(analysis, jobs, preferences, shortlist=args.shortlist,
                                       refine_pairs=2, progress=progress)
            by_id = {job["id"]: job for job in result["jobs"]}
            checks = check(fixture, by_id)
            report["semantic"] = {
                "order": [job["id"] for job in result["jobs"]],
                "scores": {job["id"]: job["score"] for job in result["jobs"]},
                "eligibility": {job["id"]: job["eligibility"] for job in result["jobs"]},
                "ndcg": round(ndcg([job["id"] for job in result["jobs"]], labels), 4) if result["complete"] else None,
                "model": client.model, "usage": result["usage"],
                "cached_jobs": result["cached_jobs"], "failed_jobs": result["failed_jobs"],
                "warnings": result["warnings"], "complete": result["complete"], "checks": checks,
            }
            report["passed"] = result["complete"] and all(item["passed"] for item in checks)
        finally:
            await client.close()
            store.close()

    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except DeepSeekError as exc:
        print(str(exc))
        raise SystemExit(1)
