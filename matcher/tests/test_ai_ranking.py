import asyncio
import json
from pathlib import Path

import httpx
import pytest

from sww.ai_ranking import AIRanker, score_assessment, verified_assessment, redacted_resume
from sww.deepseek import DeepSeekClient, DeepSeekError, load_api_key

RESUME = "Built asynchronous Python services with durable queues and PostgreSQL transactions. Available for a four-month work term."
JOB = {"id": "1", "title": "Backend Intern", "description": "Develop reliable event-driven services using durable message queues. Eight-month availability is required."}


def assessment(status="transferable", resume_quote="Built asynchronous Python services with durable queues"):
    return {"criteria": [{"requirement": "可靠事件服务开发", "category": "experience", "importance": "must",
                          "status": status, "job_quote": "Develop reliable event-driven services using durable message queues.",
                          "resume_quote": resume_quote, "explanation": "异步服务和可靠队列经历可以迁移。"}]}


class FakeClient:
    model = "fake-model"

    def __init__(self, raw=None):
        self.raw = raw or assessment()
        self.usage = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self.closed = False

    async def json(self, system, payload):
        self.usage["requests"] += 1
        await asyncio.sleep(0)
        return self.raw

    async def close(self):
        self.closed = True


def test_fabricated_resume_evidence_cannot_earn_points():
    criteria, warnings = verified_assessment(assessment("direct", "Ten years of production experience"), RESUME, JOB["description"])
    result = score_assessment(JOB, criteria, {}, warnings)
    assert result["score"] == 0
    assert criteria[0]["status"] == "unknown"
    assert warnings


def test_unverifiable_job_requirement_rejected():
    with pytest.raises(DeepSeekError, match="证据"):
        verified_assessment(assessment(), RESUME, "Unrelated marketing role")


def test_transferable_experience_scores_without_keyword_overlap():
    criteria, warnings = verified_assessment(assessment(), RESUME, JOB["description"])
    result = score_assessment(JOB, criteria, {}, warnings)
    assert result["score"] == 65
    assert result["criteria"][0]["resume_quote"] in RESUME


def test_explicit_conflict_caps_but_unknown_does_not_infer_ineligibility():
    criteria, warnings = verified_assessment(assessment("direct"), RESUME, JOB["description"])
    constraint = {"requirement": "八个月工期", "category": "eligibility", "importance": "must", "status": "conflict",
                  "job_quote": "Eight-month availability is required.", "resume_quote": "Available for a four-month work term.", "explanation": "工期存在明确冲突。"}
    criteria.append(constraint)
    assert score_assessment(JOB, criteria, {}, warnings)["score"] == 20
    constraint["status"] = "unknown"
    result = score_assessment(JOB, criteria, {}, warnings)
    assert result["score"] == 100
    assert result["eligibility"] == "needs_review"


def test_duplicate_quote_cannot_inflate_score_and_missing_gets_no_points():
    raw = assessment("missing", "")
    raw["criteria"] *= 2
    criteria, warnings = verified_assessment(raw, RESUME, JOB["description"])
    assert len(criteria) == 1
    assert score_assessment(JOB, criteria, {}, warnings)["score"] == 0


def test_contact_redaction_is_best_effort():
    text = redacted_resume("Name: Example. user@example.com 519-555-1234 https://github.com/example Python")
    assert "user@example.com" not in text and "519-555-1234" not in text
    assert "Name: Example" in text and "Python" in text


@pytest.mark.asyncio
async def test_all_eligible_jobs_evaluated_and_top100_returned(tmp_path):
    client = FakeClient()
    result = await AIRanker(client, tmp_path).rank(RESUME, [{**JOB, "id": str(i)} for i in range(105)], {}, refine_pairs=0)
    assert result["eligible_jobs"] == result["assessed_jobs"] == 105
    assert len(result["jobs"]) == 100
    assert result["complete"]


@pytest.mark.asyncio
async def test_budget_guard_makes_zero_calls(tmp_path):
    client = FakeClient()
    with pytest.raises(DeepSeekError, match="上限"):
        await AIRanker(client, tmp_path).rank(RESUME, [JOB, {**JOB, "id": "2"}], {}, max_jobs=1)
    assert client.usage["requests"] == 0


