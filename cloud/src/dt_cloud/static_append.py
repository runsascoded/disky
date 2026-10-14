"""The static name index's per-scan append (specs/static-append.md): each new scan becomes a small
**run** beside an immutable base generation, published by an immutable per-scan manifest, merged into bigger
runs on a binary counter, read by merging base and runs (the smallest `vt` of a version's rows wins).

Per scan `D`, under `static-names/<gen>/deltas/<D>/`:

1. `append` (per key range, Batch): the range's open coalesced versions (`copen`; the base's `cintervals`
   with `vt` = OPEN on the first run) and `D`'s rows → `pyrmts.intervals.append_intervals` on the answer
   columns alone (`size`, `n_files`) → `cdelta/r####.parquet` (opened `op` 1, closed `op` −1), `dhist/`
   (its suffix rows per three-character prefix) and the next `copen` (scratch bucket).
2. `shards` (one task): the delta's suffix rows, opens and closes alike (a close record is the version's
   rows with their final `vt`), planned and sorted into the run's own `shards.json`, `sx/`, `sidecar/`.
3. `catalog` (one task): `static_catalog.append` from the base + live runs' merged catalog; the run's
   catalog is what that adds (new cells, changed or new headers).
4. `publish`: `manifests/<D>.json` (written last), the run added at level 0. The binary counter's carries run apart
   (`static_merge`, `runs merge`), each published as a revision `manifests/<id>.m<NNN>.json` of the newest manifest.
5. `prune`: once `D`'s state is complete (every range's `copen` and `done/` marker, and `manifests/<D>.json`),
   every earlier scan's `state/<prev>/` goes: only the newest complete state is kept. A lost state is rebuilt
   from the base's `cintervals` and every run's `cdelta` (`rebuild-state`).
"""
from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Collection
from pathlib import Path
from time import monotonic

import pyarrow as pa
import pyarrow.parquet as pq
from click import IntRange, group, option

from . import static_catalog as sc
from .append_runner import COMPACT_LEVEL, StateIncomplete, latest_key, run_key, scans_of  # noqa: F401 — shared with the interval store
from .hex_runs import HexRule, occurs, occurs_sql, rule_json
from .static_profile import data_bucket, layouts as profile_layouts, profile, scratch_bucket
from .static_names import (
    ANSWER_COLS, CINTERVAL_SCHEMA, CODEC, INTERVAL_RG, KEY_COLS, OPEN, PREFIX, SX_RG,
    SX_SCHEMA, Reader, _batches, _src, _sx_cast, _task, answer_rows, connect, err, hist_sql, q, range_preds,
    SCAN_ID, gen_rule_at, read_json, scan_epoch, scan_sql, sidecar_rows, suffix_sql, upload_tree, write_sorted,
)

CDELTA_SCHEMA = CINTERVAL_SCHEMA.append(pa.field("op", pa.int8(), nullable=False))
#: A run's shards: about this many suffix rows each (the base's target).
RUN_SHARD_ROWS = 50_000_000
# ── 1. Coalesced append on the open versions ───────────────────────────────


def append_open(con, prev_sql: str, scan: dict, r: dict, name: str, out: Path, *, bucket: str, mount: str | None,
                rule: HexRule | None = None) -> dict:
    """Append `scan` to one key range's open coalesced versions (`prev_sql`: `CINTERVAL_SCHEMA` rows; closed ones
    may ride along and are ignored), keyed on `ANSWER_COLS` alone, so the runs are the coalesced versions
    directly. Writes `cdelta/<name>.parquet` (`CDELTA_SCHEMA`, sorted `(depth, path, usr, vf, op)`),
    `copen/<name>.parquet` (the open versions after `scan`, sorted) and `dhist/<name>.parquet` (the delta's
    suffix rows per three-character prefix) under `out`."""
    from pyrmts.intervals import append_intervals, delta_sql

    t0 = monotonic()
    D = scan["ts"]
    cols = ", ".join([*KEY_COLS, *ANSWER_COLS])
    new = f"SELECT {cols} FROM ({scan_sql(con, _src(bucket, scan['src'], mount), range_preds(r), scan.get('version'))})"
    con.execute(f"CREATE OR REPLACE TABLE pv AS SELECT {', '.join(CINTERVAL_SCHEMA.names)} FROM ({prev_sql}) WHERE vt = {OPEN}")
    n_open, n_close = append_intervals(con, "SELECT * FROM pv", new, KEY_COLS, ANSWER_COLS, D, OPEN, out="civ")
    con.execute(f"CREATE OR REPLACE TABLE cdl AS SELECT * FROM ({delta_sql('civ', D)})")
    write_sorted(_batches(con, "SELECT * FROM cdl ORDER BY depth, path, usr, vf, op"), out / "cdelta" / f"{name}.parquet",
                 CDELTA_SCHEMA, INTERVAL_RG, dictionary=["usr"])
    rows = write_sorted(_batches(con, f"SELECT * FROM civ WHERE vt = {OPEN} ORDER BY depth, path, usr, vf"),
                        out / "copen" / f"{name}.parquet", CINTERVAL_SCHEMA, INTERVAL_RG, dictionary=["usr"])
    (out / "dhist").mkdir(parents=True, exist_ok=True)
    pq.write_table(con.execute(hist_sql("cdl", rule)).to_arrow_table(), out / "dhist" / f"{name}.parquet", compression=CODEC)
    con.execute("DROP TABLE pv; DROP TABLE civ; DROP TABLE cdl")
    doc = {"range": name, "scan": scan["id"], "opened": n_open, "closed": n_close, "open": rows, "s": round(monotonic() - t0, 1)}
    err(f"append {name} {scan['id']}: {n_open:,} opened, {n_close:,} closed, {rows:,} open in {doc['s']}s")
    return doc


# ── 2. A run's shards ──────────────────────────────────────────────────────


