#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyarrow"]
# ///
"""The static name index's fixture under the hex-run rule (`staticHex.test.ts`; specs/static-hex-runs.md): suffix
shards and a catalog in `static-names/gen.py`'s layout, built with the rule `{min: 16, tail: 8}` — suffix rows only at
the positions the rule keeps, catalog members and cells by `occurs` — and `catalog/meta.json` / `scans.json` recording
`hex_runs`. The rule itself is `cloud/src/dt_cloud/hex_runs.py` (loaded by path: one definition).

- `expected.json`: the brute-force first-hit oracle under the rule (a live slice whose lowercase name has an
  occurrence of the term and whose lowercase parent has none), per (term, date);
- `expected-full.json`: the same oracle with plain substring tests (the full index), so the test can show which terms
  the rule changes (only hex-affected ones) and which it leaves exactly as they were.

Names: a 64-hex hash; hex runs at a name's start, middle and end; runs of exactly 15, 16 and 17 digits; a digits-only
run; mixed case (lowercased first); a word glued to a hash (`…3f0data.json`); a directory whose hash holds a term (so its
child holding the term is a first hit); and plain names holding the same terms.

Regenerate: `site/functions/_lib/fixtures/static-hex/gen.py` (uv runs it).
"""
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("names_gen", HERE.parent / "static-names" / "gen.py")
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
hspec = importlib.util.spec_from_file_location("hex_runs", HERE.parents[4] / "cloud" / "src" / "dt_cloud" / "hex_runs.py")
h = importlib.util.module_from_spec(hspec)
sys.modules["hex_runs"] = h
hspec.loader.exec_module(h)

RULE = h.HexRule(16, 8)
OPEN, A, S, O = g.OPEN, g.A, g.S, g.O
D = g.D
H64 = "3f9a2b7c1d0e4f5a6b7c8d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c"
VERSIONS = [
    ("bkt-a", "", A, OPEN, 10_000, 100),
    ("bkt-a/objs", "", A, OPEN, 5_000, 50),
    (f"bkt-a/objs/{H64}", "", A, OPEN, 64, 1),                               # 64-hex: `7c1d0e4f` inside
    ("bkt-a/objs/0123cafe56789abcdef0123456789abc.bin", "", A, OPEN, 32, 1),  # `cafe` deep inside a run
    ("bkt-a/objs/0123456789abcdefcafe.bin", "", S, OPEN, 20, 1),             # run at the start; `cafe` ends it
    ("bkt-a/cafe-notes.txt", "", A, O, 7, 1),                                 # `cafe` outside any run
    ("bkt-a/logs/run-1234.log", "", A, OPEN, 9, 1),                          # `1234` outside any run
    ("bkt-b", "", A, OPEN, 20_000, 200),
    ("bkt-b/x/20261009123456789.json", "", A, OPEN, 17, 1),                  # digits only: a 17-run
    ("bkt-b/x/0123456789ABCDEF1234.parquet", "", S, OPEN, 21, 1),            # mixed case, run to the end
    ("bkt-b/x/obj_3f9a2b7c1d0e4f5a6b7c3f0data.json", "", A, OPEN, 33, 1),    # glued word
    ("bkt-b/x/data.json", "alice", A, S, 5, 1),
    ("bkt-b/y/q0123456789abcdeq.txt", "", A, OPEN, 15, 1),                    # 15 digits: no run
    ("bkt-b/y/q0123456789abcdefq.txt", "", A, OPEN, 16, 1),                   # 16
    ("bkt-b/y/q0123456789abcdef0q.txt", "", S, OPEN, 18, 1),                  # 17
    ("bkt-c", "", A, OPEN, 30_000, 300),
    ("bkt-c/deadbeef00cafe0011223344556677", "", A, OPEN, 400, 4),           # `cafe` inside the dir's hash…
    ("bkt-c/deadbeef00cafe0011223344556677/cafe.txt", "", A, OPEN, 40, 1),   # …so the child is a first hit
    ("bkt-c/deadbeef00cafe0011223344556677/1234.bin", "bob", A, OPEN, 41, 1),
]
TERMS = ["cafe", "1234", "5678", "7c1d0e4f", "3f9a", "data", ".json", "data.json", "obj_3f9a", "bkt", "txt",
         "c", "ca", "1", "q0"]