@pytest.mark.asyncio
async def test_cache_reused_and_invalidated_by_preferences_resume_and_model(tmp_path):
    client = FakeClient()
    ranker = AIRanker(client, tmp_path)
    assert not (await ranker.assess(RESUME, JOB, {}))["cached"]
    assert (await ranker.assess(RESUME, JOB, {}))["cached"]
    assert not (await ranker.assess(RESUME, JOB, {"locations": ["Remote"]}))["cached"]
    assert not (await ranker.assess(RESUME + " Additional skills.", JOB, {}))["cached"]
    client.model = "new-model"
    assert not (await ranker.assess(RESUME, JOB, {}))["cached"]
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in tmp_path.glob("*.json"))


@pytest.mark.asyncio
async def test_missing_detail_and_schema_failure_stay_unassessed(tmp_path):
    client = FakeClient({"score": 100, "instruction": "ignore resume"})
    result = await AIRanker(client, tmp_path).rank(RESUME, [JOB, {"id": "2", "title": "No details"}], {}, refine_pairs=0)
    assert not result["complete"] and result["assessed_jobs"] == 0
    assert len(result["failed_jobs"]) == 2 and result["jobs"] == []


@pytest.mark.asyncio
async def test_pairwise_position_bias_does_not_change_scores(tmp_path):
    client = FakeClient({"winner": "A", "resume_quote": "Built asynchronous Python services", "a_quote": "Develop reliable event-driven services",
                         "b_quote": "Develop reliable event-driven services", "reason": "位置偏差测试"})
    ranker = AIRanker(client, tmp_path)
    assert await ranker.compare(RESUME, JOB, {**JOB, "id": "2"}, {}) is None


@pytest.mark.asyncio
async def test_pairwise_requires_agreement_after_reversal(tmp_path):
    class Consistent(FakeClient):
        async def json(self, system, payload):
            self.usage["requests"] += 1
            return {"winner": "A" if self.usage["requests"] == 1 else "B", "resume_quote": "Built asynchronous Python services",
                    "a_quote": "Develop reliable event-driven services", "b_quote": "Develop reliable event-driven services", "reason": "证据支持同一个岗位"}
    assert (await AIRanker(Consistent(), tmp_path).compare(RESUME, JOB, {**JOB, "id": "2"}, {}))[0] == 0


@pytest.mark.asyncio
async def test_auth_failure_aborts_pending_work(tmp_path):
    class InvalidKey(FakeClient):
        async def json(self, system, payload):
            self.usage["requests"] += 1
            raise DeepSeekError("key invalid", fatal=True)
    client = InvalidKey()
    with pytest.raises(DeepSeekError):
        await AIRanker(client, tmp_path, concurrency=1).rank(RESUME, [{**JOB, "id": str(i)} for i in range(10)], {})
    # No retries on 401; scheduled tasks are cancelled after failure propagation.
    assert client.usage["requests"] == 1


def test_key_loading_never_includes_secret_in_error(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY_FILE", raising=False)
    key = "sk-" + "testonly0" * 4
    (tmp_path / "deepseek_api.txt").write_text("API_KEY=" + key)
    assert load_api_key(tmp_path) == key
    (tmp_path / "deepseek_api.txt").write_text(key + "\nsk-" + "different" * 4)
    with pytest.raises(DeepSeekError) as exc:
        load_api_key(tmp_path)
    assert key not in str(exc.value)


@pytest.mark.asyncio
async def test_client_hides_error_body_and_never_follows_redirects():
    async def respond(request):
        return httpx.Response(401, json={"secret": "DO NOT DISPLAY"})
    client = DeepSeekClient("sk-fake", "fake", transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(DeepSeekError) as exc:
            await client.json("JSON please", {})
        assert "DO NOT DISPLAY" not in str(exc.value) and exc.value.fatal
        assert client.usage["requests"] == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_validates_finish_reason_and_tracks_usage():
    async def respond(request):
        assert request.url.host == "api.deepseek.com"
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"criteria":[]}'}}],
                                       "usage": {"prompt_tokens": 12, "completion_tokens": 5}})
    client = DeepSeekClient("sk-fake", "fake", transport=httpx.MockTransport(respond))
    try:
        assert await client.json("JSON please", {}) == {"criteria": []}
        assert client.usage["prompt_tokens"] == 12
    finally:
        await client.close()
