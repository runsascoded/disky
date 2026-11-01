"""The interval store's per-scan append (specs/interval-store.md §2.7): each new scan becomes a small **run**
beside an immutable base generation, published by an immutable per-scan manifest, merged into bigger runs on a
binary counter, and read by combining base and runs (rows of one version: the smallest `vt` wins). The machinery is
`static_append`'s (the static name index's runs); the rows are the store's.

Per scan `D`, under `interval-store/<gen>/deltas/<D>/` (data bucket):

1. `prepare` (local): pin the next scan's `path` sort (`scans.json`), refusing any but the next.
2. `ranges` (key ranges, Batch): per range, the open versions (the previous scan's state, or the base's range files)
   and `D`'s rows → `pyrmts.intervals.append_intervals` for the three version tables the store serves (`pvl` alone in a
   generation without the owner axis, `scans.json` `"slices": false`, which refuses a scan naming owners):
   - `pvl`: one row per path, `last_read` in the key (`interval_store.fold`'s versions);
   - `sv`: owner slices (`build -S`);
   - `svt`: owner slices split where their path's version changes, with its total (`fold -S`);
   - `attribution` (an attributed lineage, specs/attr-interval-table.md): the scan's `attribution.parquet`, by prefix
     (`append_attribution`; an uncovered scan, without one, carries the open state over and writes no delta).
   Each writes its delta (`pvl|sv|svt/r####.parquet`: opened versions, `op` 1, and close records — the closed
   version's row with its final `vt` — `op` −1) and the next open state (`state/<D>/…`, scratch bucket); the range's
   `ranges/r####.json` (its counts and reconstruction check) is written last.
3. `publish` (one task): cut the run's served sorts (`served/<sort>.parquet` + `.groups.parquet`, the five the
   Worker reads), its `meta.json`, then `manifests/<D>.json`: the newest earlier manifest's runs plus D's at level 0,
   written once and only after every file of every run it lists exists. No carries.
4. `r2` (as the R2 account): each listed run's served files → R2, checked there, then the manifest. A second runnable
   of the publish job's one task (one provisioning, not two), unless the R2 account is another service account; its
   own job when the manifest already exists (a rerun).
5. `prune` (local): every earlier scan's `state/` in the scratch bucket, once `D`'s is complete (parallel deletes); and
   the runs a carry folded, on GCS (`append_runner.prune_folded`: kept while a manifest superseded
   less than `GRACE_S`, 1 h, ago lists them; each keeping `FOLDED_RETAIN`).
   The R2 job prunes R2 the same way after copying its manifest, and the merge job GCS after its revisions.

Then, once after the scans, the **merge stage** (`append_runner`'s deferred carries, non-fatal): the binary counter's due
carries as one Batch job (`carry`: each merged run — per range `pyrmts.runs.merge_parquets` on its inputs' deltas, min-`vt`,
carries that chain folded into one N-way merge — then its own cut and `meta.json`; then a revision
`manifests/<id>.m<NNN>.json` of the newest manifest listing it), watched `-w` s; then R2 for the revision. The chain's
machinery (order, stage skipping, Batch, carries, lease, revisions, R2, prune, exit codes) is `append_runner`'s, shared
with the static name index; this module is the interval store's stages and merge.

The base+runs reconstruction is exact, version for version, with a full rebuild through `D`
(`test_interval_append.py`): the change keys are `build`'s, `wts` is carried as `build` and `fold` carry it (a
version opened by a read-day change alone keeps its path version's; a slice piece opened by its path's change
alone keeps its slice version's), and the open states carry each path's version start (`pvf`), where the
slice pieces split.

    dt-cloud interval-store append [-c] [-M] [-n] [-w SECS] SCAN_ID   # the chain, profile-driven ($INTERVAL_STORE_PROFILE / -P)
    dt-cloud interval-store merge [-n] [-w SECS]                       # the merge stage alone
    python -m dt_cloud.interval_append ranges|publish|r2|carry …       # the Batch tasks' stages
"""
from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from math import ceil
from pathlib import Path
from time import monotonic
from typing import Any

import pyarrow as pa
from click import Choice, IntRange, argument, command, group, option

from . import append_runner as ar
from . import interval_store as ist
from . import static_names as sn
from .append_runner import (  # noqa: F401 — NOT_NEXT, StateIncomplete: as this module has always offered them
    NOT_NEXT,
    Carry,
    StateIncomplete,
    duckdb_args,
    exit_on,
    latest_key,
    plan_carries,
    run_key,
    scans_of,
)
from .blobstore import Store, gs_bucket, run_store, serves, store_url
from .maint_runs import NULL, from_env
from .static_names import OPEN, U64, err, q, write_sorted
from .interval_verify import marker_key
from .static_profile import Profile, from_mapping, level_arg, parse_compact_level

PREFIX = ist.PREFIX
#: The served sorts a run carries: what the Worker reads (`R2_SERVED` less the standalone `reads` sort and the
#: superseded per-slice `slices-bysize`).
RUN_SORTS = ("path", "bysize", "slices", "slices-bytotal", "slices-bysize-user", ist.ATTRIBUTION)
#: Cost-attribution `component` of every job the append submits (`cost_labels`).
COMPONENT = "interval-store"
#: What a folded run keeps on GCS (`append_runner.plan_prune`'s `retain`): its `ranges/` docs (the next scan's
#: `prev_open_counts` balances its tasks by them) and its pinned `scans.json`, both small. R2 keeps none of it.
FOLDED_RETAIN = ("ranges/", "scans.json")

PVF = pa.field("pvf", pa.int64(), nullable=False)
OP = pa.field("op", pa.int8(), nullable=False)
#: Open state per table (scratch): the served rows, `usr` '' where no owner is named (a join key), and for `pvl` and
#: `svt` the start of the path's current path version (`pvf`: a slice piece splits there).
STATE_SCHEMA = {
    "pvl": ist.PVL_SCHEMA.append(PVF),
    "sv": ist.SV_SCHEMA,
    "svt": ist.SVT_SCHEMA.append(PVF),
    # Roster attribution (`interval_store.ATTRIBUTION`): only in an attributed lineage's runs (`tables`).
    ist.ATTRIBUTION: ist.AT_SCHEMA,
}
#: A run's delta files: the state's columns and `op` (1 opened, −1 a close record).
DELTA_SCHEMA = {k: s.append(OP) for k, s in STATE_SCHEMA.items()}


def state_schema(t: str, generation: ist.Identity = False) -> pa.Schema:
    """Table `t`'s open-state columns (`STATE_SCHEMA`); `pvl` with `GENERATION` after the served columns (before `pvf`)
    when the generation carries it."""
    if t == "pvl" and generation:
        return ist.with_generation(ist.PVL_SCHEMA, generation).append(PVF)
    return STATE_SCHEMA[t]


def delta_schema(t: str, generation: ist.Identity = False) -> pa.Schema:
    """Table `t`'s delta columns (`DELTA_SCHEMA`, with `state_schema`'s `GENERATION`)."""
    return state_schema(t, generation).append(OP)
#: Each table's version identity (a version's rows across tiers share it).
IDENT = {"pvl": ["depth", "path", "vf"], "sv": ["depth", "path", "usr", "vf"], "svt": ["depth", "path", "usr", "vf"], ist.ATTRIBUTION: ["depth", "path", "vf"]}
TABLES = tuple(STATE_SCHEMA)
AT = ist.ATTRIBUTION
#: The version tables' change columns and the carried ones (`build`'s, `fold`'s): `dr` is computed from the stored
#: `wts`/`wb` (constant within a version), `pvf` rides along to cut the slice pieces.
PVL_STATE = [*ist.CHANGE_COLS, "last_read", "wts", "pvf"]
SV_STATE = ist.SV_STATE
SVT_STATE = [*ist.SV_CHANGE, "tot", "pvf", "wts"]
DR = "CASE WHEN wb > 0 THEN round(wts / wb, 0) ELSE 0 END::DOUBLE"
#: What a reconstruction check hashes per table (`interval_store`'s hashes, `last_read` in the path's).
HASH = {
    "pvl": f"{ist.ROW_HASH[:-1]}, last_read)",
    "sv": ist.SV_HASH,
    "svt": f"hash({ist.SV_HASH[5:-1]}, tot)",
    ist.ATTRIBUTION: ist.AT_HASH,
}
#: A scan's rows per table (`append_range`'s `np` / `ns` / `na`): what the open versions, and the store's answer, must be.
SCAN_ROWS = {
    "pvl": "SELECT * FROM np",
    "sv": "SELECT * FROM ns",
    "svt": "SELECT s.*, p.size AS tot FROM ns s JOIN np p ON p.depth = s.depth AND p.path = s.path",
    ist.ATTRIBUTION: "SELECT * FROM na",
}
#: What the append's parity check compares per table, row for row: `HASH`'s columns (the key, the change columns with `dr`
#: from the stored `wts`/`wb`, `last_read`, `tot`).
CHECK_COLS = {
    "pvl": ["depth", "path", *ist.CHANGE_COLS, "last_read"],
    "sv": ["depth", "path", "usr", *ist.SV_CHANGE],
    "svt": ["depth", "path", "usr", *ist.SV_CHANGE, "tot"],
    ist.ATTRIBUTION: ["depth", "path", *ist.ATTRIBUTION_CHANGE],
}
#: What a version carries besides its check columns (`live_sql`).
CARRIED_COLS = {"pvl": ["wts"], "sv": ["wts"], "svt": ["wts"], ist.ATTRIBUTION: []}


def tables(sliced: bool = True, attributed: bool = False) -> tuple[str, ...]:
    """The version tables a generation's runs carry: `pvl`, the owner slices only with the owner axis
    (`interval_store.sliced`), the attribution table only in an attributed lineage (`lineage_attributed`)."""
    return tuple(t for t in TABLES if (sliced or t not in ist.SLICE_SUBS) and (attributed or t not in ist.ATTRIBUTION_SUBS))


def run_sorts(sliced: bool = True, attributed: bool = False) -> tuple[str, ...]:
    """The served sorts a run carries (`RUN_SORTS`, the slice sorts only with the owner axis, the attribution sort only
    when the run holds the table: some scan of it is covered)."""
    return tuple(s for s in RUN_SORTS if s in ist.served_sorts(sliced, attributed))


def has_table(root: str | Path, t: str) -> bool:
    """Whether a base's range-file dir or a run's dir under `root` holds table `t`'s files (a run before the attribution
    table, or of uncovered scans only, holds none of it)."""
    return any(Path(root, t).glob("r*.parquet"))


def lineage_attributed(base: dict, manifest: dict | None) -> bool:
    """Whether a generation carries the attribution table: its base (`scans.json` `"attribution"`), or a run (the newest
    manifest's `"attribution"`, present once a run opened it). Its appends then continue it, every scan covered or not."""
    return ist.attributed(base) or (manifest is not None and ist.ATTRIBUTION in manifest)


def covered_scans(base: dict, manifest: dict | None) -> set[str]:
    """The scans the attribution table answers for: the base's covered scans (an entry naming its file) and the runs'
    (the manifest's `"attribution"`)."""
    return {s["id"] for s in base["scans"] if s.get(ist.ATTRIBUTION_SRC)} | set((manifest or {}).get(ist.ATTRIBUTION) or [])


# ── The open versions ──────────────────────────────────────────────────────


