import json
from pathlib import Path

import pandas as pd

from dt_cloud.extras import ATTR_FILE, write_blocked, write_extras


def test_write_extras(tmp_path: Path):
    pfx = pd.DataFrame([
        {"key": "b/grug", "user": "calvin", "source": "wandb-run"},
        {"key": "b/data", "user": "ryan", "source": "manual"},
    ])
    assert write_extras(pfx, tmp_path) == {"attr": 2}
    assert (tmp_path / ATTR_FILE).read_text() == "b/data\tryan\tmanual\t\nb/grug\tcalvin\twandb-run\t\n"
    assert json.loads((tmp_path / (ATTR_FILE + ".idx.json")).read_text()) == {"v": 1, "n": 2, "size": 45, "keys": ["b/data"], "offsets": [0]}


def test_write_extras_without_attribution(tmp_path: Path):
    # No prefix frame → no sidecar written at all (the reader treats absent as "no extras").
    assert write_extras(None, tmp_path) == {}
    assert list(tmp_path.iterdir()) == []


def test_write_blocked_cuts_at_line_boundaries(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("dt_cloud.extras.BLOCK", 10)
    lines = ["a/1", "a/2", "b/1", "b/2", "c/1"]  # 4 bytes each with the newline
    assert write_blocked(tmp_path / "x.txt", lines) == {"lines": 5, "bytes": 20, "blocks": 2}
    # block 0 spans a/1..b/1 (0–12 ≥ 10 → cut before b/2), block 1 the rest
    assert json.loads((tmp_path / "x.txt.idx.json").read_text()) == {"v": 1, "n": 5, "size": 20, "keys": ["a/1", "b/2"], "offsets": [0, 12]}
