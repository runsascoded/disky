"""Compaction of the interval store into a new base generation, and the store's current-generation pointer
(specs/interval-store.md §2.7 "Compaction").

Runs pile up past the compaction level `L` (carries stop below it: `append_runner.plan_carries`), so read cost would grow
with the scans. Once the compaction policy fires (`append_runner.compaction_why`; under a level L, once the newest
manifest's runs, carried, hold two runs at level `L − 1`: every 2^L scans past the base; `every` / `geometric`: by scan
age or count), or `add -K` forces it, `interval-store add` folds the base and every run into a **new generation**, the
next name in the lineage (`next_gen`: `<root>.c<NNN>`):

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
import shutil
from collections.abc import Iterable, Callable
from pathlib import Path
from threading import Lock
from time import monotonic

import pyarrow.parquet as pq

from . import append_runner as ar
from . import interval_append as ia
from .interval_churn import has_churn, with_churn
from . import interval_store as ist
from . import static_names as sn
from .append_runner import LocalRunStore, latest_key
from .blobstore import Store
from .static_names import err, q

PREFIX = ist.PREFIX
#: The store's current generation: `{"gen": …}` (what an unpinned reader serves, what `add` appends to).
CURRENT = f"{PREFIX}/{ar.CURRENT}"
SUCCESSOR = ar.SUCCESSOR
COMPACTION = ar.COMPACTION
GEN_SUFFIX = ar.GEN_SUFFIX
next_gen = ar.next_gen


# ── The pointer (the shared lineage helpers, `append_runner`, at this store's prefix) ──


def current(store: Store) -> str | None:
    """The store's current generation (`CURRENT.json`), or None."""
    return ar.current(store, PREFIX)


def set_current(store: Store, gen: str) -> None:
    """`CURRENT.json` → `gen` (one object written whole: readers see the old pointer or the new)."""
    ar.set_current(store, PREFIX, gen)


def generations(store: Store) -> list[str]:
    """The store's generations: `interval-store/<gen>/scans.json`."""
    return ar.generations(store, PREFIX)


def chain(store: Store, gen: str) -> list[str]:
    """`gen`, then each successor a compaction recorded (`successor.json`), to the newest."""
    return ar.chain(store, PREFIX, gen)


def resolve(store: Store) -> tuple[str | None, str]:
    """`append_runner.resolve` over this store."""
    return ar.resolve(store, PREFIX)


def resolve_gen(store: Store, gen: str | None, log: Callable[[str], None] = err) -> str:
    """`append_runner.resolve_gen` over this store."""
    return ar.resolve_gen(store, PREFIX, gen, log, build_hint="build one with `interval-store build`")


# ── Folding base + runs into range files ───────────────────────────────────


def _class_renames(path: str) -> dict[str, str]:
    """A range file's or delta's storage-class columns under other names than `interval_store.CLASS_COLS` (a generation
    written before a rename): `{old: new}`, by position — they follow `wb` in every version table."""
    names = pq.read_schema(path).names
    i = names.index("wb") + 1
    old = names[i:i + len(ist.CLS)]
    return {o: n for o, n in zip(old, ist.CLS) if o != n}


def _versions_sql(base: str, runs: list[str], t: str, name: str, generation: ist.Identity = False, churn: bool = False) -> str:
    """Key range `name`'s versions of table `t`: the base's range file and every run's delta, one row per version
    (`IDENT`, `usr` as '') with the smallest `vt`, in the base's columns (`interval_store.SUB_SCHEMA[t]`, `usr` '';
    `GENERATION` last in `pvl` with `generation`). Class columns of an older generation are read by position, under
    `CLASS_COLS`' names. `churn`: `pvl` carries `churn_cum` (`interval_churn`)."""
    names = ist.with_generation(with_churn(ist.SUB_SCHEMA[t], churn and t == "pvl"), generation if t == "pvl" else False).names

    def sel(path: str) -> str:
        back = {n: o for o, n in _class_renames(path).items()}
        from disk_tree.object_version import version_sql
        token = version_sql(pq.read_schema(path).names)
        return ", ".join("coalesce(usr, '') AS usr" if c == "usr" else f"{token} AS version" if c == "version"
                         else f"{back[c]} AS {c}" if c in back else c for c in names)

    files = [f"{base}/{t}/{name}.parquet", *(f"{r}/{t}/{name}.parquet" for r in runs)]
    parts = [f"SELECT {sel(f)} FROM read_parquet({q(f)})" for f in files]
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


