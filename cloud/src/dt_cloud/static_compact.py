"""Compaction of the static name index (specs/static-compaction.md): a generation `G` (its base `static-names/<G>/` and
the newest manifest's runs) folded into a new base generation `G'`, equal to `static-names build` through the last
scan over `G`'s key ranges (`build -r G/ranges.json`), file for file. This module is the store-specific fold
(`fold_gen`); publishing, the successor record, the pointer, R2 and the predecessor's grace are the shared
orchestrator's (`append_runner.compact_store`, `compactor()` here).

How each part folds (the build's own code wherever a part is a function of others):

- `scans.json` (`G`'s, then each run scan's `deltas/<id>/scans.json` entry, kept on folded runs by
  `static_merge.FOLDED_RETAIN`), `ranges.json` (`G`'s).
- `cintervals/`, `chist/` per key range (`fold_range`): `G`'s coalesced versions and every scan's `cdelta` (a close record
  sets `vt`, an open adds a version), one row per `(depth, path, usr, vf)`, the smallest `vt`; written as
  `build_range(coalesced=True)` writes them.
- `shards.json`: the build's planner over the folded histograms (`G`'s target rows and task count); `sx/`, `sidecar/`,
  `sidecar.parquet`: `static_append.merge_shards` of `G`'s and the runs' suffix rows, cut to that plan.
- `catalog/cells.parquet`, `index.parquet`: `static_append.merge_catalogs` of `G`'s and the runs'; the members
  (`members.parquet`, `members.json`): the build's census over the new shards; `meta.json` from both.
- `drill/` and `roots-measure/` (`fold_drill`): roots are read, not recomputed from the suffix shards. Each member's
  root set through the last scan is `G`'s rows of its class there ∪ each run's rows of its class there (combined: the
  smallest `vt` per `(path, usr, vf)`); members with equal sets (digest as the build's: count and two hash sums) alias
  to the least; then the build's own measurement, rollups and writer (`static_local.finish_drill`). The digests come
  from the base's (`drill/digests-long.parquet`) plus the runs' changes (`member_digests`), not from the roots, when
  every tier has them (`digests_blocker`); the new generation writes its members' for the next compaction.
- `names/`, `anchors/` (when `G` has `anchors/meta.json`): the build's base builders over `G'` (`build_base_anchors`).
- `manifests/<last scan>.json` (no runs), and `compaction.json` (the orchestrator's completion record).
"""
from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from time import monotonic

import pyarrow as pa
import pyarrow.parquet as pq
from click import Choice, IntRange, command, option

from . import append_runner as ar
from . import heartbeat as hb
from . import static_anchors as an
from . import static_append as sa
from . import static_catalog as sc
from . import static_drill as sd
from . import static_local as sl
from . import static_measure as sme
from . import static_names as sn
from . import static_roots as sr
from .blobstore import stage_files
from .checkpoint import Checkpoint, code_version
from .static_names import CINTERVAL_SCHEMA, CODEC, INTERVAL_RG, PREFIX, err, q

U64 = 1 << 64


# ── Light: key ranges ──────────────────────────────────────────────────────


def fold_range(con, base_file: str, cdelta_files: list[str], out: Path, name: str, rule=None) -> int:
    """Key range `name`'s coalesced versions through the last scan: `G`'s (`base_file`) and every scan's `cdelta`
    (oldest first), one row per `(depth, path, usr, vf)` with the smallest `vt` → `out/cintervals/<name>.parquet` and
    `out/chist/<name>.parquet`, as `build_range(coalesced=True)` writes them. Returns the versions. Its table is named for
    the range, so ranges fold at once on cursors of one database (`fold_ranges`)."""
    cols = ", ".join(CINTERVAL_SCHEMA.names)
    parts = [f"SELECT {cols} FROM read_parquet({q(f)})" for f in [base_file, *cdelta_files]]
    civ = f"civ_{name}"
    con.execute(f"""CREATE OR REPLACE TABLE {civ} AS SELECT depth, path, usr, vf, min(vt) AS vt, any_value(size) AS size,
            any_value(n_files) AS n_files FROM ({' UNION ALL '.join(parts)}) GROUP BY depth, path, usr, vf""")
    rows = sn.write_sorted(sn._batches(con, f"SELECT * FROM {civ} ORDER BY depth, path, usr, vf"), out / "cintervals" / f"{name}.parquet",
                           CINTERVAL_SCHEMA, INTERVAL_RG, dictionary=["usr"])
    (out / "chist").mkdir(parents=True, exist_ok=True)
    pq.write_table(con.execute(sn.hist_sql(civ, rule)).to_arrow_table(), out / "chist" / f"{name}.parquet", compression=CODEC)
    con.execute(f"DROP TABLE {civ}")
    return rows


def fold_ranges(con, gen_dir: Path, days: list[str], ranges: dict, out: Path, rule=None, jobs: int = 1) -> int:
    """Every key range of `ranges` folded (`fold_range`: `gen_dir`'s base and the scans `days`' `cdelta`s), `jobs` at a time
    on cursors of `con` (`static_names.each`: one `threads` pool and `memory_limit` across them). Each range's files depend
    only on its own rows, so the bytes don't depend on `jobs`. Returns the versions."""
    def one(cur, r: dict) -> int:
        name = f"r{r['i']:04d}"
        return fold_range(cur, str(gen_dir / "cintervals" / f"{name}.parquet"),
                          [str(gen_dir / ar.run_key(d, d) / "cdelta" / f"{name}.parquet") for d in days], out, name, rule)
    return sum(sn.each(con, one, ranges["ranges"], jobs))


# ── Light: the catalog ─────────────────────────────────────────────────────


