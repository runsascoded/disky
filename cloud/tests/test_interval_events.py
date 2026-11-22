"""#82 (2): an interval-store compaction's jobs publish their ends (`with_events`), and `append -l` finds the catch-up's scan."""

import pytest

from dt_cloud.append_runner import latest_pending
from dt_cloud.interval_append import with_events

TOPIC = "projects/p/topics/d-scan-events"


def spec() -> dict:
    return {"taskGroups": [{"taskSpec": {"environment": {"variables": {"A": "1"}}}}], "labels": {"stage": "compact", "component": "interval-store"}}


def test_with_events_names_the_topic_the_label_and_the_profile():
    assert with_events(spec(), TOPIC, "catch-up", "/app/job/interval-store.json") == {
        "taskGroups": [{"taskSpec": {"environment": {"variables": {"A": "1", "INTERVAL_STORE_PROFILE": "/app/job/interval-store.json"}}}}],
        "labels": {"stage": "compact", "component": "interval-store", "events": "catch-up"},
        "notifications": [{"pubsubTopic": TOPIC, "message": {"type": "JOB_STATE_CHANGED"}}],
    }
    assert with_events(spec(), TOPIC, "alert", None) == {
        "taskGroups": [{"taskSpec": {"environment": {"variables": {"A": "1"}}}}],
        "labels": {"stage": "compact", "component": "interval-store", "events": "alert"},
        "notifications": [{"pubsubTopic": TOPIC, "message": {"type": "JOB_STATE_CHANGED"}}],
    }
    with pytest.raises(SystemExit) as e:
        with_events(spec(), "d-scan-events", "alert", None)
    assert str(e.value) == "interval store: events_topic 'd-scan-events' isn't projects/<project>/topics/<topic>"


def test_latest_pending_is_the_newest_published_scan_the_store_lacks():
    have = ["2026-11-19T0700", "2026-11-20T0700"]
    assert latest_pending(have, ["2026-11-20T0700", "2026-11-21T0703", "2026-11-21T1928"]) == "2026-11-21T1928"
    assert latest_pending(have, ["2026-11-19T0700", "2026-11-20T0700"]) is None
    assert latest_pending(have, []) is None
