"""Superseded store generations (the interval store's, the static name index's), deleted: `dt-cloud store-gc` (constant size over time, specs/lean-scans.md).

(Per-scan index generations no D1 pointer names are `index-gc -F`'s, `gen_gc.py`.) A compaction's predecessors are pruned by the next `add` already (`interval_compact.prune_predecessors`). What nothing
deletes: an interval-store generation another was *backfilled* from (`backfill.json`), one a rebuild replaced (no record
links them), and every static-names generation a rebuild superseded. This lists each store's generations, keeps the live
ones, and deletes the rest whose newest write is older than the grace (a reader mid-flight, a rollback):

- `-t interval-store`: kept are `CURRENT.json`'s generation and its successors, plus `-k`. `-l` (the scan step's mode)
  narrows the candidates to the current one's lineage (what it was compacted or backfilled from): superseded by
  construction. Without `-l`, every other generation is a candidate (a rebuild's predecessor): review the dry run, and
  `-k` any the site pins (`INTERVAL_STORE_GEN`). A lineage generation's parity markers move to the current one first
  (`carry_markers`), and it keeps its `successor.json` (the forwarding record), as a prune does.
- `-t static-names`: kept are the `-k` generations, required: the job can't see which one each site reads
  (`STATIC_GEN`, per store and per env: a dev stack may read a newer one than prod), so the caller names them all.

A dry run (the default) prints what would go; `-x` deletes. Each line: the generation, objects, bytes, newest write, and
`delete` / `kept (…)`."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from click import Choice, IntRange, command, option

from . import interval_compact as ic
from . import interval_store as ist
from .blobstore import Store, store_url
from .static_names import err

STATIC_PREFIX = "static-names"
#: Default grace: a generation's newest write at least this old (seconds).
GRACE_S = 3 * 86400


def gen_dirs(store: Store, prefix: str) -> dict[str, dict[str, int]]:
    """`{gen: {key: size}}` of every generation dir under `prefix/` (one listing of the prefix)."""
    out: dict[str, dict[str, int]] = {}
    for k, n in store.listing(prefix).items():
        parts = k.split("/")
        if len(parts) > 2:
            out.setdefault(parts[1], {})[k] = n
    return out


def plan(store: Store, kind: str, keep: list[str], *, lineage_only: bool, grace_s: float, now: float) -> list[dict]:
    """One record per generation of `kind` in `store`: `{gen, objects, bytes, newest, action, reason}`."""
    from .scan_retire import lineage

    if kind == "static-names":
        if not keep:
            raise SystemExit("-t static-names: name every live generation with -k (the job can't see which each site reads)")
        prefix, protected, line = STATIC_PREFIX, set(keep), set()
        cur = None
    else:
        prefix = ist.PREFIX
        cur, how = ic.resolve(store)
        if cur is None:
            raise SystemExit(f"{store.root}/{prefix}: no current generation ({how}): nothing is known to be live")
        newest = ic.chain(store, cur)[-1]
        protected = {*ic.chain(store, cur), *keep}
        line = set(lineage(store, newest)) - {newest}
        cur = newest
    out = []
    for g, files in sorted(gen_dirs(store, prefix).items()):
        if kind == "interval-store":
            files = {k: n for k, n in files.items() if k != f"{prefix}/{g}/{ic.SUCCESSOR}"}
        if not files:
            continue
        mt = max(store.mtimes(f"{prefix}/{g}").values())
        rec = {"gen": g, "objects": len(files), "bytes": sum(files.values()),
               "newest": datetime.fromtimestamp(mt, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "lineage": g in line}
        if g in protected:
            rec.update(action="keep", reason="current" if g == cur or (kind == "interval-store" and g not in keep) else "kept (-k)")
        elif lineage_only and g not in line:
            rec.update(action="keep", reason="not in the current generation's lineage (-l)")
        elif now - mt < grace_s:
            rec.update(action="keep", reason=f"written {round((now - mt) / 3600)}h ago (grace {round(grace_s / 3600)}h)")
        else:
            rec.update(action="delete", reason=None)
        rec["_files"] = sorted(files)
        out.append(rec)
    return out


def run(store: Store, kind: str, keep: list[str], *, lineage_only: bool = False, grace_s: float = GRACE_S, now: float,
        execute: bool = False) -> list[dict]:
    recs = plan(store, kind, keep, lineage_only=lineage_only, grace_s=grace_s, now=now)
    cur = ic.chain(store, ic.resolve(store)[0])[-1] if kind == "interval-store" else None
    for r in recs:
        files = r.pop("_files")
        if r["action"] == "delete" and execute:
            if r["lineage"]:
                r["markers"] = ic.carry_markers(store, r["gen"], cur)
            r["deleted"] = store.delete(files)
        err(f"{kind}/{r['gen']}: {r['objects']:,} objects, {r['bytes']:,} B, newest {r['newest']}: "
            + (("deleted" if execute else "would delete") if r["action"] == "delete" else f"kept ({r['reason']})"))
    return recs


@command("store-gc")
@option("-G", "--grace", "grace_s", default=GRACE_S, type=IntRange(min=0), help="Keep a generation written within this many seconds (default 3 days)")
@option("-k", "--keep", multiple=True, help="A generation to keep whatever else holds (repeat): every one a site reads (`STATIC_GEN`, a pinned `INTERVAL_STORE_GEN`)")
@option("-l", "--lineage-only", is_flag=True, help="interval-store: only what the current generation was compacted or backfilled from (the scan step's mode)")
@option("-o", "--out", "store_root", required=True, help="The store: a dir or URL (where `interval-store/` / `static-names/` live)")
@option("-t", "--kind", type=Choice(["interval-store", "static-names"]), required=True, help="Which generations")
@option("-x", "--execute", is_flag=True, help="Delete (default: a dry run)")
def store_gc_cmd(grace_s, keep, lineage_only, store_root, kind, execute) -> None:
    """Delete superseded generations of a store's interval store or static name index, past a grace (a dry run unless
    -x). Prints a JSON summary."""
    import time

    if lineage_only and kind != "interval-store":
        raise SystemExit("-l: interval-store only")
    recs = run(Store(store_url(store_root)), kind, list(keep), lineage_only=lineage_only, grace_s=grace_s, now=time.time(), execute=execute)
    print(json.dumps({"kind": kind, "execute": execute, "generations": recs}, indent=1))