def name_of(path: str) -> str:
    return path.rsplit("/", 1)[-1].lower()


def suffix_rows() -> list[dict]:
    rows = []
    for path, usr, vf, vt, size, n in VERSIONS:
        name = name_of(path)
        for p in h.kept_positions(name, RULE):
            rows.append(dict(s=name[p:], depth=g.depth_of(path), path=path, usr=usr, vf=vf, vt=vt, size=size, n_files=n))
    return sorted(rows, key=lambda r: (r["s"], r["path"], r["usr"], r["vf"]))


def hit(term: str, path: str, rule) -> bool:
    parent, name = g.split(path)
    return h.occurs(term, name.lower(), rule) and not h.occurs(term, parent.lower(), rule)


def oracle(term: str, day, rule) -> dict:
    totals: dict[str, list[int]] = {}
    for path, usr, vf, vt, size, n in VERSIONS:
        if not (vf <= day < vt) or not hit(term, path, rule):
            continue
        t = totals.setdefault(path.split("/", 1)[0], [0, 0])
        t[0] += size
        t[1] += n
    return dict(sorted(totals.items()))


def main() -> None:
    # The base writers, pointed at this fixture's versions and the rule's membership and hits.
    g.VERSIONS = VERSIONS
    g.substrings = lambda name, n: {name[p:p + n] for p in range(len(name) - n + 1) if not h.dropped(name, p, n, RULE)}
    base_cells = g.cells
    g.cells = lambda q, rows: base_cells_rule(q, rows)
    rows = suffix_rows()
    lo = [r for r in rows if r["s"][:3] < g.SPLIT]
    hi = [r for r in rows if r["s"][:3] >= g.SPLIT]
    g.write(lo, HERE / "sx" / "s0000.parquet")
    g.write(hi, HERE / "sx" / "s0001.parquet")
    shards = {"shards": [{"i": 0, "lo": "   ", "hi": g.SPLIT, "rows": len(lo), "prefixes": len({r["s"][:3] for r in lo})},
                         {"i": 1, "lo": g.SPLIT, "hi": None, "rows": len(hi), "prefixes": len({r["s"][:3] for r in hi})}]}
    (HERE / "shards.json").write_text(json.dumps(shards, indent=1) + "\n")
    (HERE / "scans.json").write_text(json.dumps({"bucket": "fixture", "scans": [{"id": d} for d in D], **h.rule_json(RULE)}, indent=1) + "\n")
    mem = g.write_catalog(rows, HERE / "catalog")
    meta = json.loads((HERE / "catalog" / "meta.json").read_text())
    (HERE / "catalog" / "meta.json").write_text(json.dumps({**meta, **h.rule_json(RULE)}, indent=1) + "\n")
    (HERE / "members.json").write_text(json.dumps({t: mem.get(t) for t in TERMS}, indent=1) + "\n")
    for name, rule in (("expected.json", RULE), ("expected-full.json", None)):
        (HERE / name).write_text(json.dumps({t: {d: oracle(t, day, rule) for d, day in D.items()} for t in TERMS}, indent=1) + "\n")
    print(f"{len(lo)} + {len(hi)} suffix rows; {len(mem)} catalog members")


def base_cells_rule(q: str, rows: int) -> list[dict]:
    """`static-names/gen.py`'s `cells`, with its first-hit test under the rule."""
    ev: dict[tuple[str, int], list[int]] = {}
    for path, usr, vf, vt, size, n in VERSIONS:
        if not hit(q, path, RULE):
            continue
        bucket = path.split("/", 1)[0]
        for t, sign in ((vf, 1), (vt, -1)):
            if t == OPEN:
                continue
            e = ev.setdefault((bucket, g.epoch(t)), [0, 0])
            e[0] += sign * size
            e[1] += sign * n
    body, run = [], {}
    for (bucket, t), (db, dn) in sorted(ev.items()):
        if db == 0 and dn == 0:
            continue
        b, o = run.get(bucket, (0, 0))
        run[bucket] = (b + db, o + dn)
        body.append(dict(q=q, bucket=bucket, vf=t, b=b + db, o=o + dn))
    return [dict(q=q, bucket="", vf=0, b=rows, o=len(body))] + body


if __name__ == "__main__":
    main()
