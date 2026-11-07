"""Attribution rows from mined W&B run metadata (``dt-cloud wandb-attr``).

Two sub-signals, both consuming the ``wandb-mine`` parquet:

1. **Run-name join**: ``checkpoints/<dir>`` and ``grug/<dir>`` names match W&B
   run names (exactly, or after stripping ``-<hex>`` artifact-hash and
   ``_resume<N>`` suffixes, or by long prefix-stem overlap). The matched run's
   user owns the dir.
2. **Writer-path configs**: ``gs://`` paths found under writer-ish config keys
   (``output*``, ``save*``, ``run_dir`` …) are owned by the run's user.
   Reader keys (``checkpoint_path`` etc.) are deliberately ignored — eval
   runs reference other people's artifacts.

Dirs matched by runs from multiple distinct users are dropped (ambiguous)
rather than guessed.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
from collections import defaultdict
from functools import partial

import pandas as pd

from .identity import IdentityMap
from .signals import AttributionRow

err = partial(print, file=sys.stderr)

_HASH_RE = re.compile(r"-[0-9a-f]{6,8}$")
_RESUME_RE = re.compile(r"[_-]resume\d+.*$")
_WRITER_KEY_RE = re.compile(r"(^|_)(output|save|run_dir|artifact_dir|out_dir|store|base_path)", re.IGNORECASE)
_MIN_STEM_OVERLAP = 16


def _stem(name: str) -> str:
    s = _HASH_RE.sub("", name)
    s = _RESUME_RE.sub("", s)
    return s.rstrip("_-")


class RunNameIndex:
    """Run-name → users lookup with hash/resume-suffix and prefix-stem fallbacks."""

    def __init__(self, runs: pd.DataFrame):
        self.name_users: dict[str, set[str]] = defaultdict(set)
        for name, user in zip(runs["name"], runs["user"]):
            if isinstance(name, str) and name and isinstance(user, str) and user:
                self.name_users[name].add(user)
        self.by12: dict[str, list[str]] = defaultdict(list)
        for name in self.name_users:
            self.by12[name[:12]].append(name)

    def match(self, dirname: str) -> set[str] | None:
        for cand in (dirname, _HASH_RE.sub("", dirname), _stem(dirname)):
            users = self.name_users.get(cand)
            if users:
                return users
        s = _stem(dirname)
        best: str | None = None
        for name in self.by12.get(s[:12], ()):
            ns = _stem(name)
            if (s.startswith(ns) or ns.startswith(s)) and min(len(s), len(ns)) >= _MIN_STEM_OVERLAP:
                if best is None or len(_stem(name)) > len(_stem(best)):
                    best = name
        return self.name_users[best] if best else None


def run_name_rows(
    runs: pd.DataFrame,
    run_dirs: pd.DataFrame,  # columns: bucket, parent, leaf (dirs under checkpoints/ and grug/)
    identities: IdentityMap,
    asof: dt.date,
) -> list[AttributionRow]:
    index = RunNameIndex(runs)
    rows: list[AttributionRow] = []
    ambiguous = 0
    for bucket, parent, leaf in zip(run_dirs["bucket"], run_dirs["parent"], run_dirs["leaf"]):
        users = index.match(leaf)
        if not users:
            continue
        if len(users) > 1:
            ambiguous += 1
            continue
        raw = next(iter(users))
        user = identities.resolve(raw)
        rows.append(
            AttributionRow(
                prefix=f"gs://{bucket}/{parent}/{leaf}/",
                user=user,
                source="wandb-run",
                evidence=f"run_name~{leaf}",
                asof=asof,
            )
        )
    if ambiguous:
        err(f"run-name join: {ambiguous} dirs matched runs from >1 user; skipped")
    return rows


def executor_rows(
    runs: pd.DataFrame,
    executor_df: pd.DataFrame,  # executor-mine output: path, name, output_path
    identities: IdentityMap,
    asof: dt.date,
) -> list[AttributionRow]:
    """Attribute legacy `.executor_info` dirs whose step name matches a W&B run.

    The prefix attributed is the sidecar's own directory (the executor output
    dir); ``output_path`` is preferred when present and consistent.
    """
    index = RunNameIndex(runs)
    rows: list[AttributionRow] = []
    ambiguous = 0
    seen_prefixes: set[str] = set()
    for path, name, output_path in zip(
        executor_df["path"], executor_df["name"], executor_df["output_path"]
    ):
        candidates = []
        if isinstance(name, str) and name:
            candidates.append(name.rsplit("/", 1)[-1])
        base_dir = path.rsplit("/", 1)[0]
        candidates.append(base_dir.rsplit("/", 1)[-1])
        users = None
        for cand in candidates:
            users = index.match(cand)
            if users:
                break
        if not users:
            continue
        if len(users) > 1:
            ambiguous += 1
            continue
        prefix = (output_path.rstrip("/") if isinstance(output_path, str) and output_path.startswith("gs://") else base_dir) + "/"
        if prefix in seen_prefixes:
            continue
        seen_prefixes.add(prefix)
        raw = next(iter(users))
        user = identities.resolve(raw)
        rows.append(
            AttributionRow(
                prefix=prefix,
                user=user,
                source="executor-wandb",
                evidence=f"executor_name~{candidates[0]}",
                asof=asof,
            )
        )
    if ambiguous:
        err(f"executor join: {ambiguous} sidecars matched runs from >1 user; skipped")
    return rows


def writer_path_rows(
    runs: pd.DataFrame,
    identities: IdentityMap,
    asof: dt.date,
) -> list[AttributionRow]:
    owner_paths: dict[str, set[str]] = defaultdict(set)  # prefix -> raw users
    evidence: dict[str, str] = {}
    for user, gs_json, run_id in zip(runs["user"], runs["gs_paths"], runs["run_id"]):
        if not isinstance(gs_json, str) or not isinstance(user, str) or not user:
            continue
        for key, path in json.loads(gs_json):
            if not _WRITER_KEY_RE.search(key or ""):
                continue
            prefix = path.rstrip("/") + "/"
            owner_paths[prefix].add(user)
            evidence.setdefault(prefix, f"config.{key}@{run_id}")
    rows = []
    ambiguous = 0
    for prefix, users in owner_paths.items():
        if len(users) > 1:
            ambiguous += 1
            continue
        raw = next(iter(users))
        user = identities.resolve(raw)
        rows.append(
            AttributionRow(
                prefix=prefix,
                user=user,
                source="wandb-config",
                evidence=evidence[prefix],
                asof=asof,
            )
        )
    if ambiguous:
        err(f"writer-path signal: {ambiguous} prefixes written by >1 user; skipped")
    return rows
