"""Opt-in live sanity benchmark; sends synthetic data only and incurs API usage."""
import argparse
import asyncio
import json
import math
from pathlib import Path

from sww.ai_ranking import AIRanker
from sww.deepseek import DeepSeekClient, DeepSeekError, load_api_key, model_name
from sww.ranking import rank_jobs

ROOT = Path(__file__).resolve().parents[1]


def ndcg(ids, labels):
    def dcg(values):
        return sum((2 ** value - 1) / math.log2(index + 2) for index, value in enumerate(values))
    return dcg([labels[identifier] for identifier in ids]) / dcg(sorted(labels.values(), reverse=True))


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.parse_args()
    fixture = json.loads((ROOT / "matcher/tests/fixtures/semantic_cases.json").read_text())
    labels = {job["id"]: job["relevance"] for job in fixture["jobs"]}
    jobs = [{key: value for key, value in job.items() if key != "relevance"} for job in fixture["jobs"]]
    baseline = rank_jobs(fixture["resume"], jobs)
    client = DeepSeekClient(load_api_key(ROOT), model_name())
    try:
        async def progress(state):
            print(json.dumps({"stage": state["stage"], "completed": state["completed"], "total": state["total"]}), flush=True)
        result = await AIRanker(client, ROOT / ".sww/evaluation-cache").rank(fixture["resume"], jobs, {}, refine_pairs=2, progress=progress)
        print(json.dumps({"model": client.model, "note": fixture["note"],
            "baseline": [{"id": j["id"], "score": j["score"]} for j in baseline["jobs"]],
            "semantic": [{"id": j["id"], "score": j["score"], "eligibility": j["eligibility"]} for j in result["jobs"]],
            "baseline_ndcg": round(ndcg([j["id"] for j in baseline["jobs"]], labels), 4),
            "semantic_ndcg": round(ndcg([j["id"] for j in result["jobs"]], labels), 4) if result["complete"] else None,
            "failed_jobs": result["failed_jobs"], "usage": result["usage"], "cached_jobs": result["cached_jobs"]}, ensure_ascii=False, indent=2))
    finally:
        await client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except DeepSeekError as exc:
        print(str(exc))
        raise SystemExit(1)