def base_state_sql(root: str, name: str, sliced: bool = True, generation: ist.Identity = False, attributed: bool = False) -> dict[str, str]:
    """A key range's open versions from a base generation's range files under `root` (`pvl/`, `pv/`, `sv/`, `svt/`,
    `attribution/` `<name>.parquet`): `state_schema` relations (`tables(sliced, attributed)`). `pvf` is the open path
    version's `vf` (`pv/`). A base without the attribution table opens it empty."""
    rd = lambda sub: f"read_parquet({q(f'{root}/{sub}/{name}.parquet')})"  # noqa: E731
    pv = f"(SELECT depth, path, vf AS pvf FROM {rd('pv')} WHERE vt = {OPEN})"
    pvl_cols = ", ".join(f"l.{c}" for c in ist.with_generation(ist.PVL_SCHEMA, generation).names)
    svt_cols = ", ".join("coalesce(s.usr, '') AS usr" if c == "usr" else f"s.{c}" for c in ist.SVT_SCHEMA.names)
    sv_cols = ", ".join("coalesce(usr, '') AS usr" if c == "usr" else c for c in ist.SV_SCHEMA.names)
    out = {
        "pvl": f"SELECT {pvl_cols}, p.pvf FROM {rd('pvl')} l JOIN {pv} p ON p.depth = l.depth AND p.path = l.path WHERE l.vt = {OPEN}",
        "sv": f"SELECT {sv_cols} FROM {rd('sv')} WHERE vt = {OPEN}",
        "svt": f"SELECT {svt_cols}, p.pvf FROM {rd('svt')} s JOIN {pv} p ON p.depth = s.depth AND p.path = s.path WHERE s.vt = {OPEN}",
        AT: (f"SELECT {ist.qcols(ist.AT_SCHEMA.names)} FROM {rd(AT)} WHERE vt = {OPEN}" if Path(root, AT, f"{name}.parquet").exists()
             else ist.empty_sql(ist.AT_SCHEMA)),
    }
    return {t: out[t] for t in tables(sliced, attributed)}


def run_state_sql(root: str, name: str, sliced: bool = True, attributed: bool = False) -> dict[str, str]:
    """A key range's open versions after a scan, from its `state/<D>/` dir `root`."""
    return {t: f"SELECT * FROM read_parquet({q(f'{root}/{t}/{name}.parquet')})" for t in tables(sliced, attributed)}


def _reconstructs(con, table: str, new: str, kind: str) -> tuple[list[int], list[int]]:
    """`[count, Σ hash]` of the open versions in `table` and of the scan's rows `new` (equal iff the open state is
    the scan exactly)."""
    h = HASH[kind]

    def dig(rel: str) -> list[int]:
        n, s = con.execute(f"SELECT count(*), coalesce(sum({h}::HUGEINT), 0) FROM ({rel})").fetchone()
        return [int(n), int(s) % U64]

    return dig(f"SELECT * REPLACE ({DR} AS dr) FROM {table} WHERE vt = {OPEN}"), dig(new)


def append_range(con, prev: dict[str, str], scan: dict, r: dict, name: str, out: Path, *, bucket: str, mount: str | None,
                 sliced: bool = True, generation: ist.Identity = False, check: list[str] | None = None, attributed: bool = False) -> dict:
    """Append `scan` to one key range's open versions (`prev`: `state_schema` relations per table, `tables(sliced,
    attributed)`; `attributed`: the lineage carries the attribution table, `append_attribution`).
    Writes under `out` each table's delta `<t>/<name>.parquet` (`delta_schema`, sorted by identity then `op`) and next
    open state `state/<t>/<name>.parquet`. Raises unless the next open state is the scan's rows exactly; refuses
    (`SystemExit`) a scan naming owners in a generation without the owner axis (`sliced` false). With `generation` (the
    generation's `"generation": true`), a version opened at the scan takes its object's `GENERATION` (NULL where the
    scan's sort has none); a continuing version keeps its own (`build`'s carry from the version's first scan).

    `check`: the published roots the store answers the range from (the base's range-file dir, then each run its newest
    earlier manifest lists): the range's answer at the scan over them and this run's deltas is compared with the scan's
    rows, row for row (`check_tables`), as `doc["check"]` (the scan's parity marker sums them: `append_marker`)."""
    from pyrmts.intervals import append_intervals

    t0 = monotonic()
    D = int(scan["ts"])
    src = sn._src(bucket, scan["src"], mount)
    preds = sn.range_preds(r)
    v = scan.get("version")
    con.execute(f"CREATE OR REPLACE TABLE np AS {ist.path_rows_sql(con, src, preds, v, generation)}")
    if not sliced and con.execute("SELECT count(*) FROM np WHERE us <> ''").fetchone()[0]:
        raise SystemExit(ist.owners_refused(scan["id"]))
    todo = tables(sliced)
    if sliced:
        con.execute(f"CREATE OR REPLACE TABLE ns AS {ist.slice_rows_sql(con, src, preds, v)}")
    for t in todo:
        con.execute(f"CREATE OR REPLACE TABLE p_{t} AS SELECT *, {DR} AS dr FROM ({prev[t]})")
    # Each path's version start after D: its open one's while the change columns hold, else D.
    same = " AND ".join(f"o.{c} IS NOT DISTINCT FROM n.{c}" for c in ist.change_cols(generation))
    con.execute(f"""CREATE OR REPLACE TABLE npf AS SELECT n.*, CASE WHEN o.path IS NOT NULL AND {same} THEN o.pvf ELSE {D} END::BIGINT AS pvf
        FROM np n LEFT JOIN p_pvl o ON o.depth = n.depth AND o.path = n.path""")
    doc: dict = {"range": name, "scan": scan["id"]}
    key2 = ["depth", "path"]
    key3 = ["depth", "path", "usr"]
    gcol = [ist.identity_column(generation)] if generation else []
    pvl_new = f"SELECT {', '.join([*key2, *ist.CHANGE_COLS, 'last_read', 'wts', 'pvf', *gcol])} FROM npf"
    sv_new = f"SELECT {', '.join([*key3, *SV_STATE])} FROM ns"
    svt_new = f"""SELECT {', '.join(f's.{c}' for c in [*key3, *ist.SV_CHANGE])}, p.size AS tot, p.pvf, s.wts
        FROM ns s JOIN npf p ON p.depth = s.depth AND p.path = s.path"""
    plan = {
        "pvl": (pvl_new, key2, [*PVL_STATE, *gcol], {"wts": "first", "pvf": "first", **({c: "first" for c in gcol} if not isinstance(generation, pa.Field) else {})}),
        "sv": (sv_new, key3, SV_STATE, ist.CARRIED),
        "svt": (svt_new, key3, SVT_STATE, ist.CARRIED),
    }
    for t, (new, key, state, carried) in ((t, plan[t]) for t in todo):
        n_open, n_close = append_intervals(con, f"SELECT * FROM p_{t}", new, key, state, D, OPEN, carried=carried, out=f"o_{t}")
        on = " AND ".join(f"o_{t}.{c} = c.{c}" for c in key)
        if t == "pvl":
            # A version opened by its read day alone continues its path version: `fold` gives it that version's `wts`
            # (and `GENERATION`).
            sets = ", ".join(f"{c} = c.{c}" for c in ["wts", *gcol])
            con.execute(f"""UPDATE o_pvl SET {sets} FROM (SELECT depth, path, {', '.join(['wts', *gcol])} FROM o_pvl WHERE vt = {D}) c
                WHERE o_pvl.vf = {D} AND o_pvl.pvf < {D} AND {on}""")
        elif t == "svt":
            # A piece opened by its path's change alone continues its slice version: `fold -S` gives it that one's `wts`.
            eq = " AND ".join(f"o_svt.{c} IS NOT DISTINCT FROM c.{c}" for c in ist.SV_CHANGE)
            con.execute(f"""UPDATE o_svt SET wts = c.wts FROM (SELECT * FROM o_svt WHERE vt = {D}) c
                WHERE o_svt.vf = {D} AND {on} AND {eq}""")
        schema = delta_schema(t, generation)
        cols = ", ".join(schema.names[:-1])
        order = ", ".join([*IDENT[t], "op"])
        dict_cols = [c for c in ("kind", "us", "usr") if c in schema.names]
        n_delta = write_sorted(sn._batches(con, f"""SELECT {cols}, 1::TINYINT AS op FROM o_{t} WHERE vf = {D}
                UNION ALL SELECT {cols}, -1::TINYINT AS op FROM o_{t} WHERE vt = {D} ORDER BY {order}"""),
                               out / t / f"{name}.parquet", schema, ist.RANGE_RG, dictionary=dict_cols)
        n_state = write_sorted(sn._batches(con, f"SELECT {cols} FROM o_{t} WHERE vt = {OPEN} ORDER BY {', '.join(IDENT[t])}"),
                               out / "state" / t / f"{name}.parquet", state_schema(t, generation), ist.RANGE_RG, dictionary=dict_cols)
        got, want = _reconstructs(con, f"o_{t}", SCAN_ROWS[t], t)
        if got != want:
            raise RuntimeError(f"append {name} {scan['id']} {t}: the open versions ({got[0]:,} rows) are not the scan's ({want[0]:,})")
        doc[t] = {"opened": n_open, "closed": n_close, "delta": n_delta, "open": n_state}
        con.execute(f"DROP TABLE o_{t}; DROP TABLE p_{t}")
    if attributed:
        doc[AT] = append_attribution(con, prev[AT], scan, preds, name, out, D, bucket=bucket, mount=mount)
    if check is not None:
        t1 = monotonic()
        doc["check"] = {**check_tables(con, [*check, str(out)], name, D, sliced=sliced, generation=generation,
                                       attribution=attributed and doc[AT]["covered"]), "s": round(monotonic() - t1, 1)}
    con.execute("DROP TABLE np; DROP TABLE IF EXISTS ns; DROP TABLE npf; DROP TABLE IF EXISTS na")
    doc["s"] = round(monotonic() - t0, 1)
    err(f"append {name} {scan['id']}: " + ", ".join(f"{t} +{doc[t]['opened']:,} −{doc[t]['closed']:,}" for t in todo)
        + (f", {AT} " + (f"+{doc[AT]['opened']:,} −{doc[AT]['closed']:,}" if doc[AT]["covered"] else "uncovered") if attributed else "") + f" in {doc['s']}s")
    return doc


