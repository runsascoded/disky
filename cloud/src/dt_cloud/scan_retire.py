"""Retire the per-scan index artifacts the stores make redundant (specs/lean-scans.md §2): `dt-cloud scan retire`.

Once the interval store (IS) and the static name index both hold a scan, its per-scan `path`/`bysize` sorts (and their
by-user copies), their `.groups.*` footers, v1 coarse tiers, search sidecars and its layer-2 listing are copies of what
the stores serve: a deployment's storage then grows by the stores' deltas alone. What it deletes, per scan:

| class    | files                                                                                  | action |
|----------|----------------------------------------------------------------------------------------|--------|
| `sort`   | `path-index*.parquet` (`path`, `bysize`, `-by-user` copies, a recompress's `.v1` copy)  | delete |
| `footer` | those sorts' `.groups.json` / `.groups.parquet`                                         | delete |
| `coarse` | `path-index-coarse*` (+ footers): v1 tiers                                              | delete |
| `search` | `path-index.{rows,trigrams,rows-search}.parquet` (+ footers)                            | delete |
| `layer2` | the layer-2 listing (`-l` templates), where the deployment keeps one                    | delete |
| `age`    | `age-pyramid-*`, `age-index*` (still read per scan)                                     | keep   |
| `extras` | `attr.tsv*`, `ck.txt` (provenance that can't be derived later)                          | keep   |
| `snapshot` | other `*.json` (`meta.json`, `verification.json`, …)                                  | keep   |
| `over-time` | `over-time*`                                                                         | keep   |
| `other`  | anything else under a scan's index dir (raw listing shards, unknown files)               | keep   |

A scan's index dirs are those its `-L` templates match (`{id}` the scan id, `{gen}` the index generation: every one, not
only the newest), under the scans root `-s`; a deployment with two copies (GCS and R2) runs once per copy (`-R` on the
second: its D1 rows are gone). Deletion is a whitelist: a file no class names stays.

**Selection**, oldest first, every scan found but the newest `-k` (so an append or a catch-up always has its inputs):
1. listed by the IS's newest manifest (the generation `-g`, default `interval-store/CURRENT.json`, followed to its
   newest successor; the base's `scans.json` when no manifest) **and** by the static index's (`-G`), else skipped;
2. a passing parity marker `interval-store/<gen>/verify/<id>.json` (`interval_verify.marker`), in the generation or one
   it was compacted from; else, executing without `-m`, `interval-store verify` runs for it now (against its newest
   per-scan `path` sort) and writes one. A dry run never verifies: such a scan is `needs-verify`.

**Order**, executing (`-x`): every to-be-deleted object is copied to `-b` first (sizes checked; any mismatch stops the
run before anything is deleted); then the scans' D1 rows (`footer_cache`, `index_row_groups`, then `index_schema`) for
the retired variants (`path`, `bysize`, `user`, `bysize-user`, `coarse*`; never `age-pyramid-*` or `over-time`); then the
files. D1 first: the per-scan reader (the site's fallback for a scan the IS doesn't serve) plans its reads from those
rows, so with the rows gone it finds no index and refuses, instead of planning reads of files that are being deleted
(a 404 mid-request). The `path` pointer (`index_schema`, `variant = 'path'`) stays unless `-P`: the site's scan list,
scan-arg resolution and start times still key on it (spec §1.4 S1).
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from click import IntRange, UsageError, argument, command, option

from . import index_footer as F
from . import interval_compact as ic
from . import interval_store as ist
from .append_runner import latest_key
from .blobstore import Store, store_url
from .scan_id import SCAN_ID
from .static_names import err

PREFIX = ist.PREFIX
STATIC_PREFIX = "static-names"
DELETE = ("sort", "footer", "coarse", "search", "layer2")
KEEP = ("age", "extras", "snapshot", "over-time", "other")
SEARCH_SUFFIXES = (".rows", ".trigrams", ".rows-search")
#: The per-scan sorts' D1 variants (`index_footer.SORT_VARIANTS`), plus the v1 `coarse*` tiers (a LIKE).
D1_VARIANTS = ("path", "bysize", "user", "bysize-user")
#: D1 statements name this many scans at most (a statement stays far under D1's 100 KB).
D1_CHUNK = 50


def classify(name: str) -> str:
    """A file's class (the module's table) by its name."""
    footer = name.endswith((".groups.json", ".groups.parquet"))
    stem = name.removesuffix(".groups.json").removesuffix(".groups.parquet").removesuffix(".parquet")
    if stem.startswith(("age-pyramid-", "age-index")):
        return "age"
    if stem.startswith("over-time"):
        return "over-time"
    if name.startswith("attr.tsv") or name == "ck.txt":
        return "extras"
    if stem.startswith("path-index") and (footer or name.endswith(".parquet")):
        if stem.startswith("path-index-coarse"):
            return "coarse"
        if stem.endswith(SEARCH_SUFFIXES):
            return "search"
        return "footer" if footer else "sort"
    if name.endswith(".json"):
        return "snapshot"
    return "other"


def template_rx(template: str) -> tuple[str, re.Pattern]:
    """A key template (`{id}` once, `{gen}` at most once, `*` any run of a segment's characters) as its listing prefix
    (the dir before the first wildcard) and a regex capturing `id` (and `gen`)."""
    if template.count("{id}") != 1 or template.count("{gen}") > 1:
        raise UsageError(f"template {template!r}: needs one {{id}} and at most one {{gen}}")
    parts = re.split(r"(\{id\}|\{gen\}|\*)", template)
    rx = "".join("(?P<id>[^/]+)" if p == "{id}" else "(?P<gen>[^/]+)" if p == "{gen}" else "[^/]*" if p == "*" else re.escape(p) for p in parts)
    lead = parts[0]
    return (lead.rsplit("/", 1)[0] if "/" in lead else ""), re.compile(rx)


@dataclass
class Scan:
    """One scan's per-scan objects under the scans root: `{key: (size, class)}`, and its newest `path` sort (`src`, the
    parity check's reference)."""
    id: str
    objects: dict[str, tuple[int, str]] = field(default_factory=dict)
    src: str | None = None
    src_gen: str = ""

    def deletable(self) -> dict[str, int]:
        return {k: n for k, (n, c) in self.objects.items() if c in DELETE}

    def by_class(self, classes: Iterable[str]) -> dict[str, list[int]]:
        out: dict[str, list[int]] = {}
        for n, c in self.objects.values():
            if c in classes:
                e = out.setdefault(c, [0, 0])
                e[0] += 1
                e[1] += n
        return dict(sorted(out.items()))


def find_scans(store: Store, layouts: list[str], layer2s: list[str]) -> dict[str, Scan]:
    """Every scan with per-scan objects under `store`: its index dirs (`layouts`) and their files, and its layer-2 files
    (`layer2s`). Ids that are not scan ids are skipped."""
    listings: dict[str, dict[str, int]] = {}

    def listing(prefix: str) -> dict[str, int]:
        if prefix not in listings:
            listings[prefix] = store.listing(prefix)
        return listings[prefix]

    scans: dict[str, Scan] = {}
    for t in layouts:
        prefix, rx = template_rx(t)
        keys = listing(prefix)
        dirs = []
        for key in keys:
            if (m := rx.fullmatch(key)) is None or not SCAN_ID.fullmatch(m["id"]):
                continue
            s = scans.setdefault(m["id"], Scan(m["id"]))
            gen = m.groupdict().get("gen") or ""
            if s.src is None or gen > s.src_gen:
                s.src, s.src_gen = key, gen
            dirs.append((s, key.rsplit("/", 1)[0] + "/"))
        for s, d in dirs:
            for key, n in keys.items():
                if key.startswith(d):
                    s.objects[key] = (n, classify(key.rsplit("/", 1)[-1]))
    for t in layer2s:
        prefix, rx = template_rx(t)
        for key, n in listing(prefix).items():
            if (m := rx.fullmatch(key)) is not None and SCAN_ID.fullmatch(m["id"]):
                scans.setdefault(m["id"], Scan(m["id"])).objects[key] = (n, "layer2")
    return dict(sorted(scans.items()))


def listed(store: Store, prefix: str) -> list[str]:
    """The scans a generation at `prefix` serves: its newest manifest's, else its base's `scans.json`'s."""
    newest = latest_key([k.removeprefix(f"{prefix}/") for k in store.keys(f"{prefix}/manifests/")])
    if newest:
        return store.read_json(f"{prefix}/{newest}")["scans"]
    if not store.exists(f"{prefix}/scans.json"):
        raise SystemExit(f"{store.uri(prefix)}: no such generation (no scans.json)")
    return [s["id"] for s in store.read_json(f"{prefix}/scans.json")["scans"]]


def lineage(store: Store, gen: str) -> list[str]:
    """`gen`, then each generation it was compacted from (`compaction.json` `from`), newest first."""
    out = [gen]
    while store.exists(key := f"{PREFIX}/{out[-1]}/{ic.COMPACTION}"):
        frm = store.read_json(key)["from"]
        if frm in out:
            break
        out.append(frm)
    return out


def find_marker(store: Store, gens: list[str], scan: str) -> dict | None:
    from .interval_verify import marker_key

    for g in gens:
        if store.exists(key := marker_key(f"{PREFIX}/{g}", scan)):
            return store.read_json(key)
    return None


# ── D1 ─────────────────────────────────────────────────────────────────────


def _variant_pred(store: str, path: bool) -> str:
    names = [F.d1_variant(v, store) for v in D1_VARIANTS if path or v != "path"]
    return f"(variant IN ({', '.join(F._q(v) for v in names)}) OR variant LIKE {F._q(F.d1_variant('coarse', store) + '%')})"


def _chunks(xs: list[str]) -> list[list[str]]:
    return [xs[i:i + D1_CHUNK] for i in range(0, len(xs), D1_CHUNK)]


def _tables(acct: str, tok: str) -> list[tuple[str, bool]]:
    """The tables to clear, in order, each with whether its `path` rows go: the footer LRU (where the D1 has it), the row
    groups, then the pointers."""
    return [*([("footer_cache", True)] if F.footer_cache_present(acct, tok) else []), ("index_row_groups", True), ("index_schema", False)]


def _has_d1() -> bool:
    """Whether D1 credentials are at hand (a deployment without D1 pointers has none)."""
    tok, acct = F._creds()
    return bool(tok and acct)


def d1_plan(dates: list[str], store: str, drop_path: bool) -> list[dict]:
    """The D1 rows retiring `dates` deletes: per table, `(date, variant, gen)` and its row count."""
    if not dates:
        return []
    tok, acct = F._creds()
    out = []
    for table, path in _tables(acct, tok):
        for ch in _chunks(dates):
            where = f"date IN ({', '.join(F._q(d) for d in ch)}) AND {_variant_pred(store, path or drop_path)}"
            if table == "index_schema" and store != F.PRIMARY_STORE:
                where = f"store = {F._q(store)} AND {where}"
            rows = F._d1_query(f"SELECT date, variant, gen, COUNT(*) AS n FROM {table} WHERE {where} GROUP BY date, variant, gen ORDER BY date, variant, gen;", acct, tok)
            out += [{"table": table, "date": r["date"], "variant": r["variant"], "gen": r["gen"], "rows": int(r["n"])} for r in rows]
    return out


def d1_delete(dates: list[str], store: str, drop_path: bool) -> int:
    """Delete `d1_plan`'s rows, table by table in its order; returns rows deleted."""
    if not dates:
        return 0
    tok, acct = F._creds()
    n = 0
    for table, path in _tables(acct, tok):
        for ch in _chunks(dates):
            where = f"date IN ({', '.join(F._q(d) for d in ch)}) AND {_variant_pred(store, path or drop_path)}"
            if table == "index_schema" and store != F.PRIMARY_STORE:
                where = f"store = {F._q(store)} AND {where}"
            n += len(F._d1_query(f"DELETE FROM {table} WHERE {where} RETURNING 1 AS n;", acct, tok))
    return n


# ── Retire ─────────────────────────────────────────────────────────────────


def backup(src: Store, dst: Store, objects: dict[str, int], tmp: Path) -> dict:
    """Every object of `objects` (`{key: size}`) copied from `src` to `dst` under the same key, each size checked; raises
    on a mismatch (nothing is deleted then)."""
    tmp.mkdir(parents=True, exist_ok=True)
    n = b = 0
    for key, size in sorted(objects.items()):
        local = tmp / "obj"
        src.get(key, local)
        dst.put(local, key)
        got = dst.size(key)
        if got != size:
            raise SystemExit(f"backup {key}: {got:,} B at {dst.uri(key)}, {size:,} B at {src.uri(key)}; nothing deleted")
        n += 1
        b += size
        local.unlink()
    return {"objects": n, "bytes": b}


def _totals(entries: Iterable[dict[str, list[int]]]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for e in entries:
        for c, (n, b) in e.items():
            t = out.setdefault(c, [0, 0])
            t[0] += n
            t[1] += b
    return dict(sorted(out.items()))


def retire(scans_root: str, layouts: list[str], *, iv_root: str, static_gen: str, iv_gen: str | None = None,
           static_root: str | None = None, layer2s: list[str] = (), keep: int = 2, only: list[str] = (),
           execute: bool = False, markers_only: bool = False, backup_root: str | None = None, d1: bool = True,
           d1_store: str = F.PRIMARY_STORE, drop_path: bool = False, work: Path | None = None) -> dict:
    """Plan (and with `execute`, carry out) the retirement; returns the report (`scans`, `totals`, `d1`, …)."""
    scans_root, iv_root = store_url(scans_root), store_url(iv_root)  # a bare bucket name is a GCS bucket (a profile's)
    src = Store(scans_root)
    iv = Store(iv_root)
    st = Store(store_url(static_root) if static_root else iv_root)
    if iv_gen is None:
        iv_gen, how = ic.resolve(iv)
        if iv_gen is None:
            raise SystemExit(f"{iv.root}/{PREFIX}: no current generation ({how}): name one with -g")
    iv_gen = ic.chain(iv, iv_gen)[-1]
    in_iv = set(listed(iv, f"{PREFIX}/{iv_gen}"))
    in_static = set(listed(st, f"{STATIC_PREFIX}/{static_gen}"))
    gens = lineage(iv, iv_gen)
    scans = find_scans(src, list(layouts), list(layer2s))
    ids = list(scans)
    kept = set(ids[len(ids) - keep:]) if keep else set()
    rows: list[dict] = []
    for sid, s in scans.items():
        r = {"id": sid, "status": "retire", "reason": None, "marker": None}
        if sid in kept:
            r.update(status="keep", reason=f"one of the newest {keep}")
        elif only and sid not in only:
            r.update(status="keep", reason="not named")
        elif sid not in in_iv:
            r.update(status="skip", reason=f"not in the interval store's {iv_gen}")
        elif sid not in in_static:
            r.update(status="skip", reason=f"not in the static index's {static_gen}")
        elif not s.deletable():
            r.update(status="clean", reason="nothing to delete")
        elif (m := find_marker(iv, gens, sid)) is not None:
            r["marker"] = "pass" if m["pass"] else "fail"
            if not m["pass"]:
                r.update(status="skip", reason=f"verify failed ({m['view_eq']}/{m['views']} views, {m['diff_eq']}/{m['diffs']} diffs equal)")
        elif markers_only:
            r.update(status="skip", reason="no verify marker", marker="missing")
        elif s.src is None:
            r.update(status="skip", reason="no verify marker, and no per-scan `path` sort to verify against", marker="missing")
        else:
            r.update(status="needs-verify", marker="missing")
        rows.append(r)

    own = work is None
    work = Path(tempfile.mkdtemp(prefix="scan-retire-")) if own else work
    try:
        if execute and (todo := [r for r in rows if r["status"] == "needs-verify"]):
            from .interval_append import scan_version
            from .interval_export import scan_entry
            from .interval_verify import verify_scans

            entries = [{"id": r["id"], "src": scans[r["id"]].src, "ts": scan_entry(iv, f"{PREFIX}/{iv_gen}", r["id"])[0]["ts"],
                        "version": scan_version(src, scans[r["id"]].src)} for r in todo]
            markers = verify_scans(iv, iv_gen, scans_root, entries, work / "verify")
            for r in todo:
                m = markers.get(r["id"])
                if m and m["pass"]:
                    r.update(status="retire", marker="verified")
                else:
                    r.update(status="skip", marker="fail", reason="verify failed" if m else "verify gave no result")
        for r in rows:
            s = scans[r["id"]]
            go = r["status"] == "retire"
            r["delete"] = s.by_class(DELETE) if go else {}
            r["keep"] = s.by_class(KEEP if go else (*DELETE, *KEEP))
        retiring = [r["id"] for r in rows if r["status"] == "retire"]
        pending = [r for r in rows if r["status"] == "needs-verify"]
        report = {
            "gen": iv_gen, "static_gen": static_gen, "keep": keep, "execute": execute, "scans": rows,
            "totals": {"delete": _totals(r["delete"] for r in rows), "keep": _totals(r["keep"] for r in rows),
                       "pending": _totals(scans[r["id"]].by_class(DELETE) for r in pending)},
            "d1": d1_plan(retiring, d1_store, drop_path) if d1 else None,
            # `-R` (another copy of the files): the pointer rows the retiring scans still have, which this pass won't
            # delete; a scan only this copy holds needs a pass with D1, or its rows outlive its files
            "d1_left": d1_plan(retiring, d1_store, drop_path) if not d1 and _has_d1() else None,
        }
        if report["d1_left"]:
            left = sorted({d["date"] for d in report["d1_left"]})
            err(f"retire: -R leaves D1 rows for {len(left)} retiring scans ({', '.join(left)}): retire them with D1 too")
        if not execute:
            return report
        doomed = {k: n for sid in retiring for k, n in scans[sid].deletable().items()}
        if backup_root:
            report["backup"] = backup(src, Store(store_url(backup_root)), doomed, work / "backup")
            err(f"retire: backed up {report['backup']['objects']} objects ({report['backup']['bytes']:,} B) to {backup_root}")
        if d1:
            report["d1_deleted"] = d1_delete(retiring, d1_store, drop_path)
            err(f"retire: deleted {report['d1_deleted']} D1 rows of {len(retiring)} scans")
        report["deleted"] = {"objects": src.delete(sorted(doomed)), "bytes": sum(doomed.values())}
        err(f"retire: deleted {report['deleted']['objects']} objects ({report['deleted']['bytes']:,} B) from {scans_root}")
        return report
    finally:
        if own:
            shutil.rmtree(work, ignore_errors=True)


def _fmt(cls: dict[str, list[int]]) -> str:
    n, b = sum(v[0] for v in cls.values()), sum(v[1] for v in cls.values())
    return f"{n} objects, {b:,} B" + (f" ({', '.join(f'{c} {v[0]}/{v[1]:,} B' for c, v in cls.items())})" if cls else "")


def render(report: dict) -> list[str]:
    """The report as text lines: one per scan, the totals, the D1 rows."""
    out = []
    for r in report["scans"]:
        line = f"{r['id']} {r['status']}"
        if r["reason"]:
            line += f" [{r['reason']}]"
        if r["delete"]:
            line += f": delete {_fmt(r['delete'])}"
        line += f"; keep {_fmt(r['keep'])}"
        out.append(line)
    t = report["totals"]
    verb = "deleted" if report["execute"] else "would delete"
    out.append(f"total: {verb} {_fmt(t['delete'])}; kept {_fmt(t['keep'])}")
    if t["pending"]:
        out.append(f"pending verify: {_fmt(t['pending'])}")
    if report["d1"] is not None:
        for d in report["d1"]:
            out.append(f"d1 {d['table']} {d['date']} {d['variant']} {d['gen']}: {d['rows']} rows")
    for d in report.get("d1_left") or []:
        out.append(f"d1 left (-R) {d['table']} {d['date']} {d['variant']} {d['gen']}: {d['rows']} rows")
    return out


@command("retire")
@option("-b", "--backup", "backup_root", help="Copy every object to be deleted here first (a dir or URL; same keys), sizes checked")
@option("-g", "--gen", help="The interval store's generation (default: its current, `interval-store/CURRENT.json`), followed to its newest successor")
@option("-G", "--static-gen", required=True, help="The static name index's generation (`static-names/GEN/` under -S)")
@option("-j", "--json", "as_json", is_flag=True, help="Print the report as JSON")
@option("-k", "--keep", default=2, type=IntRange(min=1), help="Never retire the newest N scans (appends and catch-ups read them)")
@option("-l", "--layer2", "layer2s", multiple=True, help="Key template of a scan's layer-2 listing under -s (`{id}`, `*` within a segment), e.g. `cw-l2/{id}/*.parquet`; repeat")
@option("-L", "--layout", "layouts", multiple=True, required=True, help="Key template of a scan's per-scan `path` sort under -s (`{id}`, `{gen}`): every file in its dir is classified; repeat")
@option("-m", "--markers-only", is_flag=True, help="Retire only scans with a passing verify marker (never run `interval-store verify`)")
@option("-n", "--dry-run", is_flag=True, help="Report only (the default; refused with -x)")
@option("-o", "--out", "iv_root", required=True, help="The interval store: a dir or URL (where `interval-store/` lives)")
@option("-P", "--drop-path-pointer", is_flag=True, help="Also delete the `path` pointers (`index_schema`): only once the site lists scans from the stores")
@option("-R", "--no-d1", is_flag=True, help="Leave D1 alone (a second copy of the files, or a deployment without D1 pointers)")
@option("-s", "--scans", "scans_root", required=True, help="Where the per-scan files are: a dir or URL, the root of -L's and -l's keys")
@option("-S", "--static-store", "static_root", help="The static name index's store (default: -o)")
@option("-t", "--store", "d1_store", default=F.PRIMARY_STORE, help="The D1 store these scans' rows belong to (`primary`, or a `STORES_JSON` key)")
@option("-w", "--work", help="Local work dir (verify's copies, backups' staging; default: a temporary one)")
@option("-x", "--execute", is_flag=True, help="Back up (-b), delete the D1 rows, then the files")
@argument("ids", nargs=-1)
def retire_cmd(backup_root, gen, static_gen, as_json, keep, layer2s, layouts, markers_only, dry_run, iv_root, drop_path_pointer, no_d1,
               scans_root, static_root, d1_store, work, execute, ids) -> None:
    """Retire the per-scan sorts, footers, search sidecars and layer-2 listings of the scans the interval store and the
    static name index both hold and a parity marker vouches for (all but the newest -k; IDS: only these). A dry run by
    default: per scan the objects and bytes per class it would delete and keep, the totals, and the D1 rows that would
    go. `-x` backs up (-b), deletes the D1 rows, then the files."""
    if dry_run and execute:
        raise UsageError("-n and -x: pick one (a dry run is the default)")
    report = retire(scans_root, list(layouts), iv_root=iv_root, static_gen=static_gen, iv_gen=gen, static_root=static_root,
                    layer2s=list(layer2s), keep=keep, only=list(ids), execute=execute, markers_only=markers_only,
                    backup_root=backup_root, d1=not no_d1, d1_store=d1_store, drop_path=drop_path_pointer,
                    work=Path(work) if work else None)
    if as_json:
        print(json.dumps(report))
        return
    for line in render(report):
        print(line)
