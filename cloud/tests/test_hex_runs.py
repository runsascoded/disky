"""The hex-run rule (specs/static-hex-runs.md): suffix starts, `occurs`, hex-affected literals, and the SQL forms equal
to the Python ones. `site/functions/_lib/fixtures/hex-runs/cases.json` is the shared table the TS `occurs` is tested
against (`hexRuns.test.ts`); this test checks it is exactly what the Python rule computes (regenerate with
`HEX_RUNS_UPDATE=1 pytest tests/test_hex_runs.py`)."""
import json
import os
import random
from pathlib import Path

import duckdb
import pytest

from dt_cloud.hex_runs import (
    HexRule, dropped, first_occurrence, first_sql, grams_sql, hex_affected, kept_positions, kept_sql, occurs, occurs_sql,
    parse_rule, rule_from_json,
)

R = HexRule(16, 8)
H64 = "3f9a2b7c1d0e4f5a6b7c8d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c"
CASES = Path(__file__).parents[2] / "site" / "functions" / "_lib" / "fixtures" / "hex-runs" / "cases.json"


def rng(a, b):
    return list(range(a, b))


#: (name, its suffix starts under `R`): runs at the start, middle and end, 15/16/17 digits, digits only, uppercase
#: (names are lowercased first), two runs, a glued word.
KEPT = [
    (H64, [0, *rng(56, 62)]),
    ("x" + "0123456789abcde" + "y", rng(0, 15)),                    # 15 hex digits: no run
    ("x" + "0123456789abcdef" + "y", [0, 1, *rng(9, 16)]),          # 16: inside 2..16, opaque 2..8, tail 9..16
    ("x" + "0123456789abcdef0" + "y", [0, 1, *rng(10, 17)]),        # 17
    ("0123456789abcdef.json", [0, *rng(8, 19)]),                    # at the start
    ("run-0123456789abcdef", [*rng(0, 5), *rng(12, 18)]),           # at the end
    ("20261009123456789", [0, *rng(9, 15)]),                        # digits only
    ("ab" + "0123456789abcdef" + "-" + "fedcba9876543210" + ".x",
     [0, *rng(10, 20), *rng(27, 35)]),                               # two runs (the first is ab0123…: 18 long)
    ("3f9a2b7c1d0e4f5a6b7c3f0data.json", [0, *rng(17, 30)]),        # glued: the run is …3f0da (25 long)
]


@pytest.mark.parametrize("name,kept", KEPT)
def test_kept_positions(name, kept):
    assert kept_positions(name.lower(), R) == kept


def test_kept_without_rule():
    assert kept_positions(H64, None) == rng(0, 62)


GLUED = "obj_3f9a2b7c1d0e4f5a6b7c8d9e0f1a2b3cdata.json"

#: (literal, string, first occurrence under `R`).
FIRST = [
    ("data", GLUED, 36),          # starts in the run's tail (…2b3cda), extends past it
    ("2b3c", GLUED, -1),          # wholly inside the tail
    ("7c1d", GLUED, -1),          # opaque
    ("3f9a", GLUED, 4),           # the run's start
    (".json", GLUED, 40),
    ("obj_3f9a", GLUED, 0),       # before the run
    ("cafe", "cafe", 0),          # no run: as today
    ("cafe", "x" + "0" * 20 + "cafe" + "0" * 20, -1),
    ("cafe", "cafe" + "0" * 20, 0),
    ("bed", "bed-" + "0" * 20 + "bed" + "0" * 20, 0),
    ("bed", "0" * 20 + "bed" + "0" * 20 + "-bed", 44),
    ("1234", "20261009123456789", -1),
    ("2026", "20261009123456789", 0),
    ("6789", "20261009123456789", -1),     # in the tail, but ends at the run's end (not past it)
    ("0123456789abcdef0", "x0123456789abcdef0y", 1),
    ("bcdef0y", "x0123456789abcdef0y", 12),  # tail start, extends past
    ("abcdef0y", "x0123456789abcdef0y", 11), # tail (after = 7), extends past
    ("89abcdef0y", "x0123456789abcdef0y", -1),  # opaque (after = 9 > 8), though it extends past
]


@pytest.mark.parametrize("lit,s,at", FIRST)
def test_first_occurrence(lit, s, at):
    assert first_occurrence(lit, s, R) == at
    assert occurs(lit, s, R) == (at >= 0)
    assert occurs(lit, s, None) == (lit in s)


def test_parent_occurs_per_segment():
    # `/` is not hex: runs never cross segments, so `occurs` on a whole parent path is `occurs` per segment.
    par = "bkt/" + H64 + "/x"
    assert [occurs(t, par, R) for t in ("bkt", "3f9a", "1d0e", "/x", "a2b3c")] == [True, True, False, True, False]