def append_attribution(con, prev: str, scan: dict, preds: list[str], name: str, out: Path, D: int, *, bucket: str, mount: str | None) -> dict:
    """The attribution table's append for one key range: `prev` its open versions. A covered scan (its entry names an
    `attribution.parquet`, `interval_store.ATTRIBUTION_SRC`) is appended as the path versions are (`append_intervals`,
    change columns `user`, `source`, `evidence`): its delta `attribution/<name>.parquet`, its next open state, checked to
    be the scan's prefixes exactly; its rows stay in table `na` for the parity check. An uncovered scan adds no input: the
    open versions carry over unchanged (the state rewritten as it was), no delta, as `build` skips it."""
    from pyrmts.intervals import append_intervals

    cols = ist.qcols(ist.AT_SCHEMA.names)
    schema = delta_schema(AT)
    con.execute(f"CREATE OR REPLACE TABLE p_at AS SELECT {cols} FROM ({prev})")
    state = out / "state" / AT / f"{name}.parquet"
    key = f"{scan['id']} {name} {AT}"
    if not scan.get(ist.ATTRIBUTION_SRC):
        n_state = write_sorted(sn._batches(con, f"SELECT {cols} FROM p_at ORDER BY depth, path, vf"), state, ist.AT_SCHEMA, ist.RANGE_RG,
                               dictionary=["user", "source"])
        con.execute("DROP TABLE p_at")
        return {"covered": False, "open": n_state}
    con.execute(f"CREATE OR REPLACE TABLE na AS {ist.attribution_rows_sql(sn._src(bucket, scan[ist.ATTRIBUTION_SRC], mount), preds)}")
    n_open, n_close = append_intervals(con, "SELECT * FROM p_at", "SELECT * FROM na", ist.KEY_COLS, ist.ATTRIBUTION_CHANGE, D, OPEN, out="o_at")
    n_delta = write_sorted(sn._batches(con, f"""SELECT {cols}, 1::TINYINT AS op FROM o_at WHERE vf = {D}
            UNION ALL SELECT {cols}, -1::TINYINT AS op FROM o_at WHERE vt = {D} ORDER BY depth, path, vf, op"""),
                           out / AT / f"{name}.parquet", schema, ist.RANGE_RG, dictionary=["user", "source"])
    n_state = write_sorted(sn._batches(con, f"SELECT {cols} FROM o_at WHERE vt = {OPEN} ORDER BY depth, path, vf"), state, ist.AT_SCHEMA,
                           ist.RANGE_RG, dictionary=["user", "source"])

    def dig(rel: str) -> list[int]:
        n, s = con.execute(f"SELECT count(*), coalesce(sum({ist.AT_HASH}::HUGEINT), 0) FROM ({rel})").fetchone()
        return [int(n), int(s) % U64]

    got, want = dig(f"SELECT * FROM o_at WHERE vt = {OPEN}"), dig("SELECT * FROM na")
    if got != want:
        raise RuntimeError(f"append {key}: the open versions ({got[0]:,} rows) are not the scan's ({want[0]:,})")
    con.execute("DROP TABLE o_at; DROP TABLE p_at")
    return {"covered": True, "opened": n_open, "closed": n_close, "delta": n_delta, "open": n_state}


# ── A run's served sorts, and merges ───────────────────────────────────────


def delta_relation(root: str, t: str, served: bool = True, cols: list[str] | None = None) -> str:
    """A run's delta files of table `t` under `root` as one relation; `served`: in the served sorts' columns
    (`interval_store.SUB_SCHEMA`, or `cols`; `usr` NULL where no owner is named)."""
    src = f"read_parquet({q(f'{root}/{t}/r*.parquet')})"
    if not served:
        return src
    cols = cols or ist.SUB_SCHEMA[t].names
    return f"(SELECT {', '.join('nullif(usr, ' + q('') + ') AS usr' if c == 'usr' else ist.ident(c) for c in cols)} FROM {src})"


def cut_run(con, root: str, out: Path, *, rg_rows: int = ist.SERVED_RG, sliced: bool = True, generation: ist.Identity = False) -> dict:
    """A run's served sorts (`run_sorts(sliced)`, the attribution sort when the run holds its table) from its delta files
    under `root` → `out/<sort>.parquet` + `.groups.parquet` + `<sort>.json`. Open versions then close records (`seg_sql`
    without stamps: segment 0, then 1). `GENERATION` in `interval_store.GENERATION_SORT` with `generation`."""
    docs = {}
    for sort in run_sorts(sliced, has_table(root, AT)):
        sub, _, _ = ist.SORTS[sort]
        t0 = monotonic()
        schema = ist.served_schema(sort, generation)
        doc = ist.write_served(con, delta_relation(root, sub, cols=schema.names), sort, out / f"{sort}.parquet", schema, rg_rows=rg_rows)
        doc["s"] = round(monotonic() - t0, 1)
        (out / f"{sort}.json").write_text(json.dumps(doc, sort_keys=True) + "\n")
        docs[sort] = doc
    return docs


def merge_range(inputs: list[str], name: str, out: Path, sliced: bool = True, generation: ist.Identity = False) -> dict:
    """One key range of runs (`inputs`: their roots, oldest first) merged per table (`tables(sliced)`, and the attribution
    table over the inputs holding it: none before the table opened, nor a run of uncovered scans): rows of one version
    are one (`IDENT`), the smallest `vt` (a close record over the row it closes), `op` 1 if any input opened it."""
    from pyrmts.runs import merge_parquets

    doc = {}
    for t in tables(sliced, True):
        files = [f"{r}/{t}/{name}.parquet" for r in inputs]
        if t in ist.ATTRIBUTION_SUBS and not (files := [f for f in files if Path(f).exists()]):
            continue
        schema = delta_schema(t, generation)
        stream = merge_parquets(files, [*IDENT[t], "op"], identity=IDENT[t], reduce={"vt": "min", "op": "max"})
        doc[t] = write_sorted(stream, out / t / f"{name}.parquet", schema, ist.RANGE_RG,
                              dictionary=[c for c in ("kind", "us", "usr", "user", "source") if c in schema.names])
    return doc


# ── The append's parity check (`scan retire`'s gate; specs/scan-cs3.md item 4) ──


def live_sql(roots: list[str], t: str, name: str, D: int, generation: ist.Identity = False) -> str:
    """Key range `name`'s versions of table `t` live at `D`, as the reader combines them (`interval_read.combine`): every
    root's `<t>/<name>.parquet` (a base's range files, runs' deltas), one row per version (`IDENT`) with the smallest
    `vt`, those with `vf ≤ D < vt`; in `CHECK_COLS[t]` (`usr` '' where no owner is named), `pvl` with `GENERATION` last
    when the generation carries it."""
    gcol = [ist.identity_column(generation)] if generation and t == "pvl" else []
    key = [c for c in CHECK_COLS[t] if c in IDENT[t]]
    cols = [*key, "vf", "vt", *(c for c in CHECK_COLS[t] if c not in (*key, "dr")), *CARRIED_COLS[t], *gcol]
    sel = ", ".join("coalesce(usr, '') AS usr" if c == "usr" else ist.ident(c) for c in cols)
    files = [f"{r}/{t}/{name}.parquet" for r in roots]
    if t in ist.ATTRIBUTION_SUBS:
        # The table's tiers: a base or run from before it opened holds none of it.
        files = [f for f in files if Path(f).exists()]
    parts = " UNION ALL ".join(f"SELECT {sel} FROM read_parquet({q(f)})" for f in files) or ist.empty_sql(ist.AT_SCHEMA)
    out = ", ".join(f"{DR} AS dr" if c == "dr" else ist.ident(c) for c in [*CHECK_COLS[t], *gcol])
    return f"""SELECT {out} FROM (SELECT * FROM ({parts}) WHERE vf <= {D}
        QUALIFY row_number() OVER (PARTITION BY {', '.join(IDENT[t])} ORDER BY vt) = 1) WHERE vt > {D}"""


def _differ(con, a: str, b: str, cols: str) -> int:
    """Rows of `a` not in `b`, as multisets (`EXCEPT ALL`: NULLs compare equal)."""
    return int(con.execute(f"SELECT count(*) FROM (SELECT {cols} FROM {a} EXCEPT ALL SELECT {cols} FROM {b})").fetchone()[0])


def check_tables(con, roots: list[str], name: str, D: int, *, sliced: bool = True, generation: ist.Identity = False,
                 attribution: bool = False) -> dict:
    """The scan's rows in key range `name` (`np`, `ns`, and with `attribution` — a covered scan of an attributed lineage —
    `na`: `append_range`'s tables) against the store's answer at `D` over
    `roots` (`live_sql`), row for row, per table: `rows` (the scan's), `store` (the store's), `missing` (scan rows the
    store doesn't give, as a multiset), `extra` (store rows the scan doesn't have). With `generation`, also `generation`:
    the scan's objects that carry one, and how many the store's live version gives the same (`interval_verify
    .generation_parity`'s count)."""
    out: dict = {}
    for t in tables(sliced, attribution):
        gcol = [ist.identity_column(generation)] if generation and t == "pvl" else []
        cols = ist.qcols(CHECK_COLS[t])
        con.execute(f"CREATE OR REPLACE TEMP TABLE c_store AS {live_sql(roots, t, name, D, generation)}")
        con.execute(f"CREATE OR REPLACE TEMP TABLE c_scan AS SELECT {ist.qcols([*CHECK_COLS[t], *gcol])} FROM ({SCAN_ROWS[t]})")
        n_scan, n_store = (int(con.execute(f"SELECT count(*) FROM {x}").fetchone()[0]) for x in ("c_scan", "c_store"))
        out[t] = {"rows": n_scan, "store": n_store, "missing": _differ(con, "c_scan", "c_store", cols), "extra": _differ(con, "c_store", "c_scan", cols)}
        if gcol:
            g = gcol[0]
            n, eq = con.execute(f"""SELECT count(*), count(*) FILTER (WHERE l.{g} = o.{g}) FROM c_scan o
                LEFT JOIN c_store l ON l.depth = o.depth AND l.path = o.path WHERE o.kind = 'file' AND o.{g} IS NOT NULL""").fetchone()
            out["generation"] = {"objects": int(n), "equal": int(eq)}
    con.execute("DROP TABLE IF EXISTS c_store; DROP TABLE IF EXISTS c_scan")
    return out


def check_scan(con, scan: dict, r: dict, name: str, roots: list[str], *, bucket: str, mount: str | None, sliced: bool = True,
               generation: ist.Identity = False, attributed: bool = False) -> dict:
    """`check_tables` for a scan already appended (its run one of `roots`): its rows in key range `r` (`name`) from its
    `path` sort (and, covered in an attributed lineage, its `attribution.parquet`) against the store's answer at it over
    `roots`. A re-check, outside `append_range`."""
    src = sn._src(bucket, scan["src"], mount)
    preds = sn.range_preds(r)
    con.execute(f"CREATE OR REPLACE TABLE np AS {ist.path_rows_sql(con, src, preds, scan.get('version'), generation)}")
    if sliced:
        con.execute(f"CREATE OR REPLACE TABLE ns AS {ist.slice_rows_sql(con, src, preds, scan.get('version'))}")
    covered = attributed and bool(scan.get(ist.ATTRIBUTION_SRC))
    if covered:
        con.execute(f"CREATE OR REPLACE TABLE na AS {ist.attribution_rows_sql(sn._src(bucket, scan[ist.ATTRIBUTION_SRC], mount), preds)}")
    try:
        return check_tables(con, roots, name, int(scan["ts"]), sliced=sliced, generation=generation, attribution=covered)
    finally:
        con.execute("DROP TABLE IF EXISTS np; DROP TABLE IF EXISTS ns; DROP TABLE IF EXISTS na")


def check_cut(con, root: str, served: Path, *, sliced: bool = True, generation: ist.Identity = False) -> dict:
    """Each of a run's served sorts (`served/<sort>.parquet`) against the deltas under `root` it was cut from, row for
    row in the sort's columns: `rows` (the sort's), `missing` (delta rows it lacks), `extra` (rows the deltas lack)."""
    out = {}
    for sort in run_sorts(sliced, has_table(root, AT)):
        names = ist.served_schema(sort, generation).names
        cols = ist.qcols(names)
        got = f"(SELECT {cols} FROM read_parquet({q(str(served / f'{sort}.parquet'))}))"
        want = f"(SELECT {cols} FROM {delta_relation(root, ist.SORTS[sort][0], cols=names)})"
        out[sort] = {"rows": int(con.execute(f"SELECT count(*) FROM {got}").fetchone()[0]), "missing": _differ(con, want, got, cols),
                     "extra": _differ(con, got, want, cols)}
    return out


