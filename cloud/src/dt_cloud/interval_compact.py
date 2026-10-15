"""Compaction of the interval store into a new base generation, and the store's current-generation pointer
(specs/interval-store.md §2.7 "Compaction").

Runs pile up past the compaction level `L` (carries stop below it: `append_runner.plan_carries`), so read cost would grow
with the scans. Once the newest manifest's runs, carried, hold two runs at level `L − 1` (`append_runner.compaction_due`:
every 2^L scans past the base), `interval-store add` folds the base and every run into a **new generation**, the next
name in the lineage (`next_gen`: `<root>.c<NNN>`):

- **Range files** (`compact_range`), per key range of the old generation's `ranges.json` (kept): the base's `pvl/`
  (`sv/`, `svt/` with the owner axis) and every run's deltas, one row per version (`interval_append.IDENT`) with the
  smallest `vt` — the reader's `combineLive`, kept whole instead of filtered to what is open; `pv/` (path versions)
  and `rd/` (read-day versions) are the islands of those rows (contiguous `[vf, vt)` pieces with equal change columns,
  resp. equal `last_read` ≥ 0), as `build` versions the scans. Written as `build` writes them.
- **Served sorts** (`interval_store.cut_gen`): every sort, dyadic segments over every scan's stamp — what a fresh
  `interval-store build` over the same scans writes, byte for byte (`test_interval_compact.py`).
- `scans.json` (the base's, the run scans' entries from their `deltas/<id>/scans.json`, kept on folded runs by
  `interval_append.FOLDED_RETAIN`), and `compaction.json` (`from`, the runs folded).

Publishing, in order: the new generation's files (data, then `scans.json`), then `successor.json` in the old one (an
`add` naming it appends to its successor instead), then `interval-store/CURRENT.json` (when it named the old generation,
or nothing). The site reads `CURRENT.json` when `INTERVAL_STORE_GEN` is unset (`index.ts` `ivCurrentGen`, cached a
minute), so it follows the compaction with no config change; `INTERVAL_STORE_GEN` set pins a generation.

The old generation is kept `-G` seconds (the folded-run prune's grace, `append_runner.GRACE_S`) after its successor's
`compaction.json` was written, then deleted from the store by a later `add` (`prune_predecessors`), but for its
`successor.json` (the forwarding record).
"""
from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from time import monotonic

from . import append_runner as ar
from . import interval_append as ia
from . import interval_store as ist
from . import static_names as sn
from .append_runner import LocalRunStore, latest_key
from .blobstore import Store
from .static_names import err, q

PREFIX = ist.PREFIX
#: The store's current generation: `{"gen": …}` (what an unpinned reader serves, what `add` appends to by default).
CURRENT = f"{PREFIX}/CURRENT.json"
#: In a compacted generation: `{"gen": <its successor>}`; kept when the rest of it is pruned.
SUCCESSOR = "successor.json"
#: In a generation a compaction wrote: `{"from", "runs", "scans", "base_scans", "level"}`.
COMPACTION = "compaction.json"
GEN_SUFFIX = re.compile(r"(?P<root>.+)\.c(?P<n>\d{3,})")


def next_gen(gen: str) -> str:
    """The generation a compaction of `gen` writes: `<root>.c<NNN>`, NNN one past `gen`'s (`.c001` for a root)."""
    m = GEN_SUFFIX.fullmatch(gen)
    root, n = (m["root"], int(m["n"])) if m else (gen, 0)
    return f"{root}.c{n + 1:03d}"


# ── The pointer ────────────────────────────────────────────────────────────


def current(store: Store) -> str | None:
    """The store's current generation (`CURRENT.json`), or None."""
    return store.read_json(CURRENT)["gen"] if store.exists(CURRENT) else None


def set_current(store: Store, gen: str) -> None:
    """`CURRENT.json` → `gen` (one object written whole: readers see the old pointer or the new)."""
    store.write_text(CURRENT, json.dumps({"gen": gen}) + "\n")


def generations(store: Store) -> list[str]:
    """The store's generations: `interval-store/<gen>/scans.json` (a whole listing of the prefix: only when there is no
    pointer)."""
    return sorted({k.split("/")[1] for k in store.listing(PREFIX) if k.count("/") == 2 and k.endswith("/scans.json")})