#: A `compact_range` hook: `(con, name)` sets `GENERATION` on table `pv`'s versions where it is NULL (the range's path
#: versions: the column added NULL, or read from a source that carries it), which the folded pieces (`pvl`) without one
#: then take from their path version (`interval_backfill`).
Fill = Callable[[object, str], None]


def attribution_versions_sql(base: str, runs: list[str], name: str) -> str:
    """Key range `name`'s attribution versions: the base's range file and every run's delta that hold the table (a base
    or run from before it opened holds none), one row per version (`IDENT`) with the smallest `vt`, in `AT_SCHEMA`'s
    columns. A version runs on across uncovered scans in both build and append, so these are `build`'s versions as
    they are: no islands to re-form."""
    t = ist.ATTRIBUTION
    files = [f for f in (f"{base}/{t}/{name}.parquet", *(f"{r}/{t}/{name}.parquet" for r in runs)) if Path(f).exists()]
    cols = ist.qcols(ist.AT_SCHEMA.names)
    parts = " UNION ALL ".join(f"SELECT {cols} FROM read_parquet({q(f)})" for f in files) or ist.empty_sql(ist.AT_SCHEMA)
    return f"""SELECT * FROM ({parts}) QUALIFY row_number() OVER (PARTITION BY {', '.join(ia.IDENT[t])} ORDER BY vt) = 1"""