def append_marker(gen: str, scan: str, manifest: dict, docs: list[dict], cut: dict | None) -> dict | None:
    """Scan `scan`'s parity marker (`interval_verify.marker_key`) from its append: its ranges' checks (`docs`, every key
    range's `ranges/r####.json`) summed per table, its run's cut check (`cut`), and `pass` when the scan has rows and
    every row of every table, every served sort's row and every object's generation was equal. It vouches for the store
    `manifest` (`manifests/<scan>.json`) publishes: its runs but the scan's own must be the ones each range was checked
    against. None (no marker: `scan retire -m` keeps the scan) when a range or the cut wasn't checked (appended before
    the check), or a range was checked against other runs (a carry's revision landed between the ranges and the
    publish)."""
    rk = run_key(scan, scan)
    runs = [r["key"] for r in manifest["runs"] if r["key"] != rk]
    if cut is None or not docs or any("check" not in d for d in docs):
        return None
    if any(d["check"]["runs"] != runs for d in docs):
        return None
    tabs: dict[str, dict[str, int]] = {}
    gen_eq: dict[str, int] | None = None
    for d in docs:
        for t, v in d["check"].items():
            if t == "generation":
                gen_eq = {k: (gen_eq or {}).get(k, 0) + v[k] for k in ("objects", "equal")}
            elif isinstance(v, dict):
                e = tabs.setdefault(t, {"rows": 0, "store": 0, "missing": 0, "extra": 0})
                for k in e:
                    e[k] += v[k]
    ok = (tabs.get("pvl", {}).get("rows", 0) > 0 and all(v["missing"] == v["extra"] == 0 for v in (*tabs.values(), *cut.values()))
          and (gen_eq is None or gen_eq["equal"] == gen_eq["objects"]))
    return {"scan": scan, "gen": gen, "how": "append", "pass": ok, "manifest": f"manifests/{scan}.json", "runs": runs, "ranges": len(docs),
            "tables": tabs, "cut": cut, **({"generation": gen_eq} if gen_eq is not None else {})}


def run_files(sliced: bool = True, attributed: bool = False) -> tuple[str, ...]:
    """What every run a manifest lists holds (the reader's files and the run's record), checked before it is written;
    `attributed`: the run holds the attribution table (some scan of it is covered)."""
    return ("meta.json", *(f"served/{s}{x}" for s in run_sorts(sliced, attributed) for x in (".parquet", ".groups.parquet")))


def missing_files(runs: list[dict], exists: Callable[[str], bool], sliced: bool = True, covered: set[str] | frozenset = frozenset()) -> list[str]:
    """The reader files the listed `runs` lack: each run's `run_files`, the attribution sort's where a scan of it is in
    `covered` (the manifest's `"attribution"`)."""
    return [f"{r['key']}/{f}" for r in runs for f in run_files(sliced, bool(covered.intersection(r.get("scans", [])))) if not exists(f"{r['key']}/{f}")]


def manifest(gen: str, base: dict, runs: list[dict], attribution: list[str] | None = None) -> dict:
    """`manifests/<D>.json`: the base's scans and the runs (oldest first) with each run scan's epoch (`stamps`, what
    a reader tests `vf ≤ D < vt` at); in an attributed lineage, `"attribution"`: the run scans the attribution table
    covers (present, maybe empty, once a run carried the table; a revision keeps it, its scans unchanged)."""
    scans = [*(s["id"] for s in base["scans"]), *(s for r in runs for s in r["scans"])]
    stamps = {s: ts for r in runs for s, ts in r["stamps"].items()}
    return {"gen": gen, "date": scans[-1], "base_scans": len(base["scans"]), "scans": scans, "stamps": dict(sorted(stamps.items())),
            "runs": [{k: r[k] for k in ("key", "first", "last", "level", "scans", "rows", "bytes") if k in r} for r in runs],
            **({ist.ATTRIBUTION: attribution} if attribution is not None else {})}


def run_attribution(pinned: dict) -> list[str] | None:
    """A scan run's `meta.json` `"attribution"` from its pinned `scans.json`: `[scan]` when covered, `[]` when not, None
    outside an attributed lineage."""
    if not pinned.get(ist.ATTRIBUTION):
        return None
    return [s["id"] for s in pinned["scans"] if s.get(ist.ATTRIBUTION_SRC)]


def run_meta(gen: str, run: dict, stamps: dict[str, int], docs: dict, attribution: list[str] | None = None) -> dict:
    """A run's `meta.json`; `attribution` (an attributed lineage's run): the run's scans the attribution table covers."""
    return {"gen": gen, "key": run["key"], "first": run["first"], "last": run["last"], "level": run["level"], "scans": run["scans"],
            "stamps": stamps, "rows": sum(d["rows"] for s, d in docs.items() if s == "path"),
            "bytes": sum(d["bytes"] + d["groups_bytes"] for d in docs.values()), "sorts": docs,
            **({ist.ATTRIBUTION: attribution} if attribution is not None else {})}


def publish_run(store: ar.RunStore, scan: str) -> dict:
    """Add scan `scan`'s run (level 0; its served sorts and `meta.json` cut) to the newest earlier manifest's runs (a
    scan's, or a merge's revision of it) and write `manifests/<scan>.json`, last and once (`append_runner.publish_scan`:
    refused while a listed run lacks a reader file). No carries: those run apart (`carry`). Refuses (`SystemExit`) a
    scan not past the generation's newest. Returns the manifest."""
    base = store.read_json("scans.json")
    if store.exists(f"manifests/{scan}.json"):
        raise SystemExit(f"manifests/{scan}.json exists: manifests are never rewritten")
    top = latest_key(store.keys("manifests/"))
    if (have := scans_of(base, store.read_json(top)["runs"] if top else []))[-1] >= scan:
        raise SystemExit(f"{scan} is not past the generation's newest scan {have[-1]}")
    rk = run_key(scan, scan)
    meta = store.read_json(f"{rk}/meta.json")
    new = {"key": rk, "first": scan, "last": scan, "level": 0, "scans": [scan], "rows": meta["rows"], "bytes": meta["bytes"]}
    covered: set[str] = set()

    def doc(m: dict, after: list[dict]) -> dict:
        stamps = {**m.get("stamps", {}), **meta["stamps"]}
        # The attribution table's run scans: the earlier manifest's and this run's (its `meta.json`, an attributed run's).
        at = None if ist.ATTRIBUTION not in m and ist.ATTRIBUTION not in meta else [*m.get(ist.ATTRIBUTION, []), *meta.get(ist.ATTRIBUTION, [])]
        covered.update(at or [])
        return manifest(store.gen, base, [{**r, "stamps": {s: stamps[s] for s in r["scans"]}} for r in after], at)
    return ar.publish_scan(store, scan, new, doc, lambda runs: missing_files(runs, store.exists, ist.sliced(base), covered))