def fold_catalog(con, tiers: list[Path], out: Path, work: Path, *, new_gen: str, plan: dict, membership: dict, rule, jobs: int = 1,
                 merged: Callable[[], dict] | None = None) -> dict:
    """`G`'s and the runs' catalogs merged into `out/catalog` (`merge_catalogs`: the rebuild's cells and index), the
    members cut by the build's census over `out`'s new shards (`members.parquet`, `members.json`), and `meta.json` as
    the build's `catalog_meta` (its `s`: this fold's seconds). The census runs `jobs` shards at a time on cursors of
    `con` (each shard's census file depends only on its rows). `merged`: the merge's doc, when it was started earlier
    (it reads only the tiers' catalogs: `fold_gen` runs it beside the intervals and shards); else merged here."""
    t0 = monotonic()
    cat = out / "catalog"
    census = work / "census"
    shutil.rmtree(census, ignore_errors=True)
    census.mkdir(parents=True)

    def one(cur, s: dict) -> None:
        name = f"s{s['i']:04d}"
        pq.write_table(sc.census(cur, f"read_parquet({q(str(out / 'sx' / f'{name}.parquet'))})", membership["max_rows"] + 1, tag=f"_{name}"),
                       census / f"{name}.parquet", compression=CODEC)
    sn.each(con, one, plan["shards"], jobs)
    doc = merged() if merged is not None else sa.merge_catalogs(tiers, cat)
    members = sc.cut_members(census, cat / "members.parquet", gen=new_gen, max_rows=membership["max_rows"])
    (cat / "members.json").write_text(json.dumps(members, indent=1) + "\n")
    shutil.rmtree(census)
    short, long_, top = con.execute(f"""SELECT count(*) FILTER (WHERE length(q) <= 2), count(*) FILTER (WHERE length(q) > 2), max(length(q))
        FROM read_parquet({q(str(cat / 'cells.parquet'))}) WHERE bucket = ''""").fetchone()
    assembled = {"cells_rows": doc["cells_rows"], "row_groups": doc["row_groups"], "members_short": short, "members_long": long_, "max_len": top,
                 "bytes": doc["bytes"], "index_bytes": (cat / "index.parquet").stat().st_size, "cell_rg": sc.CELL_RG}
    meta = sc.catalog_meta(new_gen, assembled, round(monotonic() - t0, 1), members, rule)
    (cat / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    return meta


# ── The drill ──────────────────────────────────────────────────────────────


def _roots_sql(files: list[str]) -> str:
    """Published roots files' rows (`ROOT_SCHEMA`: no `depth`)."""
    if not files:
        return ("SELECT NULL::VARCHAR AS q, NULL::VARCHAR AS path, NULL::VARCHAR AS usr, NULL::BIGINT AS vf, NULL::BIGINT AS vt, "
                "NULL::BIGINT AS size, NULL::BIGINT AS n_files WHERE false")
    return f"SELECT q, path, usr, vf, vt, size, n_files FROM read_parquet([{', '.join(q(f) for f in files)}])"


#: A root's `depth`: its path's segments (as the suffix shards record it).
DEPTH = "len(string_split(path, '/'))::UTINYINT"


def q_spans(files: list[str]) -> list[tuple[str, str | None, str | None]]:
    """Each roots file's `(file, min q, max q)` from its footer's row-group statistics (None, None: no statistics; the
    file is read for every chunk)."""
    out = []
    for f in files:
        md = pq.ParquetFile(f).metadata
        col = md.schema.to_arrow_schema().get_field_index("q")
        lo = hi = None
        for g in range(md.num_row_groups):
            st = md.row_group(g).column(col).statistics
            if st is None or not st.has_min_max:
                lo = hi = None
                break
            lo = st.min if lo is None or st.min < lo else lo
            hi = st.max if hi is None or st.max > hi else hi
        else:
            out.append((f, lo, hi))
            continue
        out.append((f, None, None))
    return out


def overlapping(spans: list[tuple[str, str | None, str | None]], lo: str, hi: str | None) -> list[str]:
    """The files of `spans` that may hold a `q` in `[lo, hi]` (`hi` None: no upper bound)."""
    return [f for f, a, b in spans if a is None or ((hi is None or a <= hi) and b >= lo)]


# ── Staging the tiers' roots on the work disk ──────────────────────────────
#
# The drill fold reads the base's roots per chunk by `q` span (`_long_roots`) and per short range (`_spill_roots`). Read
# in place from the bucket's mount, that ran throughput-capped (gcs's 10-08c probe at `-j 8`: a steady ~550 MB/s in,
# CPU at ~40%, ~1 TB read for a ~280 GiB base drill: each byte ~3–4× through gcsfuse). So the roots files are copied
# once onto the work disk (`blobstore.stage_files`, NIC speed) and read there. A cache: the same files, so the same
# bytes out; never checkpointed (a resumed fold stages again what its remaining steps read).

#: `stage_roots`' modes: `auto` stages when the generation isn't on the work disk already and the disk has room; `on`
#: always (tests: a local generation); `off` reads in place.
STAGE_MODES = ("auto", "on", "off")
#: `auto` stages when the work disk's free bytes cover the staged bytes × (1 + this): the rest of the fold's own peak
#: beside them (gcs's 10-08c: 1,085 GiB of work disk at canonical-roots over a 279 GiB base drill, ≈ 3.9×; the staged
#: long roots are gone by then).
STAGE_HEADROOM = 4.0
STAGE = "stage"


class Staged:
    """`stage_roots`' copies: each staged file's local path by the path the tier names it by (`Tier.files`)."""

    def __init__(self, root: Path, paths: dict[str, dict[str, str]], doc: dict):
        self.root, self.paths, self.doc = root, paths, doc

    def files(self, kind: str, files: list[str]) -> list[str]:
        """`files` (one tier's `kind` roots), the staged copy of each where there is one."""
        got = self.paths.get(kind, {})
        return [got.get(f, f) for f in files]

    def drop(self, kind: str) -> None:
        """`kind`'s copies deleted: its reads are over."""
        for p in self.paths.pop(kind, {}).values():
            Path(p).unlink(missing_ok=True)
        if not self.paths:
            shutil.rmtree(self.root, ignore_errors=True)


def _same_disk(a: Path, b: Path) -> bool:
    """Whether `a` and `b` are on one filesystem (a mirrored generation is already on the work disk)."""
    return a.stat().st_dev == b.stat().st_dev


def _free(path: Path) -> int:
    """The free bytes on `path`'s filesystem."""
    return shutil.disk_usage(path).free


def stage_roots(gen_dir: Path, tiers: list[str], kinds: tuple[str, ...], dst: Path, *, source: str | None = None, mode: str = "auto",
                log: Callable[[str], None] = err) -> Staged | None:
    """The `kinds`' roots files of `tiers` (generation-relative drill dirs: `drill`, `<run>/drill`) copied from `source`
    (the generation's store URL, `gs://<bucket>/<prefix>/<gen>`; None: `gen_dir` itself) to `dst`; None (logged why)
    when `mode` says not to, the generation is already on the work disk, or (`auto`) the disk lacks room
    (`STAGE_HEADROOM`)."""
    if mode not in STAGE_MODES:
        raise ValueError(f"stage mode {mode!r}: one of {STAGE_MODES}")
    if mode == "off":
        log(f"stage: off; roots read in place from {gen_dir}")
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    if mode == "auto" and _same_disk(gen_dir, dst.parent):
        log(f"stage: skipped, {gen_dir} is on the work disk already")
        return None
    rels = {k: [f"{t}/{k}/roots/{p.name}" for t in tiers for p in sorted((gen_dir / t / k / "roots").glob("*.parquet"))] for k in kinds}
    keys = {r: (gen_dir / r).stat().st_size for k in kinds for r in rels[k]}
    need, free = sum(keys.values()), _free(dst.parent)
    gib = lambda n: f"{n / 2**30:,.1f} GiB"  # noqa: E731
    if mode == "auto" and free < need * (1 + STAGE_HEADROOM):
        log(f"stage: skipped, the work disk has {gib(free)} free, under {gib(need)} of {'+'.join(kinds)} roots × {1 + STAGE_HEADROOM:g}; "
            f"roots read in place from {gen_dir}")
        return None
    shutil.rmtree(dst, ignore_errors=True)
    try:
        doc = stage_files(source or str(gen_dir), keys, dst, label=f"stage {'+'.join(kinds)} roots", log=log)
    except Exception as e:
        # a cache: whatever broke the copy (credentials, the pool), the roots are still readable in place
        shutil.rmtree(dst, ignore_errors=True)
        log(f"stage: failed ({type(e).__name__}: {e}); roots read in place from {gen_dir}")
        return None
    missed = f"; {len(doc['failed'])} not staged, read in place" if doc["failed"] else ""
    log(f"stage: {doc['files']} files, {gib(doc['bytes'])} of {'+'.join(kinds)} roots from {source or gen_dir} → {dst} in {doc['s']}s "
        f"({doc['bytes'] / max(doc['s'], 0.1) / 1e9:.2f} GB/s; {gib(free)} free before{missed})")
    bad = set(doc["failed"])
    return Staged(dst, {k: {str(gen_dir / r): str(dst / r) for r in rels[k] if r not in bad} for k in kinds}, {**doc, "kinds": list(kinds)})


def fold_drill(con, tiers: list[sd.Tier], out: Path, work: Path, *, R: int, K: int, floor: int, chunk_rows: int = sl.CHUNK_ROWS,
               jobs: int = 1, step=sl._nop_step, log: Callable[[str], None] = err, ck: Checkpoint | None = None,
               staged: Callable[[], Staged | None] = lambda: None, fanout: Callable[[Checkpoint, str], object] | None = None,
               meas: Path | None = None, scan: str | None = None) -> dict:
    """`G'`'s `drill/` and `roots-measure/` in `out` from the tiers' drills (`G`'s, then each run's, oldest first) and
    `out/catalog/members.parquet`: each member's roots through the last scan, read from the tiers through each one's
    class map (every tier's `aliases.parquet` maps every member it knows to its canonical, which holds that class's rows
    there; a member a tier doesn't know has none there), combined; the classes by root-set digest, as the build's
    (`static_local.build_base_drill`); then `static_local.finish_drill` over the canonical members' roots and the short
    literals'.

    The classes: each member's digest is the base's plus every run's change to its class there (`member_digests`,
    specs/static-incremental-compaction.md step 3) when the base has its members' digests and every run its classes'
    changes (`digests_blocker`); each chunk then only checks each tuple's count against the roots it combined, taking
    the digest from those roots where they differ (logged; the result's `fixed`: the members whose digests were). Else
    every chunk digests its roots. `log` says which, and the result's `classes` (`digests` | `roots`).

    `meas`: `G`'s `roots-measure/`. When every run counts only new roots (`static_measure.blocker` finds nothing in the
    way), `finish_drill` only sorts each range's roots and takes the measurement and rollups from base ⊕ runs
    (specs/static-incremental-compaction.md, step 4); else it measures and rolls up every root. `log` says which, and
    the result's `measure` (`incremental` | `roots`).

    Chunked, so the working set is `chunk_rows` roots, not the corpus (gcs's first compaction ran one aggregate over all
    299 GB of roots and logged nothing for 6 h): members with equal tuples of classes (one per tier) hold equal rows, so
    the tuples (`tid`, ordered by the base's class) are cut into chunks of about `chunk_rows` rows (each tier's rows per
    class, summed, as its roots index estimates them: `static_roots.index_estimates`). The runs' rows are spilled once
    by chunk, into at most `static_roots.SPILL_PARTS` partitions of consecutive chunks (`static_roots.ChunkSpill`); the
    base's are read per chunk by its classes' `q` span (the roots files are sorted by `q`, so a chunk reads the files,
    and row groups, that span overlaps). Each chunk's combined roots and digests are exact (a tuple's rows are all in its chunk);
    the canonical members' roots are then spilled by `q` range chunk by chunk, and the short literals' by `q` range
    (planned from their roots indexes too), as the build's. The chunks and partitions never change the bytes. `log`: a
    progress line per chunk (at most one per 30 s).

    `jobs`: the chunks (long roots, canonical roots, short ranges) folded this many at a time, each on its own cursor of
    `con` (one DuckDB database: its `threads` and `memory_limit` shared, not multiplied), each writing its own files
    (gcs's 10-23 compaction ran its 4,881 chunks one after another at ~9 of 48 cores). It never changes the bytes.

    `ck`: saved after the long roots (`long-roots`: the tuples' digests and the chunks' roots) and after every root is spilled (`spilled`:
    the classes and the spills), so a resubmitted job resumes past the last one saved. Resumed past `spilled` with a
    measurement `fanout`, only its tables are restored: the fan-out fetches this VM's share of the spills.

    `staged()`: the tiers' roots copied onto the work disk (`stage_roots`; `fold_gen` starts the copy early, this waits
    on it), read instead of the tier's files; each kind's copies deleted once its last read is done. None: read in place."""
    t = monotonic()
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    if ck is not None and ck.done("spilled"):
        if fanout is not None:
            # the measurement's fan-out fetches only this VM's share of the spills (`Fanout.start`), not all of them (c001's
            # 730 GB, read back whole before)
            ck.load(con, "spilled-tables")
            groups = ck.store.read_json(ck.key("spilled/done.json"))["doc"]
        else:
            groups = _restore(con, ck, "spilled", work)
    else:
        st = staged()
        if st is not None:
            t = step("drill/fold/stage", t, files=st.doc["files"], bytes=st.doc["bytes"])
        saving: Future | None = None
        if ck is not None and ck.done("long-roots"):
            cdoc = _restore(con, ck, "long-roots", work)
        else:
            t, cdoc = _long_roots(con, tiers, out, work, chunk_rows=chunk_rows, jobs=jobs, step=step, log=log, t=t, st=st)
            if ck is not None:
                # the tables now (they're read off `con`); the chunks' roots upload while the classes are found and the
                # canonical roots spilled from them (c001: the fold waited 16 min on this save), until they're dropped
                ck.save(con, "long-roots-tables", ("dmem", "mtid", "tdig"))
                ex = ThreadPoolExecutor(1)
                saving = ex.submit(ck.save_files, "long-roots", work, _files(work, "troots"), cdoc)
                ex.shutdown(wait=False)
        if st is not None:
            st.drop("long")
        t, groups = _spill_roots(con, tiers, work, chunk_rows=chunk_rows, jobs=jobs, step=step, log=log, t=t, st=st,
                                 before_drop=saving.result if saving is not None else lambda: None)
        groups = {**groups, **cdoc}
        if st is not None:
            st.drop("short")
        _save(con, ck, "spilled", work, tables=("al", "dgq"), subs=("long", "short"), doc=groups)
    if ck is not None:
        # the spills hold every long root again: the chunks' roots are no longer needed to resume (an attempt stopped
        # right after saving the spills left them)
        ck.drop("long-roots-tables")
        ck.drop("long-roots")
    aliases = con.execute("SELECT q, canonical FROM al ORDER BY q").to_arrow_table()
    # the measurement's helper tasks fetch the spills the checkpoint just saved (specs/static-measure-fanout.md)
    fan = fanout(ck, scan) if fanout is not None and ck is not None else None
    why = sme.blocker(tiers, meas, floor=floor, R=R) if meas is not None else "no base measurement given"
    log(f"drill measurement: {'incremental (base ⊕ runs; each range only sorted)' if why is None else f'every root ({why})'}")
    done = sl.finish_drill(con, out, work, aliases=aliases, long_groups=groups["long"], short_groups=groups["short"], R=R, K=K, floor=floor,
                           step=step, t=t, fanout=fan, incremental=(tiers, meas) if why is None else None)
    return {"members": aliases.num_rows, "canonical": len(set(aliases.column("canonical").to_pylist())), "groups": done["groups"],
            "measure": "roots" if why else "incremental", "classes": groups["classes"], "fixed": groups["fixed"]}


def _roots_files(tr: sd.Tier, kind: str, st: Staged | None) -> list[str]:
    """`tr`'s `kind` roots files, their staged copies where `st` has them."""
    files = tr.files(kind, "roots")
    return st.files(kind, files) if st is not None else files


def _files(root: Path, *subs: str) -> list[str]:
    """The files (paths relative to `root`) of `root`'s `subs`: each a file or a directory (its files, recursively)."""
    out = []
    for sub in subs:
        p = root / sub
        out += [sub] if p.is_file() else sorted(f.relative_to(root).as_posix() for f in p.rglob("*") if f.is_file())
    return out


def _save(con, ck: Checkpoint | None, step: str, work: Path, *, tables: tuple[str, ...], subs: tuple[str, ...], doc: dict | None = None) -> None:
    """Step `step`: `tables`, then the files of `work`'s `subs` with `doc` (its marker, written last)."""
    if ck is not None:
        ck.save(con, f"{step}-tables", tables)
        ck.save_files(step, work, _files(work, *subs), doc)


def _restore(con, ck: Checkpoint, step: str, work: Path) -> dict | None:
    """`_save`'s tables and files back (into `work`); its doc."""
    ck.load(con, f"{step}-tables")
    return ck.load_files(step, work)


def member_classes(con, tiers: list[sd.Tier], members: str) -> None:
    """Tables `dmem (q)` (the members of parquet `members`), `ta<i> (q, canonical)` (tier `i`'s class map) and `mt (q,
    c0, …)`: each member's canonical in every tier (NULL where the tier doesn't know it, so holds none of its rows)."""
    con.execute(f"CREATE OR REPLACE TABLE dmem AS SELECT q FROM read_parquet({q(members)})")
    for i, tr in enumerate(tiers):
        con.register(f"al_in{i}", tr.alias_table())
        con.execute(f"CREATE OR REPLACE TABLE ta{i} AS SELECT q, canonical FROM al_in{i}")
        con.unregister(f"al_in{i}")
    cs = ", ".join(f"ta{i}.canonical AS c{i}" for i in range(len(tiers)))
    joins = " ".join(f"LEFT JOIN ta{i} USING (q)" for i in range(len(tiers)))
    con.execute(f"CREATE OR REPLACE TABLE mt AS SELECT m.q, {cs} FROM dmem AS m {joins}")


def digests_blocker(tiers: list[sd.Tier]) -> str | None:
    """Why a compaction over `tiers` (the base's drill, then the runs') can't take its classes from the persisted digests
    (`member_digests`), or None: a base without its members' digests (`static_drill.DIGESTS`: one built before them), or
    runs without their classes' changes (`meta.json` `digests: delta`: built before them, or a change `day_digests`
    couldn't place)."""
    if not tiers[0].exists(sd.DIGESTS):
        return f"a base without {sd.DIGESTS}"
    if stale := [t.name for t in tiers[1:] if t.meta.get("digests") != sd.DIGESTS_DELTA]:
        return f"runs without `digests: {sd.DIGESTS_DELTA}`: {', '.join(stale)}"
    return None


def member_digests(con, tiers: list[sd.Tier], into: str) -> None:
    """Table `into (q, n, h1, h2)`: each member's root-set digest through the last tier, from table `mt` (`member_classes`)
    and the tiers' digests (`digests_blocker` finds none missing): the base's (absolute, per member; 0 for a member it
    doesn't know) plus each run's change to the member's class there (`mt`'s canonical; none where the run doesn't know
    it), the hash sums mod 2⁶⁴ as the digest pass's `% 2⁶⁴`. A tier's class holds equal roots for all its members, so a
    class's change is each member's."""
    n = len(tiers)
    xs = " ".join(f"LEFT JOIN read_parquet({q(tiers[i].local(sd.DIGESTS))}) AS x{i} ON x{i}.q = mt.c{i}" for i in range(1, n))

    def total(b: str, d: str) -> str:
        return " + ".join([f"coalesce(b.{b}, 0)::HUGEINT", *(f"coalesce(x{i}.{d}, 0)" for i in range(1, n))])
    con.execute(f"""CREATE OR REPLACE TABLE {into} AS SELECT mt.q, ({total('n', 'dn')})::BIGINT AS n, (({total('h1', 'd1')}) % {U64})::UBIGINT AS h1,
            (({total('h2', 'd2')}) % {U64})::UBIGINT AS h2
        FROM mt LEFT JOIN read_parquet({q(tiers[0].local(sd.DIGESTS))}) AS b ON b.q = mt.q {xs}""")


def _tuple_digests(con, tiers: list[sd.Tier]) -> str | None:
    """Table `tdig (tid, n, h1, h2)` from the persisted digests (`member_digests` over `mt`, per tuple of `mtid`; a tuple
    with no roots left out), or (nothing made) why not: `digests_blocker`'s reason, or members of one tuple whose digests
    differ (their classes are equal in every tier, so their digests can't but for a wrong input)."""
    if (why := digests_blocker(tiers)) is not None:
        return why
    member_digests(con, tiers, "mdig")
    split = con.execute("""SELECT count(*) FROM (SELECT tid FROM mdig JOIN mtid USING (q) GROUP BY tid
        HAVING count(DISTINCT n) > 1 OR count(DISTINCT h1) > 1 OR count(DISTINCT h2) > 1)""").fetchone()[0]
    if not split:
        con.execute("""CREATE OR REPLACE TABLE tdig AS SELECT tid, min(n)::BIGINT AS n, min(h1)::UBIGINT AS h1, min(h2)::UBIGINT AS h2
            FROM mdig JOIN mtid USING (q) GROUP BY tid HAVING min(n) > 0""")
    con.execute("DROP TABLE mdig")
    return f"the digests differ within {split:,} tuples of equal classes" if split else None


#: No roots: a chunk's combined rows when no tier has any there (`_long_roots`).
NO_ROOTS = ("SELECT NULL::BIGINT AS tid, NULL::VARCHAR AS path, NULL::VARCHAR AS usr, NULL::BIGINT AS vf, NULL::BIGINT AS vt, "
            "NULL::BIGINT AS size, NULL::BIGINT AS n_files WHERE false")


def _long_roots(con, tiers: list[sd.Tier], out: Path, work: Path, *, chunk_rows: int, jobs: int, step, log: Callable[[str], None], t: float,
                st: Staged | None = None) -> tuple[float, dict]:
    """`fold_drill`'s plan, runs' spill and long roots: tables `dmem`, `mtid (q, tid)`, `tdig` (each tuple's digest) and
    `work/troots/c<chunk>.parquet` (each chunk's combined roots). `st`: the roots' local copies. The digests come from the
    tiers' (`_tuple_digests`), each tuple's count checked against its combined roots (a tuple whose count differs takes
    its digest from them, logged), or else from the roots. Returns the time and `{classes: digests | roots, fixed:
    members whose digests their roots corrected}`."""
    n = len(tiers)
    member_classes(con, tiers, str(out / "catalog" / "members.parquet"))
    # a member's tuple of classes, one per tier: members with equal tuples hold equal rows
    con.execute(f"""CREATE OR REPLACE TABLE tup AS SELECT row_number() OVER (ORDER BY {', '.join(f'c{i} NULLS FIRST' for i in range(n))}) AS tid, *
        FROM (SELECT DISTINCT {', '.join(f'c{i}' for i in range(n))} FROM mt)""")
    on = " AND ".join(f"t.c{i} IS NOT DISTINCT FROM mt.c{i}" for i in range(n))
    con.execute(f"CREATE OR REPLACE TABLE mtid AS SELECT mt.q, t.tid FROM mt JOIN tup AS t ON {on}")
    why = _tuple_digests(con, tiers)
    derived = why is None
    log("drill classes: " + ("from the digests (the base's ⊕ the runs' changes; each tuple's count checked against its roots)" if derived
                             else f"every root's digest ({why})"))
    long_files = [_roots_files(tr, "long", st) for tr in tiers]
    # each tier's rows per class, estimated from its roots index (`static_roots.index_estimates`): chunk sizes only
    for i, tr in enumerate(tiers):
        sr.index_estimates(con, [tr.local("long-roots-index.parquet")] if long_files[i] else [], f"SELECT canonical AS q FROM ta{i}", f"tc{i}")
    est = " + ".join(f"coalesce(tc{i}.n, 0)" for i in range(n))
    cjoin = " ".join(f"LEFT JOIN tc{i} ON tc{i}.q = t.c{i}" for i in range(n))
    con.execute(f"""CREATE OR REPLACE TABLE tch AS SELECT tid, est, (dense_rank() OVER (ORDER BY ch) - 1)::BIGINT AS ch FROM (
        SELECT tid, est, ((sum(est) OVER (ORDER BY tid ROWS UNBOUNDED PRECEDING) - est) // {chunk_rows})::BIGINT AS ch
        FROM (SELECT t.tid, {est} AS est FROM tup AS t {cjoin}))""")
    chunks = con.execute("""SELECT ch, min(tid), max(tid), min(t.c0), max(t.c0), sum(est)::BIGINT FROM tch JOIN tup AS t USING (tid)
        GROUP BY ch ORDER BY ch""").fetchall()
    t = step("drill/fold/plan", t, tuples=sl.count(con, "tup"), chunks=len(chunks))
    runs = sr.ChunkSpill(con, work / "runs", len(chunks))
    spills = [i for i in range(1, n) if long_files[i]]
    k = max(1, min(jobs, len(spills)))
    # a partitioned write keeps per-thread, per-partition buffers outside `memory_limit` (gcs 10-08c: RSS past `-M` here),
    # so the runs written at once split its flush threshold (a connection's own setting) between them
    flush = con.execute("SELECT current_setting('partitioned_write_flush_threshold')").fetchone()[0]

    def spill(cur, i: int) -> tuple[int, None]:
        cur.execute(f"SET partitioned_write_flush_threshold = {max(1, int(flush) // k)}")
        runs.write(f"""SELECT c.ch, t.tid, r.path, r.usr, r.vf, r.vt, r.size, r.n_files FROM ({_roots_sql(long_files[i])}) AS r
            JOIN tup AS t ON t.c{i} = r.q JOIN tch AS c USING (tid)""", f"t{i:03d}", con=cur)
        return 0, None

    _pool(con, spill, spills, k, sn.Progress("drill/fold/runs-spill", len(spills), log))
    t = step("drill/fold/runs-spill", t, runs=n - 1, parts=runs.p)
    spans = q_spans(long_files[0])
    troots = work / "troots"
    troots.mkdir()
    prog = sn.Progress("drill/fold/long-roots", len(chunks), log)
    split: list[int] = []  # the heavy chunks' combine parts
    spilled: dict[int, list[Path]] = {}

    def chunk(cur, c: tuple) -> tuple[int, object]:
        """Chunk `c`'s combined roots → `troots/c<ch>.parquet`; its rows and its tuples' digests (by `tid`), or (`derived`)
        the tuples whose rows aren't `tdig`'s count, with their digests from the rows (None: every count holds)."""
        ch, a, b, lo, hi, est = c
        parts = []
        base = overlapping(spans, lo, hi) if lo is not None else []
        if base:
            parts.append(f"""SELECT t.tid, r.path, r.usr, r.vf, r.vt, r.size, r.n_files FROM ({_roots_sql(base)}) AS r
                JOIN tup AS t ON t.c0 = r.q WHERE r.q BETWEEN {q(lo)} AND {q(hi)} AND t.tid BETWEEN {a} AND {b}""")
        if spilled[ch]:
            parts.append(f"""SELECT tid, path, usr, vf, vt, size, n_files
                FROM read_parquet([{', '.join(q(str(f)) for f in spilled[ch])}], hive_partitioning = false)""")
        if not parts:
            if not derived:
                return 0, None
            parts.append(NO_ROOTS)  # `tdig`'s counts here are checked all the same
        combine = f"""SELECT tid, path, usr, vf, min(vt) AS vt, any_value(size) AS size, any_value(n_files) AS n_files
            FROM ({' UNION ALL '.join(parts)}) {{}} GROUP BY tid, path, usr, vf"""
        # a heavy chunk (one class can hold 440M roots: c001's tail OOM'd at a 186 GiB limit) combines in parts by
        # `hash(path)`: its groups lie in one part each, so the parts' files hold the same rows; `ct` reads them back
        k = max(1, -(-int(est or 0) // COMBINE_PART_ROWS))
        if k > 1:
            split.append(k)
        if k == 1:
            cur.execute(f"CREATE OR REPLACE TEMP TABLE ct AS {combine.format('')}")
        else:
            files = [troots / f"c{ch:06d}-p{i:03d}.parquet" for i in range(k)]
            for i, f in enumerate(files):
                cur.execute(f"COPY ({combine.format(f'WHERE hash(path) % {k} = {i}')}) TO {q(str(f))} (FORMAT parquet, COMPRESSION zstd)")
            cur.execute(f"CREATE OR REPLACE TEMP VIEW ct AS SELECT * FROM read_parquet([{', '.join(q(str(f)) for f in files)}])")
        dig_sql = f"""SELECT tid, count(*)::BIGINT AS n, (sum({ROW_HASH}) % {U64})::UBIGINT AS h1,
            (sum(hash({ROW_HASH}, 'x')) % {U64})::UBIGINT AS h2 FROM ct {{}} GROUP BY tid ORDER BY tid"""
        if derived:
            cur.execute(f"""CREATE OR REPLACE TEMP TABLE cbad AS SELECT tid FROM (SELECT tid, count(*)::BIGINT AS n FROM ct GROUP BY tid) AS r
                FULL JOIN (SELECT tid, n FROM tdig WHERE tid BETWEEN {a} AND {b}) AS d USING (tid) WHERE r.n IS DISTINCT FROM d.n""")
            bad = [x for (x,) in cur.execute("SELECT tid FROM cbad ORDER BY tid").fetchall()]
            dig = (bad, cur.execute(dig_sql.format("SEMI JOIN cbad USING (tid)")).to_arrow_table()) if bad else None
            cur.execute("DROP TABLE cbad")
        else:
            dig = cur.execute(dig_sql.format("")).to_arrow_table()
        rows = sl.count(cur, "ct")
        if k > 1:
            cur.execute("DROP VIEW ct")
            for f in files:
                if not pq.ParquetFile(f).metadata.num_rows:
                    f.unlink()
            return rows, dig
        if rows:
            cur.execute(f"COPY ct TO {q(str(troots / f'c{ch:06d}.parquet'))} (FORMAT parquet, COMPRESSION zstd)")
        cur.execute("DROP TABLE ct")
        return rows, dig

    # the chunks run `jobs` at a time, whole runs' spill partitions at once (each split by chunk first, removed after)
    digs: list = []  # digest tables, or (`derived`) each corrected chunk's `(tids, digest table)`
    for parts in _part_batches(runs, jobs):
        for p in parts:
            spilled.update(runs.prepare(p))
        digs += [d for d in _pool(con, chunk, [chunks[k] for p in parts for k in runs.chunks(p)], jobs, prog) if d is not None]
        for p in parts:
            runs.release(p)
        spilled.clear()
    runs.close()
    fixed = 0
    if not derived:
        con.execute("CREATE OR REPLACE TABLE tdig (tid BIGINT, n BIGINT, h1 UBIGINT, h2 UBIGINT)")
    elif digs:
        # tuples whose roots aren't their digests' count (a run's change that wasn't exact): their digests from the roots
        bad = [x for b, _ in digs for x in b]
        con.register("fix_bad", pa.table({"tid": pa.array(bad, pa.int64())}))
        fixed = con.execute("SELECT count(*) FROM mtid SEMI JOIN fix_bad USING (tid)").fetchone()[0]
        log(f"drill classes: {len(bad):,} tuples ({fixed:,} members) whose roots aren't their digests' count; their digests taken from their roots")
        con.execute("DELETE FROM tdig WHERE tid IN (SELECT tid FROM fix_bad)")
        con.unregister("fix_bad")
        digs = [d for _, d in digs]
    if digs:
        con.register("tdig_in", pa.concat_tables(digs))
        con.execute("INSERT INTO tdig SELECT tid, n, h1, h2 FROM tdig_in")
        con.unregister("tdig_in")
    return (step("drill/fold/long-roots", t, tuples=sl.count(con, "tup"), rows=prog.rows, chunks=len(chunks), split=len(split)),
            {"classes": "digests" if derived else "roots", "fixed": fixed})


#: Estimated root rows per part of a long-roots chunk's combine (`_long_roots`' `chunk`): a chunk past it (one heavy
#: class is one chunk) combines in parts by `hash(path)`, so its hash table is a part's.
COMBINE_PART_ROWS = 1 << 26


def _part_batches(spill: sr.ChunkSpill, jobs: int) -> list[list[int]]:
    """`spill`'s partitions in order, in consecutive batches of at least `4 × jobs` chunks (the last: what remains), so
    a pool of `jobs` stays busy across partitions of one chunk each and only a batch's partitions are held at once."""
    out: list[list[int]] = [[]]
    for p in range(spill.p):
        if sum(len(spill.chunks(x)) for x in out[-1]) >= 4 * jobs:
            out.append([])
        out[-1].append(p)
    return out


def _pool(con, fn: Callable[..., tuple[int, object]], items: list, jobs: int, prog: sn.Progress) -> list:
    """`fn(cursor, item)` → `(rows, result)` for every item, `jobs` at a time, each on its own cursor of `con`
    (`static_names.each`: one `threads` pool and one `memory_limit` across them, its own temp tables), `prog` ticked as
    each ends; the results in `items`' order. Each item writes its own files, so the outputs never depend on `jobs`."""
    lock = Lock()

    def one(cur, item):
        cur.execute("SET preserve_insertion_order=false")  # a connection's own setting, as `connect` sets it
        rows, res = fn(cur, item)
        with lock:
            prog.finish(rows)  # its count, not this call's: the long roots run one `_pool` per batch of spill parts
        return res

    return sn.each(con, one, items, jobs)


def _spill_roots(con, tiers: list[sd.Tier], work: Path, *, chunk_rows: int, jobs: int, step, log: Callable[[str], None], t: float,
                 st: Staged | None = None, before_drop: Callable[[], object] = lambda: None) -> tuple[float, dict]:
    """`fold_drill`'s classes (table `al (q, canonical)`, by root-set digest) and every root spilled by `q` range: the
    canonical members' long roots (`work/long/`) and the short literals' (`work/short/`); the ranges per kind. `st`: the
    roots' local copies. `before_drop()` runs before the chunks' roots (`work/troots/`) are removed: their save's end."""
    n, troots = len(tiers), work / "troots"
    con.execute("CREATE OR REPLACE TABLE dgq AS SELECT m.q, d.n, d.h1, d.h2 FROM mtid AS m JOIN tdig AS d USING (tid)")
    con.execute("CREATE OR REPLACE TABLE al AS SELECT q, min(q) OVER (PARTITION BY n, h1, h2) AS canonical FROM dgq")
    missing = con.execute("SELECT count(*) FROM dmem ANTI JOIN al USING (q)").fetchone()[0]
    if missing:
        raise RuntimeError(f"{missing} members without roots in any tier: the runs' drills don't cover the folded catalog's members")
    aliases = con.execute("SELECT q, canonical FROM al ORDER BY q").to_arrow_table()
    con.execute("CREATE OR REPLACE TABLE canon AS SELECT DISTINCT canonical AS q FROM al")
    con.execute("CREATE OR REPLACE TABLE ctid AS SELECT c.q, m.tid FROM canon AS c JOIN mtid AS m USING (q)")
    t = step("drill/fold/aliases", t, members=aliases.num_rows, canonical=sl.count(con, "canon"))
    long_groups = sl.plan_groups(con, con.execute("SELECT c.q, d.n FROM ctid AS c JOIN tdig AS d USING (tid) ORDER BY c.q").fetchall(), chunk_rows)
    files = sorted(troots.glob("*.parquet"))
    prog = sn.Progress("drill/fold/canonical-roots", len(files), log)

    def canonical(cur, kf: tuple[int, Path]) -> tuple[int, None]:
        k, f = kf
        cur.execute(f"""CREATE OR REPLACE TEMP TABLE rt AS SELECT c.q, {DEPTH} AS depth, r.path, r.usr, r.vf, r.vt, r.size, r.n_files
            FROM ctid AS c JOIN read_parquet({q(str(f))}) AS r USING (tid)""")
        rows = sl.count(cur, "rt")
        if rows:
            sl.spill_roots(cur, "rt", work / "long" / f"fold{k:06d}")
        cur.execute("DROP TABLE rt")
        return rows, None

    _pool(con, canonical, list(enumerate(files)), jobs, prog)
    n_long = prog.rows
    before_drop()
    shutil.rmtree(troots)
    # a resumed fold (`long-roots` restored) never made `mt`, `tup`, `tch` or the per-tier tables
    for tb in ("rt", "tdig", "ctid", "mt", "mtid", "tup", "tch", *(f"t{x}{i}" for i in range(n) for x in "ac")):
        con.execute(f"DROP TABLE IF EXISTS {tb}")
    t = step("drill/fold/canonical-roots", t, lcan=n_long, groups=long_groups)
    short_files = [_roots_files(tr, "short", st) for tr in tiers]
    hb.set_stage("drill/fold/short-roots: plan")
    # the q ranges planned from the tiers' roots indexes (the literals: every group's first and last `q`), not counted
    idx = [tr.local("short-roots-index.parquet") for tr, fs in zip(tiers, short_files) if fs]
    keys = " UNION ".join(f"SELECT q_min AS q FROM read_parquet({q(f)}) UNION SELECT q_max FROM read_parquet({q(f)})" for f in idx)
    sr.index_estimates(con, idx, keys, "sest")
    counts = con.execute("SELECT q, n FROM sest ORDER BY q").fetchall()
    con.execute("DROP TABLE sest")
    bounds = sl.q_bounds(counts, chunk_rows)
    short_groups = sl.plan_groups(con, counts, chunk_rows)
    short_spans = [q_spans(fs) for fs in short_files]
    prog = sn.Progress("drill/fold/short-roots", len(bounds), log)

    def short(cur, g: int) -> tuple[int, None]:
        lo = bounds[g]
        hi = bounds[g + 1] if g + 1 < len(bounds) else None
        where = f"q >= {q(lo)}" + (f" AND q < {q(hi)}" if hi is not None else "")
        parts = [f"SELECT * FROM ({_roots_sql(fs)}) WHERE {where}" for sp in short_spans if (fs := overlapping(sp, lo, hi))]
        if not parts:
            return 0, None
        cur.execute(f"""CREATE OR REPLACE TEMP TABLE rt AS SELECT q, {DEPTH} AS depth, path, usr, vf, min(vt) AS vt, any_value(size) AS size,
                any_value(n_files) AS n_files FROM ({' UNION ALL '.join(parts)}) GROUP BY q, path, usr, vf""")
        rows = sl.count(cur, "rt")
        if rows:
            sl.spill_roots(cur, "rt", work / "short" / f"fold{g:06d}")
        cur.execute("DROP TABLE rt")
        return rows, None

    _pool(con, short, list(range(len(bounds))), jobs, prog)
    n_short = prog.rows
    return step("drill/fold/short-roots", t, sall=n_short, groups=short_groups), {"long": long_groups, "short": short_groups}


ROW_HASH = "hash(path, usr, vf, vt, size, n_files)"


# ── The whole fold ─────────────────────────────────────────────────────────


def run_scans(gen_dir: Path, gen: str, runs: list[dict]) -> list[dict]:
    """The `scans.json` entries of every scan the runs hold (`deltas/<id>/scans.json`, kept on folded runs)."""
    out = []
    for r in runs:
        for sid in r["scans"]:
            p = gen_dir / ar.run_key(sid, sid) / "scans.json"
            if not p.is_file():
                raise SystemExit(f"{gen}: {p.relative_to(gen_dir)} missing: scan {sid}'s entry for the compacted `scans.json`")
            out.append(json.loads(p.read_text())["scans"][0])
    return out


def fold_gen(gen_dir: Path, out: Path, work: Path, *, gen: str, new_gen: str, runs: list[dict], threads: int = 4, mem: str = "4GB",
             chunk_rows: int = sl.CHUNK_ROWS, jobs: int = 1, log: Callable[[str], None] = err, progress: Callable[..., None] | None = None,
             ck: Checkpoint | None = None, source: str | None = None, stage: str = "auto",
             fanout: Callable[[Checkpoint, str], object] | None = None, anchor_fanout: Callable[[Checkpoint, str], object] | None = None) -> dict:
    """Generation `gen` (its base in `gen_dir`, the runs `runs`: the newest manifest's, oldest first) folded into `new_gen`
    in `out` (every part but `compaction.json`); its `manifests/<last>.json` lists no runs. Returns a summary (rows, and
    per part its seconds, peak work-disk GiB and peak RSS GiB). `progress(note)`: the run record's hook
    (`append_runner.compact_store`), the parts done so far as each ends.

    `ck` (`compact_checkpoint`): each part's files in `out` saved as it ends (the drill's also within it, `fold_drill`),
    so a resubmitted job restores the parts saved and folds from the first that wasn't (gcs's 10-08c compaction ran 16 h
    and was killed in its drill's write with nothing kept). Each save runs beside the next part (in part order, one at a
    time; the drill, which saves its own steps, starts once the saves before it ended).

    `jobs`: key ranges (`fold_ranges`) and census shards (`fold_catalog`) at a time, on cursors of one DuckDB database
    (`threads` and `mem` shared). The catalogs' merge needs no other part and runs beside the intervals and shards. The
    shards need every range's histogram (their plan), the census the shards, the drill the members: those stay in order.

    `stage` (`STAGE_MODES`): the roots files the drill fold reads copied onto the work disk (`stage_roots`, from `source`:
    the generation's store URL; None: from `gen_dir`), started beside the intervals, so the drill reads them locally.

    `fanout(ck, scan)`, `anchor_fanout(ck, scan)` (both need `ck`): the drill measurement's and the base anchors'
    rollups' helper tasks (`static_fanout.Fanout`, `AnchorFanout`), named by the newest scan folded."""
    with ThreadPoolExecutor(3, thread_name_prefix="compact") as bg:
        return _fold_gen(gen_dir, out, work, gen=gen, new_gen=new_gen, runs=runs, threads=threads, mem=mem, chunk_rows=chunk_rows, jobs=jobs,
                         log=log, progress=progress, ck=ck, bg=bg, source=source, stage=stage, fanout=fanout, anchor_fanout=anchor_fanout)


def _fold_gen(gen_dir: Path, out: Path, work: Path, *, gen: str, new_gen: str, runs: list[dict], threads: int, mem: str, chunk_rows: int, jobs: int,
              log: Callable[[str], None], progress: Callable[..., None] | None, ck: Checkpoint | None, bg: ThreadPoolExecutor, source: str | None,
              stage: str, fanout: Callable[[Checkpoint, str], object] | None = None,
              anchor_fanout: Callable[[Checkpoint, str], object] | None = None) -> dict:
    """`fold_gen`, its background work (the catalogs' merge, the parts' saves) on `bg` (waited on as `fold_gen` returns or
    fails)."""
    t0, times, peaks = monotonic(), {}, {}
    if not runs:
        raise SystemExit(f"{gen}: no runs to compact")
    base = json.loads((gen_dir / "scans.json").read_text())
    rule = sn.gen_rule(base)
    run_dirs = [gen_dir / r["key"] for r in runs]
    days = [s for r in runs for s in r["scans"]]
    lack = [r["key"] for r in runs if not (gen_dir / r["key"] / "drill" / "meta.json").is_file()]
    if not (gen_dir / "drill" / "meta.json").is_file() or lack:
        raise SystemExit(f"{gen}: compaction needs every tier's drill/ (the base's{' and ' + ', '.join(lack) if lack else ''})")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    work.mkdir(parents=True, exist_ok=True)
    disk = sl.DiskPeak(work)
    steps: dict[str, float] = {}
    noted: set[str] = set()  # parts and sub-steps an ended phase's note carried

    def done(part: str, t: float, next_phase: str | None = None) -> float:
        """Part `part` ended: logged, and on the run's record (`progress`) its running phase's note, or with `next_phase`
        that phase ended and the next begun (/scans: `fold` = intervals, shards and the catalog; `drill`; `anchors`)."""
        times[part] = round(monotonic() - t, 1)
        peaks[part] = {"disk_gb": disk.take(), "rss_gb": sl.rss_peak_gb()}
        log(f"compact {gen} → {new_gen}: {part} in {times[part]}s (work disk peak {peaks[part]['disk_gb']} GiB, RSS peak {peaks[part]['rss_gb']} GiB)")
        hb.after(f"compact/{part}")
        if progress is not None:
            progress(phase_note(), next_phase)
            if next_phase is not None:
                noted.update(times, steps)
        return monotonic()

    def phase_note() -> str:
        """The running phase's note: its parts' and the drill's sub-steps' seconds (those an ended phase didn't carry)."""
        return "; ".join([*(f"{p} {s}s (disk {peaks[p]['disk_gb']} GiB, RSS {peaks[p]['rss_gb']} GiB)" for p, s in times.items() if p not in noted),
                          *(f"{k} {v}s" for k, v in steps.items() if k not in noted)])

    def step(name: str, t: float, **counts: int) -> float:
        steps[name] = round(monotonic() - t, 1)
        n = "".join(f", {k} {v:,}" for k, v in counts.items())
        log(f"compact {gen} → {new_gen}: {name} in {steps[name]}s (work disk peak {disk.take_step()} GiB, RSS peak {sl.rss_peak_gb()} GiB{n})")
        hb.after(name)
        if progress is not None:  # the drill's sub-steps (measure, the helpers' collect, write) in its phase's note
            progress(phase_note())
        return monotonic()

    # In the background: the catalogs' merge (it reads only the tiers' catalogs) and each part's checkpoint save, one at a
    # time and in part order (a part's save overlaps the next part's work; it ends before the next part's save starts).
    saving: list[Future] = []

    def saved() -> None:
        while saving:
            saving.pop().result()

    def resumed(part: str, subs: tuple[str, ...], run: Callable[[], dict], *, after_saves: bool = False) -> dict:
        """`run()`'s doc, its files (`out`'s `subs`) saved as `part` in the background once the previous part's save
        ended; or, saved by an earlier attempt, restored. `after_saves`: run only once the saves before it ended (a part
        with checkpoints of its own, which would otherwise land before the previous part's marker)."""
        if ck is not None and ck.done(part):
            return ck.load_files(part, out)
        if after_saves:
            saved()
        doc = run()
        saved()
        if ck is not None:
            saving.append(bg.submit(ck.save_files, part, out, _files(out, *subs), doc))
        return doc

    con = sn.connect(threads, mem, work / "duckdb")
    # every input is read about once (a chunk's base row groups, each run's spill): DuckDB's cache of the parquet bytes it
    # read only filled the buffer pool to `-M` (gcs's 10-08c plan step: 174 GiB of a read-only scan)
    con.execute("SET enable_external_file_cache=false")
    doc = dict(base)
    doc["scans"] = [*base["scans"], *run_scans(gen_dir, gen, runs)]
    (out / "scans.json").write_text(json.dumps(doc, indent=1) + "\n")
    shutil.copyfile(gen_dir / "ranges.json", out / "ranges.json")
    ranges = json.loads((out / "ranges.json").read_text())
    # the drill's roots onto the work disk, beside the light parts: what the fold's remaining steps read (none once the
    # drill or its spills were saved, only the short roots once the long roots were)
    fck = ck.sub("fold") if ck is not None else None
    if (ck is not None and ck.done("drill")) or (fck is not None and fck.done("spilled")):
        kinds: tuple[str, ...] = ()
    else:
        kinds = ("short",) if fck is not None and fck.done("long-roots") else sd.KINDS
    staging: Future | None = None
    stage_log: list[str] = []

    def slog(line: str) -> None:
        stage_log.append(line)
        log(f"compact {gen} → {new_gen}: {line}")
    if kinds:
        tdirs = ["drill", *(f"{r['key']}/drill" for r in runs)]
        staging = bg.submit(stage_roots, gen_dir, tdirs, kinds, work / STAGE, source=source, mode=stage, log=slog)
    merging: Future | None = None
    if ck is None or not ck.done("catalog"):
        def merge() -> dict:
            tm = monotonic()
            m = sa.merge_catalogs([gen_dir / "catalog", *(d / "catalog" for d in run_dirs)], out / "catalog")
            log(f"compact {gen} → {new_gen}: catalog merge in {monotonic() - tm:.1f}s ({m['cells_rows']:,} cells; beside the intervals and shards)")
            return m
        merging = bg.submit(merge)
    t = monotonic()
    versions = resumed("intervals", ("cintervals", "chist"), lambda: {"versions": fold_ranges(con, gen_dir, days, ranges, out, rule, jobs)})["versions"]
    t = done("intervals", t)

    def shards() -> dict:
        old = json.loads((gen_dir / "shards.json").read_text())
        plan = sn.plan_shards(sorted((out / "chist").glob("*.parquet")), old["target_rows"], len(old["tasks"]))
        sa.merge_shards([gen_dir, *run_dirs], out, plan=plan)
        (out / "shards.json").write_text(json.dumps(plan, indent=1) + "\n")
        side = sn.concat_sidecars([pq.read_table(f) for f in sorted((out / "sidecar").glob("*.parquet"))])
        sn.write_sidecar(side, out / "sidecar.parquet")
        return {"suffix_rows": int(sum(side.column("rows").to_pylist()))}
    suffix_rows = resumed("shards", ("sx", "sidecar", "sidecar.parquet", "shards.json"), shards)["suffix_rows"]
    plan = json.loads((out / "shards.json").read_text())
    t = done("shards", t)
    membership = json.loads((gen_dir / "catalog" / "meta.json").read_text())["membership"]
    cat = resumed("catalog", ("catalog",), lambda: fold_catalog(con, [gen_dir / "catalog", *(d / "catalog" for d in run_dirs)], out, work,
                                                                new_gen=new_gen, plan=plan, membership=membership, rule=rule, jobs=jobs,
                                                                merged=merging.result if merging is not None else None))
    t = done("catalog", t, "drill")
    dm = json.loads((gen_dir / "drill" / "meta.json").read_text())
    tiers = [sd.Tier(gen_dir / "drill", "base"), *(sd.Tier(d / "drill", r["key"]) for d, r in zip(run_dirs, runs))]
    drill = resumed("drill", ("drill", "roots-measure"), lambda: fold_drill(
        con, tiers, out, work / "drill", R=dm["R"], K=dm["K"], floor=dm.get("floor", sl.FLOOR), chunk_rows=chunk_rows, jobs=jobs, step=step,
        log=lambda s: log(f"compact {gen} → {new_gen}: {s}"), ck=fck, staged=staging.result if staging is not None else lambda: None,
        fanout=fanout, meas=gen_dir / "roots-measure", scan=days[-1]), after_saves=True)
    staged = staging.result() if staging is not None else None
    has_anchors = (gen_dir / an.ANCHORS / "meta.json").is_file()
    t = done("drill", t, "anchors" if has_anchors else None)
    anchors = None
    if has_anchors:
        am = json.loads((gen_dir / an.ANCHORS / "meta.json").read_text())
        # the rollups' helper tasks read the name and suffix shards uploaded under `ck` (specs/static-anchors-fanout.md)
        afan = (lambda: anchor_fanout(ck, days[-1])) if anchor_fanout is not None and ck is not None else None
        anchors = resumed("anchors", (an.NAMES, an.ANCHORS),
                          lambda: sl.build_base_anchors(con, out, R=am["R"], K=am["K"], scans=[s["id"] for s in doc["scans"]], rule=rule, fanout=afan))
        t = done("anchors", t)
    (out / "manifests").mkdir()
    ids = [s["id"] for s in doc["scans"]]
    (out / "manifests" / f"{ids[-1]}.json").write_text(json.dumps(sa.manifest(new_gen, ids, []), indent=1) + "\n")
    saved()
    disk.close()
    return {"from": gen, "gen": new_gen, "runs": [r["key"] for r in runs], "base_scans": len(base["scans"]), "scans": len(ids),
            "versions": versions, "suffix_rows": suffix_rows, "shards": len(plan["shards"]),
            "members": drill["members"], "cells_rows": cat["cells_rows"], "drill": drill, "anchors": anchors, "s": round(monotonic() - t0, 1),
            "staged": staged.doc if staged is not None else None, "stage_log": stage_log,
            "stages": times, "steps": steps, "peaks": peaks}


def fold(gen_dir: Path, out: Path, work: Path, *, gen: str, new_gen: str, runs: list[dict], threads: int = 4, mem: str = "4GB",
         chunk_rows: int = sl.CHUNK_ROWS, jobs: int = 1, progress: Callable[..., None] | None = None, checkpoint: str | None = None,
         source: str | None = None, stage: str = "auto", fanout: Callable[[Checkpoint, str], object] | None = None,
         anchor_fanout: Callable[[Checkpoint, str], object] | None = None) -> dict:
    """`append_runner.Compactor.fold` for the static store (`fold_gen`; `jobs`: key ranges, census shards and drill-fold chunks at a time).
    `checkpoint`: a store root (the scratch bucket) for `compact_checkpoint`. `source`, `stage`: the drill's roots staged
    from the generation's store URL (`stage_roots`). `fanout`, `anchor_fanout`: the drill measurement's and the base
    anchors' helper tasks, given the checkpoint (`static_fanout.Fanout`, `AnchorFanout`; need `checkpoint`)."""
    ck = compact_checkpoint(checkpoint, gen, new_gen, runs, work=work / "ckpt") if checkpoint else None
    try:
        return fold_gen(gen_dir, out, work, gen=gen, new_gen=new_gen, runs=runs, threads=threads, mem=mem, chunk_rows=chunk_rows, jobs=jobs,
                        progress=progress, ck=ck, source=source, stage=stage, fanout=fanout, anchor_fanout=anchor_fanout)
    finally:
        if ck is not None:
            ck.close()


#: Under a checkpoint root: the folds' checkpoints, `<new generation>/<inputs>/` (`compact_checkpoint`).
CHECKPOINTS = f"{PREFIX}/compact-checkpoints"


def compact_checkpoint(root: str, gen: str, new_gen: str, runs: list[dict], **kw) -> Checkpoint:
    """The fold of `gen` and `runs` into `new_gen`'s checkpoint under `root`: keyed by its inputs (a later compaction
    into the same `new_gen` folds more runs) and, as every `Checkpoint`, by the code (another build's isn't resumed)."""
    from .blobstore import store_url

    return Checkpoint(store_url(root), checkpoint_prefix(gen, new_gen, runs), code=code_version(), **kw)


#: A fold's checkpoint steps, in the order it saves them (`fold_gen`'s parts, the drill's own within it).
STEPS = ("intervals", "shards", "catalog", "fold/long-roots-tables", "fold/long-roots", "fold/spilled-tables", "fold/spilled", "drill", "anchors")


def checkpoint_prefix(gen: str, new_gen: str, runs: list[dict]) -> str:
    """`compact_checkpoint`'s prefix under its root: the fold of `gen` and `runs` into `new_gen`."""
    inputs = hashlib.sha256(json.dumps([gen, [r["key"] for r in runs]]).encode()).hexdigest()[:16]
    return f"{CHECKPOINTS}/{new_gen}/{inputs}"


def same_bucket(a: str, b: str) -> tuple[str, str, str] | None:
    """Two store roots (URLs) in one bucket: `(bucket URL, a's key prefix, b's key prefix)`, each prefix empty for the
    bucket's own root or ending in `/` (gcs's race seeded from `gs://<scratch>` itself: an empty prefix joined with `/`
    made keys `/static-names/…`); None for different buckets or a local dir."""
    def split(u: str) -> tuple[str, str] | None:
        if "://" not in u:
            return None
        scheme, rest = u.split("://", 1)
        bucket, _, path = rest.partition("/")
        return f"{scheme}://{bucket}", f"{path.strip('/')}/" if path.strip("/") else ""

    sa, sb = split(a), split(b)
    return (sa[0], sa[1], sb[1]) if sa and sb and sa[0] == sb[0] else None


def seed_checkpoint(src_root: str, dst_root: str, gen: str, new_gen: str, runs: list[dict], through: str, *, workers: int = 32,
                    log: Callable[[str], None] = err) -> dict:
    """Another run's checkpoint of the same fold (`src_root`'s), its saved steps up to and including `through`
    (`STEPS`' order), copied under `dst_root` and claimed for this code (`code.json`: the steps after `through` run on
    it): a fold resumed there on new code, the source never written (a run still holding it goes on). The copied steps
    were made by the code that saved them; that they equal this code's is what CI's byte-identity tests hold (the
    drill's measurement at any pool width, fan-out, staging). So are the fan-outs' finished units of the step after
    `through` (`helper_units`): the drill measurement's ranges when the drill isn't copied, the anchors' shard tasks when
    the anchors aren't; the resumed fold deals only the rest. Same bucket: copied server side. Returns the steps, files
    and units copied."""
    from concurrent.futures import ThreadPoolExecutor

    from .blobstore import Store, store_url
    from .checkpoint import code_version
    from .static_fanout import ANCHORED, ANCHORS_IN, MEASURED

    if through not in STEPS:
        raise SystemExit(f"--through {through}: one of {', '.join(STEPS)}")
    src_root, dst_root = store_url(src_root).rstrip("/"), store_url(dst_root).rstrip("/")
    if src_root == dst_root:
        raise SystemExit(f"{dst_root}: the seeded checkpoint must be another root than its source")
    prefix = checkpoint_prefix(gen, new_gen, runs)
    src = Store(src_root)
    steps = [st for st in STEPS[:STEPS.index(through) + 1] if src.exists(f"{prefix}/{st}/done.json")]
    keys = [k for st in steps for k in src.keys(f"{prefix}/{st}/")]
    units = []
    if "fold/spilled" in steps and "drill" not in steps:
        units += helper_units(src, f"{prefix}/fold/{MEASURED}/")
    if "drill" in steps and "anchors" not in steps:
        units += helper_units(src, f"{prefix}/{ANCHORED}/")
        keys += src.keys(f"{prefix}/{ANCHORS_IN}/")
    keys += [k for u in units for k in u]
    same = same_bucket(src_root, dst_root)
    if same is not None:
        base, src_in, dst_in = same
        within = Store(base)
        copy = lambda k: within.copy(f"{src_in}{k}", f"{dst_in}{k}")  # noqa: E731
    else:
        import tempfile

        dst, tmp = Store(dst_root), Path(tempfile.mkdtemp(prefix="seed-"))

        def copy(k: str) -> None:
            f = tmp / k
            src.get(k, f)
            dst.put(f, k)
            f.unlink()
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(copy, keys))
    Store(dst_root).write_text(f"{prefix}/code.json", json.dumps({"code": code_version()}) + "\n")
    log(f"seed checkpoint: {len(keys)} files of steps {', '.join(steps)} (+ {len(units)} finished helper units) from {src_root} to "
        f"{dst_root}/{prefix}/")
    return {"steps": steps, "files": len(keys), "units": len(units)}


def helper_units(src, root: str) -> list[list[str]]:
    """A fan-out's finished units under `root` (`<root><kind>/<unit>/…`, `static_fanout`): each unit dir holding a
    `done.json`, its files (the `done.json` last). Plans and task reports aren't units."""
    by: dict[str, list[str]] = {}
    for k in src.keys(root):
        rel = k[len(root):].split("/")
        if len(rel) == 3:
            by.setdefault("/".join(rel[:2]), []).append(k)
    return [sorted(fs, key=lambda k: k.endswith("/done.json")) for fs in by.values() if any(k.endswith("/done.json") for k in fs)]


def clear_checkpoints(root: str, new_gen: str) -> int:
    """Every fold checkpoint into `new_gen` under `root` (its compaction published, or superseded); the files deleted."""
    from .blobstore import Store, store_url

    store = Store(store_url(root))
    return store.delete(sorted(store.listing(f"{CHECKPOINTS}/{new_gen}")))


def prebuilt_fold(url: str) -> Callable[..., dict]:
    """A `Compactor.fold` that adopts a fold done elsewhere (`compact-probe -U url`: `<url>/gen/` and its report
    `<url>/probe.json`, written last): refused unless the report folds the same generation and runs into the same
    successor; its files downloaded into `out`, each one's size and md5 checked against the report. The race's output
    published as the compaction's own (the lease, the supersession checks, the upload and pointers unchanged)."""
    from concurrent.futures import ThreadPoolExecutor

    from .blobstore import Store

    def fold(gen_dir: Path, out: Path, work: Path, *, gen: str, new_gen: str, runs: list[dict], progress=None, **_) -> dict:
        src = Store(url.rstrip("/"))
        doc = src.read_json("probe.json")
        want = (gen, new_gen, [r["key"] for r in runs])
        if (doc["gen"], doc["new_gen"], doc["runs"]) != want:
            raise SystemExit(f"{url}: folds {doc['gen']} → {doc['new_gen']} over {doc['runs']}, not {want[0]} → {want[1]} over {want[2]}")
        files = doc["files"]

        def get(rel: str) -> str | None:
            f = out / rel
            src.get(f"gen/{rel}", f)
            h = hashlib.md5()
            with open(f, "rb") as fh:
                while b := fh.read(1 << 24):
                    h.update(b)
            ok = f.stat().st_size == files[rel]["size"] and h.hexdigest() == files[rel]["md5"]
            return None if ok else rel

        with ThreadPoolExecutor(32) as ex:
            bad = [r for r in ex.map(get, sorted(files)) if r]
        if bad:
            raise SystemExit(f"{url}: {len(bad)} of {len(files)} files differ from the report (e.g. {bad[0]})")
        if extra := sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file() and p.relative_to(out).as_posix() not in files):
            raise SystemExit(f"{url}: files the report doesn't list ({extra[0]}, …)")
        err(f"compact {gen} → {new_gen}: adopted {len(files)} files from {url} (md5 checked)")
        return {**doc["summary"], "prebuilt": url}

    return fold


def compactor(chunk_rows: int = sl.CHUNK_ROWS, checkpoint: str | None = None, stage: str = "auto",
              fanout: Callable[[Checkpoint, str], object] | None = None, prebuilt: str | None = None,
              anchor_fanout: Callable[[Checkpoint, str], object] | None = None) -> ar.Compactor:
    """The static store's plug into the shared compaction (`append_runner.compact_store`): its fold and upload order; its
    predecessors are left to `store-gc -k` (readers pin `STATIC_GEN`, so a compacted generation may still be served).
    `checkpoint`: the folds' checkpoint root (`fold`). `stage`: the drill's roots staged on the work disk (`STAGE_MODES`;
    from the store, given as the fold's `source`). `fanout`, `anchor_fanout`: its drill measurement's and base anchors'
    helper tasks (`fold`). `prebuilt`:
    adopt a fold done elsewhere instead (`prebuilt_fold`)."""
    folder = prebuilt_fold(prebuilt) if prebuilt else (lambda *a, **kw: fold(*a, **kw, chunk_rows=chunk_rows, checkpoint=checkpoint, stage=stage, fanout=fanout,
                                                                                         anchor_fanout=anchor_fanout))
    return ar.Compactor(prefix=PREFIX, fold=folder,
                        publish_order=sl.publish_order, last=("scans.json", "catalog/meta.json", "drill/meta.json", f"{an.ANCHORS}/meta.json"),
                        prune=False, md5s=True, keep=reads, publish_tier=sl.publish_tier, source=True)


#: What the fold never reads of the base: the anchored search (rebuilt), the parity markers. (Its measurement,
#: `roots-measure/`, is read: an incremental compaction's `static_measure` starts from it.)
UNREAD = (f"{an.NAMES}/", "verify/")


def reads(rel: str) -> bool:
    """Whether the fold reads generation-relative `rel` (`Compactor.keep`): all but `UNREAD` and the base's `anchors/`
    (whose `meta.json` says the base has anchored search)."""
    return not rel.startswith(UNREAD) and (not rel.startswith(f"{an.ANCHORS}/") or rel == f"{an.ANCHORS}/meta.json")


# ── CLI (one machine) ──────────────────────────────────────────────────────


@command("compact")
@option("-c", "--chunk-rows", default=sl.CHUNK_ROWS, type=IntRange(min=1), help="The drill's chunks: roots per measurement / rollup q range")
@option("-g", "--gen", required=True, help="The generation (`<store>/static-names/GEN/`); one compacted already is followed to its newest successor")
@option("-j", "--jobs", default=1, type=IntRange(min=1), help="Key ranges, census shards and drill-fold chunks folded this many at a time, on cursors of one DuckDB database (-p threads and -M memory shared); the bytes don't depend on it")
@option("-M", "--mem", default="4GB", help="DuckDB memory limit")
@option("-o", "--out", "store_root", required=True, help="The store: a dir or URL (`r2://bucket[/prefix]`, `s3://…`, `gs://…`)")
@option("-p", "--threads", default=4, type=IntRange(min=1), help="DuckDB threads")
@option("-s", "--stage", default="auto", type=Choice(STAGE_MODES), help="Stage the drill's roots on the work disk (auto: when the generation isn't there already and the disk has room)")
@option("-w", "--work", help="Local work dir (default: a temporary one, removed after); the generation is mirrored into it")
def compact_cmd(chunk_rows, gen, jobs, mem, store_root, stage, threads, work) -> None:
    """Fold the generation's base and its newest manifest's runs into a new generation now (`<gen>.c<NNN>`, equal to
    `build -r <gen>/ranges.json` through the last scan), whatever the compaction policy says; published (data, then
    `compaction.json`), then `successor.json` in the old generation and `static-names/CURRENT.json` when it named it (or
    nothing). The old generation stays (`store-gc -k` collects it). Prints a JSON summary."""
    from .blobstore import Store, mirror

    sl._check_gen(gen)
    store = Store(store_root)
    gen = ar.resolve_gen(store, PREFIX, gen)
    prefix = f"{PREFIX}/{gen}"
    with sl.workdir(work) as w:
        gen_dir = w / "store" / prefix
        mirror(store, prefix, gen_dir)
        why = ar.forced(gen_dir, "forced (compact)")
        if why is None:
            raise SystemExit(f"{store.root}/{prefix}: no runs to compact")
        out = ar.compact_store(store, compactor(chunk_rows, stage=stage), gen, gen_dir, w, policy=ar.NEVER, why=why, threads=threads, mem=mem, jobs=jobs)
    print(json.dumps(out, indent=1))