def compact_range(con, base: str, runs: list[str], name: str, out: Path, sliced: bool = True, generation: ist.Identity = False,
                  fill: Fill | None = None, fill_nulls: bool = False, attributed: bool = False, churn: bool = False) -> dict:
    """Key range `name` of the generation folded from the base under `base` and the runs `runs` (dirs, oldest first) into
    `out`'s range files, as `build` writes them: `pvl/`, `pv/` and `rd/` (the islands of `pvl`'s pieces), and with the
    owner axis `sv/` and `svt/`. `generation`: the path versions carry `GENERATION` — read from the inputs, or, with
    `fill`, set by it on the path versions and taken by their pieces (`fill_nulls`: the inputs carry it, and only its NULLs
    are filled). `churn`: the folded path versions carry `churn_cum`, kept as the inputs have it (`pv`'s islands don't
    split on it). Its tables are `con`'s temp tables, so ranges
    run concurrently on cursors of one database (`fold_gen` `jobs`). Returns their row counts."""
    rf = lambda sub: out / sub / f"{name}.parquet"
    gen_in = generation if fill is None or fill_nulls else False
    gcol = [ist.identity_column(gen_in)] if gen_in else []
    con.execute(f"CREATE OR REPLACE TEMP TABLE pvl AS {_versions_sql(base, runs, 'pvl', name, gen_in, churn)}")
    # A path version is the run of its pieces with equal change columns (`dr` as `build` derives it); `wts` (and
    # `GENERATION`) is carried from the version's first scan, which every piece shares (`fold`).
    change = [c for c in ist.change_cols(gen_in) if c != "dr"]
    pv_src = f"SELECT *, {ia.DR} AS dr FROM pvl"
    con.execute(f"CREATE OR REPLACE TEMP TABLE pv AS {_islands_sql(pv_src, ist.change_cols(gen_in), list(dict.fromkeys([*change, 'wts', *gcol])))}")
    if fill is not None:
        if not fill_nulls:
            for t in ("pv", "pvl"):
                con.execute(f"ALTER TABLE {t} ADD COLUMN {ist.GENERATION} BIGINT")
        fill(con, name)
        g = ist.GENERATION
        con.execute(f"""UPDATE pvl SET {g} = p.{g} FROM pv p
            WHERE p.depth = pvl.depth AND p.path = pvl.path AND p.vf <= pvl.vf AND pvl.vf < p.vt AND pvl.{g} IS NULL AND p.{g} IS NOT NULL""")
    pvl_schema, pv_schema = (ist.with_generation(s, generation) for s in (with_churn(ist.PVL_SCHEMA, churn), ist.PV_SCHEMA))
    doc = {"pvl": sn.write_sorted(sn._batches(con, f"SELECT {', '.join(pvl_schema.names)} FROM pvl ORDER BY depth, path, vf"), rf("pvl"),
                                  pvl_schema, ist.RANGE_RG, dictionary=["kind", "us"])}
    doc["pv"] = sn.write_sorted(sn._batches(con, f"SELECT {', '.join(pv_schema.names)} FROM pv ORDER BY depth, path, vf"), rf("pv"), pv_schema,
                                ist.RANGE_RG, dictionary=["kind", "us"])
    con.execute(f"CREATE OR REPLACE TEMP TABLE rd AS {_islands_sql('SELECT * FROM pvl', ['last_read'], ['last_read'], 'last_read >= 0')}")
    doc["rd"] = sn.write_sorted(sn._batches(con, "SELECT depth, path, vf, vt, last_read FROM rd ORDER BY depth, path, vf"), rf("rd"),
                                ist.RD_SCHEMA, ist.RANGE_RG)
    con.execute("DROP TABLE pvl; DROP TABLE pv; DROP TABLE rd")
    if sliced:
        # `build -S`'s and `fold -S`'s statements, over the same relations (`sv` with `usr` '', `svt` with it NULL).
        con.execute(f"CREATE OR REPLACE TEMP TABLE sv AS {_versions_sql(base, runs, 'sv', name)}")
        sel = ", ".join("nullif(usr, '') AS usr" if c == "usr" else c for c in ist.SV_SCHEMA.names)
        doc["sv"] = sn.write_sorted(sn._batches(con, f"SELECT {sel} FROM sv ORDER BY depth, path, usr, vf"), rf("sv"), ist.SV_SCHEMA, ist.RANGE_RG,
                                    dictionary=["kind", "usr"])
        con.execute(f"CREATE OR REPLACE TEMP TABLE svt AS SELECT * REPLACE (nullif(usr, '') AS usr) FROM ({_versions_sql(base, runs, 'svt', name)})")
        doc["svt"] = sn.write_sorted(sn._batches(con, f"SELECT {', '.join(ist.SVT_SCHEMA.names)} FROM svt ORDER BY depth, path, usr NULLS FIRST, vf"),
                                     rf("svt"), ist.SVT_SCHEMA, ist.RANGE_RG, dictionary=["kind", "usr"])
        con.execute("DROP TABLE sv; DROP TABLE svt")
    if attributed:
        t = ist.ATTRIBUTION
        doc[t] = sn.write_sorted(sn._batches(con, f"SELECT {ist.qcols(ist.AT_SCHEMA.names)} FROM ({attribution_versions_sql(base, runs, name)}) ORDER BY depth, path, vf"),
                                 rf(t), ist.AT_SCHEMA, ist.RANGE_RG, dictionary=["user", "source"])
    return doc


def run_scans(gen_dir: Path, gen: str, runs: list[dict]) -> list[dict]:
    """The `scans.json` entries of every scan the runs `runs` hold (their `deltas/<id>/scans.json`, kept on folded runs by
    `interval_append.FOLDED_RETAIN`)."""
    docs = []
    for r in runs:
        for sid in r["scans"]:
            p = gen_dir / ar.run_key(sid, sid) / "scans.json"
            if not p.is_file():
                raise SystemExit(f"{gen}: {p.relative_to(gen_dir)} missing: scan {sid}'s entry for the compacted `scans.json`")
            docs.append(json.loads(p.read_text())["scans"][0])
    return docs


