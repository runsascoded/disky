"""Sweep executor — dry-run by default (specs/sweep-executor.md phase 3).

Consumes a `sweep manifest` plan dir. Per eligible directory: fresh re-list
(captures generations — the pinned listing has none), intersect with the
manifest, verify `timeCreated` matches (an overwrite since the scan keeps the
object), detect drift (new keys under a swept dir → skip the dir by default),
and — only with ``--for-real`` — issue generation-matched batch deletes.

Every decision lands in a per-bucket log parquet under the plan dir
(``would-delete/`` or ``deleted/``): name, size, generation, decision.
"""

from __future__ import annotations

import datetime as dt
import json
import random
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from functools import partial

err = partial(print, file=sys.stderr)

#: Per-key decisions (the log's `decision` column).
DECISIONS = (
    "delete",              # in manifest ∩ live, created matches → deleted (or would be)
    "skipped_gone",        # in manifest, no longer live — graceful no-op
    "skipped_overwritten", # live but created moved — rewritten since the scan; keep
    "delete_failed",       # real run: no definitive answer from GCS after every retry — state unknown, dir reported in `failed_dirs`
)

BATCH = 100  # GCS JSON batch limit per request
#: Retries of a delete batch on a transient answer (whole request or item):
#: 2^n s + jitter, capped, so a bucket-wide 503 (the 2026-09-11 third real
#: run's first batch) rides out a minute of unavailability.
DELETE_ATTEMPTS = 8
DELETE_BACKOFF_CAP = 60.0
#: How often a running bucket writes `progress/<bucket>.json` (the console's
#: progress bar; also the only live signal a job gives).
PROGRESS_EVERY = 30.0
TRANSIENT_CODES = frozenset({408, 429, 500, 502, 503, 504})
_sleep = time.sleep  # patched in tests


def _status_code(resp) -> int | None:
    """HTTP status of one batch sub-response — a `requests.Response`, or (with
    `raise_exception=False`) the `GoogleAPICallError` the library built for a
    non-2xx part."""
    code = getattr(resp, "status_code", None)
    if code is None:
        code = getattr(resp, "code", None)
    return int(code) if code is not None else None


def _outcome(code: int | None) -> str | None:
    """A sub-response's decision, or None when it must be retried."""
    if code is not None and 200 <= code < 300:
        return "delete"
    if code == 404:
        return "skipped_gone"           # already gone (an earlier attempt landed, or someone else's delete)
    if code == 412:
        return "skipped_overwritten"    # generation moved since the listing: not the object we planned on
    return None


def delete_batch(client, bkt, blobs: list) -> list[tuple[object, str]]:
    """Generation-matched deletes of `blobs` in one GCS batch (≤ `BATCH`),
    each item settled by its own sub-response: 2xx deleted, 404 gone, 412
    overwritten; anything else — or a whole-request failure (5xx, 429, a
    connection error, a malformed batch reply) — is retried with backoff.
    Returns `(blob, decision)` per input; items still unanswered after
    `DELETE_ATTEMPTS` come back `delete_failed`."""
    from google.api_core import exceptions as gax

    remaining = list(blobs)
    settled: dict[int, str] = {}
    # Items whose last attempt got no per-item answer: the server may have
    # applied the delete and lost the reply, so a 404 on the retry is ours
    # (run 4 on east5, 2026-09-11: two deletes landed at 06:19–06:20 at their
    # manifest generation and came back "gone"). A 404 after a per-item
    # transient (the server answered: not applied) is someone else's.
    unanswered: set[int] = set()
    for attempt in range(DELETE_ATTEMPTS):
        responses = None
        try:
            with client.batch(raise_exception=False) as b:
                for blob in remaining:
                    bkt.delete_blob(blob.name, if_generation_match=blob.generation)
            responses = list(getattr(b, "_responses", []))
            if len(responses) != len(remaining):
                responses = None  # a reply we can't attribute per item: retry the whole batch
        except gax.GoogleAPICallError as e:
            if e.code not in TRANSIENT_CODES:
                raise
        except (ConnectionError, TimeoutError, ValueError, OSError):
            pass
        retry = []
        for blob, resp in zip(remaining, responses or []):
            code = _status_code(resp)
            decision = _outcome(code)
            if decision is None and code is not None and code not in TRANSIENT_CODES:
                raise RuntimeError(f"delete {blob.name}@{blob.generation}: unexpected HTTP {code}")
            if decision is None:
                retry.append(blob)
                unanswered.discard(id(blob))
            else:
                settled[id(blob)] = "delete" if decision == "skipped_gone" and id(blob) in unanswered else decision
                unanswered.discard(id(blob))
        if responses is None:
            unanswered.update(id(b) for b in remaining)
        else:
            remaining = retry
        if not remaining:
            break
        err(f"delete batch: {len(remaining)} of {len(blobs)} unsettled after attempt {attempt + 1}/{DELETE_ATTEMPTS} — retrying")
        _sleep(min(DELETE_BACKOFF_CAP, 2.0 ** attempt) + random.uniform(0, 1))
    return [(blob, settled.get(id(blob), "delete_failed")) for blob in blobs]


