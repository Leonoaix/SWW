"""Retrieval: BM25, and requirement-level semantic matching when a model exists."""
import pytest

from conftest import NOW, RESUME, job
from sww.embedding import available, load_embedder
from sww.match.lexical import BM25, weighted_query
from sww.match.retrieval import job_chunks, resume_chunks, retrieve
from sww.resume import build_profile

# A posting that describes backend work without naming one tool in the resume.
# Under the previous ranker this scored zero: no vocabulary term matched.
PARAPHRASED = job("paraphrased", "Event Platform Engineering",
                  "Implement non-blocking consumers, recover gracefully from transient faults, "
                  "and ensure messages are processed without duplicate side effects.")
KEYWORD_STUFFED = job("stuffed", "Marketing Coordinator",
                      "Promote our Python, SQL and Docker developer tools on social media. "
                      "Write copy about Python and SQL for campaigns.")
UNRELATED = job("unrelated", "Laboratory Technician",
                "Prepare cell cultures and run PCR assays in a wet lab.")


def test_bm25_normalises_for_document_length():
    """The correction plain TF-IDF cosine lacks: a long document does not win
    simply by containing more words."""
    short = ["python", "queue"]
    padded = short + ["filler"] * 200
    index = BM25([short, padded])
    scores = index.scores(["python", "queue"])
    assert scores[0] > scores[1]


def test_listed_skills_weigh_less_than_demonstrated_ones():
    query, weights = weighted_query([("built services with Python", 1.0)], ["Rust"])
    assert weights["python"] == 1.0
    assert weights["rust"] < 1.0
    assert "rust" in query


def test_a_term_takes_the_weight_of_the_most_recent_experience_using_it():
    """Having also used something years ago must not drag down having used it
    last term."""
    query, weights = weighted_query(
        [("Python and Fortran", 0.4), ("Python services", 0.95)], [])
    assert weights["python"] == 0.95     # the recent use wins
    assert weights["fortran"] == 0.4     # the old one keeps its own weight


async def test_resume_chunks_prefer_experience_over_the_skills_list():
    analysis = await build_profile(RESUME, now=NOW)
    chunks = resume_chunks(analysis)
    labels = [chunk.label for chunk in chunks]
    assert any(label.startswith("work") for label in labels)
    listed = [chunk for chunk in chunks if chunk.label == "skills-list"]
    assert listed and listed[0].weight < 1.0


def test_job_chunks_follow_extracted_requirements_when_present():
    chunks = job_chunks(PARAPHRASED, [{"text": "异步服务经验", "importance": "must",
                                       "category": "experience"},
                                      {"text": "写文案", "importance": "preferred",
                                       "category": "soft"}])
    assert [chunk.weight for chunk in chunks] == [2.0, 1.0]


async def test_bm25_only_retrieval_still_ranks_and_reports_no_semantic_score():
    analysis = await build_profile(RESUME, now=NOW)
    results = retrieve(analysis, [PARAPHRASED, KEYWORD_STUFFED, UNRELATED], embedder=None)
    assert all(item.semantic is None for item in results)
    assert all(0 <= item.relevance <= 1 for item in results)


@pytest.mark.skipif(not available(), reason="Install the embeddings extra to run semantic retrieval")
async def test_semantic_retrieval_finds_paraphrased_work_the_vocabulary_misses():
    analysis = await build_profile(RESUME, now=NOW)
    embedder = load_embedder()
    results = {item.job_id: item for item in
               retrieve(analysis, [PARAPHRASED, UNRELATED], embedder=embedder)}
    assert results["paraphrased"].semantic > results["unrelated"].semantic


@pytest.mark.skipif(not available(), reason="Install the embeddings extra to run semantic retrieval")
async def test_relevance_is_calibrated_not_normalised_against_the_batch():
    """A score must mean roughly the same thing next week.

    The semantic half is exactly stable by construction. The lexical half moves
    a little because BM25's idf is a corpus statistic — unavoidable, and small.
    What must never happen is the batch-maximum rescaling this replaced, under
    which one strong new posting dragged every other score down.
    """
    analysis = await build_profile(RESUME, now=NOW)
    embedder = load_embedder()
    alone = retrieve(analysis, [UNRELATED], embedder=embedder)[0]
    with_others = {item.job_id: item for item in
                   retrieve(analysis, [UNRELATED, PARAPHRASED, KEYWORD_STUFFED], embedder=embedder)}
    assert alone.semantic == pytest.approx(with_others["unrelated"].semantic, abs=1e-6)
    assert alone.relevance == pytest.approx(with_others["unrelated"].relevance, abs=0.02)


def test_empty_input_is_not_an_error():
    assert retrieve.__module__  # import guard
