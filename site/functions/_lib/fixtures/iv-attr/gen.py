"""The attribution-table fixture (specs/attr-interval-table.md) for `intervalAttribution.test.ts`.

`cloud/tests/test_interval_attribution.py`'s eight scans (two uncovered: no map; one whose map is empty) built into two
generations of one interval store under `interval-store/` here, by `dt-cloud interval-store build` / `add` at 4 rows per
group, keeping only what the site reads (`scans.json`, `served/`, `manifests/`, each run's `served/`):

- `g`: a base built with the table (`build -A`, four scans), the other four added as level-0 runs (`add -N`: no carry;
  the uncovered scan's run holds no attribution sort);
- `o`: a base without the table, the four added with `-A` (the table opens at the first run; the carry merges them).

Plus each covered scan's per-scan sidecar (`listing/<id>/index/g1/attr.tsv` + `.idx.json`) and `expected.json`: per
scan its map's rows `[path, user, source, evidence]`, null when uncovered.

    .venv/bin/python site/functions/_lib/fixtures/iv-attr/gen.py
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

from click.testing import CliRunner

HERE = Path(__file__).parent
ROOT = HERE.parents[4]


def main() -> None:
    sys.path.insert(0, str(ROOT / "cloud/tests"))
    spec = importlib.util.spec_from_file_location("tia", ROOT / "cloud/tests/test_interval_attribution.py")
    t = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(t)
    from dt_cloud import interval_local as il

    work = HERE / "work"
    shutil.rmtree(work, ignore_errors=True)
    scans = t.write_world(work / "root", sidecars=True)
    store = work / "store"
    for gen, b, a in (("g", ["-A"], ["-A", "-N"]), ("o", [], ["-A"])):
        args = ["-g", gen, "-L", t.LAYOUT, "-s", str(work / "root"), "-o", str(store), "-k", "3", "-r", "4", "-p", "2", "-M", "1GB"]
        res = CliRunner().invoke(il.build_cmd, [*args, "-w", str(work / f"b-{gen}"), "-t", t.DATES[t.BASE - 1], *b], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        res = CliRunner().invoke(il.add_cmd, [*[x for x in args if x not in ("-k", "3")], "-w", str(work / f"a-{gen}"), *a], catch_exceptions=False)
        assert res.exit_code == 0, res.output
    out = HERE / "interval-store"
    shutil.rmtree(out, ignore_errors=True)
    for p in sorted((store / "interval-store").rglob("*")):
        rel = p.relative_to(store / "interval-store")
        parts = rel.parts
        # the generation's `scans.json`, its manifests, and the attribution sorts (base and runs) with their group indexes
        if p.is_file() and len(parts) > 1 and (parts[1:] == ("scans.json",) or parts[1] == "manifests"
                                               or (parts[-2] == "served" and p.name.startswith("attribution.") and p.suffix == ".parquet")):
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            if p.name == "scans.json":  # the build machine's work dir isn't the fixture's
                (out / rel).write_text(json.dumps({**json.loads(p.read_text()), "bucket": "b"}, indent=1) + "\n")
            else:
                shutil.copyfile(p, out / rel)
    listing = HERE / "listing"
    shutil.rmtree(listing, ignore_errors=True)
    expected = {}
    for j, s in enumerate(scans):
        d = s["id"]
        if d in t.UNCOVERED:
            expected[d] = None
            continue
        for name in ("attr.tsv", "attr.tsv.idx.json"):
            dst = listing / d / "index" / "g1" / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(work / "root" / "listing" / d / name, dst)
        expected[d] = [[r["path"], r["user"], r["source"], r["evidence"]] for r in t.map_rows(j)]
    (HERE / "expected.json").write_text(json.dumps({"scans": [s["id"] for s in scans], "maps": expected}, indent=1) + "\n")
    shutil.rmtree(work)


if __name__ == "__main__":
    main()