def write_run_shards(batches, out: Path, target_rows: int = RUN_SHARD_ROWS, plan: dict | None = None) -> dict:
    """Sorted suffix-row batches (`SX_SCHEMA`) as shards, cut on the fly. Without `plan`, a shard closes at the first
    three-character-prefix boundary after it holds `target_rows` rows (a prefix is never split, so a literal's rows
    are one contiguous range of one file). With `plan` (a `shards.json`), at its boundaries instead: shard `i` holds
    the prefixes in `[lo, hi)`, written (empty or not) as the build writes it — a compaction cut to a full build's
    plan is that build, byte for byte. Writes `sx/s####.parquet` in `SX_RG`-row groups, `sidecar/s####.parquet`,
    `sidecar.parquet` and `shards.json` (`[lo, hi)` per shard)."""
    from itertools import groupby

    import pyarrow.compute as pc

    state = {"sid": 0, "n": 0, "last": None}
    shards: list[dict] = [] if plan is None else [{"i": x["i"], "lo": x["lo"], "rows": 0, "p3": set()} for x in plan["shards"]]
    los = [x["lo"] for x in shards]

    def tagged():
        for b in batches:
            if b.num_rows == 0:
                continue
            p3 = pc.utf8_slice_codeunits(b.column("s"), 0, 3)
            off = 0
            while off < b.num_rows:
                rest, rp3 = b.slice(off), p3.slice(off)
                cut = rest.num_rows
                if plan is not None:
                    while state["sid"] + 1 < len(los) and rp3[0].as_py() >= los[state["sid"] + 1]:
                        state["sid"] += 1
                    if state["sid"] + 1 < len(los):
                        i = pc.index(pc.greater_equal(rp3, los[state["sid"] + 1]), True).as_py()
                        if i > 0:
                            cut = i
                elif state["n"] >= target_rows:
                    i = pc.index(pc.not_equal(rp3, state["last"]), True).as_py()
                    if i == 0:
                        state["sid"] += 1
                        state["n"] = 0
                    elif i > 0:
                        cut = i
                if plan is None and state["n"] == 0 and state["sid"] == len(shards):
                    shards.append({"i": state["sid"], "lo": rp3[0].as_py(), "rows": 0, "p3": set()})
                sh = shards[state["sid"]]
                sh["rows"] += cut
                sh["p3"].update(pc.unique(rp3.slice(0, cut)).to_pylist())
                state["n"] += cut
                state["last"] = rp3[cut - 1].as_py()
                off += cut
                yield state["sid"], rest.slice(0, cut)

    out.mkdir(parents=True, exist_ok=True)
    (out / "sidecar").mkdir(parents=True, exist_ok=True)

    def write(sid: int, batches_) -> None:
        name = f"s{sid:04d}"
        stats: list[tuple[str, str, int]] = []

        def on_group(g: pa.Table) -> None:
            col = g.column("s")
            stats.append((col[0].as_py(), col[g.num_rows - 1].as_py(), g.num_rows))

        dst = out / "sx" / f"{name}.parquet"
        write_sorted(batches_, dst, SX_SCHEMA, SX_RG, on_group=on_group, dictionary=["usr"])
        pq.write_table(sidecar_rows(dst, f"sx/{name}.parquet", stats), out / "sidecar" / f"{name}.parquet", compression=CODEC)

    written = set()
    for sid, grp in groupby(tagged(), key=lambda x: x[0]):
        write(sid, (b for _, b in grp))
        written.add(sid)
    for sh in shards:
        if sh["i"] not in written:
            write(sh["i"], iter(()))
    for k, sh in enumerate(shards):
        sh["hi"] = shards[k + 1]["lo"] if k + 1 < len(shards) else None
        sh["prefixes"] = len(sh.pop("p3"))
        if plan is not None and sh["rows"] != plan["shards"][k]["rows"]:
            raise RuntimeError(f"shard {k}: {sh['rows']:,} rows written, {plan['shards'][k]['rows']:,} planned")
    total = sum(sh["rows"] for sh in shards)
    doc = {"target_rows": target_rows, "total_rows": total, "shards": [{k: sh[k] for k in ("i", "lo", "hi", "rows", "prefixes")} for sh in shards]}
    (out / "shards.json").write_text(json.dumps(plan if plan is not None else doc, indent=1) + "\n")
    side = pa.concat_tables([pq.read_table(out / "sidecar" / f"s{sh['i']:04d}.parquet") for sh in shards]) if shards else sidecar_rows_empty()
    pq.write_table(side, out / "sidecar.parquet", compression=CODEC, row_group_size=1 << 20)
    return {"rows": total, "shards": len(shards), "row_groups": side.num_rows,
            "bytes": sum(f.stat().st_size for f in (out / "sx").glob("*.parquet")) if (out / "sx").exists() else 0}


def sidecar_rows_empty() -> pa.Table:
    from .static_names import SIDECAR_SCHEMA

    return pa.table({f.name: pa.array([], f.type) for f in SIDECAR_SCHEMA}, schema=SIDECAR_SCHEMA)


def delta_shards(con, cdelta_files: list[str], out: Path, target_rows: int = RUN_SHARD_ROWS, rule: HexRule | None = None) -> dict:
    """One scan's run shards from its `cdelta` files: every version's suffix rows (depth ≥ 1), the opened ones
    open and the closed ones with their final `vt` (close records), sorted `(s, path, usr, vf)` by DuckDB."""
    sql = f"SELECT s, depth, path, usr, vf, vt, size, n_files FROM ({suffix_sql(cdelta_files, rule=rule)}) ORDER BY s, path, usr, vf"
    return write_run_shards((_sx_cast(b) for b in _batches(con, sql)), out, target_rows)


def _run_batches(d: Path, columns: list[str] | None = None):
    """A run's suffix rows in order: its shard files (in prefix order) row group by row group."""
    from pyrmts.runs import parquet_batches

    for f in sorted((d / "sx").glob("*.parquet")):
        yield from parquet_batches(f, columns)


def merge_shards(run_dirs: list[Path], out: Path, target_rows: int = RUN_SHARD_ROWS, plan: dict | None = None) -> dict:
    """Runs (oldest first; the base first for a compaction) merged by `pyrmts.runs.merge_sorted` (streaming k-way):
    rows equal on `(s, path, usr, vf)` are one version's row, and the smallest `vt` wins (a close record over the row
    it closes). `plan`: cut to that shard plan (a compaction into a new base) instead of by `target_rows`."""
    from pyrmts.runs import merge_sorted

    stream = merge_sorted([_run_batches(d) for d in run_dirs], ["s", "path", "usr", "vf"], reduce={"vt": "min"})
    return write_run_shards(stream, out, target_rows, plan)


