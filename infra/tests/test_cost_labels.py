from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gcp"))
from gcp_jobs import cost_labels, parse_labels, submitter_spec


def test_cost_labels_from_env():
    assert cost_labels({"DISKY_LABELS": "app=disky, deployment=gcs"}) == {"app": "disky", "deployment": "gcs"}
    assert cost_labels({"DISKY_LABELS": ""}) == {}


def test_cost_labels_unset_refuses():
    with pytest.raises(ValueError) as e:
        cost_labels({})
    assert str(e.value) == "$DISKY_LABELS is unset: set it (e.g. app=disky,deployment=<stack>), or to '' to manage no labels"


@pytest.mark.parametrize("text", ["app", "App=x", "app=X", "app=a,app=b"])
def test_parse_labels_refuses(text: str):
    with pytest.raises(ValueError) as e:
        parse_labels(text)
    assert str(e.value) == f"DISKY_LABELS: bad or duplicate label {text.split(',')[-1]!r}"


def test_submitter_spec_sees_only_pin_and_labels(tmp_path, monkeypatch):
    """The cron body is a function of the submitter and the stack's labels, nothing else ambient."""
    job = tmp_path / "job"
    job.mkdir()
    sub = job / "submit.sh"
    sub.write_text('python3 -c \'import json, os; print(json.dumps({k: os.environ.get(k) for k in ["PIN", "DRY", "DISKY_LABELS", "OTHER"]}))\'\n')
    monkeypatch.setenv("OTHER", "x")
    monkeypatch.setenv("DISKY_LABELS", "app=disky")
    assert submitter_spec(sub) == {"PIN": "1", "DRY": "1", "DISKY_LABELS": "app=disky", "OTHER": None}
    monkeypatch.delenv("DISKY_LABELS")
    assert submitter_spec(sub) == {"PIN": "1", "DRY": "1", "DISKY_LABELS": None, "OTHER": None}
