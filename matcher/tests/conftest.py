"""Shared fixtures. Every fixture is synthetic; none touches a real account."""
import asyncio
from datetime import date

import pytest

from sww.llm import Extractor
from sww.resume import build_profile
from sww.store import Store

# Recency is measured against today, so every test pins a reference date.
# Without it an assertion about a score would drift a little each month and
# fail some morning for no reason anyone could reproduce.
NOW = date(2026, 9, 15)

RESUME = """Example Student
student@example.com | Waterloo, ON

EDUCATION
University of Waterloo, BASc Computer Science          Sep 2023 - Apr 2028

WORK EXPERIENCE
Software Engineering Intern, Example Corp              May 2025 - Aug 2025
- Built asynchronous Python services with durable queues and PostgreSQL transactions.
- Reduced duplicate events by 90 percent using idempotency keys.

PROJECTS
Telemetry Pipeline                                     Sep 2024 - Dec 2024
- Normalized records and aggregated daily metrics with SQL.

TECHNICAL SKILLS
Languages: Python, SQL, Rust, Go
Available for a four-month work term.
"""

JOB = {
    "id": "1", "title": "Backend Intern", "company": "Example Employer",
    "location": "Waterloo, ON",
    "description": "Develop reliable event-driven services using durable message queues.",
    "requirements": "Experience with asynchronous services.",
    "deadline": "2099-12-31",
    "url": "https://waterlooworks.uwaterloo.ca/myAccount/co-op/full/jobs.htm?jobId=1",
    "metadata": {},
}


def job(identifier, title="Software Developer", description="Python, SQL, Docker", **fields):
    return {"id": identifier, "title": title, "company": "Example", "location": "Waterloo, ON",
            "description": description, "requirements": "", "deadline": "2099-12-31",
            "url": "https://waterlooworks.uwaterloo.ca/myAccount/co-op/full/jobs.htm?jobId=1",
            "metadata": {}, **fields}


def requirements_payload(text="可靠事件服务开发", category="experience",
                         importance="must",
                         quote="Develop reliable event-driven services using durable message queues."):
    return {"role_family": "backend", "term_months": [4],
            "requirements": [{"text": text, "category": category,
                              "importance": importance, "quote": quote}]}


def judgement_payload(status="transferable",
                      resume_quote="Built asynchronous Python services with durable queues",
                      index=1, evidence_ref="b3"):
    return {"judgements": [{"index": index, "status": status, "evidence_ref": evidence_ref,
                            "resume_quote": resume_quote,
                            "explanation": "异步服务和可靠队列经历可以迁移。"}]}


class FakeClient:
    """A DeepSeek client that answers from a script instead of the network.

    Responses are keyed by the extraction kind the system prompt belongs to, so
    one fake can serve the requirement, match and pair stages in a single run.
    """

    model = "fake-model"

    def __init__(self, requirements=None, judgements=None, pair=None):
        self.responses = {
            "requirements": requirements if requirements is not None else requirements_payload(),
            "match": judgements if judgements is not None else judgement_payload(),
            "pair": pair if pair is not None else {"winner": "tie", "reason": "证据相近。"},
        }
        self.usage = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self.prompts = []
        self.closed = False

    def _kind(self, system):
        if system.startswith("You extract the hiring requirements"):
            return "requirements"
        if system.startswith("Compare two nearby"):
            return "pair"
        return "match"

    async def json(self, system, payload):
        self.usage["requests"] += 1
        self.prompts.append((self._kind(system), payload))
        await asyncio.sleep(0)
        return self.responses[self._kind(system)]

    async def close(self):
        self.closed = True


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path)
    yield instance
    instance.close()


@pytest.fixture
def extractor(store):
    return Extractor(FakeClient(), store, "test-version")


@pytest.fixture
def analysis():
    """The local (no-model) split of the sample resume, at a fixed date."""
    return asyncio.new_event_loop().run_until_complete(build_profile(RESUME, now=NOW))


def seed_resume(app, text=RESUME, filename="private.pdf"):
    """Install a parsed resume without going through the upload endpoint."""
    analysis = asyncio.new_event_loop().run_until_complete(build_profile(text))
    analysis.filename = filename
    app.state.matcher["resume"] = analysis
    return analysis


def seed_jobs(app, jobs):
    app.state.store.replace_jobs(jobs)
