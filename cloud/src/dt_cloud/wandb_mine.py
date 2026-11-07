"""Mine the W&B API for run metadata used in storage attribution.

One row per run across every project of an entity: identity (username +
display name), timestamps, and any ``gs://`` paths found in the run config
(with the config key that held them, so writer-ish keys — ``output_path``,
``save_*`` — can be distinguished from reader keys like ``checkpoint_path``).

Requires ``WANDB_API_KEY`` (read from the environment by the wandb SDK) and
the ``wandb`` extra (``uv sync --extra wandb``).
"""

from __future__ import annotations

import json
import re
import sys
from functools import partial
from pathlib import Path

import pandas as pd

err = partial(print, file=sys.stderr)

_GS_RE = re.compile(r"gs://[a-zA-Z0-9._-]+/[^\s\"'`,)}\]]*")


def _gs_paths(obj, key: str = "") -> list[tuple[str, str]]:
    """(config_key, gs_path) pairs anywhere in a nested config value."""
    out: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_gs_paths(v, k))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(_gs_paths(v, key))
    elif isinstance(obj, str) and "gs://" in obj:
        out.extend((key, m) for m in _GS_RE.findall(obj))
    return out


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


# wandb's Runs paginator retains every fetched Run (config included) for the
# lifetime of the collection — a project with tens of thousands of large-config
# runs OOMs the process (observed twice on the `marin` project, wedging a 61GB
# node). Bound memory by time-bisecting any window with more runs than this:
# each window is a fresh Runs object that goes out of scope after collection.
WINDOW_MAX_RUNS = 2000


def _row_of(proj: str, run) -> dict:
    try:
        user = run.user.username if run.user else None
        display = getattr(run.user, "name", None) if run.user else None
    except Exception:
        user, display = None, None
    try:
        cfg = {k: v for k, v in run.config.items()} if run.config else {}
    except Exception:
        cfg = {}
    paths = _gs_paths(cfg)
    return {
        "project": proj,
        "run_id": run.id,
        "name": run.name,
        "user": user,
        "display_name": display,
        "state": run.state,
        "created_at": str(getattr(run, "created_at", "") or ""),
        "gs_paths": json.dumps(paths) if paths else None,
    }


def _mine_window(entity: str, proj: str, lo: str, hi: str, parts_dir, depth: int = 0, spawn=None) -> int:
    """Mine runs created in [lo, hi); bisect when the window is too big.

    Each *leaf* window gets a fresh ``wandb.Api()`` (the SDK accumulates
    tens of GB of internal state across queries on a shared Api — observed
    wedging the work node three times on the ``marin`` project) and writes
    its own part file, so restarts skip completed windows.

    ``spawn(lo, hi, depth)``, when given, receives the two halves of an
    oversized window instead of recursing — the parallel miner points it at
    the shared thread pool, so one dense project fans out across workers.
    """
    from datetime import datetime

    import wandb

    part = parts_dir / f"{_safe(proj)}__{_safe(lo)}__{_safe(hi)}.parquet"
    if part.exists():
        err(f"  window [{lo} .. {hi}): exists, skipping", flush=True)
        return 0

    filters = {"$and": [{"createdAt": {"$gte": lo}}, {"createdAt": {"$lt": hi}}]}
    api = wandb.Api(timeout=60)
    runs = api.runs(f"{entity}/{proj}", filters=filters, per_page=500)
    n = len(runs)  # count query; does not fetch run payloads
    if n == 0:
        pd.DataFrame().to_parquet(part, index=False)
        return 0
    if n > WINDOW_MAX_RUNS and depth < 20:
        t0 = datetime.fromisoformat(lo)
        t1 = datetime.fromisoformat(hi)
        mid = (t0 + (t1 - t0) / 2).isoformat(timespec="seconds")
        if lo < mid < hi:
            del runs, api
            if spawn is not None:
                spawn(lo, mid, depth + 1)
                spawn(mid, hi, depth + 1)
                return 0
            total = _mine_window(entity, proj, lo, mid, parts_dir, depth + 1)
            total += _mine_window(entity, proj, mid, hi, parts_dir, depth + 1)
            return total
        # window no longer splittable (timestamp collision); fall through and fetch
    rows = [_row_of(proj, run) for run in runs]
    pd.DataFrame(rows).to_parquet(part, index=False)
    err(f"  window [{lo} .. {hi}): {n} runs", flush=True)
    return n


ROOT_SINCE = "2019-01-01T00:00:00"
ROOT_UNTIL = "2027-01-01T00:00:00"


def window_edges(lo: str = ROOT_SINCE, hi: str = ROOT_UNTIL, depth: int = 4) -> list[str]:
    """The bisection-tree node edges at ``depth`` — valid ``--since/--until``
    values for parallel workers (part names align with any other run)."""
    from datetime import datetime

    edges = [lo, hi]
    for _ in range(depth):
        out = [edges[0]]
        for a, b in zip(edges, edges[1:]):
            t0, t1 = datetime.fromisoformat(a), datetime.fromisoformat(b)
            out += [(t0 + (t1 - t0) / 2).isoformat(timespec="seconds"), b]
        edges = out
    return edges