def fold_gen(gen_dir: Path, out: Path, work: Path, *, gen: str, runs: list[dict], rg_rows: int = ist.SERVED_RG, threads: int = 4,
             mem: str = "4GB", jobs: int = 1, fill: Fill | None = None, on_range: Callable[[int, int], None] | None = None,
             progress: Callable[..., None] | None = None,
             identity: ist.Identity | None = None, before_cut: Callable[[object, Path, dict], None] | None = None) -> tuple[dict, dict, dict, dict]:
    """The generation `gen` (its base in `gen_dir` and the runs `runs`) folded into a new one in `out`: its range files
    (`compact_range` over the old `ranges.json`), served sorts and churn (`interval_store.cut_gen`), and `scans.json`
    (the base's, every run scan's entry; `GENERATION` recorded when the result carries it). With `fill`, the result
    carries `GENERATION` as the hook sets it: on every version, or, when `gen` carries it, on those where it is NULL. `jobs` ranges, then `jobs` sorts, at a time (`static_names.each`: cursors
    of one DuckDB database, `threads` and `mem` shared): each ends in a serial pyarrow writer, the bulk of a range's
    time; the output's bytes don't depend on it. Logs a line per range as it ends (`i/k`, its rows, seconds, how many
    are done) and around the served sorts' cut: an hour of silence on gcs read as a hang. `on_range(i, k)` after each
    range (a caller's heartbeat; in completion order, one call at a time). `progress(note[, next_phase])`: the run
    record's hook (`append_runner.compact_store`): each range's `i/k` note, then the ranges' total with `next_phase`
    `sorts` as the served sorts' cut begins. `before_cut(con, out, doc)`: a hook over the folded range files before the
    cut (`interval_churn.backfill` sets `churn_cum` there, and records it in `doc`). Returns `(scans.json, rows per table,
    sorts, stats)`."""
    base = json.loads((gen_dir / "scans.json").read_text())
    sliced = ist.sliced(base)
    generation = (ist.has_generation(base) or fill is not None) if identity is None else identity
    fill_nulls = fill is not None and ist.has_generation(base) is True
    # The attribution table: the base's, or opened by a run (its `meta.json` says so). The covered scans ride their
    # entries (`ATTRIBUTION_SRC`, kept in a run scan's pinned `scans.json`).
    attributed = ist.attributed(base) or any(ist.ATTRIBUTION in json.loads((gen_dir / r["key"] / "meta.json").read_text()) for r in runs)
    doc = {**{k: v for k, v in base.items() if k not in ("scans", "slices", ist.GENERATION, ist.ATTRIBUTION)},
           "scans": [*base["scans"], *run_scans(gen_dir, gen, runs)], "slices": sliced, **({ist.ATTRIBUTION: True} if attributed else {})}
    doc = ist.record_generation(doc, generation)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copyfile(gen_dir / "ranges.json", out / "ranges.json")
    k = json.loads((out / "ranges.json").read_text())["k"]
    con = sn.connect(threads, mem, work / "duckdb-compact")
    run_dirs = [str(gen_dir / r["key"]) for r in runs]
    lock, done = Lock(), [0]
    t_all = monotonic()

    def one(cur, i: int) -> dict:
        t0 = monotonic()
        got = compact_range(cur, str(gen_dir), run_dirs, f"r{i:04d}", out, sliced, generation, fill, fill_nulls, attributed, has_churn(base))
        with lock:
            done[0] += 1
            err(f"fold {gen}: range {i + 1}/{k}: {sum(got.values()):,} rows in {monotonic() - t0:.1f}s "
                f"({done[0]}/{k} done, {monotonic() - t_all:.0f}s so far)")
            if on_range is not None:
                on_range(i, k)
            if progress is not None:
                progress(f"{done[0]}/{k} ranges in {monotonic() - t_all:.0f}s")
        return got

    rows: dict[str, int] = {}
    for got in sn.each(con, one, list(range(k)), jobs):
        for t, n in got.items():
            rows[t] = rows.get(t, 0) + n
    if before_cut is not None:
        before_cut(con, out, doc)
    t0 = monotonic()
    err(f"fold {gen}: {k} ranges, {sum(rows.values()):,} rows in {monotonic() - t_all:.0f}s; cutting the served sorts")
    if progress is not None:
        progress(f"{k} ranges, {sum(rows.values()):,} rows in {monotonic() - t_all:.0f}s", "sorts")
    sorts, stats = ist.cut_gen(con, out, doc, rg_rows=rg_rows, jobs=jobs)
    err(f"fold {gen}: served sorts cut in {monotonic() - t0:.0f}s")
    (out / "scans.json").write_text(json.dumps(doc, indent=1) + "\n")
    return doc, rows, sorts, stats