def chain(store: Store, gen: str) -> list[str]:
    """`gen`, then each successor a compaction recorded (`successor.json`), to the newest."""
    out = [gen]
    while store.exists(f"{PREFIX}/{out[-1]}/{SUCCESSOR}"):
        nxt = store.read_json(f"{PREFIX}/{out[-1]}/{SUCCESSOR}")["gen"]
        if nxt in out:
            raise RuntimeError(f"{PREFIX}: successor cycle {out + [nxt]}")
        out.append(nxt)
    return out


def resolve(store: Store) -> tuple[str | None, str]:
    """The generation an unpinned `add` appends to, and how it was found: `CURRENT.json`'s (`"current"`), else the
    store's only generation (`"sole"`), else `(None, "none")` (or `"ambiguous"`: several, no pointer)."""
    if (cur := current(store)) is not None:
        return cur, "current"
    gens = generations(store)
    if len(gens) == 1:
        return gens[0], "sole"
    return None, "ambiguous" if gens else "none"


def resolve_gen(store: Store, gen: str | None, log: Callable[[str], None] = err) -> str:
    """The generation `add` appends to: `gen` (else `resolve`'s; the store's sole generation adopted as current), followed
    to its newest successor. A pointer naming a compacted generation (a compaction stopped between its successor record
    and the pointer) is moved to the successor."""
    cur = current(store)
    if gen is None:
        gen, how = resolve(store)
        if gen is None:
            raise SystemExit(f"{store.root}/{PREFIX}: no current generation ({how}: "
                             + ("name one with -g" if how == "ambiguous" else "build one with `interval-store build`") + ")")
        if how == "sole":
            log(f"{store.root}/{CURRENT}: none; adopting the store's only generation {gen}")
            set_current(store, gen)
            cur = gen
    gens = chain(store, gen)
    if len(gens) > 1:
        log(f"{gen} was compacted into {gens[-1]} ({' → '.join(gens)}): appending there")
    if cur is not None and cur != gens[-1] and cur in gens:
        log(f"{store.root}/{CURRENT}: {cur} → {gens[-1]} (its successor)")
        set_current(store, gens[-1])
    return gens[-1]


# ── Folding base + runs into range files ───────────────────────────────────


def _versions_sql(base: str, runs: list[str], t: str, name: str) -> str:
    """Key range `name`'s versions of table `t`: the base's range file and every run's delta, one row per version
    (`IDENT`, `usr` as '') with the smallest `vt`, in the base's columns (`interval_store.SUB_SCHEMA[t]`, `usr` '')."""
    cols = [("coalesce(usr, '') AS usr" if c == "usr" else c) for c in ist.SUB_SCHEMA[t].names]
    sel = ", ".join(cols)
    parts = [f"SELECT {sel} FROM read_parquet({q(f'{base}/{t}/{name}.parquet')})",
             *(f"SELECT {sel} FROM read_parquet({q(f'{r}/{t}/{name}.parquet')})" for r in runs)]
    ident = ", ".join(ia.IDENT[t])
    return f"""SELECT * FROM ({" UNION ALL ".join(parts)}) QUALIFY row_number() OVER (PARTITION BY {ident} ORDER BY vt) = 1"""


def _islands_sql(src: str, same: list[str], keep: list[str], where: str = "true") -> str:
    """`src`'s rows (one path's versions never overlap) merged where contiguous (`vt` = the next one's `vf`) with equal
    `same` columns: `depth, path, vf, vt` and `keep` (the first piece's)."""
    eq = " AND ".join(f"lag({c}) OVER w IS NOT DISTINCT FROM {c}" for c in same)
    first = ", ".join(f"arg_min({c}, vf) AS {c}" for c in keep)
    return f"""WITH m AS (SELECT *, CASE WHEN lag(vt) OVER w = vf AND {eq} THEN 0 ELSE 1 END AS __new
            FROM ({src}) WHERE {where} WINDOW w AS (PARTITION BY depth, path ORDER BY vf)),
        g AS (SELECT *, sum(__new) OVER (PARTITION BY depth, path ORDER BY vf ROWS UNBOUNDED PRECEDING) AS __run FROM m)
        SELECT depth, path, min(vf) AS vf, max(vt) AS vt, {first} FROM g GROUP BY depth, path, __run"""


