#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.1", "click>=8"]
# ///
"""The full-scan bench's oracle: every query answered by plain DuckDB SQL over the same generation's rows, written
without the scanner's code, and compared with the scanner's answers exactly (same nodes, bytes, counts, remainders).

The semantics are `src/query.rs`'s doc comment. Versions are combined as the reader's `combineLive`: one row per
`(depth, path, vf)` over the base's and every listed run's `path` sort, the smallest `vt`.
"""
import json
import re
import sys
from functools import partial
from pathlib import Path
from time import monotonic

import click
import duckdb

err = partial(print, file=sys.stderr)
OPEN = 4291747200
MANIFEST = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{4})?(\.m\d{3})?\.json$")


def generation(gen: Path) -> tuple[list[dict], list[str]]:
    """`(scans, path sorts)`: `scans.json`'s stamped scans then the newest manifest's run scans, and the base's `path`
    sort then each listed run's."""
    scans = [s for s in json.loads((gen / "scans.json").read_text())["scans"] if s.get("ts") is not None]
    files = [str(gen / "served/path.parquet")]
    names = sorted(p.name for p in (gen / "manifests").glob("*.json") if MANIFEST.match(p.name)) if (gen / "manifests").is_dir() else []
    if names:
        m = json.loads((gen / "manifests" / names[-1]).read_text())
        have = {s["id"] for s in scans}
        scans += [{"id": i, "ts": m["stamps"][i]} for i in m["scans"] if i not in have]
        files += [str(gen / r["key"] / "served/path.parquet") for r in m["runs"]]
    return scans, files


def lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def anchors(t: str) -> tuple[str, bool, bool]:
    a = t.startswith("^")
    t = t[1:] if a else t
    z = t.endswith("$")
    return (t[:-1] if z else t), a, z


def search_sql(e: dict) -> str:
    """A file passes when … (over `segs`, its lowercase segments, and `lp`, its lowercase path)."""
    (k, v), = e.items()
    if k == "and":
        return "(" + " AND ".join(search_sql(x) for x in v) + ")"
    if k == "or":
        return "(" + " OR ".join(search_sql(x) for x in v) + ")"
    if k == "not":
        return f"(NOT {search_sql(v)})"
    if k == "re":
        return f"len(list_filter(segs, s -> regexp_matches(s, {lit(v)}))) > 0"
    body, a, z = anchors(v.lower())
    if k == "p" and "/" in body:
        hay = ("'/' || " if a else "") + "lp" + (" || '/'" if z else "")
        return f"contains({hay}, {lit(('/' if a else '') + body + ('/' if z else ''))})"
    pred = {(False, False): "contains(s, {})", (True, False): "starts_with(s, {})", (False, True): "ends_with(s, {})", (True, True): "s = {}"}[(a, z)]
    return f"len(list_filter(segs, s -> {pred.format(lit(body))})) > 0"


def owner_cte(sel: dict) -> str:
    """Per live file (`key`): its owner slices (`''` → unnamed; a name → all bytes; JSON → each entry, clamped at 0,
    plus the unnamed rest when positive), and the selected ones' bytes."""
    (mode, names), = sel.items()
    inlist = "[" + ", ".join(lit(n) for n in names) + "]::VARCHAR[]"
    test = f"list_contains({inlist}, u)" if mode == "in" else f"NOT list_contains({inlist}, u)"
    return f"""js AS (
        SELECT key, size, us, CASE WHEN starts_with(us, '[') THEN from_json(us, '[["VARCHAR"]]') END AS j FROM live),
    sl AS (
        SELECT key, '' AS u, size AS b FROM js WHERE us = ''
        UNION ALL SELECT key, us, size FROM js WHERE us <> '' AND NOT starts_with(us, '[')
        UNION ALL SELECT key, e[1], greatest(e[2]::BIGINT, 0) FROM (SELECT key, unnest(j) AS e FROM js WHERE j IS NOT NULL)
        UNION ALL SELECT key, '', size - n FROM (
            SELECT key, size, list_sum(list_transform(j, e -> greatest(e[2]::BIGINT, 0))) AS n FROM js WHERE j IS NOT NULL) WHERE size > n),
    own AS (
        SELECT key, coalesce(sum(b) FILTER (WHERE {test}), 0)::BIGINT AS own_sel, count(*) FILTER (WHERE {test}) > 0 AS own_pass,
            count(*) AS n_own
        FROM sl GROUP BY key)"""


