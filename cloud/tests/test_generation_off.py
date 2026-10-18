"""Without the `generation` opt-in (every deployment but gcs), the files are today's, byte for byte: the per-scan sorts
`path-index` writes (attributed and plain), every file of an interval store built and appended over the shared fixture
(range files, deltas, served sorts), and every scan's export. The digests in `golden/generation-off.json` were taken
before the opt-in existed (specs/lean-scans.md §3.1); `UPDATE_GOLDEN=1` rewrites them — only for a deliberate change
to these files, never to make the opt-in's off path pass."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from click.testing import CliRunner
from test_interval_local import DATES, local, world  # noqa: F401 (fixtures)
from test_viz import IDENTITIES_YAML, attribution, listing  # noqa: F401 (fixtures)

from dt_cloud import interval_export as ie
from dt_cloud.viz import write_path_index

GOLDEN = Path(__file__).parent / "golden" / "generation-off.json"


def _digests(d: Path) -> dict[str, str]:
    return {p.relative_to(d).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.rglob("*.parquet"))}


def test_off_path_files_are_unchanged(tmp_path, listing, attribution, world, local):  # noqa: F811
    ident = tmp_path / "identities.yaml"
    ident.write_text(IDENTITIES_YAML)
    write_path_index((listing,), tmp_path / "o1", "2026-07-20", (attribution,), ident, path_index=tmp_path / "a" / "path-index.parquet")
    write_path_index((listing,), tmp_path / "o2", "2026-07-20", path_index=tmp_path / "p" / "path-index.parquet")
    for d in DATES:
        res = CliRunner().invoke(ie.export_cmd, ["-o", str(local["store"]), "-w", str(tmp_path / "w" / d), d, str(tmp_path / "x" / d)],
                                 catch_exceptions=False)
        assert res.exit_code == 0, res.output
    got = {"viz_attr": _digests(tmp_path / "a"), "viz_plain": _digests(tmp_path / "p"), "store": _digests(local["store"]),
           "export": _digests(tmp_path / "x")}
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    assert got == json.loads(GOLDEN.read_text())