def build_merged_run(dirs: list[Path], run: dict, outp: Path, *, gen: str, k: int, tmp: Path, threads: int = 16, mem: str = "100GB",
                     rg_rows: int = ist.SERVED_RG, log: Callable[[str], None] = err, sliced: bool = True, generation: ist.Identity = False) -> dict:
    """The merged run `run` (`plan_carries`' output) of input run dirs `dirs` (oldest first) into `outp`: each key range's
    deltas merged per table (`merge_range`, the `k` ranges), its served sorts cut from them (`cut_run`, `outp/served/`),
    then `meta.json` (`run_meta`, the inputs' stamps). Returns `{rows, bytes, s}`."""
    shutil.rmtree(outp, ignore_errors=True)
    t0 = monotonic()
    for i in range(k):
        merge_range([str(d) for d in dirs], f"r{i:04d}", outp, sliced, generation)
    merge_s = round(monotonic() - t0, 1)
    t1 = monotonic()
    docs = cut_run(sn.connect(threads, mem, tmp), str(outp), outp / "served", rg_rows=rg_rows, sliced=sliced, generation=generation)
    cut_s = round(monotonic() - t1, 1)
    metas = [json.loads((d / "meta.json").read_text()) for d in dirs]
    stamps = {sid: ts for m in metas for sid, ts in m["stamps"].items()}
    at = [s for m in metas for s in m.get(ist.ATTRIBUTION, [])] if any(ist.ATTRIBUTION in m for m in metas) else None
    meta = {**run_meta(gen, run, stamps, docs, at), "merge_s": merge_s, "cut_s": cut_s}
    (outp / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    log(f"merge {run['key']}: {len(dirs)} runs merged in {merge_s}s, cut in {cut_s}s ({meta['bytes']:,} B)")
    return {"rows": meta["rows"], "bytes": meta["bytes"], "s": {"merge": merge_s, "cut": cut_s}}


def carry(gen: str, k: int, *, threads: int = 16, mem: str = "100GB", rg_rows: int = ist.SERVED_RG, sliced: bool = True,
          generation: ist.Identity = False) -> Carry:
    """The interval store's `Carry`: merged runs by `build_merged_run`, every listed run holding `run_files(sliced)` (and
    the attribution sort, a run some scan of which the newest manifest's `"attribution"` covers), no drills."""
    def build(dirs, run, outp, *, drilled, tmp, log, rec=None):
        return build_merged_run(dirs, run, outp, gen=gen, k=k, tmp=tmp, threads=threads, mem=mem, rg_rows=rg_rows, log=log, sliced=sliced,
                                generation=generation)

    def missing(store, runs):
        cur = ar.newest(store)
        return missing_files(runs, store.exists, sliced, set((cur[1] if cur else {}).get(ist.ATTRIBUTION) or []))
    return Carry(build=build, missing=missing)


# ── Key ranges → tasks ─────────────────────────────────────────────────────


def assign_ranges(k: int, tasks: int, counts: list[int] | None) -> list[list[int]]:
    """Each of `tasks` tasks' key ranges (of `k`), ascending. With `counts` (each range's open rows after the previous
    scan, its cost): longest first, each to the least-loaded task (ties: the lower task), so the big ranges don't pile
    up in one task as contiguous blocks do. Without: interleaved (task `t`: `t`, `t + tasks`, …). Deterministic: every
    task computes the same plan and runs its own row of it."""
    if counts is None:
        return [list(range(t, k, tasks)) for t in range(tasks)]
    if len(counts) != k:
        raise ValueError(f"{len(counts)} counts for {k} ranges")
    load = [0] * tasks
    out: list[list[int]] = [[] for _ in range(tasks)]
    for i in sorted(range(k), key=lambda i: (-counts[i], i)):
        t = min(range(tasks), key=lambda t: (load[t], t))
        out[t].append(i)
        load[t] += counts[i]
    return [sorted(o) for o in out]


def open_counts(docs: dict[str, dict], k: int, sliced: bool = True) -> list[int] | None:
    """Each range's open rows (Σ over `tables(sliced)`) from a scan's `ranges/r####.json` docs (by name), or None unless
    all `k` are there with every table's count (a run before the counts, or the base: no docs)."""
    ts = tables(sliced)
    out = []
    for i in range(k):
        d = docs.get(f"r{i:04d}")
        if d is None or any(not isinstance(d.get(t), dict) or "open" not in d[t] for t in ts):
            return None
        out.append(sum(int(d[t]["open"]) for t in ts))
    return out


def prev_open_counts(store: Store, prefix: str, prev: str, k: int, *, workers: int = 8, sliced: bool = True) -> list[int] | None:
    """`open_counts` of the previous scan's run (`<prefix>/deltas/<prev>/ranges/`), read in parallel."""
    root = f"{prefix}/{run_key(prev, prev)}/ranges/"
    names = [n for n in store.keys(root) if n.endswith(".json")]
    if len(names) < k:
        return None
    with ThreadPoolExecutor(workers) as ex:
        docs = dict(zip((Path(n).stem for n in names), ex.map(store.read_json, names)))
    return open_counts(docs, k, sliced)


# ── The open-version state: the newest complete scan only ─────────────────


def prune_plan(objects: list[tuple[str, int]], prefix: str, k: int, published: bool, scan: str, sliced: bool = True,
               attributed: bool = False) -> dict:
    """What `prune` deletes, from the scratch bucket's `(name, size)` listing under `<prefix>/state/`: every scan's
    state before `scan`, once `scan`'s is complete (every table's `k` range files, `tables(sliced, attributed)`) and its
    manifest `published`. Raises `StateIncomplete` otherwise."""
    root = f"{prefix}/state/"
    want_tables = tables(sliced, attributed)
    scans: dict[str, dict] = {}
    for name, size in objects:
        if not name.startswith(root):
            raise ValueError(f"{name}: not under {root}")
        sid, _, rest = name[len(root):].partition("/")
        if not sn.SCAN_ID.fullmatch(sid):
            raise ValueError(f"{name}: {sid!r} is not a scan id")
        d = scans.setdefault(sid, {"names": [], "bytes": 0, "files": {t: set() for t in TABLES}})
        d["names"].append(name)
        d["bytes"] += size
        t, _, file = rest.partition("/")
        if t in TABLES and file.endswith(".parquet"):
            d["files"][t].add(file.removesuffix(".parquet"))
    want = {f"r{i:04d}" for i in range(k)}
    cur = scans.get(scan, {"files": {t: set() for t in TABLES}})
    missing = {t: len(want - cur["files"][t]) for t in want_tables}
    if any(missing.values()) or not published:
        why = [f"{n} of {k} ranges without {t}" for t, n in missing.items() if n] + ([] if published else ["no manifest"])
        raise StateIncomplete(f"state/{scan} incomplete: {', '.join(why)}")
    drop = sorted(s for s in scans if s < scan)
    return {"scan": scan, "keep": sorted(s for s in scans if s >= scan),
            "delete": [{"scan": s, "objects": len(scans[s]["names"]), "bytes": scans[s]["bytes"]} for s in drop],
            "names": [n for s in drop for n in sorted(scans[s]["names"])]}


# ── The deployment's settings ──────────────────────────────────────────────


@dataclass(frozen=True)
class Config:
    """The append's settings: a `static_profile.Profile` (buckets, layouts, Batch, R2) from the interval profile's
    `append` section, plus the generation whose range files hold the base's versions (`ranges_gen`: the served
    generation may be a re-cut of another's ranges)."""
    p: Profile
    ranges_gen: str
    #: `append.attribution`: each appended scan's `attribution.parquet` goes into the store's attribution table
    #: (specs/attr-interval-table.md). Once a generation carries the table its appends continue it whatever this says.
    attribution: bool = False


#: Env overrides of an interval profile's `append` fields (a staged source tree, a job image, the R2 endpoint).
ENV = {"src": "INTERVAL_STORE_SRC", "image": "INTERVAL_STORE_IMAGE", "r2_endpoint": "R2_ENDPOINT", "project": "GCP_PROJECT",
       "gen": "INTERVAL_STORE_GEN", "machine": "INTERVAL_STORE_MACHINE", "compact_level": "INTERVAL_STORE_COMPACT_LEVEL",
       "compact": "INTERVAL_STORE_COMPACT", "read_from": "INTERVAL_STORE_READ_FROM"}


def load_config(profile: str | None, environ: dict[str, str] | None = None) -> Config:
    """The interval profile's `append` section (the deployment's profile JSON; `$INTERVAL_STORE_PROFILE`)
    with `ENV` over it; every field a run needs is checked (`need`)."""
    env = os.environ if environ is None else environ
    doc = ist.load_profile(profile)
    a = dict(doc.get("append") or {})
    if not a:
        raise SystemExit("interval store: the profile has no `append` section")
    ranges_gen = a.pop("ranges_gen", None)
    attribution = a.pop("attribution", False)
    if not isinstance(attribution, bool):
        raise SystemExit(f"interval store: the profile's `append.attribution` must be true or false, not {attribution!r}")
    p = from_mapping({"name": doc.get("name", profile or ""), "bucket": doc["bucket"], **a})
    over: dict[str, Any] = {}
    for k, var in ENV.items():
        raw = (env.get(var) or "").strip()
        if not raw:
            continue
        if k == "src":
            over[k] = tuple(s.strip() for s in raw.split(",") if s.strip())
        elif k == "compact_level":
            over[k] = parse_compact_level(var, raw)
        elif k == "compact":
            over[k] = ar.parse_policy(var, raw)
        else:
            over[k] = raw
    p = replace(p, **over)
    # The staged chain's tasks mount the store (Batch's GCS volumes): a whole GCS bucket, by name. Any other store
    # (`r2://`, `s3://`, a dir) appends on one machine (`interval-store add`, `interval_local`).
    for f in ("bucket", "scratch"):
        if getattr(p, f):
            if (b := gs_bucket(getattr(p, f))) is None:
                raise SystemExit(f"interval store: the staged append mounts a GCS bucket; {f} {getattr(p, f)!r} is not one "
                                 "(append on one machine: `dt-cloud interval-store add`)")
            p = replace(p, bucket=b) if f == "bucket" else replace(p, scratch=b)
    for f in ("gen", "bucket", "scratch", "layouts", "region", "image", "sa", "r2_bucket"):
        if getattr(p, f) in (None, "", ()):
            raise SystemExit(f"interval store: no {f} in the profile's `append` section" + (f" (or ${ENV[f]})" if f in ENV else ""))
    return Config(p, ranges_gen or p.gen, attribution)


# ── The chain ──────────────────────────────────────────────────────────────


def job_id(stage: str, scan_id: str, now: datetime | None = None) -> str:
    """`iv-<stage>-<scan>-<hhmmss>` (Batch ids: lowercase letters, digits and hyphens)."""
    return ar.job_id("iv", stage, scan_id, now)


@dataclass(kw_only=True)
class Runner(ar.Runner):
    """The interval store's chain (`append_runner.Runner`; `cfg` a `Config`): prepare → ranges → publish (+ R2) → prune
    per scan, then the merge stage."""
    store_prefix = PREFIX
    job_prefix = "iv"

    @property
    def p(self) -> Profile:
        return self.cfg.p

    def command(self, module: str, args: list[str], *, mount: bool = True) -> str:
        """`python -m dt_cloud.interval_append <stage> <args> -b … -g … -R … -S … [-m /gcs/<bucket>]`."""
        p = self.p
        return ar.task_command(p, module, [*args, "-b", p.bucket, "-g", p.gen, "-R", self.cfg.ranges_gen, "-S", p.scratch], mount=mount)

    def spec(self, name: str, tasks: int, commands: list[str], *, stage: str, **kw) -> dict:
        return ar.job_spec(self.p, name, tasks, commands, stage=stage, purpose="interval-store", component=COMPONENT, **kw)

    def merge_job(self, scan: str, long: bool = False) -> tuple[str, dict]:
        return self.job("merge", scan, 1, "interval_append", ["carry", "-L", level_arg(self.p.policy.carry_level), *self.read_args(), *duckdb_args(self.p.machine)],
                        r2=self.merge_r2(), long=long)

    def compact_jobs(self, scan: str) -> list[tuple[str, dict]]:
        r2 = self.merge_r2()
        out = [self.job("compact", scan, 1, "interval_append", ["compact", "-C", str(self.p.policy), *(["-K"] if self.compact_now else []), *self.read_args(),
                                                              *(["-r"] if r2 else []), "-j", str(self.p.compact_jobs), *(["-N"] if self.candidate else []),
                                                              *duckdb_args(self.p.machine_for("compact"))], r2=r2)]
        if not r2:
            out.append(self.job("compact-r2", scan, 1, "interval_append", ["compact-r2", *(["-N"] if self.candidate else [])], r2=True, mount=False))
        return out

    def with_gen(self, gen: str) -> Config:
        """A compacted generation holds its own range files and `ranges.json`."""
        return Config(replace(self.p, gen=gen), gen, self.cfg.attribution)

    def follow(self) -> None:
        """`append_runner.Runner.follow`, then the generation's own ranges over the profile's `ranges_gen` when it holds
        them (`own_ranges`: a generation pinned by `INTERVAL_STORE_GEN` past the profile's)."""
        super().follow()
        if (own := own_ranges(self.p.gen, self.cfg.ranges_gen, self.exists)) != self.cfg.ranges_gen:
            self.log(f"{self.p.gen} holds its own key ranges and base: reading them, not {self.cfg.ranges_gen}'s")
            self.cfg = Config(self.p, own, self.cfg.attribution)

    def one(self, d: str) -> None:
        run = f"{self.root}/deltas/{d}"
        self.log(f"{d}: interval store {self.p.gen}")
        # 1. prepare (local): the scan's `path` sort, pinned (checks it is the next one).
        if self.exists(f"{run}/scans.json"):
            self.log(f"{d} prepare: done")
        else:
            self.stage(f"{d} prepare", lambda: self.prepare(d))
        # 2. ranges: every key range, `append_tasks` tasks.
        k = self.read_json(f"{PREFIX}/{self.cfg.ranges_gen}/ranges.json")["k"]
        n = self.count(f"{run}/ranges/", ".json")
        if n >= k:
            self.log(f"{d} ranges: done ({n}/{k})")
        else:
            per = ceil(k / self.p.append_tasks)
            name, spec = self.job("ranges", d, ceil(k / per), "interval_append", ["ranges", "-d", d, "-n", str(per), *duckdb_args(self.p.machine)])
            self.stage(f"{d} ranges ({n}/{k})", lambda: self.run_job(name, spec))
        # 3. publish: the run's cut, then `manifests/<d>.json` (written once; the run at level 0, carries after: `carries`);
        # 4. R2: the runs the manifest lists, checked, then the manifest — one task's two runnables, so one provisioning.
        if self.exists(f"{self.root}/manifests/{d}.json"):
            self.log(f"{d} publish: done")
            self.r2(d)
        elif self.p.r2_sa in (None, "", self.p.sa):
            name, spec = self.publish_job(d, r2=True)
            self.stage(f"{d} publish+r2", lambda: self.run_job(name, spec))
        else:
            # The R2 account is another service account: a job runs as one, so the copy is its own job.
            name, spec = self.publish_job(d)
            self.stage(f"{d} publish", lambda: self.run_job(name, spec))
            self.r2(d)
        # 5. prune: only the newest complete open-version state is kept.
        self.stage(f"{d} prune", lambda: self.prune(d))

    def publish_job(self, d: str, r2: bool = False) -> tuple[str, dict]:
        """The publish job; with `r2`, as the R2 account, its task's second runnable the R2 copy (Batch runs a task's
        runnables in order and stops at a failed one, so the copy only follows a written manifest). A retried task's
        publish skips a manifest it already wrote (`-s`), and the copy skips objects already on R2."""
        args = ["publish", "-d", d, *duckdb_args(self.p.machine), *(["-s"] if r2 else [])]
        name, spec = self.job("publish", d, 1, "interval_append", args, r2=r2)
        if r2:
            ts = spec["taskGroups"][0]["taskSpec"]
            copy = deepcopy(ts["runnables"][0])
            copy["container"]["commands"] = ["-c", self.command("interval_append", ["r2", "-d", d], mount=False)]
            ts["runnables"].append(copy)
        return name, spec

    def r2(self, d: str, manifest: str | None = None) -> None:
        """The R2 job for `manifests/<manifest>.json` (default `d`'s own): its runs' served files, checked, then it."""
        stem = manifest or d
        if self.dry_run:
            self.log(f"{d} r2: would copy manifests/{stem}.json's runs, check them, then the manifest")
            return
        name = self.job_name("r2", d)
        cmd = self.command("interval_append", ["r2", "-d", d, *(["-m", manifest] if manifest else [])], mount=False)
        spec = self.spec(name, 1, [cmd], stage="r2", r2=True, machine="n2-highmem-4", ssd_gb=375)
        spec = ar.label_batch_spec(spec, COMPONENT, scan_id=d)
        self.stage(f"{d} r2" + (f" (manifests/{stem}.json)" if manifest else ""), lambda: self.run_job(name, spec))


def store_runner(cfg: Config, *, dry_run: bool = False, merge: bool = True, merge_wait: float | None = 0) -> Runner:
    """`Runner` over the profile's store (`blobstore.Store`), Batch, and this module's `prepare` / `prune`."""
    from google.cloud import storage

    p = cfg.p
    store = Store(store_url(p.bucket))
    client = storage.Client(project=p.project)

    def published(layouts, start):
        return [s["id"] for s in list_scans(store, layouts, start=start)["scans"]]

    # the local stages read the runner's generation when they run: `follow` moves it to a compacted successor
    def prepare(d):
        try:
            prepare_scan(p.bucket, runner.p.gen, d, p.layouts, cfg.attribution)
        except SystemExit as e:
            raise ar.NotNext(str(e)) from e

    def prune(d):
        gen = runner.p.gen
        k = store.read_json(f"{PREFIX}/{runner.cfg.ranges_gen}/ranges.json")["k"]
        sl = ist.sliced(store.read_json(f"{PREFIX}/{gen}/scans.json"))
        pinned = f"{PREFIX}/{gen}/{run_key(d, d)}/scans.json"
        at = store.exists(pinned) and bool(store.read_json(pinned).get(ist.ATTRIBUTION))
        doc = prune_state(client, gen, d, k, bucket=p.bucket, scratch=p.scratch, dry_run=dry_run, sliced=sl, attributed=at)
        err(json.dumps({k_: v for k_, v in doc.items() if k_ != "delete"} | {"deleted_scans": [x["scan"] for x in doc["delete"]]}))
        folded = ar.prune_folded(ar.GcsRunStore(gs_bucket(p.bucket), None, gen, prefix=PREFIX, client=client), retain=FOLDED_RETAIN, dry_run=dry_run,
                                 where=" gcs")
        err(json.dumps(_summary(folded)))

    runner = Runner(
        cfg=cfg,
        exists=store.exists,
        count=lambda prefix, suffix: sum(1 for k in store.keys(prefix) if k.endswith(suffix)),
        read_json=store.read_json,
        list_keys=store.keys,
        published=published,
        run_job=ar.BatchRunner(p, err),
        prepare=prepare,
        prune=prune,
        dry_run=dry_run,
        merge=merge,
        merge_wait=merge_wait,
        coordination=None if dry_run else lambda: ar.gcs_coordinator_lease(gs_bucket(p.scratch), ar.coordinator_prefix(store, PREFIX, runner.p.gen), client),
        compacting=None if dry_run or not gs_bucket(p.scratch) else lambda: ar.gcs_compaction_lease_file(gs_bucket(p.scratch), PREFIX, client),
    )
    return runner


# ── Store-side stages (local, or a Batch task) ─────────────────────────────


def list_scans(store: Store, layouts, *, start: str | None = None, through: str | None = None) -> dict:
    """Every scan's newest `path` sort in the store under `layouts` (`static_names.pick_scans`): on a whole GCS bucket
    pinned by generation, size, md5 and crc32c (`static_names.list_scans`), elsewhere by size."""
    if (b := gs_bucket(store.root)) is not None:
        return sn.list_scans(b, layouts=layouts, start=start, through=through)
    objects = []
    for t in layouts:
        lead = sn.layout_glob(t)[0].split("*", 1)[0]
        objects += [(k, {"size": n}) for k, n in store.listing(lead.rsplit("/", 1)[0] if "/" in lead else "").items()]
    return {"bucket": store.root, "layouts": list(layouts), "scans": sn.pick_scans(objects, layouts, start=start, through=through)}


def scan_version(store: Store, key: str) -> int:
    """A scan's `path` sort's format (1: a v1 index, 2: a store generation's sort), from its footer."""
    if (b := gs_bucket(store.root)) is not None:
        from google.cloud import storage

        return sn._version_from_footer(storage.Client().bucket(b), key)
    from .static_local import source_version

    with store.open(key) as fh:
        return source_version(fh)


def _latest_manifest(store: Store, gen: str, before: str | None = None) -> dict | None:
    """The newest manifest (of a scan strictly before `before`), revisions included (`append_runner.latest_key`)."""
    prefix = f"{PREFIX}/{gen}/"
    key = latest_key([k.removeprefix(prefix) for k in store.keys(f"{prefix}manifests/")], before)
    return store.read_json(f"{prefix}{key}") if key else None


def own_ranges(gen: str, ranges_gen: str, exists: Callable[[str], bool]) -> str:
    """The generation holding `gen`'s base range files: `gen` itself when it has its own `ranges.json` (a compacted or
    backfilled generation always does; only a staged build's borrows its `ranges_gen`'s), else `ranges_gen`. A profile's
    `ranges_gen` names its own `gen`'s, so a generation pinned past it (`INTERVAL_STORE_GEN`) must not read it: gcs
    2026-10-23 appended onto a backfilled generation from the profile's older one's base, every pre-backfill version
    and its NULL `generation` with it."""
    return gen if exists(f"{PREFIX}/{gen}/ranges.json") else ranges_gen


def check_ranges_gen(store: Store, gen: str, ranges_gen: str) -> None:
    """Refuse (SystemExit) a stage's `-R` other than `gen` when `gen` holds its own ranges (`own_ranges`)."""
    if (own := own_ranges(gen, ranges_gen, store.exists)) != ranges_gen:
        raise SystemExit(f"{store.root}/{PREFIX}/{gen}/ranges.json: the generation holds its own ranges and base; -R {ranges_gen} "
                         f"would append onto another's (use -R {own})")


def _state(store: Store, gen: str, scan: str) -> tuple[dict, list[dict]]:
    """The generation's base `scans.json` and the runs live before `scan` (the newest earlier manifest's)."""
    base = store.read_json(f"{PREFIX}/{gen}/scans.json")
    m = _latest_manifest(store, gen, before=scan)
    return base, (m["runs"] if m else [])


def prepare_scan(bucket: str, gen: str, scan: str, layouts: tuple[str, ...], attribution: bool = False) -> dict:
    """Pin `scan`'s newest `path` sort (size, source format; on GCS also its generation, md5, crc32c) as
    `deltas/<scan>/scans.json` in the store `bucket` (a bucket name or URL), refusing (SystemExit) unless it is the next
    scan after the base and the live runs. With `attribution` (the profile's), or in a lineage that carries the
    attribution table (`lineage_attributed`), the doc says `"attribution": true` and the scan's entry names its
    `attribution.parquet` beside the sort when there is one (`interval_store.ATTRIBUTION_SRC`; none: uncovered)."""
    store = Store(store_url(bucket))
    base, runs = _state(store, gen, scan)
    have = scans_of(base, runs)
    found = list_scans(store, layouts, start=have[-1])["scans"]
    nxt = [s for s in found if s["id"] > have[-1]]
    if not nxt or nxt[0]["id"] != scan:
        raise SystemExit(f"the next scan after {have[-1]} is {nxt[0]['id'] if nxt else 'none'}, not {scan}")
    s = nxt[0]
    if s["ts"] <= max(x["ts"] for x in base["scans"]):
        raise SystemExit(f"{scan}: its stamp {s['ts']} is not past the generation's")
    s["version"] = scan_version(store, s["src"])
    doc = {"bucket": bucket, "scans": [s]}
    if attribution or lineage_attributed(base, _latest_manifest(store, gen, before=scan)):
        doc[ist.ATTRIBUTION] = True
        if store.exists(src := ist.attribution_src(s)):
            s[ist.ATTRIBUTION_SRC] = src
        else:
            err(f"{scan}: no {src}: the attribution table doesn't cover the scan (readers answer \"unknown\" there)")
    store.create(f"{PREFIX}/{gen}/{run_key(scan, scan)}/scans.json", json.dumps(doc, indent=1) + "\n")
    err(json.dumps(doc))
    return doc


def prune_state(gcs, gen: str, scan: str, k: int, *, bucket: str, scratch: str, dry_run: bool = False, workers: int = 8,
                sliced: bool = True, attributed: bool = False) -> dict:
    """Delete every `state/<prev>/` (prev < `scan`) of generation `gen` in the scratch bucket (nowhere else), once
    `scan`'s state is complete and its manifest published (`prune_plan`), `workers` deletes at a time
    (`append_runner.prune_state`). Idempotent."""
    prefix = f"{PREFIX}/{gen}"
    return ar.prune_state(gcs, prefix, scan, lambda objects, published: prune_plan(objects, prefix, k, published, scan, sliced, attributed),
                          bucket=bucket, scratch=scratch, dry_run=dry_run, workers=workers)


# ── CLI ────────────────────────────────────────────────────────────────────


@group("runs")
def cli() -> None:
    """The interval store's per-scan runs beside a base generation (specs/interval-store.md §2.7): the chain's stages."""


def stage_options(f):
    for o in reversed([
        option("-b", "--bucket", required=True, help="The store: a bucket name (gs://) or URL"),
        option("-g", "--gen", required=True, help="Base generation (`interval-store/<gen>/`: its `scans.json`, the runs, the manifests)"),
        option("-R", "--ranges-gen", required=True, help="The generation holding the base's range files and `ranges.json`"),
        option("-S", "--scratch", required=True, help="Bucket for the open versions (`state/<D>/`)"),
    ]):
        f = o(f)
    return f


@cli.command("ranges")
@stage_options
@option("-d", "--scan", required=True, help="The scan to append (its run's `scans.json`, from `prepare`)")
@option("-f", "--force", is_flag=True, help="Redo ranges already done")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-m", "--mount", required=True, help="Local mount of the data bucket (the scratch bucket beside it)")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-n", "--per-task", default=1, type=IntRange(min=1), help="Ranges per task")
@option("-o", "--out", default="/stage/out", help="Local output dir")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
def ranges_cmd(bucket, gen, ranges_gen, scratch, scan, force, index, mount, mem, per_task, out, threads, tmp) -> None:
    """Append the scan to key ranges' open versions: `deltas/<D>/{pvl,sv,svt}/r####.parquet` and `ranges/r####.json`
    (data bucket; the marker last), `state/<D>/{pvl,sv,svt}/r####.parquet` (scratch). The open versions come from the
    previous scan's state, else (the first run) the base's range files. Each range's answer at D over the published
    store (the base's range files, the newest earlier manifest's runs, D's deltas) is checked against D's rows, row for
    row (`append_range`'s `check`), in its `ranges/r####.json`."""
    prefix = f"{PREFIX}/{gen}"
    run = f"{prefix}/{run_key(scan, scan)}"
    store, sstore = Store(store_url(bucket)), Store(store_url(scratch))
    check_ranges_gen(store, gen, ranges_gen)
    doc_scan = store.read_json(f"{run}/scans.json")
    s = doc_scan["scans"][0]
    ranges = store.read_json(f"{PREFIX}/{ranges_gen}/ranges.json")
    base, runs = _state(store, gen, scan)
    sl = ist.sliced(base)
    prev = scans_of(base, runs)[-1]
    smount = str(Path(mount).parent / scratch)
    t = sn._task(index)
    k = ranges["k"]
    # Balanced by the previous scan's open rows (a range's cost), else interleaved: tasks = the job's.
    counts = prev_open_counts(store, prefix, prev, k, sliced=sl) if runs else None
    plan = assign_ranges(k, ceil(k / per_task), counts)
    todo = plan[t] if t < len(plan) else []
    err(f"ranges task {t}: {len(todo)} ranges" + (f", {sum(counts[i] for i in todo):,} open rows at {prev} (max task "
                                                  f"{max(sum(counts[i] for i in p) for p in plan):,})" if counts else ", interleaved"))
    con = sn.connect(threads, mem, tmp)
    # What the store answers each range from once D's run is published beside them: the base's range files and the runs
    # the newest earlier manifest lists (`append_range`'s check).
    check = [f"{mount}/{PREFIX}/{ranges_gen}", *(f"{mount}/{prefix}/{r['key']}" for r in runs)]
    # The attribution table (`prepare` pinned the flag): its open versions are the previous scan's state when that run
    # carried the table, else the base's (empty in a base without it: the table opens at this run).
    at = bool(doc_scan.get(ist.ATTRIBUTION))
    prev_at = at and bool(runs) and bool(store.read_json(f"{prefix}/{run_key(prev, prev)}/scans.json").get(ist.ATTRIBUTION))
    if at and runs and not prev_at and ist.ATTRIBUTION in (_latest_manifest(store, gen, before=scan) or {}):
        raise SystemExit(f"{scan}: the generation carries the attribution table but {prev}'s run doesn't: no open state to append to")
    for i in todo:
        name = f"r{i:04d}"
        if not force and store.exists(f"{run}/ranges/{name}.json"):
            err(f"ranges {name}: done")
            continue
        state = (run_state_sql(f"{smount}/{prefix}/state/{prev}", name, sl) if runs
                 else base_state_sql(f"{mount}/{PREFIX}/{ranges_gen}", name, sl, ist.has_generation(base)))
        if at:
            state[AT] = (run_state_sql(f"{smount}/{prefix}/state/{prev}", name, sl, True)[AT] if prev_at
                         else base_state_sql(f"{mount}/{PREFIX}/{ranges_gen}", name, sl, ist.has_generation(base), True)[AT])
        outp = Path(out) / name
        shutil.rmtree(outp, ignore_errors=True)
        doc = append_range(con, state, s, ranges["ranges"][i], name, outp, bucket=doc_scan["bucket"], mount=mount, sliced=sl,
                           generation=ist.has_generation(base), check=check, attributed=at)
        doc["prev"] = prev
        doc["check"]["runs"] = [r["key"] for r in runs]
        sstore.put_tree(outp / "state", f"{prefix}/state/{scan}")
        for tb in tables(sl, at):
            if (outp / tb).is_dir():  # an uncovered scan writes no attribution delta
                store.put_tree(outp / tb, f"{run}/{tb}")
        shutil.rmtree(outp)
        store.write_text(f"{run}/ranges/{name}.json", json.dumps(doc) + "\n")
        print(json.dumps(doc), flush=True)


