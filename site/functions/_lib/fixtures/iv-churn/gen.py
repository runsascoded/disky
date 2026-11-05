"""The churn-carrying interval store fixture (specs/diff-progressive.md §3) for `diffLevel.test.ts`.

Seven synthetic scans (two v1 indexes, then five v2 with files: some missing, some resized, `b2/z`'s two files trading
sizes) from `cloud/tests/test_interval_churn.py`'s generator, under `interval-store/g/` here: a base over the first four
(`interval-store build`, 4 rows per group) and the other three appended as runs (`interval-store add`). Plus
`expected.json`: per `(from, to, path)` with both scans at or after the churn base (the first v2 scan), the level answer
computed from the scans' files alone: each child that changed (its range churn, bytes and objects at both scans), and
the path's own.

With `PAGES` (an int), the same store cut in `PAGES`-row data pages with a page index, under `interval-store-p<PAGES>/`
(2b.1's page reads; `diffLevel.test.ts` checks both answer `expected.json`). `interval-store/` was cut before served
sorts carried a page index, and stays so: the reader's whole-group path for older generations.

With `ROOT` (a top-level name, `b1` or `b2`), a store of only the files under it, under `interval-store-<ROOT>/`, and no
`expected.json`: two of them, one per root, are a union view's members (`unionLevel.test.ts`), whose union answers as
the whole store does.

With `level`, the same store built with the level sort (`build -l`; its runs carry it), in 2-row pages, under
`interval-store-level/` (`/api/diff/level`'s level index, specs/diff-progressive.md §3b); `expected.json` is checked, not
rewritten.

With `slash`, that store under `interval-store-slash/`, plus one key with a doubled slash from scan `SLASH_FROM` on,
`SLASH_KEY`: as `path-index` lists it, a directory `b1/x/` under `b1/x` (name '', as `b1/x`'s own rows are). No
`expected.json`: `diffLevel.test.ts` compares it with the store's own `path` sort.

    cloud/.venv-or-root/bin/python site/functions/_lib/fixtures/iv-churn/gen.py [PAGES | ROOT | level | slash]
"""
from __future__ import annotations

import importlib.util
import json
import random
import shutil
import sys
from pathlib import Path

from click.testing import CliRunner

HERE = Path(__file__).parent
ROOT = HERE.parents[4]
LAYOUT = "listing/{id}/path-index.parquet"
BASE = 4
PATHS = ["", "b1", "b1/x", "b1/x/y", "b2", "b2/z"]
SLASH_KEY = "b1/x//k.bin"
SLASH_FROM = 5