def list_roots(dirs: set[str], approved: tuple[str, ...], bucket: str) -> list[str]:
    """Prefix-free listing roots covering every manifest dir: each dir cut to
    one segment below its band (its top-level segment when no band covers
    it), so a band fans out into its children's listings; a dir that *is* its
    band (or a root's ancestor) becomes the root itself and swallows the
    deeper ones. `''` = the whole bucket."""
    root_of_band: dict[str, int] = {}
    for a in approved:
        pre = f"gs://{bucket}/"
        if a.startswith(pre):
            rel = a[len(pre):].rstrip("/")
            root_of_band[rel] = (rel.count("/") + 1) if rel else 0
    roots: set[str] = set()
    for dn in dirs:
        hit = max((r for r in root_of_band if dn == r or (dn.startswith(r + "/") if r else True)), key=len, default=None)
        depth = root_of_band[hit] if hit is not None else 0
        roots.add("/".join(dn.split("/")[: depth + 1]) if dn else "")
    out = sorted(roots)
    pruned: list[str] = []
    for r in out:
        if any(r == p or (r.startswith(p + "/") if p else True) for p in pruned):
            continue
        pruned.append(r)
    return pruned


def execute_plan(
    plan_dir: str,
    for_real: bool = False,
    only_buckets: tuple[str, ...] = (),
    drift: str = "skip",  # skip | proceed — dirs that gained NEW keys since the scan
    workers: int = 8,
    delete_workers: int = 32,
    max_root_objects: int = 250_000,  # a listing root bigger than this splits into its children (one listing thread per root)
    min_soft_delete_days: int = 7,
    client=None,
    stop: threading.Event | None = None,  # set → roots not yet started are skipped; the log and summary still land (`interrupted`)
) -> dict:
    import fsspec
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    from google.cloud import storage

    fs, ppath = fsspec.core.url_to_fs(plan_dir)
    with fs.open(f"{ppath}/plan-summary.json") as fh:
        plan = json.load(fh)
    client = client or storage.Client()
    approved = tuple(plan.get("approved") or ())

    def band_of(bucket: str, dn: str) -> str:
        p = f"gs://{bucket}/{dn}/" if dn else f"gs://{bucket}/"
        hits = [a for a in approved if p.startswith(a)]
        if hits:
            return max(hits, key=len)
        top = dn.split("/", 1)[0] if dn else ""
        return f"gs://{bucket}/{top}/" if top else f"gs://{bucket}/"

    mode = "deleted" if for_real else "would-delete"
    summary: dict = {"plan": plan_dir, "for_real": for_real, "drift": drift, "buckets": {}}
    # Deletes run on their own pool, fed by every listing root: a batch of 100
    # is ~1–2 s of sequential server work, so a lopsided root (east5's
    # largest held 31% of the objects) no longer serializes a third of the run
    # on one thread — the bucket's ~1000 writes/s is the ceiling instead.
    dpool = ThreadPoolExecutor(max_workers=delete_workers) if for_real else None
    log_schema = pa.schema([
        ("name", pa.string()), ("size_bytes", pa.int64()), ("generation", pa.int64()),
        ("decision", pa.string()), ("dir", pa.string()),
    ])

    for bucket, binfo in plan["buckets"].items():
        if only_buckets and bucket not in only_buckets:
            continue
        if "eligible" not in binfo:
            continue
        mpath = f"{ppath}/manifest/{bucket}.parquet"
        if not fs.exists(mpath):
            raise SystemExit(f"plan says {bucket} has eligible keys but {mpath} is missing")
        missing_perms = _missing_perms(client.bucket(bucket))
        if missing_perms:
            msg = (
                f"{bucket}: the job identity lacks {', '.join(missing_perms)}"
                " — refusing --for-real (grant roles/storage.objectUser + roles/storage.legacyBucketReader on the bucket)"
            )
            if for_real:
                raise SystemExit(msg)
            err(f"WARNING {msg.replace('refusing', 'a real run would be refused:')}")
        soft_delete_days = _soft_delete_days(client, bucket)
        if soft_delete_days < min_soft_delete_days:
            msg = f"{bucket}: soft delete retention {soft_delete_days:.0f}d < required {min_soft_delete_days}d — refusing --for-real"
            if for_real:
                raise SystemExit(msg)
            err(f"WARNING {msg.replace('refusing', 'a real run would be refused:')}")
        # The manifest stays an Arrow table sorted by name (35M keys on the
        # biggest bucket: ~8 GB as Arrow strings, vs ~25 GB as two pandas
        # copies), and each listing root takes its contiguous slice by binary
        # search — no per-root scans, no per-dir DataFrame dict.
        # `dir` repeats each object's directory (5 GB of strings on the 35M-key
        # bucket, ~500k distinct): keep it dictionary-encoded. `name` gets
        # 64-bit offsets — `take` over 35M ~150-byte names concatenates past
        # `string`'s 2 GB limit ("offset overflow", the 2026-09-10 dry run).
        with fs.open(mpath, "rb") as fh:  # deterministic close: see `sweep manifest`
            mt = pq.read_table(fh, columns=["name", "size_bytes", "created", "dir"], read_dictionary=["dir"]).unify_dictionaries()
        mt = mt.set_column(mt.schema.get_field_index("name"), "name", pc.cast(mt["name"], pa.large_string()))
        # One chunk per column (a `take` over a 438-chunk column concatenates
        # it on every call — seconds per root), then sort an index (8
        # bytes/row), not the table: the bisection reads names through it and
        # only each root's slice is ever materialized, so the 35M-key bucket
        # peaks near the ~8 GB read instead of twice that.
        mt = mt.combine_chunks()
        names = mt["name"]
        order = pc.sort_indices(mt, sort_keys=[("name", "ascending")])
        dirs_all = set(pc.unique(mt["dir"]).to_pylist())
        err(f"{bucket}: {len(mt):,} manifest keys in {len(dirs_all):,} dirs ({mode})")
        bkt = client.bucket(bucket)
        counts: Counter = Counter()
        drift_dirs: list[dict] = []
        failed_dirs: list[dict] = []
        roots_skipped = 0

        def _lower_bound(key: str) -> int:
            # first sorted position whose name >= key, by binary search through
            # the index (~25 × 2 scalar reads per probe; never a column scan)
            lo, hi = 0, len(order)
            while lo < hi:
                mid = (lo + hi) // 2
                if names[order[mid].as_py()].as_py() < key:
                    lo = mid + 1
                else:
                    hi = mid
            return lo

        def _bisect(prefix: str) -> tuple[int, int]:
            # [lo, hi) sorted positions of names starting with `prefix`
            return _lower_bound(prefix), _lower_bound(prefix + "\x7f")

        BATCH_ROWS = 262_144

        def root_rows(root: str):
            """The manifest rows under `root/` (every row for the bucket root)
            in name order, as `(name, size, created, dir)` tuples — materialized
            256k rows at a time, so a root holding most of the bucket never
            becomes one frame."""
            lo, hi = _bisect(root + "/") if root else (0, len(order))
            sl = order.slice(lo, hi - lo)
            for start in range(0, len(sl), BATCH_ROWS):
                t = mt.take(sl.slice(start, BATCH_ROWS))
                yield from zip(*(t[c].to_pylist() for c in ("name", "size_bytes", "created", "dir")))

        # One recursive listing per *root* (a band's child directory, or the
        # band itself when it is directly eligible) instead of one per
        # directory: 1.4M eligible dirs would be 1.4M list calls; the roots are
        # a few thousand, each a streamed page walk. GCS lists names in
        # lexicographic order and the manifest is sorted the same way, so each
        # root is a merge: manifest-only → gone, both → created check, live-only
        # under a manifest dir → drift for that dir. A directory's decisions are
        # buffered until the listing has moved past it (its keys are contiguous
        # under `dn/`, nested dirs form a stack), and only then deleted — drift
        # discovered late still gates the whole directory.
        roots = list_roots(dirs_all, approved, bucket)

        def root_count(root: str) -> int:
            lo, hi = _bisect(root + "/") if root else (0, len(order))
            return hi - lo

        # Each root is one listing thread. Two things keep the pool busy to the
        # end: an oversized root splits into its children (when no manifest
        # object sits directly in it — those would be missed), repeatedly, and
        # the roots run largest first (longest-processing-time first), so the
        # tail is small roots filling in, not one big listing everyone waits
        # on (central2 on 2026-09-11: 1,750 → 400 deletes/s over its last
        # two hours, alphabetical order, one huge root left).
        from bisect import bisect_right
        for _ in range(6):
            big = [r for r in roots if root_count(r) > max_root_objects]
            if not big:
                break
            srt = sorted(roots)
            kids: dict[str, set[str]] = {r: set() for r in big}
            direct: set[str] = set()
            for dn in dirs_all:
                i = bisect_right(srt, dn)
                r = srt[i - 1] if i else None
                if r is None or not (dn == r or (dn.startswith(r + "/") if r else True)):
                    continue
                if r not in kids:
                    continue
                if dn == r:
                    direct.add(r)
                else:
                    rel = dn[len(r) + 1:] if r else dn
                    kids[r].add(rel.split("/", 1)[0])
            out: list[str] = []
            for r in roots:
                if r in kids and r not in direct and len(kids[r]) > 1:
                    out.extend(f"{r}/{k}" if r else k for k in sorted(kids[r]))
                else:
                    out.append(r)
            if len(out) == len(roots):
                break
            roots = out
        roots = sorted(roots, key=root_count, reverse=True)

        def do_root(root: str):
            if stop is not None and stop.is_set():
                return None  # asked to stop: leave this root for a re-run
            prefix = f"{root}/" if root else ""
            want = root_rows(root)
            w = next(want, None)
            pend: dict[str, dict] = {}
            stack: list[str] = []
            done: list[tuple] = []

            def flush(dn: str) -> None:
                p = pend.pop(dn)
                todo, out = p["todo"], p["out"]
                drifted = p["extra_o"] > 0
                if drifted and drift == "skip":
                    emit(out)
                    done.append((dn, out, {"dir": dn, "new_objects": p["extra_o"], "new_bytes": p["extra_b"], "skipped_deletes": len(todo)}, 0))
                    return
                deleted_b = 0
                if for_real:
                    n_failed = 0
                    futs = [dpool.submit(delete_batch, client, bkt, todo[i : i + BATCH]) for i in range(0, len(todo), BATCH)]
                    for fut in futs:
                        for blob, decision in fut.result():
                            out.append((blob.name, int(blob.size or 0), int(blob.generation), decision, dn))
                            if decision == "delete":
                                deleted_b += blob.size or 0
                            elif decision == "delete_failed":
                                n_failed += 1
                    if n_failed:
                        failed_dirs.append({"dir": dn, "objects": n_failed})
                else:
                    for blob in todo:
                        out.append((blob.name, int(blob.size or 0), int(blob.generation), "delete", dn))
                        deleted_b += blob.size or 0
                # The dir's rows reach the log now, from this thread — not when
                # the root's result is consumed — so a later failure elsewhere
                # (or a kill) loses at most one unflushed chunk, never the run.
                emit(out)
                done.append((dn, out, ({"dir": dn, "new_objects": p["extra_o"], "new_bytes": p["extra_b"], "skipped_deletes": 0} if drifted else None), deleted_b))

            def settle(name: str) -> None:
                # close every open dir the listing has moved past
                while stack and stack[-1] != "" and not name.startswith(stack[-1] + "/"):
                    flush(stack.pop())

            def ensure(dn: str) -> dict:
                if dn not in pend:
                    pend[dn] = {"todo": [], "out": [], "extra_o": 0, "extra_b": 0}
                    stack.append(dn)
                return pend[dn]

            def gone(row) -> None:
                name, size, _created, dn = row
                settle(name)
                ensure(dn)["out"].append((name, int(size), 0, "skipped_gone", dn))

            for blob in client.list_blobs(bucket, prefix=prefix):
                n = blob.name
                while w is not None and w[0] < n:
                    gone(w)
                    w = next(want, None)
                settle(n)
                dn = n.rpartition("/")[0]
                if w is not None and w[0] == n:
                    p = ensure(w[3])
                    created = blob.time_created.replace(tzinfo=dt.timezone.utc) if blob.time_created.tzinfo is None else blob.time_created
                    if abs((created - w[2]).total_seconds()) > 1:
                        p["out"].append((n, int(w[1]), int(blob.generation), "skipped_overwritten", w[3]))
                    else:
                        p["todo"].append(blob)
                    w = next(want, None)
                elif dn in dirs_all:
                    p = ensure(dn)
                    p["extra_o"] += 1
                    p["extra_b"] += blob.size or 0
            while w is not None:
                gone(w)
                w = next(want, None)
            while stack:
                flush(stack.pop())
            return done

        total_deleted_b = 0
        bands: dict[str, Counter] = {}
        log_dir = f"{ppath}/{mode}/{bucket}"
        fs.makedirs(log_dir, exist_ok=True)
        # Decisions stream to the log as roots complete (35M of them on the
        # biggest bucket — never all in memory at once), as *part files*: each
        # chunk is its own complete parquet (`part-00042.parquet`), durable the
        # moment it lands — a job killed from outside loses at most the chunk
        # in memory, never the run (the 2026-09-11 eu-west4 run's single
        # parquet had no footer when its job was deleted: ~2M deletes with no
        # record). Readers glob the directory. Small
        # chunks on a real run (the undo record); the site's parquet viewer
        # pages within a row group, so 64k rows (~1.7 MB) keeps a dry run's
        # pages cheap.
        ROWS_PER_GROUP = 8_192 if for_real else 65_536
        buf: list[tuple] = []
        n_written = 0
        n_parts = 0
        log_lock = threading.Lock()

        def flush_log(final: bool = False) -> None:
            nonlocal buf, n_written, n_parts
            while len(buf) >= ROWS_PER_GROUP or (final and buf):
                chunk, buf = buf[:ROWS_PER_GROUP], buf[ROWS_PER_GROUP:]
                cols = list(zip(*chunk))
                with fs.open(f"{log_dir}/part-{n_parts:05d}.parquet", "wb") as fh:
                    pq.write_table(pa.table(dict(zip(log_schema.names, cols)), schema=log_schema), fh, row_group_size=ROWS_PER_GROUP)
                n_parts += 1
                n_written += len(chunk)

        # Live progress for the console: what the workers have logged so far,
        # written every PROGRESS_EVERY seconds and once more at the end.
        prog: dict = {"bucket": bucket, "mode": mode, "roots": len(roots), "roots_done": 0, "decisions": Counter(), "delete_bytes": 0,
                      "started": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "updated": None, "done": False}

        def write_progress(final: bool = False) -> None:
            with log_lock:
                snap = {**prog, "decisions": dict(prog["decisions"]), "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "done": final}
            try:
                fs.makedirs(f"{ppath}/progress", exist_ok=True)
                with fs.open(f"{ppath}/progress/{bucket}.json", "w") as fh:
                    json.dump(snap, fh)
            except Exception as e:  # progress is advisory; never the run's problem
                err(f"WARN: progress write failed: {e}")

        prog_stop = threading.Event()

        def progress_loop() -> None:
            while not prog_stop.wait(PROGRESS_EVERY):
                write_progress()

        threading.Thread(target=progress_loop, name="progress", daemon=True).start()

        def emit(rows: list[tuple]) -> None:
            with log_lock:
                buf.extend(rows)
                flush_log()
                for _name, size, _gen, decision, _dn in rows:
                    prog["decisions"][decision] += 1
                    if decision == "delete":
                        prog["delete_bytes"] += size

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for done in pool.map(do_root, roots):
                    with log_lock:
                        prog["roots_done"] += 1
                    if done is None:
                        roots_skipped += 1
                        continue
                    for dn, out, drifted, dbytes in done:
                        band = bands.setdefault(band_of(bucket, dn), Counter())
                        if drifted:
                            drift_dirs.append(drifted)
                            band["drift_new_objects"] += drifted["new_objects"]
                        total_deleted_b += dbytes
                        for _name, size, _gen, decision, _dn in out:
                            counts[decision] += 1
                            if decision == "delete":
                                band["bytes"] += size
                                band["objects"] += 1
                            elif decision == "skipped_gone":
                                band["gone"] += 1
                            elif decision == "skipped_overwritten":
                                band["overwritten"] += 1
                            else:
                                band["failed"] += 1
        finally:
            # Whatever the workers emitted lands as a final part — a root that
            # raised (a listing error) doesn't take the rest with it.
            with log_lock:
                flush_log(final=True)
            prog_stop.set()
            write_progress(final=True)
        summary["buckets"][bucket] = {
            "missing_perms": missing_perms,
            "soft_delete_days": soft_delete_days,
            "decisions": dict(counts),
            "delete_bytes": total_deleted_b,
            "drift_dirs": drift_dirs,
            "failed_dirs": failed_dirs,
            "bands": {b: dict(c) for b, c in bands.items()},
            **({"interrupted": {"roots_skipped": roots_skipped, "roots": len(roots)}} if roots_skipped else {}),
        }
        err(
            f"  {bucket}: {counts['delete']:,} {mode} ({total_deleted_b / 1e12:.2f} TB), "
            f"{counts['skipped_gone']:,} gone, {counts['skipped_overwritten']:,} overwritten, "
            f"{len(drift_dirs):,} drifted dir(s){' (skipped)' if drift == 'skip' else ''}"
            + (f", {counts['delete_failed']:,} deletes UNANSWERED in {len(failed_dirs):,} dir(s)" if failed_dirs else "")
            + (f" — STOPPED with {roots_skipped:,} of {len(roots):,} roots not started" if roots_skipped else "")
        )

    if dpool is not None:
        dpool.shutdown(wait=True)
    with fsspec.open(f"{plan_dir}/{mode}-summary.json", "w") as fh:
        json.dump(summary, fh, indent=2)
    summary["_plan"] = plan
    return summary


def read_log(fs, ppath: str, mode: str, bucket: str):
    """A run's decision log for `bucket`: the part files under
    `<mode>/<bucket>/` (current layout) plus the single `<mode>/<bucket>.parquet`
    of older runs, as one table (None if neither exists)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    tables = []
    single = f"{ppath}/{mode}/{bucket}.parquet"
    if fs.exists(single):
        with fs.open(single, "rb") as fh:
            tables.append(pq.read_table(fh))
    for part in sorted(fs.glob(f"{ppath}/{mode}/{bucket}/part-*.parquet")):
        with fs.open(part, "rb") as fh:
            tables.append(pq.read_table(fh))
    return pa.concat_tables(tables) if tables else None


def stop_file_watch(plan_dir: str, stop: threading.Event, every: float = 10.0) -> threading.Thread:
    """Set `stop` once `PLAN_DIR/STOP` exists (`sweep stop`); a daemon thread
    polling every `every` seconds."""
    import fsspec

    fs, ppath = fsspec.core.url_to_fs(plan_dir)
    flag = f"{ppath}/STOP"

    def poll() -> None:
        while not stop.is_set():
            try:
                if fs.exists(flag):
                    err(f"STOP file present ({plan_dir}/STOP) — finishing started roots, skipping the rest")
                    stop.set()
                    return
            except Exception as e:  # a flaky HEAD must not end the run
                err(f"WARN: STOP poll failed: {e}")
            stop.wait(every)

    t = threading.Thread(target=poll, name="stop-file-watch", daemon=True)
    t.start()
    return t


#: Per-key outcomes of an undo (the `restored/` log's `decision` column).
UNDO_DECISIONS = (
    "restored",        # soft-deleted generation restored as a new live generation
    "would_restore",   # dry run: the restore that would be issued
    "already_live",    # a live object exists under that name (an earlier undo, or a rewrite) — untouched
    "unrestorable",    # GCS has no soft-deleted copy any more (window elapsed, or never soft-deleted)
    "failed",          # any other error, message in `error`
)


def undo_run(
    log_dir: str,
    only_buckets: tuple[str, ...] = (),
    prefixes: tuple[str, ...] = (),
    dry_run: bool = False,
    workers: int = 16,
    client=None,
    deadline: int | None = None,
    now: int | None = None,
) -> dict:
    """Restore what a real run deleted: every `decision == 'delete'` row of its
    `deleted/<bucket>.parquet` logs (optionally only under `prefixes`,
    `gs://bucket/dir/` or bare `bucket/dir/`), via the GCS soft-delete
    restore of exactly the logged generation. `if_generation_match=0` makes it
    safe to re-run and safe against rewrites: a name that is live again is
    left alone (`already_live`), not clobbered. Per-object calls on a thread
    pool (restores aren't batched: a partial failure must be attributable per
    key). Writes `restored/<bucket>-<stamp>.parquet` + `undo-<stamp>-summary.json`
    beside the run's own logs; returns the summary. `deadline` (the run's
    `undo_deadline`) refuses a late undo up front — GCS would just answer 404
    per object, slowly."""
    import fsspec
    import pyarrow as pa
    import pyarrow.parquet as pq

    now = now or int(dt.datetime.now(dt.timezone.utc).timestamp())
    if deadline is not None and now > deadline:
        raise SystemExit(
            f"undo window closed {dt.datetime.fromtimestamp(deadline, dt.timezone.utc):%Y-%m-%d %H:%MZ} "
            f"(soft-delete retention elapsed) — nothing can be restored"
        )
    fs, ppath = fsspec.core.url_to_fs(log_dir)
    with fs.open(f"{ppath}/deleted-summary.json") as fh:
        dsum = json.load(fh)
    if not dsum.get("for_real"):
        raise SystemExit(f"{log_dir} is a dry run — it deleted nothing")
    with fs.open(f"{ppath}/plan-summary.json") as fh:
        plan = json.load(fh)
    approved = tuple(plan.get("approved") or ())
    if client is None:
        from google.cloud import storage
        client = storage.Client()
    from google.api_core.exceptions import NotFound, PreconditionFailed

    def band_of(bucket: str, dn: str) -> str:
        p = f"gs://{bucket}/{dn}/" if dn else f"gs://{bucket}/"
        hits = [a for a in approved if p.startswith(a)]
        if hits:
            return max(hits, key=len)
        top = dn.split("/", 1)[0] if dn else ""
        return f"gs://{bucket}/{top}/" if top else f"gs://{bucket}/"

    def wanted(bucket: str, name: str) -> bool:
        if not prefixes:
            return True
        full = f"gs://{bucket}/{name}"
        for p in prefixes:
            q = p if p.startswith("gs://") else f"gs://{p}"
            if full.startswith(q):
                return True
        return False

    stamp = f"{dt.datetime.fromtimestamp(now, dt.timezone.utc):%Y%m%dT%H%M%SZ}"
    schema = pa.schema([
        ("name", pa.string()), ("size_bytes", pa.int64()), ("generation", pa.int64()),
        ("new_generation", pa.int64()), ("decision", pa.string()), ("error", pa.string()), ("dir", pa.string()),
    ])
    summary: dict = {"log_dir": log_dir, "stamp": stamp, "dry_run": dry_run, "prefixes": list(prefixes), "buckets": {}}
    for bucket in dsum["buckets"]:
        if only_buckets and bucket not in only_buckets:
            continue
        log = read_log(fs, ppath, "deleted", bucket)
        if log is None:
            continue
        t = log.select(["name", "size_bytes", "generation", "decision", "dir"]).to_pandas()
        t = t[t["decision"] == "delete"]
        t = t[[wanted(bucket, n) for n in t["name"]]]
        todo = list(t[["name", "size_bytes", "generation", "dir"]].itertuples(index=False, name=None))
        err(f"{bucket}: {len(todo):,} deleted object(s) to restore{' (dry run)' if dry_run else ''}")
        bkt = client.bucket(bucket)

        def one(row) -> dict:
            name, size, gen, dn = row
            base = {"name": name, "size_bytes": int(size), "generation": int(gen), "new_generation": 0, "error": None, "dir": dn}
            if dry_run:
                return {**base, "decision": "would_restore"}
            try:
                blob = bkt.restore_blob(name, generation=int(gen), if_generation_match=0)
                return {**base, "decision": "restored", "new_generation": int(getattr(blob, "generation", 0) or 0)}
            except PreconditionFailed:
                return {**base, "decision": "already_live"}
            except NotFound:
                return {**base, "decision": "unrestorable"}
            except Exception as e:  # keep going: the log names every failure, the summary counts them
                return {**base, "decision": "failed", "error": f"{type(e).__name__}: {e}"[:500]}

        rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            rows.extend(pool.map(one, todo))
        counts: Counter = Counter(r["decision"] for r in rows)
        bands: dict[str, Counter] = {}
        restored_b = 0
        for r in rows:
            band = bands.setdefault(band_of(bucket, r["dir"]), Counter())
            band[r["decision"]] += 1
            if r["decision"] == "restored":
                restored_b += r["size_bytes"]
                band["bytes"] += r["size_bytes"]
        if rows:
            rpath = f"{ppath}/restored/{bucket}-{stamp}.parquet"
            fs.makedirs(rpath.rsplit("/", 1)[0], exist_ok=True)
            pq.write_table(pa.Table.from_pylist(rows, schema=schema), rpath, filesystem=fs, row_group_size=65_536)
        summary["buckets"][bucket] = {
            "decisions": dict(counts), "restored_bytes": restored_b,
            "bands": {b: dict(c) for b, c in bands.items()},
        }
        err(f"  {bucket}: " + ", ".join(f"{n:,} {d}" for d, n in sorted(counts.items())) + f" ({restored_b / 1e12:.2f} TB restored)")
    with fsspec.open(f"{log_dir}/undo-{stamp}-summary.json", "w") as fh:
        json.dump(summary, fh, indent=2)
    return summary


def record_undo(run_id: str, summary: dict, deleted_objects: int) -> str:
    """Persist an undo to D1: `deletion_runs.undo_state` ('full' when every
    object the run deleted is live again — restored now or already — else
    'partial') and `deletion_bands.undone_objects` per band."""
    from .index_footer import _creds, _d1_query, _q

    tok, acct = _creds()
    live = sum(b["decisions"].get("restored", 0) + b["decisions"].get("already_live", 0) for b in summary["buckets"].values())
    state = "full" if deleted_objects and live >= deleted_objects else "partial"
    stmts = [f"UPDATE deletion_runs SET undo_state = {_q(state)} WHERE run_id = {_q(run_id)}"]
    for b in summary["buckets"].values():
        for prefix, c in b["bands"].items():
            if c.get("restored"):
                stmts.append(
                    f"UPDATE deletion_bands SET undone_objects = undone_objects + {int(c['restored'])} "
                    f"WHERE run_id = {_q(run_id)} AND prefix = {_q(prefix)}"
                )
    _d1_query("; ".join(stmts), acct, tok)
    return state


def run_id_for(plan: dict, started_ts: int) -> str:
    """`<date>-p<plan_id>/<stamp>`: the staged plan the run executes
    (`sweep manifest --plan` writes `plan_id` into the summary)."""
    stamp = f"{dt.datetime.fromtimestamp(started_ts, dt.timezone.utc):%Y%m%dT%H%M%SZ}"
    return f"{plan['date']}-p{int(plan['plan_id'])}/{stamp}"


# `deletion_runs.head` / `exec_head` recorded a ledger position a plan-first
# run doesn't have (it reads no ledger); both are written as 0. Likewise
# `ledger_drift_dirs`: nothing re-classifies the manifest.
def record_run_start(plan: dict, plan_dir: str, actor: str, started_ts: int, for_real: bool, buckets: tuple[str, ...] = ()) -> str:
    """Insert the run's D1 row as soon as it starts (`finished_ts` NULL, zero
    totals) so the console lists it while it runs; `record_run` fills it in.
    `buckets` = the `-b` cut (empty = every bucket in the plan → NULL)."""
    from .index_footer import _creds, _d1_query, _q

    run_id = run_id_for(plan, started_ts)
    tok, acct = _creds()
    _d1_query(
        "INSERT INTO deletion_runs (run_id, plan, scan, head, exec_head, actor, mode, started_ts, finished_ts, "
        "deleted_bytes, deleted_objects, skipped_gone, skipped_overwritten, drift_dirs, ledger_drift_dirs, "
        "undo_deadline, log_dir, buckets, plan_id) VALUES ("
        f"{_q(run_id)}, {_q(plan_dir)}, {_q(plan['date'])}, 0, 0, {_q(actor)}, "
        f"{_q('real' if for_real else 'dry')}, {started_ts}, NULL, 0, 0, 0, 0, 0, 0, NULL, {_q(plan_dir)}, "
        f"{_q(','.join(sorted(buckets))) if buckets else 'NULL'}, {int(plan['plan_id'])})",
        acct, tok,
    )
    return run_id


def record_run(
    summary: dict,
    plan: dict,
    actor: str,
    started_ts: int,
    finished_ts: int,
    soft_delete_days: int = 7,
) -> str:
    """Persist the run + per-band rows to D1 (migration 0015) — deletions as
    first-class records the site can surface per path. Returns the run_id.
    Completes the row `record_run_start` opened (or inserts it, for a run that
    skipped the start record)."""
    from .index_footer import _creds, _d1_query, _q

    mode = "real" if summary["for_real"] else "dry"
    run_id = run_id_for(plan, started_ts)
    tot = Counter()
    band_rows = []
    for bucket, b in summary["buckets"].items():
        d = b.get("decisions", {})
        tot["deleted_objects"] += d.get("delete", 0)
        tot["deleted_bytes"] += b.get("delete_bytes", 0)
        tot["skipped_gone"] += d.get("skipped_gone", 0)
        tot["skipped_overwritten"] += d.get("skipped_overwritten", 0)
        tot["drift_dirs"] += len(b.get("drift_dirs", []))
        for prefix, c in (b.get("bands") or {}).items():
            band_rows.append(
                f"({_q(run_id)}, {_q(prefix)}, {c.get('bytes', 0)}, {c.get('objects', 0)}, "
                f"{c.get('gone', 0)}, {c.get('overwritten', 0)}, {c.get('drift_new_objects', 0)}, 0)"
            )
    undo = f"{finished_ts + soft_delete_days * 86400}" if mode == "real" else "NULL"
    tok, acct = _creds()
    _d1_query(
        "INSERT INTO deletion_runs (run_id, plan, scan, head, exec_head, actor, mode, started_ts, finished_ts, "
        "deleted_bytes, deleted_objects, skipped_gone, skipped_overwritten, drift_dirs, ledger_drift_dirs, "
        "undo_deadline, log_dir, plan_id) VALUES ("
        f"{_q(run_id)}, {_q(summary['plan'])}, {_q(plan['date'])}, 0, 0, {_q(actor)}, "
        f"{_q(mode)}, {started_ts}, {finished_ts}, {tot['deleted_bytes']}, {tot['deleted_objects']}, "
        f"{tot['skipped_gone']}, {tot['skipped_overwritten']}, {tot['drift_dirs']}, 0, "
        f"{undo}, {_q(summary['plan'])}, {int(plan['plan_id'])}) "
        "ON CONFLICT (run_id) DO UPDATE SET finished_ts = excluded.finished_ts, deleted_bytes = excluded.deleted_bytes, "
        "deleted_objects = excluded.deleted_objects, skipped_gone = excluded.skipped_gone, "
        "skipped_overwritten = excluded.skipped_overwritten, drift_dirs = excluded.drift_dirs, "
        "ledger_drift_dirs = excluded.ledger_drift_dirs, undo_deadline = excluded.undo_deadline",
        acct, tok,
    )
    if band_rows:
        _d1_query(
            "INSERT INTO deletion_bands (run_id, prefix, bytes, objects, gone, overwritten, drift_new_objects, undone_objects) VALUES "
            + ", ".join(band_rows),
            acct, tok,
        )
    return run_id


# What a real run exercises beyond the listing's read access: the bucket GET
# behind the soft-delete guard, the deletes, and the restores `sweep undo`
# needs. Checked before any listing — the 2026-09-11 real run got as far as
# the guard on `objectViewer` alone.
REAL_PERMS = ("storage.buckets.get", "storage.objects.delete", "storage.objects.restore")


def _missing_perms(bkt) -> list[str]:
    """Real-run permissions the job identity lacks on `bkt` (GCS answers
    testIamPermissions with the granted subset; no permission is needed to ask)."""
    granted = set(bkt.test_iam_permissions(list(REAL_PERMS)))
    return [p for p in REAL_PERMS if p not in granted]


def _soft_delete_days(client, bucket: str) -> float:
    """The bucket's soft-delete window in days (0 = off) — what `sweep undo`
    has to work with. Read in every mode, so a dry run exercises the same
    bucket GET and parse a real run's guard does."""
    pol = client.get_bucket(bucket).soft_delete_policy
    return (pol.retention_duration_seconds or 0) / 86400 if pol else 0