@cli.command("publish")
@stage_options
@option("-d", "--scan", required=True, help="The scan")
@option("-L", "--compact-level", type=ar.Level(), default=ar.COMPACT_LEVEL, help=ar.LEVEL_HELP)
@option("-m", "--mount", required=True, help="Local mount of the data bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-n", "--dry-run", is_flag=True, help="Print the manifest (and the carries it makes due); cut and write nothing")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-s", "--skip-published", is_flag=True, help="Exit 0 when `manifests/<D>.json` exists (a retried publish+r2 task goes on to the copy)")
@option("-T", "--tmp", default="/stage/tmp", help="Scratch dir")
def publish_cmd(bucket, gen, ranges_gen, scratch, scan, compact_level, mount, mem, dry_run, threads, skip_published, tmp) -> None:
    """Cut the run's served sorts (`deltas/<D>/served/`, then its `meta.json`), then write `manifests/<D>.json` once: the
    newest earlier manifest's runs plus D's at level 0 (`publish_run`), after checking every file of every run it lists.
    No carries: those run apart (`carry`, the merge stage)."""
    prefix = f"{PREFIX}/{gen}"
    store = run_store(bucket, None, gen, prefix=PREFIX)
    key = f"manifests/{scan}.json"
    st = Store(store_url(bucket))
    if skip_published and store.exists(key):
        err(f"{prefix}/{key}: published")
        if not st.exists(marker_key(prefix, scan)):
            write_marker(st, gen, ranges_gen, scan)
        return
    if store.exists(key):
        raise SystemExit(f"{prefix}/{key} exists: manifests are never rewritten")
    check_ranges_gen(st, gen, ranges_gen)
    k = Store(store_url(bucket)).read_json(f"{PREFIX}/{ranges_gen}/ranges.json")["k"]
    rk = run_key(scan, scan)
    done = len(store.keys(f"{rk}/ranges/"))
    if done < k:
        raise SystemExit(f"{prefix}/{rk}: {done} of {k} ranges appended")
    if dry_run:
        prev = latest_key(store.keys("manifests/"), before=scan)
        after = [*(store.read_json(prev)["runs"] if prev else []), {"key": rk, "first": scan, "last": scan, "level": 0, "scans": [scan]}]
        print(json.dumps({"runs": [r["key"] for r in after], "carries_due": [[[r["key"] for r in ins], m["key"]] for ins, m in plan_carries(after, max_level=compact_level)[1]]}, indent=1))
        return
    if store.exists(f"{rk}/meta.json"):
        err(f"{rk}: cut")
    else:
        pinned = store.read_json(f"{rk}/scans.json")
        s = pinned["scans"][0]
        outp = Path(tmp) / "served"
        shutil.rmtree(outp, ignore_errors=True)
        t0 = monotonic()
        base = Store(store_url(bucket)).read_json(f"{prefix}/scans.json")
        sl = ist.sliced(base)
        con = sn.connect(threads, mem, tmp)
        docs = cut_run(con, f"{mount}/{prefix}/{rk}", outp, sliced=sl, generation=ist.has_generation(base))
        cut_s = round(monotonic() - t0, 1)
        cut = check_cut(con, f"{mount}/{prefix}/{rk}", outp, sliced=sl, generation=ist.has_generation(base))
        Store(store_url(bucket)).put_tree(outp, f"{prefix}/{rk}/served")
        shutil.rmtree(outp)
        new = {"key": rk, "first": scan, "last": scan, "level": 0, "scans": [scan]}
        meta = {**run_meta(gen, new, {scan: s["ts"]}, docs, run_attribution(pinned)), "cut_s": cut_s, "check": cut,
                "check_s": round(monotonic() - t0 - cut_s, 1)}
        store.create(f"{rk}/meta.json", json.dumps(meta, indent=1) + "\n")
        err(f"{rk}: cut in {meta['cut_s']}s ({meta['bytes']:,} B)")
    print(json.dumps(publish_run(store, scan), indent=1))
    write_marker(st, gen, ranges_gen, scan)