def _load(name: str):
    sys.path.insert(0, str(ROOT / "cloud/tests"))
    spec = importlib.util.spec_from_file_location(name, ROOT / f"cloud/tests/{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main(pages: int | None = None, root: str | None = None, with_level: bool = False, slash: bool = False) -> None:
    from dt_cloud import interval_local as il
    from dt_cloud import interval_store as ist
    from dt_cloud import static_names as sn

    if pages:
        ist.SERVED_PAGE_ROWS = pages

    t = _load("test_interval_churn")
    work = HERE / "work"
    shutil.rmtree(work, ignore_errors=True)
    rng = random.Random(5)
    scans, files = [], []
    for j, d in enumerate(t.DATES):
        v = 2 if j >= t.V2_FROM else 1
        fs = t._files(rng, j)
        if root:
            fs = {f: b for f, b in fs.items() if f.startswith(f"{root}/")}
        if slash and j >= SLASH_FROM:
            fs[SLASH_KEY] = 7
        key = LAYOUT.replace("{id}", d)
        (work / "scans" / key).parent.mkdir(parents=True, exist_ok=True)
        t._write(t._rows(fs, v), work / "scans" / key, v)
        scans.append({"id": d, "ts": sn.scan_epoch(d)})
        files.append(fs)
    common = ["-g", "g", "-L", LAYOUT, "-s", str(work / "scans"), "-o", str(work / "store"), "-r", "4", "-p", "2", "-M", "1GB"]
    build = ["-w", str(work / "build"), "-k", "3", "-t", t.DATES[BASE - 1], *(["-l"] if with_level else [])]
    for cmd, extra in [(il.build_cmd, build), (il.add_cmd, ["-w", str(work / "add")])]:
        res = CliRunner().invoke(cmd, [*common, *extra], catch_exceptions=False)
        assert res.exit_code == 0, res.output
    out = HERE / ("interval-store-slash" if slash else "interval-store-level" if with_level else f"interval-store-p{pages}" if pages else f"interval-store-{root}" if root else "interval-store")
    shutil.rmtree(out, ignore_errors=True)
    # What the Worker reads: the served sorts, `scans.json`, the manifests and the runs' `meta.json` / `scans.json`.
    shutil.copytree(work / "store" / ist.PREFIX / "g", out / "g", ignore=shutil.ignore_patterns("verify", "state", "lease", "pv", "pvl", "rd", "ranges", "digest"))
    for f in [out / "g" / "scans.json", *(out / "g" / "deltas").glob("*/scans.json")]:
        doc = json.loads(f.read_text())
        f.write_text(json.dumps({**doc, "bucket": "b"}, indent=1) + "\n")
    if root or slash:
        shutil.rmtree(work)
        return
    expected = []
    base = t.V2_FROM
    for i in range(base, len(scans)):
        for j in range(i + 1, len(scans)):
            for p in PATHS:
                a = level(files, i, j, p)
                if a is not None:
                    expected.append({"from": scans[i]["id"], "to": scans[j]["id"], **a})
    text = json.dumps(expected, indent=1, sort_keys=True) + "\n"
    if with_level:
        assert (HERE / "expected.json").read_text() == text, "the level fixture's scans differ from expected.json's"
    else:
        (HERE / "expected.json").write_text(text)
    shutil.rmtree(work)
    print(f"{len(expected)} level answers", file=sys.stderr)


def _under(f: str, p: str) -> str | None:
    """The child of `p` that file `f` is under (or is), else None."""
    if p and not f.startswith(p + "/"):
        return None
    rest = f[len(p) + 1:] if p else f
    head = rest.split("/", 1)[0]
    return f"{p}/{head}" if p else head


def level(files: list[dict[str, int]], i: int, j: int, p: str) -> dict | None:
    """`p`'s level answer between scans `i` and `j` from the files alone (None: `p` absent at both)."""
    def agg(fs: dict[str, int]) -> dict[str, list[int]]:
        out: dict[str, list[int]] = {}
        for f, b in fs.items():
            c = _under(f, p)
            if c is not None:
                out.setdefault(c, [0, 0])
                out[c][0] += b
                out[c][1] += 1
        return out

    A, B = agg(files[i]), agg(files[j])
    if not A and not B:
        return None
    churn: dict[str, int] = {}
    for s in range(i + 1, j + 1):
        for f in set(files[s - 1]) | set(files[s]):
            c = _under(f, p)
            if c is not None:
                churn[c] = churn.get(c, 0) + abs(files[s].get(f, 0) - files[s - 1].get(f, 0))
    # A file under any scan of the range (one that came and went is in neither end's).
    kinds = {c: "file" if any(c in files[s] for s in range(i, j + 1)) else "dir" for c in {*A, *B, *churn}}
    rows = []
    for c in sorted({*A, *B, *churn}):
        a, b = A.get(c, [0, 0]), B.get(c, [0, 0])
        ch = churn.get(c, 0)
        if ch == 0 and a == b:
            continue
        rows.append({"p": c, "k": kinds.get(c, "dir"), "a": a[0], "b": b[0], "oa": a[1], "ob": b[1], "churn": ch})
    tot = lambda m: [sum(v[0] for v in m.values()), sum(v[1] for v in m.values())]  # noqa: E731
    (ta, toa), (tb, tob) = tot(A), tot(B)
    return {"path": p, "a": ta, "b": tb, "oa": toa, "ob": tob, "churn": sum(churn.values()), "rows": rows}


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg == "level":
        main(2, with_level=True)
    elif arg == "slash":
        main(2, with_level=True, slash=True)
    else:
        main(int(arg) if arg and arg.isdigit() else None, arg if arg and not arg.isdigit() else None)