def compact_range(con, base: str, runs: list[str], name: str, out: Path, sliced: bool = True) -> dict:
    """Key range `name` of the generation folded from the base under `base` and the runs `runs` (dirs, oldest first) into
    `out`'s range files, as `build` writes them: `pvl/`, `pv/` and `rd/` (the islands of `pvl`'s pieces), and with the
    owner axis `sv/` and `svt/`. Returns their row counts."""
    rf = lambda sub: out / sub / f"{name}.parquet"
    con.execute(f"CREATE OR REPLACE TABLE pvl AS {_versions_sql(base, runs, 'pvl', name)}")
    doc = {"pvl": sn.write_sorted(sn._batches(con, f"SELECT {', '.join(ist.PVL_SCHEMA.names)} FROM pvl ORDER BY depth, path, vf"), rf("pvl"),
                                  ist.PVL_SCHEMA, ist.RANGE_RG, dictionary=["kind", "us"])}
    # A path version is the run of its pieces with equal change columns (`dr` as `build` derives it); `wts` is carried
    # from the version's first scan, which every piece shares (`fold`).
    change = [c for c in ist.CHANGE_COLS if c != "dr"]
    pv_src = f"SELECT *, {ia.DR} AS dr FROM pvl"
    con.execute(f"CREATE OR REPLACE TABLE pv AS {_islands_sql(pv_src, ist.CHANGE_COLS, [*change, 'wts'])}")
    doc["pv"] = sn.write_sorted(sn._batches(con, f"SELECT {', '.join(ist.PV_COLS)} FROM pv ORDER BY depth, path, vf"), rf("pv"), ist.PV_SCHEMA,
                                ist.RANGE_RG, dictionary=["kind", "us"])
    con.execute(f"CREATE OR REPLACE TABLE rd AS {_islands_sql('SELECT * FROM pvl', ['last_read'], ['last_read'], 'last_read >= 0')}")
    doc["rd"] = sn.write_sorted(sn._batches(con, "SELECT depth, path, vf, vt, last_read FROM rd ORDER BY depth, path, vf"), rf("rd"),
                                ist.RD_SCHEMA, ist.RANGE_RG)
    con.execute("DROP TABLE pvl; DROP TABLE pv; DROP TABLE rd")
    if sliced:
        # `build -S`'s and `fold -S`'s statements, over the same relations (`sv` with `usr` '', `svt` with it NULL).
        con.execute(f"CREATE OR REPLACE TABLE sv AS {_versions_sql(base, runs, 'sv', name)}")
        sel = ", ".join("nullif(usr, '') AS usr" if c == "usr" else c for c in ist.SV_SCHEMA.names)
        doc["sv"] = sn.write_sorted(sn._batches(con, f"SELECT {sel} FROM sv ORDER BY depth, path, usr, vf"), rf("sv"), ist.SV_SCHEMA, ist.RANGE_RG,
                                    dictionary=["kind", "usr"])
        con.execute(f"CREATE OR REPLACE TABLE svt AS SELECT * REPLACE (nullif(usr, '') AS usr) FROM ({_versions_sql(base, runs, 'svt', name)})")
        doc["svt"] = sn.write_sorted(sn._batches(con, f"SELECT {', '.join(ist.SVT_SCHEMA.names)} FROM svt ORDER BY depth, path, usr NULLS FIRST, vf"),
                                     rf("svt"), ist.SVT_SCHEMA, ist.RANGE_RG, dictionary=["kind", "usr"])
        con.execute("DROP TABLE sv; DROP TABLE svt")
    return doc