def write_marker(st: Store, gen: str, ranges_gen: str, scan: str, *, workers: int = 16) -> dict | None:
    """Scan `scan`'s parity marker from its published append (`append_marker`: its manifest, its run's `meta.json` and
    every range's doc), written to `verify/<scan>.json` in the generation, where `scan retire` finds it; None (nothing
    written) when the append can't vouch for the scan."""
    prefix = f"{PREFIX}/{gen}"
    rk = run_key(scan, scan)
    k = st.read_json(f"{PREFIX}/{ranges_gen}/ranges.json")["k"]
    with ThreadPoolExecutor(workers) as ex:
        docs = list(ex.map(lambda i: st.read_json(f"{prefix}/{rk}/ranges/r{i:04d}.json"), range(k)))
    m = append_marker(gen, scan, st.read_json(f"{prefix}/manifests/{scan}.json"), docs, st.read_json(f"{prefix}/{rk}/meta.json").get("check"))
    if m is None:
        err(f"{scan}: no parity marker (a range or the cut wasn't checked, or was checked against other runs than its manifest's)")
        return None
    st.write_text(marker_key(prefix, scan), json.dumps(m) + "\n")
    err(f"{scan}: parity {'passed' if m['pass'] else 'FAILED'} " + json.dumps({k_: m[k_] for k_ in ("tables", "cut", "generation") if k_ in m}))
    return m


