"""SQLite persistence: incremental checkpoints and a re-verified model cache."""
from datetime import datetime, timedelta, timezone

from conftest import job


def stamped(identifier, when, status="complete", **fields):
    fields.setdefault("metadata", {"detail_status": status, "detail_collected_at": when.isoformat()})
    return job(identifier, **fields)


def test_listing_fields_never_overwrite_a_populated_detail(store):
    store.upsert_jobs([job("1", description="a full detail description")])
    store.upsert_jobs([job("1", description="")])
    assert store.jobs()[0]["description"] == "a full detail description"


def test_a_complete_detail_is_not_downgraded_by_a_later_listing_row(store):
    now = datetime.now(timezone.utc)
    store.upsert_jobs([stamped("1", now)])
    store.upsert_jobs([job("1", metadata={"detail_status": "pending"})])
    assert store.jobs()[0]["metadata"]["detail_status"] == "complete"


def test_details_expire_from_their_own_read_time(store):
    now = datetime.now(timezone.utc)
    store.upsert_jobs([stamped("fresh", now - timedelta(hours=1)),
                       stamped("stale", now - timedelta(hours=30)),
                       stamped("pending", now, status="pending")])
    assert set(store.reusable_details(now=now)) == {"fresh"}


def test_reusing_a_detail_does_not_extend_its_freshness(store):
    now = datetime.now(timezone.utc)
    read_at = now - timedelta(hours=23)
    store.upsert_jobs([stamped("1", read_at)])
    reusable = store.reusable_details(now=now)
    store.upsert_jobs(list(reusable.values()))
    assert store.reusable_details(now=now + timedelta(hours=2)) == {}


def test_postings_off_the_board_are_hidden_but_not_destroyed(store):
    store.upsert_jobs([job("1"), job("2")])
    assert store.mark_off_board(["1"]) == 1
    assert [item["id"] for item in store.jobs()] == ["1"]
    assert [item["id"] for item in store.jobs(on_board_only=False)] == ["1", "2"]


def test_detail_only_metadata_survives_a_later_listing_refresh(store):
    """A listing row knows nothing about work term or targeted disciplines;
    writing it must not discard what the detail page cost a visit to collect."""
    now = datetime.now(timezone.utc)
    store.upsert_jobs([stamped("1", now, **{"metadata": {
        "detail_status": "complete", "detail_collected_at": now.isoformat(),
        "Work Term Duration": "4 month", "Targeted Degrees and Disciplines": "Computer Science"}})])
    store.upsert_jobs([job("1", metadata={"detail_status": "pending", "posting_status": "Open"})])
    metadata = store.jobs()[0]["metadata"]
    assert metadata["Work Term Duration"] == "4 month"
    assert metadata["Targeted Degrees and Disciplines"] == "Computer Science"
    assert metadata["posting_status"] == "Open"
    assert metadata["detail_status"] == "complete"


def test_import_replaces_the_whole_board(store):
    store.upsert_jobs([job("1"), job("2")])
    store.replace_jobs([job("9")])
    assert [item["id"] for item in store.jobs()] == ["9"]


def test_cache_round_trips_and_counts_by_kind(store):
    store.put_cached("a", "requirements", {"x": 1})
    store.put_cached("b", "match", {"y": 2})
    assert store.cached("a") == {"x": 1}
    assert store.cached("missing") is None
    assert store.cache_counts() == {"match": 1, "requirements": 1}
    assert store.clear_cache("match") == 1
    assert store.cache_counts() == {"requirements": 1}


def test_database_and_directory_stay_private(store):
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.directory.stat().st_mode & 0o777 == 0o700