def compact_gen(gen_dir: Path, out: Path, work: Path, *, gen: str, new_gen: str, level: int | None, rg_rows: int = ist.SERVED_RG,
                threads: int = 4, mem: str = "4GB", jobs: int = 1, rule: dict | None = None) -> dict:
    """The generation `gen` (its base and the newest manifest's runs, in `gen_dir`) folded into the new generation
    `new_gen` in `out` (`fold`), and `compaction.json` (its rule: `rule`, `append_runner.compaction_doc`'s; else
    `{"level": level}`). Returns a summary. (The published path is `append_runner.compact_store` with `compactor()`.)"""
    t0 = monotonic()
    store = LocalRunStore(gen_dir, work / "lease", gen)
    base = store.read_json("scans.json")
    newest = latest_key(store.keys("manifests/"))
    if newest is None:
        raise SystemExit(f"{gen}: no runs to compact")
    runs = store.read_json(newest)["runs"]
    got = fold(gen_dir, out, work, gen=gen, new_gen=new_gen, runs=runs, rg_rows=rg_rows, threads=threads, mem=mem, jobs=jobs)
    comp = {"from": gen, "manifest": newest, "runs": [r["key"] for r in runs], "base_scans": len(base["scans"]), "scans": got["scans"],
            **(rule if rule is not None else {"level": level})}
    (out / COMPACTION).write_text(json.dumps(comp, indent=1) + "\n")
    s = round(monotonic() - t0, 1)
    return {"from": gen, "gen": new_gen, "runs": comp["runs"], "scans": got["scans"], "rows": got["rows"], "sorts": got["sorts"], "s": s}


def fold(gen_dir: Path, out: Path, work: Path, *, gen: str, new_gen: str, runs: list[dict], rg_rows: int = ist.SERVED_RG, threads: int = 4,
         mem: str = "4GB", jobs: int = 1, progress: Callable[..., None] | None = None) -> dict:
    """`append_runner.Compactor.fold` for the interval store: `fold_gen` (refusing a generation without its own
    `ranges.json`; `progress` its run-record hook). Returns `{scans, rows, sorts, versions}`."""
    if not (gen_dir / "ranges.json").is_file():
        raise SystemExit(f"{gen}: no ranges.json: a staged build's generation borrowing its ranges and base from another "
                         f"(its `ranges_gen`) can't be compacted in place; backfill-generation -R makes a self-contained one")
    doc, rows, sorts, stats = fold_gen(gen_dir, out, work, gen=gen, runs=runs, rg_rows=rg_rows, threads=threads, mem=mem, jobs=jobs, progress=progress)
    err(f"compact {gen} → {new_gen}: {len(runs)} runs and the base, {len(doc['scans'])} scans, {stats['versions']:,} versions")
    return {"scans": len(doc["scans"]), "rows": rows, "sorts": sorts, "versions": stats["versions"]}


def compactor(rg_rows: int = ist.SERVED_RG) -> ar.Compactor:
    """The interval store's plug into the shared compaction (`append_runner.compact_store`): `fold`, the publish order
    (data, then `scans.json`, the generation's liveness marker), predecessors pruned after the grace."""
    from .interval_local import UNREAD, publish_order, publish_tier

    return ar.Compactor(prefix=PREFIX, fold=lambda *a, **kw: fold(*a, **kw, rg_rows=rg_rows), publish_order=publish_order, last=("scans.json",),
                        keep=lambda rel: not rel.startswith(UNREAD), publish_tier=publish_tier)


def due(gen_dir: Path, policy: ar.Policy | int | None) -> str | None:
    """Why the generation in `gen_dir` (its base's scans and newest manifest) is due a compaction under `policy` (a
    `Policy`, or a level), naming the rule that fired; None when it isn't (`append_runner.due_in`)."""
    return ar.due_in(gen_dir, policy)


compaction_doc = ar.compaction_doc


# ── Pruning compacted generations ──────────────────────────────────────────


def carry_markers(store: Store, old: str, into: str, files: Iterable[str] | None = None) -> int:
    """`append_runner.carry_markers` over this store."""
    return ar.carry_markers(store, PREFIX, old, into, files)


def prune_predecessors(store: Store, gen: str, *, now: float, grace_s: float = ar.GRACE_S, dry_run: bool = False,
                       log: Callable[[str], None] = err) -> list[dict]:
    """`append_runner.prune_predecessors` over this store."""
    return ar.prune_predecessors(store, PREFIX, gen, now=now, grace_s=grace_s, dry_run=dry_run, log=log)
