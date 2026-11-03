#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyarrow"]
# ///
"""`static-names`' suffix rows cut at finer shard boundaries (`staticNames.test.ts`): boundaries that split the
three-character prefixes starting with a one- or two-character key (`f`, `o`, `oo`) across several shards, so a
short key's rows lie in more than one shard file.

- `sx/s####.parquet`: one per `[lo, hi)` of `BOUNDS`, the rows whose first three characters fall in it (the
  `static-names` writer's settings: sorted, `RG`-row groups, zstd, statistics on).
- `shards.json`: the plan (`i, lo, hi, rows, prefixes`).

Regenerate: `site/functions/_lib/fixtures/static-names-split/gen.py` (uv runs it).
"""
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("names_gen", HERE.parent / "static-names" / "gen.py")
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)

#: Shard `i` holds the rows whose first three characters are in `[BOUNDS[i], BOUNDS[i + 1])` (the last: unbounded).
BOUNDS = ["   ", "f", "fo", "g", "oo", "oof", "p"]
#: Keys shorter than the prefix width whose rows the boundaries split.
SPLIT_KEYS = ["f", "o", "oo"]


def main() -> None:
    rows = g.suffix_rows()
    plan = []
    for i, lo in enumerate(BOUNDS):
        hi = BOUNDS[i + 1] if i + 1 < len(BOUNDS) else None
        part = [r for r in rows if lo <= r["s"][:3] and (hi is None or r["s"][:3] < hi)]
        g.write(part, HERE / "sx" / f"s{i:04d}.parquet")
        plan.append({"i": i, "lo": lo, "hi": hi, "rows": len(part), "prefixes": len({r["s"][:3] for r in part})})
    assert sum(p["rows"] for p in plan) == len(rows)
    for k in SPLIT_KEYS:
        held = [p["i"] for p in plan if any(r["s"].startswith(k) and p["lo"] <= r["s"][:3] and (p["hi"] is None or r["s"][:3] < p["hi"]) for r in rows)]
        assert len(held) > 1, (k, held)
    (HERE / "shards.json").write_text(json.dumps({"shards": plan}, indent=1) + "\n")
    print(f"{len(rows)} suffix rows in {len(plan)} shards")


if __name__ == "__main__":
    main()