def merge_cdeltas(files_oldest_first: list[str], out: Path) -> int:
    """Version deltas merged (`(depth, path, usr, vf)` identity, smallest `vt`, `op` 1 if opened in any)."""
    from pyrmts.runs import merge_parquets

    stream = merge_parquets(files_oldest_first, ["depth", "path", "usr", "vf", "op"], identity=["depth", "path", "usr", "vf"],
                            reduce={"vt": "min", "op": "max"})
    return write_sorted(stream, out, CDELTA_SCHEMA, INTERVAL_RG, dictionary=["usr"])


# ── 3. Catalogs ────────────────────────────────────────────────────────────


def merge_drills(dirs: list[Path], out: Path, run: dict, tmp: str | Path | None = None, *, threads: int = 16, mem: str = "100GB") -> dict:
    """The merged run's `drill/` (`static_drill.merge_tiers` of the inputs', oldest first): every input must have one (the
    counter merges runs carrying `drill/` only with each other, `push_run`)."""
    from .static_drill import Tier, merge_tiers

    if lack := [d.name for d in dirs if not (d / "drill" / "meta.json").exists()]:
        raise ValueError(f"{run['key']}: inputs without drill/: {lack}")
    con = connect(threads, mem, tmp)
    return merge_tiers(con, [Tier(d / "drill", d.name) for d in dirs], out,
                       tier={k: run[k] for k in ("key", "first", "last", "level", "scans")})