AFFECTED = [
    ("cafe", True), ("bad", True), ("1234", True), ("2024", True), ("deadbeef00", True), ("deadbeef0x", True),
    ("deadbeefx", False), ("data", False), (".json", False), ("checkpoint", False), ("Cafe", True), ("x", False), ("a", True),
]


@pytest.mark.parametrize("lit,want", AFFECTED)
def test_hex_affected(lit, want):
    assert hex_affected(lit, R) == want
    assert hex_affected(lit, None) is False


def test_parse_rule():
    assert [parse_rule("16,8"), parse_rule(" off "), rule_from_json(None), rule_from_json({"min": 16, "tail": 0})] == [
        R, None, None, HexRule(16, 0)]
    for bad in ("16", "", "16,x", "8,8"):
        with pytest.raises(ValueError):
            parse_rule(bad)


def _strings(n: int, seed: int) -> list[str]:
    r = random.Random(seed)
    alpha = "0123456789abcdef" * 3 + "xyz-._/"
    out = []
    for _ in range(n):
        s = "".join(r.choice(alpha) for _ in range(r.randint(0, 48)))
        if r.random() < 0.5:
            k = r.randint(0, len(s))
            s = s[:k] + "".join(r.choice("0123456789abcdef") for _ in range(r.randint(14, 40))) + s[k:]
        out.append(s)
    return out


@pytest.mark.parametrize("rule", [R, HexRule(16, 0), HexRule(4, 2), None])
def test_sql_equals_python(rule):
    """Every SQL form against the Python rule on random strings with runs around the threshold."""
    strs = _strings(400, 7)
    lits = ["0", "a", "ab", "0a", "x", "abc", "0-", "-0", "a0b1", "f.", "0000", "xyz"] + [s[3:7] for s in strs[:40] if len(s) > 8]
    con = duckdb.connect()
    con.execute("CREATE TABLE t (l VARCHAR)")
    con.executemany("INSERT INTO t VALUES (?)", [(s,) for s in strs])
    con.execute("CREATE TABLE q (x VARCHAR)")
    con.executemany("INSERT INTO q VALUES (?)", [(x,) for x in lits])
    kept = con.execute(f"""SELECT l, list(p - 1 ORDER BY p) FILTER (WHERE p IS NOT NULL) FROM (
        SELECT l, unnest(CASE WHEN length(l) >= 3 THEN generate_series(1, length(l) - 2) ELSE [NULL] END) AS p FROM t)
        WHERE p IS NULL OR {kept_sql('l', 'p', rule)} GROUP BY l""").fetchall()
    assert sorted((l, k or []) for l, k in kept if (k or [])) == sorted((s, kept_positions(s, rule)) for s in set(strs) if kept_positions(s, rule))
    got = con.execute(f"SELECT l, x, {first_sql('l', 'x', rule)} - 1, {occurs_sql('l', 'x', rule)} FROM t, q").fetchall()
    assert sorted(got) == sorted((s, x, first_occurrence(x, s, rule), occurs(x, s, rule)) for s in strs for x in lits)
    grams = con.execute(f"SELECT l, list_sort({grams_sql('l', rule)}) FROM t").fetchall()
    want = {s: sorted({s[p:p + m] for m in (1, 2) for p in range(len(s) - m + 1) if not dropped(s, p, m, rule)}) for s in strs}
    assert sorted(grams) == sorted((s, want[s]) for s in strs)


def cases() -> dict:
    """The shared table for the TS `occurs`: per rule, strings × literals → first occurrence, suffix starts, hex-affected."""
    strs = [n for n, _ in KEPT] + [GLUED, "bkt/" + H64 + "/x"] + [s for _, s, _ in FIRST] + _strings(20, 11)
    strs = list(dict.fromkeys(s.lower() for s in strs))
    lits = list(dict.fromkeys([t for t, _, _ in FIRST] + [t.lower() for t, _ in AFFECTED] + ["0", "ab", "a0b1", "/x"]))
    out = {}
    for name, rule in (("16,8", R), ("16,0", HexRule(16, 0)), ("off", None)):
        out[name] = {
            "kept": {s: kept_positions(s, rule) for s in strs},
            "first": [[x, s, first_occurrence(x, s, rule)] for s in strs for x in lits],
            "affected": {x: hex_affected(x, rule) for x in lits},
        }
    return out


def test_shared_cases_file():
    doc = cases()
    if os.environ.get("HEX_RUNS_UPDATE"):
        CASES.parent.mkdir(parents=True, exist_ok=True)
        CASES.write_text(json.dumps(doc, indent=None, separators=(",", ":")) + "\n")
    assert json.loads(CASES.read_text()) == doc