def class_cols(cls: list[int]) -> str:
    """Per live file: class slices `c2..c4` where positive, class 1 the rest when positive (neither: class 1, 0 B)."""
    c1 = "(size - greatest(c2, 0) - greatest(c3, 0) - greatest(c4, 0))"
    has = {1: f"({c1} > 0 OR (c2 <= 0 AND c3 <= 0 AND c4 <= 0 AND {c1} <= 0))", 2: "(c2 > 0)", 3: "(c3 > 0)", 4: "(c4 > 0)"}
    byt = {1: f"greatest({c1}, 0)", 2: "greatest(c2, 0)", 3: "greatest(c3, 0)", 4: "greatest(c4, 0)"}
    sel = " + ".join(f"CASE WHEN {has[k]} THEN {byt[k]} ELSE 0 END" for k in cls) or "0"
    ok = " OR ".join(has[k] for k in cls) or "false"
    n = " + ".join(f"{has[k]}::INT" for k in (1, 2, 3, 4))
    return f"({sel})::BIGINT AS cls_sel, ({ok}) AS cls_pass, ({n}) AS n_cls"


def answer(con, q: dict, scans: dict[str, int]) -> dict:
    p = q.get("path", "")
    dp = 0 if p == "" else p.count("/") + 1
    K = q["depth"]
    if "scan" in q:
        ta, tb = scans[q["scan"]], -1
    else:
        ta, tb = scans[q["from"]], scans[q["to"]]
    where = [f"((vf <= {ta} AND {ta} < vt) OR (vf <= {tb} AND {tb} < vt))"]
    if p:
        where.append(f"starts_with(path, {lit(p + '/')})")
    if q.get("staged") is not None:
        ors = " OR ".join(f"path = {lit(s)} OR starts_with(path, {lit(s + '/')})" for s in q["staged"]) or "false"
        where.append(f"({ors})")
    owners, classes = q.get("owners"), q.get("classes")
    ctes = [f"""live AS (
        SELECT row_number() OVER () AS key, path, depth, size, c2, c3, c4, us,
            (vf <= {ta} AND {ta} < vt) AS la, (vf <= {tb} AND {tb} < vt) AS lb,
            string_split(lower(path), '/') AS segs, lower(path) AS lp
        FROM f WHERE {' AND '.join(where)})"""]
    if owners:
        ctes.append(owner_cte(owners))
    cls_sql = class_cols(classes) if classes else "size::BIGINT AS cls_sel, true AS cls_pass, 1 AS n_cls"
    join = "LEFT JOIN own USING (key)" if owners else ""
    own_cols = "own_sel, own_pass, n_own" if owners else "size::BIGINT AS own_sel, true AS own_pass, 1 AS n_own"
    if owners and classes:
        b = """CASE WHEN n_cls = 1 THEN CASE WHEN cls_pass THEN own_sel ELSE 0 END
                    WHEN n_own = 1 THEN CASE WHEN own_pass THEN cls_sel ELSE 0 END
                    ELSE error('several owner and class slices') END"""
    elif owners:
        b = "own_sel"
    elif classes:
        b = "cls_sel"
    else:
        b = "size"
    srch = search_sql(q["q"]) if q.get("q") else "true"
    ctes.append(f"""m AS (
        SELECT path, depth, la, lb, {own_cols}, {cls_sql} FROM live {join} WHERE {srch})""")
    ctes.append(f"""sel AS (SELECT path, depth, la, lb, ({b})::BIGINT AS b FROM m WHERE own_pass AND cls_pass)""")
    base = "WITH " + ",\n".join(ctes)
    tot = con.execute(f"""{base}
        SELECT coalesce(sum(b) FILTER (WHERE la), 0), count(*) FILTER (WHERE la),
               coalesce(sum(b) FILTER (WHERE lb), 0), count(*) FILTER (WHERE lb) FROM sel""").fetchone()
    tot = [int(x) for x in tot]
    rows = con.execute(f"""{base},
        nodes AS (
            SELECT array_to_string(string_split(path, '/')[1:{dp} + k], '/') AS node, k, la, lb, b
            FROM sel, range(1, {K + 1}) t(k) WHERE k <= depth - {dp})
        SELECT node, k, coalesce(sum(b) FILTER (WHERE la), 0), count(*) FILTER (WHERE la),
               coalesce(sum(b) FILTER (WHERE lb), 0), count(*) FILTER (WHERE lb)
        FROM nodes GROUP BY node, k
        HAVING greatest(coalesce(sum(b) FILTER (WHERE la), 0), coalesce(sum(b) FILTER (WHERE lb), 0))::HUGEINT * 1000000
               >= {q['min_ppm']}::HUGEINT * {max(tot[0], tot[2])}""").fetchall()
    kept = {r[0]: (r[1], [int(x) for x in r[2:]]) for r in rows}
    nodes = sorted([n, *v[1]] for n, v in kept.items())
    # Each kept node above the deepest level, and the folder itself: what its kept children leave.
    kids: dict[str, list[int]] = {}
    for n, (k, v) in kept.items():
        par = n.rsplit("/", 1)[0] if k > 1 else p
        acc = kids.setdefault(par, [0, 0, 0, 0])
        for i in range(4):
            acc[i] += v[i]
    rest = []
    for n, (k, v) in [(p, (0, tot)), *kept.items()]:
        if k < K:
            c = kids.get(n, [0, 0, 0, 0])
            r = [v[i] - c[i] for i in range(4)]
            if any(r):
                rest.append([n, *r])
    rest.sort()
    return {"id": q["id"], "total": tot, "nodes": nodes, "rest": rest}