def merge_catalogs(tiers: list[Path], out: Path, membership: dict | None = None, rule: HexRule | None = None) -> dict:
    """Tiers' catalogs (oldest first) merged into `out/{cells,index}.parquet` by `pyrmts.runs` on `(q, bucket, vf)`,
    the newest tier's row winning (headers; cells are never duplicated across tiers): the base plus every run is
    the whole catalog, equal to a rebuild; runs alone, a merged run's catalog. `membership` (a merged run's: the
    newest input's): also `out/meta.json`, which the readers require of every tier."""
    from pyrmts.runs import merge_parquets

    out.mkdir(parents=True, exist_ok=True)
    stream = merge_parquets([t / "cells.parquet" for t in tiers], ["q", "bucket", "vf"], reduce="newest")
    rows, index = sc.write_cells(stream, out / "cells.parquet")
    pq.write_table(index, out / "index.parquet", compression=CODEC)
    doc = {"cells_rows": rows, "row_groups": index.num_rows, "bytes": (out / "cells.parquet").stat().st_size}
    if membership is not None:
        meta = {**doc, "index_bytes": (out / "index.parquet").stat().st_size, "cell_rg": sc.CELL_RG, "membership": membership,
                **rule_json(rule)}
        (out / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    return doc


#: What every run a manifest lists must hold (the readers' files), checked before the manifest is written.
RUN_FILES = ("meta.json", "shards.json", "sidecar.parquet", "catalog/meta.json", "catalog/cells.parquet", "catalog/index.parquet")


def missing_files(runs: list[dict], exists: Callable[[str], bool], drill_meta: Callable[[str], dict | None] = lambda key: None) -> list[str]:
    """`<run key>/<file>` for every `RUN_FILES` entry a listed run lacks (`exists(key/file)`), and for a run with a drill
    (`drill_meta(key)`: its `drill/meta.json`, or None) every file of it (`static_drill.tier_files`)."""
    from .static_drill import tier_files

    out = []
    for r in runs:
        meta = drill_meta(r["key"])
        out += [f"{r['key']}/{f}" for f in [*RUN_FILES, *(f"drill/{f}" for f in (tier_files(meta) if meta else []))] if not exists(f"{r['key']}/{f}")]
    return out


def catalog_delta(con, tiers: list[Path], base: sc.BaseShards, deltas: list[list[str]], V: int, out: Path, tmp: Path,
                  rule: HexRule | None = None) -> dict:
    """A run's catalog: `static_catalog.append` from the merged `tiers` (the base and every live run) and every
    scan's `cdelta` since the base (`deltas`, oldest first, the last = this run's scan), minus what the tiers
    hold: the new cells and the new or changed headers."""
    prev, full = tmp / "prev", tmp / "full"
    for d in (prev, full):
        if d.exists():
            shutil.rmtree(d)
    merge_catalogs(tiers, prev)
    doc = sc.append(con, prev=prev, base=base, deltas=deltas, V=V, out=full, rule=rule)
    pf, ff = q(str(prev / "cells.parquet")), q(str(full / "cells.parquet"))
    gone = con.execute(f"SELECT count(*) FILTER (WHERE bucket <> ''), count(*) FROM (SELECT * FROM read_parquet({pf}) EXCEPT SELECT * FROM read_parquet({ff}))").fetchone()
    if gone[0]:
        raise RuntimeError(f"catalog append dropped {gone[0]} cells: cells are only ever added")
    out.mkdir(parents=True, exist_ok=True)
    rows, index = sc.write_cells(_batches(con, f"SELECT * FROM (SELECT * FROM read_parquet({ff}) EXCEPT SELECT * FROM read_parquet({pf})) ORDER BY q, bucket, vf"),
                                 out / "cells.parquet")
    pq.write_table(index, out / "index.parquet", compression=CODEC)
    heads = con.execute(f"SELECT count(*) FROM read_parquet({q(str(out / 'cells.parquet'))}) WHERE bucket = ''").fetchone()[0]
    meta = {"cells_rows": rows, "row_groups": index.num_rows, "bytes": (out / "cells.parquet").stat().st_size,
            "index_bytes": (out / "index.parquet").stat().st_size, "cell_rg": sc.CELL_RG, "headers": heads,
            "headers_changed": gone[1], "membership": {"max_rows": V}, "append": doc, **rule_json(rule)}
    (out / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    return meta


# ── Readers over base + runs (the Worker's merge) ──────────────────────────


def combine_rows(rows: list[dict]) -> list[dict]:
    """Suffix rows from several tiers: one per `(s, path, usr, vf)`, the smallest `vt` (a close record wins)."""
    best: dict[tuple, dict] = {}
    for r in rows:
        k = (r["s"], r["path"], r["usr"], r["vf"])
        cur = best.get(k)
        if cur is None or r["vt"] < cur["vt"]:
            best[k] = r
    return list(best.values())


class TieredReader:
    """The suffix reader over a base and its runs: every tier's range rows, combined, then `answer_rows`."""

    def __init__(self, readers: list[Reader], rule: HexRule | None = None):
        self.readers, self.rule = readers, rule

    def rows(self, key: str) -> tuple[list[dict], dict]:
        rows, io = [], {"groups": 0, "bytes": 0, "rows_read": 0, "files": 0, "tiers": len(self.readers)}
        for r in self.readers:
            got, i = r.rows(key)
            rows += got
            for k in ("groups", "bytes", "rows_read", "files"):
                io[k] += i[k]
        return combine_rows(rows), io

    def answer(self, term: str, dates: list[str]) -> dict:
        key = term.lower()
        rows, io = self.rows(key)
        return answer_rows(key, rows, io, dates, self.rule)

    def hits(self, term: str) -> list[tuple]:
        """Every first hit `(path, usr, vf, vt, size, n_files)` (ms stamps), sorted: what the map filter cuts by path."""
        from .static_names import _ms

        key = term.lower()
        rows, _ = self.rows(key)
        out = set()
        for r in rows:
            name = r["path"].rsplit("/", 1)[-1].lower()
            parent = r["path"].rsplit("/", 1)[0].lower() if "/" in r["path"] else ""
            if r["depth"] >= 1 and occurs(key, name, self.rule) and not occurs(key, parent, self.rule):
                out.add((r["path"], r["usr"], _ms(r["vf"]), _ms(r["vt"]), r["size"], r["n_files"]))
        return sorted(out)


class TieredCatalog:
    """The catalog over a base and its runs: a literal is a member iff some tier holds its header; its header is
    the newest tier's, its cells the union (their count checked against the header)."""

    def __init__(self, catalogs: list[sc.Catalog]):
        self.catalogs = catalogs

    def rows(self, key: str) -> list[dict] | None:
        head, cells = None, []
        for c in self.catalogs:
            got = c.rows(key)
            if got is None:
                continue
            head = got[0]
            cells += got[1:]
        if head is None:
            return None
        cells.sort(key=lambda c: (c["bucket"], c["vf"]))
        if len(cells) != head["o"]:
            raise RuntimeError(f"catalog {key!r}: header counts {head['o']} cells, the tiers hold {len(cells)}")
        return [head, *cells]

    def answer(self, term: str, dates: list[str]) -> dict | None:
        key = term.lower()
        cells = self.rows(key)
        if cells is None:
            return None
        head, body = cells[0], cells[1:]
        out = {}
        for d in dates:
            D = scan_epoch(d)
            cur: dict[str, tuple[int, int]] = {}
            for c in body:
                if c["vf"] <= D:
                    cur[c["bucket"]] = (c["b"], c["o"])
            out[d] = {k: list(v) for k, v in sorted(cur.items()) if v != (0, 0)}
        return {"q": key, "rows": head["b"], "cells": head["o"], "answers": out}


# ── 4. The binary counter and manifests ────────────────────────────────────


def push_run(runs: list[dict], new: dict, drilled: Collection[str] = frozenset()) -> tuple[list[dict], list[tuple[list[dict], dict]]]:
    """Add a level-0 run (oldest first) and carry: while the two newest runs share a level, they merge into one a
    level up. `drilled`: the keys of the runs carrying the drilldown's `drill/`. Two runs merge only when both carry one
    (`merge_drills` makes the merged run's) or neither does: a merge of one with and one without would drop the one's
    drill (the reader stops at the first run without one), so it stops the carry there. Returns the runs after, and the
    merges to perform in order (`(inputs, output)`)."""
    drilled = set(drilled)
    runs = [*runs, {**new, "level": 0}]
    merges = []
    while len(runs) >= 2 and runs[-1]["level"] == runs[-2]["level"] and (runs[-1]["key"] in drilled) == (runs[-2]["key"] in drilled):
        a, b = runs[-2], runs[-1]
        m = {"key": run_key(a["first"], b["last"]), "first": a["first"], "last": b["last"], "level": a["level"] + 1,
             "scans": [*a["scans"], *b["scans"]]}
        if a["key"] in drilled:
            drilled.add(m["key"])
        merges.append(([a, b], m))
        runs = [*runs[:-2], m]
    return runs, merges


def drill_scans(runs: list[dict], drilled: Collection[str]) -> list[str]:
    """The scans past the base the drilldown's reader covers: the runs' up to the first without a `drill/`."""
    out = []
    for r in runs:
        if r["key"] not in drilled:
            break
        out += r["scans"]
    return out


def manifest(gen: str, base_scans: list[str], runs: list[dict]) -> dict:
    scans = [*base_scans, *(s for r in runs for s in r["scans"])]
    return {"gen": gen, "date": scans[-1], "base_scans": len(base_scans), "scans": scans,
            "runs": [{k: r[k] for k in ("key", "first", "last", "level", "scans", "rows", "bytes") if k in r} for r in runs]}


# ── 5. The open-version state: the newest complete day only ────────────────


def prune_plan(objects: list[tuple[str, int]], prefix: str, k: int, published: bool, date: str) -> dict:
    """What `prune` deletes, from the scratch bucket's `(name, size)` listing under `<prefix>/state/`: every day
    before `date`, once `date`'s state is complete — all `k` ranges' `copen/r####.parquet` and `done/r####.json`,
    and its run `published` (`manifests/<date>.json`). Raises `StateIncomplete` otherwise. Returns
    `{"date", "keep": [days ≥ date], "delete": [{"day", "objects", "bytes"}], "names": [objects to delete]}`."""
    root = f"{prefix}/state/"
    days: dict[str, dict] = {}
    for name, size in objects:
        if not name.startswith(root):
            raise ValueError(f"{name}: not under {root}")
        day, _, rest = name[len(root):].partition("/")
        if not SCAN_ID.fullmatch(day):
            raise ValueError(f"{name}: {day!r} is not a scan id")
        d = days.setdefault(day, {"names": [], "bytes": 0, "copen": set(), "done": set()})
        d["names"].append(name)
        d["bytes"] += size
        kind, _, file = rest.partition("/")
        if kind == "copen" and file.endswith(".parquet"):
            d["copen"].add(file.removesuffix(".parquet"))
        elif kind == "done" and file.endswith(".json"):
            d["done"].add(file.removesuffix(".json"))
    want = {f"r{i:04d}" for i in range(k)}
    cur = days.get(date, {"copen": set(), "done": set()})
    missing = {"copen": len(want - cur["copen"]), "done": len(want - cur["done"])}
    if missing["copen"] or missing["done"] or not published:
        why = [f"{n} of {k} ranges without {kind}" for kind, n in missing.items() if n] + ([] if published else ["no manifest"])
        raise StateIncomplete(f"state/{date} incomplete: {', '.join(why)}")
    drop = sorted(d for d in days if d < date)
    return {"date": date, "keep": sorted(d for d in days if d >= date),
            "delete": [{"day": d, "objects": len(days[d]["names"]), "bytes": days[d]["bytes"]} for d in drop],
            "names": [n for d in drop for n in sorted(days[d]["names"])]}


def prune_state(gcs, gen: str, date: str, k: int, *, bucket: str | None = None, scratch: str | None = None, dry_run: bool = False,
                workers: int = 8) -> dict:
    """Delete every `state/<prev>/` (prev < `date`) of generation `gen` in the scratch bucket (the profile's,
    nowhere else), once `date`'s state is complete and its run published (`prune_plan`), `workers` deletes at a time
    (`append_runner.prune_state`). Idempotent: a rerun finds nothing before `date`. `gcs`: a
    `google.cloud.storage.Client`. Returns the plan, `deleted` = objects."""
    from .append_runner import prune_state as prune

    prefix = f"{PREFIX}/{gen}"
    return prune(gcs, prefix, date, lambda objects, published: prune_plan(objects, prefix, k, published, date),
                 bucket=bucket or data_bucket(), scratch=scratch or scratch_bucket(), dry_run=dry_run, workers=workers)


def rebuild_open(con, base_cintervals: str, cdeltas_oldest_first: list[str], out: Path) -> int:
    """A key range's open versions after the last of `cdeltas_oldest_first`, refolded from the base's
    `cintervals/r####.parquet` and every run's `cdelta/r####.parquet` since: the base's open rows, minus every
    version a delta closed (`op` −1), plus every version one opened (`op` 1). A version's `(depth, path, usr, vf)`
    is its identity, and one a run opened has that run's `vf`, past every base version's. Writes `copen`'s
    `out` (sorted `(depth, path, usr, vf)`, the append's layout); returns rows."""
    cols = ", ".join(CINTERVAL_SCHEMA.names)
    key = ["depth", "path", "usr", "vf"]
    if cdeltas_oldest_first:
        d = f"read_parquet([{', '.join(q(f) for f in cdeltas_oldest_first)}])"
        on = " AND ".join(f"o.{c} = c.{c}" for c in key)
        sql = f"""SELECT {', '.join(f'o.{c}' for c in CINTERVAL_SCHEMA.names)} FROM (
                SELECT {cols} FROM read_parquet({q(base_cintervals)}) WHERE vt = {OPEN}
                UNION ALL SELECT {cols} FROM {d} WHERE op = 1
            ) o ANTI JOIN (SELECT {', '.join(key)} FROM {d} WHERE op = -1) c ON {on}"""
    else:
        sql = f"SELECT {cols} FROM read_parquet({q(base_cintervals)}) WHERE vt = {OPEN}"
    return write_sorted(_batches(con, f"SELECT * FROM ({sql}) ORDER BY {', '.join(key)}"), out, CINTERVAL_SCHEMA,
                        INTERVAL_RG, dictionary=["usr"])


# ── CLI ────────────────────────────────────────────────────────────────────


@group("runs")
def cli() -> None:
    """The static name index's per-scan runs beside a base generation (specs/static-append.md)."""


def _gcs():
    from google.cloud import storage

    return storage.Client()


def _latest_manifest(bucket: str, gen: str, before: str | None = None) -> dict | None:
    """The newest manifest of the generation (of a scan strictly before `before`, when given), or None: the greatest
    `manifests/<id>.json` or merge revision `manifests/<id>.m<NNN>.json` (`append_runner.latest_key`), which sort by
    scan, then revision."""
    prefix = f"{PREFIX}/{gen}/"
    keys = latest_key([b.name.removeprefix(prefix) for b in _gcs().list_blobs(bucket, prefix=f"{prefix}manifests/")], before)
    return read_json(f"gs://{bucket}/{prefix}{keys}") if keys else None


def _state(bucket: str, gen: str, date: str) -> tuple[dict, list[dict]]:
    """The generation's base scans and the runs live before `date` (from the newest earlier manifest)."""
    base = read_json(f"gs://{bucket}/{PREFIX}/{gen}/scans.json")
    m = _latest_manifest(bucket, gen, before=date)
    return base, (m["runs"] if m else [])


@cli.command("prepare")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-d", "--date", required=True, help="The scan to append")
@option("-g", "--gen", required=True, help="Base generation")
def prepare_cmd(bucket, date, gen) -> None:
    """Pin the scan to append (its newest `path` sort, by GCS generation and md5) as the run's `scans.json`, checking
    it is the next scan after the base and the live runs."""
    from .static_names import _version_from_footer, list_scans

    base, runs = _state(bucket, gen, date)
    have = scans_of(base, runs)
    # The base's layouts (its `scans.json`; gcs's when it names none), so a run's scan is found where the base's were.
    found = list_scans(bucket, layouts=base.get("layouts") or profile_layouts(), start=have[-1])["scans"]
    nxt = [s for s in found if s["id"] > have[-1]]
    if not nxt or nxt[0]["id"] != date:
        raise SystemExit(f"the next scan after {have[-1]} is {nxt[0]['id'] if nxt else 'none'}, not {date}")
    s = nxt[0]
    s["version"] = _version_from_footer(_gcs().bucket(bucket), s["src"])
    doc = {"bucket": bucket, "scans": [s]}
    _gcs().bucket(bucket).blob(f"{PREFIX}/{gen}/{run_key(date, date)}/scans.json").upload_from_string(json.dumps(doc, indent=1) + "\n")
    print(json.dumps(doc, indent=1))


@cli.command("append")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-d", "--date", required=True, help="The scan to append (its run's `scans.json`, from `prepare`)")
@option("-f", "--force", is_flag=True, help="Redo ranges already done")
@option("-g", "--gen", required=True, help="Base generation")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-m", "--mount", required=True, help="Local mount of the data bucket (the scratch bucket beside it)")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-n", "--per-task", default=1, type=IntRange(min=1), help="Ranges per task")
@option("-o", "--out", default="/stage/out", help="Local output dir")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-S", "--scratch", default=scratch_bucket, help="Bucket for the open versions (`state/<D>/copen/`)")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
def append_cmd(bucket, date, force, gen, index, mount, mem, per_task, out, threads, scratch, tmp) -> None:
    """Append the scan to key ranges' open coalesced versions: `deltas/<D>/{cdelta,dhist}/r####.parquet` (data
    bucket; `dhist` written last marks a range done) and `state/<D>/copen/r####.parquet` (scratch). The open
    versions come from the previous run's `copen`, else (the first run) the base's `cintervals`."""
    prefix = f"{PREFIX}/{gen}"
    run = f"{prefix}/{run_key(date, date)}"
    scan = read_json(f"gs://{bucket}/{run}/scans.json")["scans"][0]
    ranges = read_json(f"gs://{bucket}/{PREFIX}/{gen}/ranges.json")
    base, runs = _state(bucket, gen, date)
    prev_day = scans_of(base, runs)[-1]
    smount = str(Path(mount).parent / scratch)
    t = _task(index)
    todo = list(range(t * per_task, min((t + 1) * per_task, ranges["k"])))
    b, sb = _gcs().bucket(bucket), _gcs().bucket(scratch)
    con = connect(threads, mem, tmp)
    for i in todo:
        name = f"r{i:04d}"
        if not force and b.blob(f"{run}/dhist/{name}.parquet").exists():
            err(f"append {name}: done")
            continue
        if runs:
            src = f"{smount}/{prefix}/state/{prev_day}/copen/{name}.parquet"
        else:
            src = f"{mount}/{prefix}/cintervals/{name}.parquet"
        outp = Path(out) / name
        doc = append_open(con, f"SELECT * FROM read_parquet({q(src)})", scan, ranges["ranges"][i], name, outp,
                          bucket=read_json(f"gs://{bucket}/{run}/scans.json")["bucket"], mount=mount, rule=gen_rule_at(bucket, gen))
        upload_tree(outp / "copen", scratch, f"{prefix}/state/{date}/copen")
        upload_tree(outp / "cdelta", bucket, f"{run}/cdelta")
        upload_tree(outp / "dhist", bucket, f"{run}/dhist")
        shutil.rmtree(outp)
        sb.blob(f"{prefix}/state/{date}/done/{name}.json").upload_from_string(json.dumps(doc) + "\n")
        print(json.dumps(doc), flush=True)


@cli.command("shards")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-d", "--date", required=True, help="The scan")
@option("-g", "--gen", required=True, help="Base generation")
@option("-m", "--mount", required=True, help="Local mount of the data bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-n", "--target-rows", default=RUN_SHARD_ROWS, type=int, help="Suffix rows per shard")
@option("-o", "--out", default="/stage/out", help="Local output dir")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
def shards_cmd(bucket, date, gen, mount, mem, target_rows, out, threads, tmp) -> None:
    """The run's suffix shards from its `cdelta/` (every range's): `shards.json`, `sx/`, `sidecar/`, `sidecar.parquet`."""
    run = f"{PREFIX}/{gen}/{run_key(date, date)}"
    ranges = read_json(f"gs://{bucket}/{PREFIX}/{gen}/ranges.json")
    files = sorted(str(f) for f in (Path(mount) / run / "cdelta").glob("*.parquet"))
    if len(files) != ranges["k"]:
        raise SystemExit(f"{len(files)} of {ranges['k']} ranges appended")
    con = connect(threads, mem, tmp)
    t0 = monotonic()
    outp = Path(out) / "run"
    doc = delta_shards(con, files, outp, target_rows, gen_rule_at(bucket, gen))
    upload_tree(outp, bucket, run)
    shutil.rmtree(outp)
    doc["s"] = round(monotonic() - t0, 1)
    print(json.dumps(doc))


@cli.command("catalog")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-d", "--date", required=True, help="The scan")
@option("-g", "--gen", required=True, help="Base generation")
@option("-m", "--mount", required=True, help="Local mount of the data bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
def catalog_cmd(bucket, date, gen, mount, mem, threads, tmp) -> None:
    """The run's catalog delta → `deltas/<D>/catalog/{cells,index}.parquet`, `meta.json`."""
    prefix = f"{PREFIX}/{gen}"
    run = f"{prefix}/{run_key(date, date)}"
    _, runs = _state(bucket, gen, date)
    days = [s for r in runs for s in r["scans"]] + [date]
    deltas = [sorted(str(f) for f in (Path(mount) / prefix / run_key(d, d) / "cdelta").glob("*.parquet")) for d in days]
    tiers = [Path(mount) / prefix / "catalog", *(Path(mount) / prefix / r["key"] / "catalog" for r in runs)]
    V = read_json(f"gs://{bucket}/{prefix}/catalog/meta.json")["membership"]["max_rows"]
    side = pq.read_table(f"{mount}/{prefix}/sidecar.parquet")
    con = connect(threads, mem, tmp)
    t0 = monotonic()
    out = Path(tmp) / "catalog-run"
    meta = catalog_delta(con, tiers, sc.BaseShards(f"{mount}/{prefix}", side), deltas, V, out, Path(tmp) / "catalog-work",
                         gen_rule_at(bucket, gen))
    upload_tree(out, bucket, f"{run}/catalog")
    shutil.rmtree(out)
    meta["s"] = round(monotonic() - t0, 1)
    print(json.dumps(meta, indent=1))


def publish_run(store, date: str, rule: HexRule | None = None, dry_run: bool = False, max_level: int | None = COMPACT_LEVEL) -> dict:
    """Add scan `date`'s run (level 0) to the newest earlier manifest's runs (a scan's, or a merge's revision of it) and
    write `manifests/<date>.json`, last and once (`store`: a `static_merge.RunStore` over the generation). No carries: the
    binary counter's merges run apart (`static_merge`), each published as a revision of the newest manifest, so a scan's
    publish takes seconds however big a carry it makes due. Refuses (`SystemExit`) a manifest that exists, whose runs lack
    a reader file, or whose drill covers fewer scans than the last's. Returns the manifest (dry run: and the carries due,
    none reaching `max_level`, the compaction level; None: unbounded)."""
    from .append_runner import plan_carries, publish_scan
    from .static_merge import drilled_runs, missing, refuse

    base = store.read_json("scans.json")
    run = run_key(date, date)
    key = f"manifests/{date}.json"
    if store.exists(key):
        raise SystemExit(f"{key} exists: manifests are never rewritten")
    if not store.exists(f"{run}/catalog/meta.json"):
        raise SystemExit(f"{run}: no catalog yet")
    meta = {"rows": store.read_json(f"{run}/shards.json")["total_rows"], "bytes": sum(store.listing(f"{run}/sx/").values())}
    new = {"key": run, "first": date, "last": date, "level": 0, "scans": [date], **meta}
    doc = lambda m, after: manifest(store.gen, [s["id"] for s in base["scans"]], after)  # noqa: E731
    if dry_run:
        prev = latest_key(store.keys("manifests/"), before=date)
        after = [*(store.read_json(prev)["runs"] if prev else []), new]
        return {"manifest": doc({}, after), "carries_due": [[[r["key"] for r in ins], m["key"]] for ins, m in plan_carries(after, drilled_runs(store, after), max_level)[1]]}
    if not store.exists(f"{run}/meta.json"):
        store.create(f"{run}/meta.json", json.dumps({"gen": store.gen, "first": date, "last": date, "level": 0, "scans": [date], **meta,
                                                     **rule_json(rule)}, indent=1) + "\n")
    # A manifest only after every file of every run it lists exists (the readers need each tier whole), and never one
    # whose drill covers fewer scans than the last's.
    return publish_scan(store, date, new, doc, lambda runs: missing(store, runs), lambda before, after: refuse(store, before, after))


@cli.command("publish")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-d", "--date", required=True, help="The scan")
@option("-g", "--gen", required=True, help="Base generation")
@option("-n", "--dry-run", is_flag=True, help="Print the manifest (and the carries it makes due); write nothing")
def publish_cmd(bucket, date, gen, dry_run) -> None:
    """Add the scan's run (level 0) to the newest earlier manifest's runs and write `manifests/<D>.json`, last and once
    (`publish_run`). No carries: those run apart (`runs merge`)."""
    from .static_merge import gcs_store

    print(json.dumps(publish_run(gcs_store(bucket, None, gen), date, gen_rule_at(bucket, gen), dry_run, profile().compact_level), indent=1))


@cli.command("prune")
@option("-b", "--bucket", default=data_bucket, help="Data bucket (its `manifests/<D>.json` says D is published)")
@option("-d", "--date", required=True, help="The newest complete state to keep (the scan just published)")
@option("-g", "--gen", required=True, help="Base generation")
@option("-n", "--dry-run", is_flag=True, help="Print what would be deleted; delete nothing")
def prune_cmd(bucket, date, gen, dry_run) -> None:
    """Keep only the newest complete open-version state: once `state/<D>/` holds every range's `copen` and `done/`
    marker and `manifests/<D>.json` exists, delete `gs://SCRATCH/static-names/<gen>/state/<prev>/` for every
    prev < D. Refuses (exit 1, deletes nothing) while D is incomplete. Idempotent."""
    k = read_json(f"gs://{bucket}/{PREFIX}/{gen}/ranges.json")["k"]
    try:
        doc = prune_state(_gcs(), gen, date, k, bucket=bucket, dry_run=dry_run)
    except StateIncomplete as e:
        raise SystemExit(str(e)) from e
    print(json.dumps({**doc, "dry_run": dry_run}, indent=1))


@cli.command("rebuild-state")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-d", "--date", required=True, help="A published day (`manifests/<D>.json`): rebuild `state/<D>/`")
@option("-f", "--force", is_flag=True, help="Redo ranges already done")
@option("-g", "--gen", required=True, help="Base generation")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-m", "--mount", required=True, help="Local mount of the data bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-n", "--per-task", default=1, type=IntRange(min=1), help="Ranges per task")
@option("-o", "--out", default="/stage/out", help="Local output dir")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
def rebuild_state_cmd(bucket, date, force, gen, index, mount, mem, per_task, out, threads, tmp) -> None:
    """Rebuild a lost `state/<D>/` (the scratch bucket's open versions after D, e.g. expired) from the base's
    `cintervals/` and every run's `cdelta/` through D (`rebuild_open`): `copen/r####.parquet` and `done/` markers,
    as `append` writes them. Run per task like `append`."""
    prefix = f"{PREFIX}/{gen}"
    m = read_json(f"gs://{bucket}/{prefix}/manifests/{date}.json")
    days = [s for r in m["runs"] for s in r["scans"]]
    if not days or days[-1] != date:
        raise SystemExit(f"manifests/{date}.json ends at {days[-1] if days else 'the base'}, not {date}")
    ranges = read_json(f"gs://{bucket}/{prefix}/ranges.json")
    t = _task(index)
    sb = _gcs().bucket(scratch_bucket())
    con = connect(threads, mem, tmp)
    for i in range(t * per_task, min((t + 1) * per_task, ranges["k"])):
        name = f"r{i:04d}"
        if not force and sb.blob(f"{prefix}/state/{date}/done/{name}.json").exists():
            err(f"rebuild-state {name}: done")
            continue
        t0 = monotonic()
        outp = Path(out) / name
        rows = rebuild_open(con, f"{mount}/{prefix}/cintervals/{name}.parquet",
                            [f"{mount}/{prefix}/{run_key(d, d)}/cdelta/{name}.parquet" for d in days], outp / "copen" / f"{name}.parquet")
        upload_tree(outp / "copen", scratch_bucket(), f"{prefix}/state/{date}/copen")
        shutil.rmtree(outp)
        doc = {"range": name, "scan": date, "open": rows, "rebuilt_from": len(days), "s": round(monotonic() - t0, 1)}
        sb.blob(f"{prefix}/state/{date}/done/{name}.json").upload_from_string(json.dumps(doc) + "\n")
        print(json.dumps(doc), flush=True)


def verify_terms(con, src: str, version: int, date: str, before: str, terms: list[str], reader: TieredReader, catalog: TieredCatalog,
                 base_reader: Reader, base_catalog: sc.Catalog, base_last: str, rule: HexRule | None = None) -> dict:
    """`verify`'s checks over given readers (the base plus runs, tiered; the base alone) and the date's scan file `src`.
    The previous-scan check (tiered = the base alone) runs only when `before` is in the base (≤ `base_last`)."""
    from .static_catalog import PARENT
    from .static_names import NAME

    t0 = monotonic()
    con.execute(f"""CREATE TABLE sc AS SELECT path, usr, size, n_files, {NAME} AS l, {PARENT} AS par
        FROM ({scan_sql(con, src, ['depth >= 1'], version)})""")
    err(f"verify: {con.execute('SELECT count(*) FROM sc').fetchone()[0]:,} keys of {date} in {monotonic() - t0:.0f}s")
    D = scan_epoch(date) * 1000
    report: dict = {"date": date, "before": before, "terms": {}}
    pairs = equal = 0
    for t in terms:
        hit = f"{occurs_sql('l', q(t), rule)} AND NOT {occurs_sql('par', q(t), rule)}"
        brute = [tuple(r) for r in con.execute(f"""SELECT path, usr, size, n_files FROM sc WHERE {hit}
            ORDER BY path, usr""").fetchall()] if len(t) >= 3 else None
        bucket_tot: dict[str, list[int]] = {}
        for bk, b_, o_ in con.execute(f"""SELECT split_part(path, '/', 1), sum(size)::BIGINT, sum(n_files)::BIGINT FROM sc
                WHERE {hit} GROUP BY 1""").fetchall():
            if b_ or o_:
                bucket_tot[bk] = [int(b_), int(o_)]
        bucket_tot = dict(sorted(bucket_tot.items()))
        nz = lambda d: {k: list(v) for k, v in d.items() if v[0] or v[1]}  # noqa: E731

        def check_before(answer: dict) -> dict:
            return {"before": nz(answer) == base_before()} if before <= base_last else {}

        def base_before() -> dict:
            """The base generation alone, the previous scan (verified when it was built)."""
            c = base_catalog.answer(t, [before])
            if c is not None:
                return nz(c["answers"][before])
            return nz(base_reader.answer(t, [before])["answers"][before]) if len(t) >= 3 else {}

        doc: dict = {}
        cat = catalog.answer(t, [before, date])
        if cat is not None:
            doc["source"] = "catalog"
            doc["equal"] = {"buckets": nz(cat["answers"][date]) == bucket_tot, **check_before(cat["answers"][before])}
        elif len(t) < 3:  # a short literal in no name of any tier: zero everywhere
            doc["source"] = "absent-short"
            doc["equal"] = {"buckets": bucket_tot == {}, **check_before({})}
        else:
            doc["source"] = "static"
            hits = sorted((p, u, s_, n) for p, u, vf, vt, s_, n in reader.hits(t) if vf <= D < vt)
            roots = _roots(brute, t)
            doc["hits"] = len(brute)
            doc["roots"] = {r: len(_under(brute, r)) for r in roots}
            ans = reader.answer(t, [before, date])["answers"]
            doc["equal"] = {"hits": hits == brute, **{f"root:{r}": _under(hits, r) == _under(brute, r) for r in roots},
                            "buckets": nz(ans[date]) == bucket_tot, **check_before(ans[before])}
        pairs += len(doc["equal"])
        equal += sum(1 for v in doc["equal"].values() if v)
        report["terms"][t] = doc
        err(f"verify {t!r}: {doc['source']}, {sum(doc['equal'].values())}/{len(doc['equal'])} equal")
    report.update(checks=pairs, equal=equal, s=round(monotonic() - t0, 1))
    return report


def _roots(hits: list[tuple], term: str, k: int = 2) -> list[str]:
    """Drill roots to check a literal's hits under: `''`, its `k` buckets and `k` depth-2 dirs holding the most hits
    (none containing the literal: a root the literal matches is itself a match, not a view root)."""
    from collections import Counter

    out = [""]
    for depth in (1, 2):
        c = Counter("/".join(h[0].split("/")[:depth]) for h in hits if h[0].count("/") >= depth)
        out += [r for r, _ in sorted(c.items(), key=lambda x: (-x[1], x[0])) if term not in r.lower()][:k]
    return out


def _under(hits: list[tuple], root: str) -> list[tuple]:
    return [h for h in hits if root == "" or h[0].startswith(root + "/")]


@cli.command("verify")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-d", "--date", required=True, help="The run's scan")
@option("-g", "--gen", required=True, help="Base generation")
@option("-m", "--mount", required=True, help="Local mount of the data bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-t", "--terms-file", required=True, help="Literals, one per line (a path or gs:// URL)")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
def verify_cmd(bucket, date, gen, mount, mem, threads, terms_file, tmp) -> None:
    """The base plus the newest manifest's runs, read as the Worker reads them, against brute force straight from the
    date's scan file (its rows merged per key, the first-hit rule): per literal, a catalog member's per-bucket totals,
    else its live first hits `(path, usr, size, n_files)` as a list — whole, and under a few drill roots. Also the scan
    before: the tiered answers equal the base's. JSON report → `deltas/<D>/verify.json`; exit 1 on any difference."""
    from .static_catalog import gcs_catalog
    from .static_names import gcs_reader, read_text

    prefix = f"{PREFIX}/{gen}"
    m = read_json(f"gs://{bucket}/{prefix}/manifests/{date}.json")
    scan = read_json(f"gs://{bucket}/{prefix}/{run_key(date, date)}/scans.json")["scans"][0]
    before = m["scans"][m["scans"].index(date) - 1]
    terms = sorted({x.lower() for x in read_text(terms_file).splitlines() if x.strip()})
    dirs = [prefix, *(f"{prefix}/{r['key']}" for r in m["runs"])]
    rule = gen_rule_at(bucket, gen)
    con = connect(threads, mem, tmp)
    report = verify_terms(con, f"{mount}/{scan['src']}", scan["version"], date, before, terms,
                          TieredReader([gcs_reader(bucket, d) for d in dirs], rule), TieredCatalog([gcs_catalog(bucket, d) for d in dirs]),
                          gcs_reader(bucket, prefix, rule), gcs_catalog(bucket, prefix), m["scans"][m["base_scans"] - 1], rule)
    report["tiers"] = len(dirs)
    body = json.dumps(report, indent=1) + "\n"
    _gcs().bucket(bucket).blob(f"{prefix}/{run_key(date, date)}/verify.json").upload_from_string(body)
    print(body)
    if report["equal"] != report["checks"]:
        raise SystemExit(1)


if __name__ == "__main__":
    cli()
