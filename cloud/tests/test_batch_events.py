"""`dt-cloud batch-events`: a Batch state-change push → reconcile + converge on a terminal state, nothing otherwise."""

import base64
import json

from dt_cloud.batch_events import handle


def push(attrs: dict | None, data: bytes = b"", mid: str = "m1") -> bytes:
    msg: dict = {"data": base64.b64encode(data).decode(), "messageId": mid}
    if attrs is not None:
        msg["attributes"] = attrs
    return json.dumps({"message": msg, "subscription": "projects/p/subscriptions/s"}).encode()


def changed(state: str, uid: str = "gcs-usage-snapshot-abc") -> dict:
    return {"Type": "JOB_STATE_CHANGED", "JobUID": uid, "JobName": f"projects/p/locations/r/jobs/{uid}", "NewJobState": state}


def run(body: bytes, fail: bool = False) -> tuple[int, list[dict]]:
    seen: list[dict] = []

    def act(attrs):
        seen.append(dict(attrs))
        if fail:
            raise RuntimeError("D1 down")

    return handle(body, act), seen


def test_a_terminal_state_reconciles_and_converges():
    for state in ("SUCCEEDED", "FAILED", "CANCELLED"):
        assert run(push(changed(state))) == (204, [changed(state)])


def test_other_changes_are_acked_untouched():
    assert run(push(changed("RUNNING"))) == (204, [])
    assert run(push(changed("CANCELLATION_IN_PROGRESS"))) == (204, [])
    assert run(push({"Type": "TASK_STATE_CHANGED", "NewJobState": "FAILED"})) == (204, [])
    assert run(push(None)) == (204, [])


def test_a_failing_action_is_redelivered():
    assert run(push(changed("CANCELLED")), fail=True) == (500, [changed("CANCELLED")])


def test_a_body_that_isnt_a_push_is_refused():
    assert run(b"not json") == (400, [])
    assert run(json.dumps({"nope": 1}).encode()) == (400, [])
    assert run(json.dumps({"message": {"data": "!!"}}).encode()) == (400, [])