@cli.command("carry")
@stage_options
@option("-F", "--read-from", type=Choice(ar.READ_FROM), default="gcs", help="Read the input runs from the data bucket's mount (gcs) or the R2 copy (r2, mirrored locally; the job's R2 env)")
@option("-L", "--compact-level", type=ar.Level(), default=ar.COMPACT_LEVEL, help=ar.LEVEL_HELP)
@option("-m", "--mount", required=True, help="Local mount of the data bucket (the merges read the runs)")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit (the merged runs' cut)")
@option("-N", "--max-merges", type=IntRange(min=1), help="Stop after this many merged runs")
@option("-n", "--dry-run", is_flag=True, help="Print the plan; merge and write nothing")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-T", "--tmp", default="/stage/tmp", help="Scratch dir for merges")
def carry_cmd(bucket, gen, ranges_gen, scratch, read_from, compact_level, mount, mem, max_merges, dry_run, threads, tmp) -> None:
    """Run the newest manifest's due carries (`append_runner.plan_carries`, `merge_pending`): each merged run into its own
    dir (its deltas, its cut, its `meta.json` last), then a revision `manifests/<id>.m<NNN>.json` listing it. One merger
    per generation (a lease in the scratch bucket, mirrored to R2's `interval-store/<gen>/merging.json` for /health when the
    job has the R2 env); resumable."""
    store = run_store(bucket, scratch, gen, prefix=PREFIX)
    check_ranges_gen(Store(store_url(bucket)), gen, ranges_gen)
    k = Store(store_url(bucket)).read_json(f"{PREFIX}/{ranges_gen}/ranges.json")["k"]
    base = Store(store_url(bucket)).read_json(f"{PREFIX}/{gen}/scans.json")
    sl = ist.sliced(base)
    status = None if dry_run else ar.r2_merge_status(f"{PREFIX}/{gen}")
    rec = NULL if dry_run else from_env("merge")
    doc = ar.merge_pending(store, carry(gen, k, threads=threads, mem=mem, sliced=sl, generation=ist.has_generation(base)), Path(mount) / PREFIX / gen, tmp=Path(tmp), dry_run=dry_run,
                           max_merges=max_merges, max_level=compact_level, status=status,
                           source=(ar.R2Source(), f"{PREFIX}/{gen}") if read_from == "r2" else None, rec=rec)
    doc["prune"] = _summary(ar.finish_merge(rec, doc, lambda: ar.prune_folded(store, retain=FOLDED_RETAIN, dry_run=dry_run, where=" gcs")))
    print(json.dumps(doc, indent=1))


def _summary(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k != "keys"}


@cli.command("compact")
@stage_options
@ar.compact_options
@option("-w", "--rg-rows", "rg_rows", default=ist.SERVED_RG, type=IntRange(min=1), help="Rows per served row group (the compacted generation's)")
def compact_cmd(bucket, gen, ranges_gen, scratch, policy, read_from, grace_s, jobs, force, mount, mem, promote, threads, to_r2, tmp, rg_rows) -> None:
    """The compaction stage (`append_runner.run_compaction` with `interval_compact`): when the policy fires (or `-K`),
    the generation's base and runs folded into `<gen>.c<NNN>` (a fresh build's bytes), published on GCS (`scans.json`,
    then `compaction.json`, `successor.json`, `interval-store/CURRENT.json`), then with `--r2` on R2 (its pointer last);
    predecessors past the grace leave GCS. Holds the compaction lease (the scratch bucket's `interval-store/compacting.lease.json`)
    throughout: appends to the generation wait; exit 4 (`BUSY`) while another compaction holds it. `-N`: a candidate (no
    lease, no pointer; `promote` points at it). Prints a JSON summary."""
    from . import interval_compact as ic

    if (b := gs_bucket(bucket)) is None:
        raise SystemExit(f"interval store: the staged compaction reads a mounted GCS bucket; {bucket!r} is not one")
    try:
        doc = ar.run_compaction(Store(f"gs://{b}"), ic.compactor(rg_rows), gen, Path(mount), Path(tmp),
                                lease=ar.gcs_compaction_lease_file(gs_bucket(scratch), PREFIX) if promote else None, policy=policy, force=force,
                                r2_bucket=b if to_r2 else None, grace_s=grace_s, threads=threads, mem=mem, jobs=jobs, read_from=read_from,
                                promote=promote, rec=from_env("compact"))
    except ar.Busy as e:
        err(f"compact: {e}")
        raise SystemExit(ar.BUSY)
    print(json.dumps(doc, indent=1))


@cli.command("promote")
@option("-b", "--bucket", required=True, help="The store: a bucket name (gs://) or URL")
@option("-r", "--r2", "to_r2", is_flag=True, help="Then the R2 copy's pointers (the candidate must be on R2 already: `compact -N --r2`)")
@argument("gen")
def promote_cmd(bucket, to_r2, gen) -> None:
    """Point prod at candidate generation GEN (`compact -N`): its source's `successor.json`, then `interval-store/CURRENT.json`
    (when it named the source), then with `-r` the R2 copy's. Refused unless GEN holds every scan its source does."""
    if (b := gs_bucket(bucket)) is None:
        raise SystemExit(f"interval store: promote moves a GCS bucket's pointers; {bucket!r} is not one")
    print(json.dumps(ar.promote_gen(Store(f"gs://{b}"), PREFIX, gen, r2_bucket=b if to_r2 else None), indent=1))


@cli.command("compact-r2")
@stage_options
@option("-N", "--candidate", is_flag=True, help="Instead: the candidate compacted from it (`compact -N`), without its pointers")
def compact_r2_cmd(bucket, gen, ranges_gen, scratch, candidate) -> None:
    """Every compaction from the generation on, to R2 (the R2 account's job): each new generation, then its pointer, last."""
    if (b := gs_bucket(bucket)) is None:
        raise SystemExit(f"interval store: {bucket!r} is not a GCS bucket")
    if candidate:
        print(json.dumps(ar.publish_r2_candidate(b, PREFIX, gen, last=("scans.json",)), indent=1))
        return
    print(json.dumps(ar.publish_r2_chain(b, PREFIX, gen, last=("scans.json",)), indent=1))


@cli.command("r2")
@stage_options
@option("-d", "--scan", required=True, help="The published scan (`manifests/<D>.json`)")
@option("-m", "--manifest", help="The manifest to copy: `manifests/<this>.json` (default D's own; a merge's revision `<id>.mNNN`)")
@option("-w", "--workers", default=8, type=int, help="Parallel copies")
def r2_cmd(bucket, gen, ranges_gen, scratch, scan, manifest, workers) -> None:
    """Copy the served files of every run the manifest lists GCS → R2 (objects already there with the same size and md5
    skipped), check every one is there, then copy the manifest, last (`append_runner.r2_publish`). R2 via `R2_ENDPOINT`,
    `R2_BUCKET` and AWS_* (or R2_*) keys. Then, the manifest on R2, the runs a carry folded are deleted there
    (`append_runner.prune_folded`, planned by R2's own manifests). A no-op when the store is the serving R2 bucket
    itself (its own `add` prunes it)."""
    if serves(bucket, os.environ.get("R2_BUCKET")):
        err(f"r2 {manifest or scan}: the store {bucket} is the serving R2 bucket; nothing to copy")
        return
    if gs_bucket(bucket) is None:
        raise SystemExit(f"r2: copies from a whole GCS bucket, not {bucket}")
    doc = ar.r2_publish(gs_bucket(bucket), f"{PREFIX}/{gen}", manifest or scan, served=("served/",), workers=workers)
    pruned = ar.prune_folded(ar.r2_run_store(f"{PREFIX}/{gen}"), where=" r2")
    print(json.dumps({"gen": gen, "scan": scan, **doc, "prune": _summary(pruned)}))


@cli.command("prune")
@stage_options
@option("-G", "--grace-s", default=ar.GRACE_S, type=IntRange(min=ar.MIN_GRACE_S), help=f"Keep a superseded manifest's runs this many seconds (default {ar.GRACE_S}, at least {ar.MIN_GRACE_S})")
@option("-n", "--dry-run", is_flag=True, help="Report what would be deleted; delete nothing")
@option("-r", "--r2", is_flag=True, help="Prune the R2 copy (`R2_ENDPOINT`, `R2_BUCKET`, keys from the env), planned by R2's own manifests; default: GCS")
def prune_cmd(bucket, gen, ranges_gen, scratch, grace_s, dry_run, r2) -> None:
    """Delete the runs a carry folded (`append_runner.prune_folded`): every run dir listed by no manifest but the newest
    and those superseded less than `-G` s ago, inside one of the newest's runs. On GCS a folded run keeps `FOLDED_RETAIN`; on R2, nothing. The
    chain does it after each publish (GCS: the prune stage and the merge job; R2: the R2 job); this is the same, alone."""
    if r2:
        doc = ar.prune_folded(ar.r2_run_store(f"{PREFIX}/{gen}"), grace_s=grace_s, dry_run=dry_run, where=" r2")
    else:
        if gs_bucket(bucket) is None:
            raise SystemExit(f"prune: the GCS store's prune; for {bucket}, `interval-store add` prunes its own store")
        doc = ar.prune_folded(ar.GcsRunStore(gs_bucket(bucket), None, gen, prefix=PREFIX), grace_s=grace_s, retain=FOLDED_RETAIN, dry_run=dry_run, where=" gcs")
    print(json.dumps({"gen": gen, "store": "r2" if r2 else "gcs", **_summary(doc)}, indent=1))


def ready(profile: str | None) -> Config:
    """The profile's `Config` (`load_config`), its project defaulting to the credentials', the R2 secrets checked."""
    cfg = load_config(profile)
    if not cfg.p.project:
        from .gcp import gcp_project

        cfg = replace(cfg, p=replace(cfg.p, project=gcp_project()))
    cfg.p.r2_env_secrets()
    return cfg


@command("append")
@option("-c", "--catch-up", is_flag=True, help="Append every earlier published scan still pending first, in scan-id order")
@option("-K", "--compact-now", is_flag=True, help="After the merge stage, compact into a new generation whatever the policy says (when the generation has runs)")
@option("-M", "--no-merge", is_flag=True, help="Skip the merge stage (the due carries wait for a later run, or `interval-store merge`)")
@option("-n", "--dry-run", is_flag=True, help="Report each stage's state and what would run; submit and write nothing")
@option("-N", "--candidate", is_flag=True, help="The compaction (due, or -K) writes a candidate: published, nothing pointed at it, appends not held off (`interval_append promote` points prod at it)")
@option("-P", "--profile", help="Deployment profile JSON (default $INTERVAL_STORE_PROFILE)")
@option("-w", "--merge-wait", default=0, type=float, help="Seconds to wait on the merge job (default 0: submit it and go; it publishes on GCS when done)")
@argument("scan_id")
def append_cmd(catch_up: bool, compact_now: bool, no_merge: bool, dry_run: bool, candidate: bool, profile: str | None, merge_wait: float, scan_id: str) -> None:
    """Append SCAN_ID to the interval store: prepare → ranges → publish → R2 → prune, each stage skipped when its output
    exists; then the merge stage (the due carries, non-fatal), and when the compaction policy fires (or `-K`) the
    compaction stage (`compact`: GCS, then R2 with its pointer last). A generation compacted since the profile named it
    is followed to its successor. Exit 3 when SCAN_ID is not published yet, or an earlier published scan is pending
    (without -c); exit 4 when another `append` holds the coordinator lease."""
    cfg = ready(profile)
    runner = store_runner(cfg, dry_run=dry_run, merge=not no_merge, merge_wait=merge_wait)
    runner.compact_now = compact_now
    runner.candidate = candidate
    t0 = monotonic()
    done = exit_on(f"interval-store append {scan_id}", lambda: runner.run(scan_id, catch_up=catch_up), lambda m: err(m))
    print(json.dumps({"gen": runner.p.gen, "scan": scan_id, "appended": done, "dry_run": dry_run, "s": round(monotonic() - t0, 1),
                      "stages": runner.timings, "compaction": runner.compaction}))


@command("merge")
@option("-n", "--dry-run", is_flag=True, help="Report the due carries; submit and write nothing")
@option("-P", "--profile", help="Deployment profile JSON (default $INTERVAL_STORE_PROFILE)")
@option("-w", "--wait", type=float, help="Seconds to wait on the merge job (default: to its end, Batch's own cap)")
def merge_cmd(dry_run: bool, profile: str | None, wait: float | None) -> None:
    """The newest manifest's due carries, on their own: the merge job (`carry`), then R2 for the revision it publishes
    (`append`'s merge stage, alone: for a schedule of its own, or to catch up). Exit 1 when it fails (the store stays as
    it was)."""
    cfg = ready(profile)
    runner = store_runner(cfg, dry_run=dry_run, merge_wait=wait)
    exit_on("interval-store merge", lambda: runner.carries(fatal=True), lambda m: err(m))
    print(json.dumps({"gen": cfg.p.gen, "manifest": (runner.manifests() or [None])[-1], "dry_run": dry_run}))


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    cli()