def mine_entity(
    entity: str,
    out_path: Path,
    project_filter: str | None = None,
    since: str = ROOT_SINCE,
    until: str = ROOT_UNTIL,
    merge: bool = True,
    jobs: int = 1,
) -> pd.DataFrame:
    """Mine every project of ``entity`` into ``out_path``.

    Incremental/resumable: work lands as parquet parts under
    ``<out_path stem>-parts/`` (per project, or per window for big projects)
    and is skipped on re-runs (an interrupt loses at most the in-flight
    windows, ≤``WINDOW_MAX_RUNS`` runs each); the final ``out_path`` is the
    deduplicated concatenation.

    Parallelism (network-bound, so threads): ``jobs`` mines that many
    projects concurrently — each leaf window already uses its own
    ``wandb.Api``, so tasks share nothing. W&B rate limits are per API key;
    a single key sustains ~8 workers comfortably (the serial miner is
    latency-bound, nowhere near quota). For multi-*machine* runs, give each
    worker a ``--since/--until`` bisection-edge range (``window_edges()``)
    and a shared parts dir; run once more with defaults to fill gaps + merge.
    """
    from concurrent.futures import ThreadPoolExecutor

    import wandb

    # The projects listing can silently truncate (observed: 15 of 205 projects,
    # which made a range worker skip the biggest project entirely). Take the
    # longest of a few attempts and warn when they disagree.
    attempts = []
    for _ in range(3):
        api = wandb.Api(timeout=60)
        attempts.append([p.name for p in api.projects(entity)])
        if len(attempts) > 1 and len(attempts[-1]) == len(attempts[-2]):
            break
    projects = max(attempts, key=len)
    if any(len(a) != len(projects) for a in attempts):
        err(f"WARNING: unstable project listing ({[len(a) for a in attempts]}); using longest", flush=True)
    if project_filter:
        projects = [p for p in projects if project_filter in p]
    err(f"{len(projects)} projects under {entity} [{since} .. {until})", flush=True)

    parts_dir = out_path.parent / (out_path.stem + "-parts")
    parts_dir.mkdir(parents=True, exist_ok=True)
    total = 0

    # Whole-project part (small projects / pre-window era) means done; window
    # parts are skipped inside _mine_window.
    todo = []
    for i, proj in enumerate(projects, 1):
        if (parts_dir / f"{_safe(proj)}.parquet").exists():
            err(f"[{i}/{len(projects)}] {proj}: exists, skipping", flush=True)
        else:
            todo.append(proj)

    if jobs > 1:
        # Work-queue over (project, window) tasks: an oversized window enqueues
        # its halves via `spawn` and returns, so no task ever waits on another
        # (deadlock-free with a bounded pool) and one dense project fans out
        # across all workers. `total` += is CPython-atomic enough for progress.
        from concurrent.futures import wait as fwait
        from threading import Lock

        futures: set = set()
        flock = Lock()
        pool = ThreadPoolExecutor(max_workers=jobs)

        def submit(proj: str, lo: str, hi: str, depth: int = 0) -> None:
            def work() -> None:
                nonlocal total
                try:
                    total += _mine_window(
                        entity, proj, lo, hi, parts_dir, depth,
                        spawn=lambda a, b, d: submit(proj, a, b, d),
                    )
                except Exception as e:
                    err(f"  {proj} [{lo} .. {hi}): FAILED {type(e).__name__}: {e}", flush=True)
            with flock:
                futures.add(pool.submit(work))

        for proj in todo:
            submit(proj, since, until)
        while True:
            with flock:
                pending = {f for f in futures if not f.done()}
            if not pending:
                break
            fwait(pending)
        pool.shutdown()
        err(f"parallel mine done: {total} new runs across {len(todo)} projects", flush=True)
    else:
        for i, proj in enumerate(todo, 1):
            try:
                n = _mine_window(entity, proj, since, until, parts_dir)
            except Exception as e:
                err(f"  {proj}: FAILED {type(e).__name__}: {e}", flush=True)
                continue
            total += n
            err(f"[{i}/{len(todo)}] {proj}: {n} runs ({total} new so far)", flush=True)

    if not merge:
        err(f"worker done ({total} runs); skipping merge", flush=True)
        return pd.DataFrame()
    parts = sorted(parts_dir.glob("*.parquet"))
    frames = [f for f in (pd.read_parquet(p) for p in parts) if len(f)]
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if len(df):
        df = df.drop_duplicates(subset=["project", "run_id"], ignore_index=True)
    df.to_parquet(out_path, index=False)
    err(f"wrote {len(df)} runs to {out_path} (from {len(parts)} part files)", flush=True)
    return df