@click.command()
@click.option("-c", "--cache", help="DuckDB database to keep the combined object versions in (built once)")
@click.option("-i", "--only", help="Comma-separated query ids to check")
@click.option("-m", "--memory", default="48GB", help="DuckDB memory limit")
@click.option("-o", "--out", help="Write the oracle's answers here (JSONL)")
@click.option("-t", "--threads", default=16, type=int, help="DuckDB threads")
@click.option("-T", "--tmp", default="tmp/duckdb", help="DuckDB spill dir")
@click.argument("gen")
@click.argument("queries")
@click.argument("answers")
def main(cache, only, memory, out, threads, tmp, gen, queries, answers):
    """Check the scanner's ANSWERS (JSONL) for QUERIES (JSONL) against SQL over generation GEN; exit 1 on any
    difference."""
    gen = Path(gen)
    scans, files = generation(gen)
    stamps = {s["id"]: s["ts"] for s in scans}
    con = duckdb.connect(cache or ":memory:")
    con.execute(f"SET memory_limit = {lit(memory)}; SET threads = {threads}; SET temp_directory = {lit(tmp)}; SET preserve_insertion_order = false")
    t0 = monotonic()
    have = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'f'").fetchone()[0]
    if not have:
        src = "[" + ", ".join(lit(f) for f in files) + "]"
        con.execute(f"""CREATE TABLE f AS
            SELECT path, any_value(depth) AS depth, vf, min(vt) AS vt, any_value(size) AS size, any_value(c2) AS c2,
                   any_value(c3) AS c3, any_value(c4) AS c4, any_value(us) AS us
            FROM read_parquet({src}, union_by_name = true)
            GROUP BY depth, path, vf HAVING any_value(kind) = 'file'""")
    n = con.execute("SELECT count(*) FROM f").fetchone()[0]
    err(f"oracle: {n:,} object versions from {len(files)} sorts, {len(scans)} scans ({monotonic() - t0:.1f}s)")
    qs = [json.loads(l) for l in Path(queries).read_text().splitlines() if l.strip()]
    got = {a["id"]: a for a in (json.loads(l) for l in Path(answers).read_text().splitlines() if l.strip())}
    ids = set(only.split(",")) if only else None
    bad = []
    outf = open(out, "w") if out else None
    for q in qs:
        if ids and q["id"] not in ids:
            continue
        t1 = monotonic()
        want = answer(con, q, stamps)
        have = got.get(q["id"])
        ok = have == want
        if outf:
            outf.write(json.dumps(want) + "\n")
        err(f"{'ok  ' if ok else 'DIFF'} {q['id']}: total {want['total']}, {len(want['nodes'])} nodes, {len(want['rest'])} rests ({monotonic() - t1:.1f}s)")
        if not ok:
            bad.append(q["id"])
            if have is None:
                err("  no scanner answer")
            else:
                for k in ("total", "nodes", "rest"):
                    if have[k] != want[k]:
                        a = {tuple(x) if isinstance(x, list) else x for x in (have[k] if k != "total" else [tuple(have[k])])}
                        b = {tuple(x) if isinstance(x, list) else x for x in (want[k] if k != "total" else [tuple(want[k])])}
                        err(f"  {k}: scanner-only {sorted(a - b)[:5]}, oracle-only {sorted(b - a)[:5]}")
    checked = len([q for q in qs if not ids or q["id"] in ids])
    print(json.dumps({"checked": checked, "equal": checked - len(bad), "different": bad}))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