def compact_gen(gen_dir: Path, out: Path, work: Path, *, gen: str, new_gen: str, level: int | None, rg_rows: int = ist.SERVED_RG,
                threads: int = 4, mem: str = "4GB") -> dict:
    """The generation `gen` (its base and the newest manifest's runs, in `gen_dir`) folded into the new generation
    `new_gen` in `out`: its range files (`compact_range`, the old `ranges.json`), served sorts and churn
    (`interval_store.cut_gen`), `scans.json` and `compaction.json`. Returns a summary."""
    t0 = monotonic()
    store = LocalRunStore(gen_dir, work / "lease", gen)
    base = store.read_json("scans.json")
    sliced = ist.sliced(base)
    newest = latest_key(store.keys("manifests/"))
    if newest is None:
        raise SystemExit(f"{gen}: no runs to compact")
    runs = store.read_json(newest)["runs"]
    docs = []
    for r in runs:
        for sid in r["scans"]:
            p = gen_dir / ar.run_key(sid, sid) / "scans.json"
            if not p.is_file():
                raise SystemExit(f"{gen}: {p.relative_to(gen_dir)} missing: scan {sid}'s entry for the compacted `scans.json`")
            docs.append(json.loads(p.read_text())["scans"][0])
    doc = {**{k: v for k, v in base.items() if k not in ("scans", "slices")}, "scans": [*base["scans"], *docs], "slices": sliced}
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copyfile(gen_dir / "ranges.json", out / "ranges.json")
    k = json.loads((out / "ranges.json").read_text())["k"]
    con = sn.connect(threads, mem, work / "duckdb-compact")
    rows: dict[str, int] = {}
    for i in range(k):
        for t, n in compact_range(con, str(gen_dir), [str(gen_dir / r["key"]) for r in runs], f"r{i:04d}", out, sliced).items():
            rows[t] = rows.get(t, 0) + n
    sorts, stats = ist.cut_gen(con, out, doc, rg_rows=rg_rows)
    (out / "scans.json").write_text(json.dumps(doc, indent=1) + "\n")
    comp = {"from": gen, "manifest": newest, "runs": [r["key"] for r in runs], "base_scans": len(base["scans"]), "scans": len(doc["scans"]),
            "level": level}
    (out / COMPACTION).write_text(json.dumps(comp, indent=1) + "\n")
    s = round(monotonic() - t0, 1)
    err(f"compact {gen} → {new_gen}: {len(runs)} runs and the base, {len(doc['scans'])} scans, {stats['versions']:,} versions in {s}s")
    return {"from": gen, "gen": new_gen, "runs": comp["runs"], "scans": len(doc["scans"]), "rows": rows, "sorts": sorts, "s": s}


def due(gen_dir: Path, level: int | None) -> bool:
    """Whether the generation in `gen_dir` (its newest manifest) is due a compaction at `level`."""
    keys = sorted(p.relative_to(gen_dir).as_posix() for p in (gen_dir / "manifests").glob("*.json")) if (gen_dir / "manifests").is_dir() else []
    newest = latest_key(keys)
    return newest is not None and ar.compaction_due(json.loads((gen_dir / newest).read_text())["runs"], level)


# ── Pruning compacted generations ──────────────────────────────────────────


def prune_predecessors(store: Store, gen: str, *, now: float, grace_s: float = ar.GRACE_S, dry_run: bool = False,
                       log: Callable[[str], None] = err) -> list[dict]:
    """Each generation `gen` was compacted from (its `compaction.json`'s `from`, and theirs while unpruned), deleted from
    `store` once its successor's `compaction.json` is `grace_s` old — but for its `successor.json` — unless the pointer
    names it. One record per predecessor still holding files: `{gen, objects, bytes, age_s, deleted}`."""
    # The chain first (a pruned generation's `compaction.json` goes with it), newest first, to the first pruned one.
    found = []
    g = gen
    while store.exists(key := f"{PREFIX}/{g}/{COMPACTION}"):
        old = store.read_json(key)["from"]
        files = {k: n for k, n in store.listing(f"{PREFIX}/{old}").items() if k != f"{PREFIX}/{old}/{SUCCESSOR}"}
        if not files:
            break
        found.append((g, old, store.mtimes(f"{PREFIX}/{g}")[key], files))
        g = old
    out = []
    cur = current(store)
    for g, old, mt, files in found:
        age = now - mt
        go = age >= grace_s and old != cur
        rec = {"gen": old, "objects": len(files), "bytes": sum(files.values()), "age_s": round(age), "deleted": 0}
        if go and not dry_run:
            rec["deleted"] = store.delete(sorted(files))
        log(f"prune {old} (compacted into {g} {round(age)}s ago): {len(files)} objects, {rec['bytes']:,} B "
            + ("deleted" if go and not dry_run else "would be deleted" if go else f"kept (grace {grace_s:.0f}s" + (", current" if old == cur else "") + ")"))
        out.append(rec)
    return out
