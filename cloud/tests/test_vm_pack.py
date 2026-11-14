"""`dt-cloud vm-pack` (specs/vm-mode.md §6): the mirror, the pack, the pointer, idempotence, pruning; and the
`vm-pack` scan step's command."""
import json
import os
import stat
from pathlib import Path

import pytest

from dt_cloud import vm_pack as V

GEN = "2026-10-09b.g3.c001"


def store(root: Path, manifests: dict[str, list[str]]) -> Path:
    """An interval store with `CURRENT.json` → GEN, its base sort, and `manifests` (`{name: [run keys]}`)."""
    g = root / "interval-store" / GEN
    (g / "served").mkdir(parents=True)
    (root / "interval-store/CURRENT.json").write_text(json.dumps({"gen": GEN}))
    (g / "scans.json").write_text("{}")
    (g / "served/path.parquet").write_text("base")
    (g / "manifests").mkdir()
    for name, runs in manifests.items():
        (g / "manifests" / name).write_text(json.dumps({"runs": [{"key": k} for k in runs]}))
        for k in runs:
            (g / k / "served").mkdir(parents=True, exist_ok=True)
            (g / k / "served/path.parquet").write_text(k)
    return root


@pytest.fixture
def fsb(tmp_path) -> str:
    """A stand-in `fsb pack-slim GEN -o FILE`: records the mirrored files, writes a pack naming them, prints its JSON."""
    p = tmp_path / "fsb"
    p.write_text("""#!/usr/bin/env python3
import json, sys
from pathlib import Path
gen, out = Path(sys.argv[2]), sys.argv[4]
files = sorted(str(f.relative_to(gen)) for f in gen.rglob("*") if f.is_file())
Path(out).write_text(json.dumps(files))
print(json.dumps({"bytes": 1, "scans": 3, "load": {"peak_rss_gb": 1.5}}))
""")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_build_mirrors_the_newest_manifest_and_publishes(tmp_path, fsb):
    root = store(tmp_path / "s", {"2026-11-12T0702.json": ["deltas/a"], "2026-11-13T1734.json": ["deltas/a"],
                                  "2026-11-13T1734.m001.json": ["deltas/a_b", "deltas/c"]})
    doc = V.build(str(root), tmp_path / "w", fsb)
    key = f"vm-pack/{GEN}/2026-11-13T1734.m001.fsb"
    assert {k: doc[k] for k in ("gen", "manifest", "key", "scans", "peak_rss_gb")} == {
        "gen": GEN, "manifest": "2026-11-13T1734.m001.json", "key": key, "scans": 3, "peak_rss_gb": 1.5}
    # Exactly what fsb reads: the newest manifest's runs, not the older manifests'.
    assert json.loads((root / key).read_text()) == [
        "deltas/a_b/served/path.parquet", "deltas/c/served/path.parquet", "manifests/2026-11-13T1734.m001.json", "scans.json", "served/path.parquet"]
    assert json.loads((root / V.POINTER).read_text()) == doc
    assert sorted(p.name for p in (tmp_path / "w").iterdir()) == ["gen"]
    # Again: the pointer already names this generation and manifest.
    assert V.build(str(root), tmp_path / "w", fsb) == {**doc, "skipped": True}


def test_build_prunes_beyond_keep(tmp_path, fsb):
    root = store(tmp_path / "s", {"2026-11-12T0702.json": []})
    m = root / "interval-store" / GEN / "manifests"
    keys = []
    for i, name in enumerate(["2026-11-13T0702.json", "2026-11-14T0702.json", "2026-11-15T0702.json"]):
        (m / name).write_text(json.dumps({"runs": []}))
        keys.append(V.build(str(root), tmp_path / "w", fsb, keep=2)["key"])
        os.utime(root / keys[-1], (1000 + i, 1000 + i))
    assert sorted(str(p.relative_to(root)) for p in (root / "vm-pack").rglob("*.fsb")) == keys[1:]


def test_build_without_a_generation(tmp_path, fsb):
    (tmp_path / "s").mkdir()
    with pytest.raises(FileNotFoundError) as e:
        V.build(str(tmp_path / "s"), tmp_path / "w", fsb)
    assert str(e.value) == f"{tmp_path}/s/interval-store: no CURRENT.json"
