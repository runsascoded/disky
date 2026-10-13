"""The staged set as the executor's delete set (specs/staged-delete.md;
sweep-plan-union checkpoint 3).

A dispatch snapshots a plan's items into ``plan.json`` in the run dir; ``sweep
manifest --plan`` reads it here. Items are the canonical
``gs://<bucket>/<path>/`` prefixes the plans store keeps, and a gcs plan may
span buckets — so the plan is read into per-bucket relative prefix sets, and
the manifest's bucket set is derived from it. Under the opt-in model the plan
is the whole intent: nothing carves out.

plan.json::

    {
      "plan_id": 12,
      "name": "Staged",
      "sweep": ["gs://marin-us-east1/checkpoints/old/", "gs://marin-eu-west4/tmp/x/"],
      "objects": ["gs://marin-us-east1/clips/a.mp3"],                # optional: exact keys
      "buckets": ["marin-eu-west4", "marin-us-east1"],          # optional, derived here anyway
      "as_of": {"gs://marin-us-east1/checkpoints/old/": "2026-10-06"}  # optional
    }

``as_of`` maps an item to the scan it was staged against (``plan_items.as_of``).
The manifest deletes an object under such an item only if it is in both that
scan and the dispatch scan with the same identity (``sweep_manifest.AsOfHold``);
an item without one (staged before ``as_of`` existed) is as of the dispatch
scan.

``objects`` are exact items (specs/file-assign.md): each names one key and
matches only ``name == key`` — never ``key.bak`` or ``key/…``. The kind comes
from which list an item is in, never from its trailing slash; an exact key that
ends in ``/`` is refused (so the two kinds' strings never collide either).
``sweep`` may be empty when ``objects`` is not.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .scan_id import is_scan_id

# `gs://<bucket>/<path>`: the bucket per GCS naming (lowercase, digits, `-`,
# `_`, `.`), then a non-empty path — the bucket root is not a plan item.
CANONICAL_RE = re.compile(r"^gs://([a-z0-9][a-z0-9._-]*)/(.+)$")
SEGMENT_RE = re.compile(r"^[^/\\]+$")

class PlanError(Exception):
    """A malformed plan.json — the dispatch's snapshot is wrong, not the data."""


def split_prefix(raw: str) -> tuple[str, str]:
    """``gs://b/a/c`` or ``gs://b/a/c/`` -> ``("b", "a/c/")``: the bucket and the
    relative prefix (trailing slash). Raises ``PlanError`` for anything that is
    not a canonical, non-root, ``.``/``..``-free ``gs://`` prefix."""
    m = CANONICAL_RE.match(raw.strip())
    if not m:
        raise PlanError(f"bad plan prefix {raw!r} (want gs://<bucket>/<path>/)")
    bucket, path = m.groups()
    rel = path.rstrip("/") + "/"
    segments = rel[:-1].split("/")
    if any(s in (".", "..") or not SEGMENT_RE.match(s) for s in segments):
        raise PlanError(f"bad plan prefix {raw!r} (empty, '.', '..' or backslash segment)")
    return bucket, rel


def split_object(raw: str) -> tuple[str, str]:
    """``gs://b/a/c.mp3`` -> ``("b", "a/c.mp3")``: an exact item's bucket and
    relative key. Raises ``PlanError`` for a non-canonical URI, a key ending in
    ``/`` (a folder placeholder is not an exact item), or an empty / ``.`` /
    ``..`` / backslash segment."""
    m = CANONICAL_RE.match(raw.strip())
    if not m:
        raise PlanError(f"bad plan object {raw!r} (want gs://<bucket>/<key>)")
    bucket, key = m.groups()
    if key.endswith("/"):
        raise PlanError(f"bad plan object {raw!r} (an exact key must not end in '/')")
    if any(s in (".", "..") or not SEGMENT_RE.match(s) for s in key.split("/")):
        raise PlanError(f"bad plan object {raw!r} (empty, '.', '..' or backslash segment)")
    return bucket, key


def _by_bucket(items: list[str], split=split_prefix) -> dict[str, tuple[str, ...]]:
    out: dict[str, set[str]] = {}
    for p in items:
        bucket, rel = split(p)
        out.setdefault(bucket, set()).add(rel)
    return {b: tuple(sorted(rels)) for b, rels in sorted(out.items())}


#: Why a directory's keys are (or aren't) in the manifest.
CATEGORIES = (
    "eligible",             # under a staged prefix → delete
    "outside_bands",        # not under any staged prefix — never classified
    "skipped_after_as_of",  # under a staged prefix, but not in its `as_of` scan unchanged → kept
)


@dataclass(frozen=True)
class StagedPlan:
    """A plan's items grouped by bucket: ``sweep[bucket]`` are sorted relative
    prefixes (trailing slash), ``objects[bucket]`` sorted relative exact keys."""

    plan_id: int
    name: str
    sweep: dict[str, tuple[str, ...]]
    #: ``as_of[bucket][rel]``: the scan an item (prefix or exact key) was staged against (absent = the dispatch scan).
    as_of: dict[str, dict[str, str]] = field(default_factory=dict)
    objects: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def buckets(self) -> tuple[str, ...]:
        """Every bucket an item (prefix or exact) names, sorted — the manifest's bucket set."""
        return tuple(sorted(set(self.sweep) | set(self.objects)))

    def exact(self, bucket: str) -> tuple[str, ...]:
        """The bucket's exact items in canonical form (``approved_objects``)."""
        return tuple(f"gs://{bucket}/{key}" for key in self.objects.get(bucket, ()))

    def bands(self, bucket: str) -> tuple[str, ...]:
        """The bucket's sweep items back in canonical form — what the executor
        takes as the run's bands (its listing roots + per-band accounting)."""
        return tuple(f"gs://{bucket}/{rel}" for rel in self.sweep.get(bucket, ()))

    def classify(self, bucket: str, dirname: str) -> str:
        """One directory (``''`` = bucket root, else ``a/b``) under the plan:
        ``eligible`` when a staged prefix covers it, else ``outside_bands``."""
        key = f"{dirname}/" if dirname else ""
        return "eligible" if any(key.startswith(p) for p in self.sweep.get(bucket, ())) else "outside_bands"


def parse_plan(d: object) -> StagedPlan:
    """A plan.json object -> ``StagedPlan``; ``PlanError`` when malformed
    (no/invalid ``plan_id``, empty or non-list ``sweep``, a bad prefix)."""
    if not isinstance(d, dict):
        raise PlanError("plan.json must be a JSON object")
    plan_id = d.get("plan_id")
    if not isinstance(plan_id, int) or isinstance(plan_id, bool):
        raise PlanError(f"plan_id must be an integer, got {plan_id!r}")
    sweep = d.get("sweep", [])
    objects = d.get("objects", [])
    if not isinstance(sweep, list) or not all(isinstance(p, str) for p in sweep):
        raise PlanError("sweep must be a list of gs:// prefixes")
    if not isinstance(objects, list) or not all(isinstance(p, str) for p in objects):
        raise PlanError("objects must be a list of gs:// keys")
    if not sweep and not objects:
        raise PlanError("plan has no items (sweep and objects both empty)")
    name = d.get("name", f"plan {plan_id}")
    if not isinstance(name, str):
        raise PlanError(f"name must be a string, got {name!r}")
    raw_as_of = d.get("as_of", {})
    if not isinstance(raw_as_of, dict):
        raise PlanError("as_of must be an object of prefix -> scan date")
    as_of: dict[str, dict[str, str]] = {}
    prefixes, exact = set(sweep), set(objects)
    for item, date in raw_as_of.items():
        if item not in prefixes and item not in exact:
            raise PlanError(f"as_of names {item!r}, which is not a plan item")
        if not isinstance(date, str) or not is_scan_id(date):
            raise PlanError(f"as_of[{item!r}] must be a scan date, got {date!r}")
        bucket, rel = split_prefix(item) if item in prefixes else split_object(item)
        as_of.setdefault(bucket, {})[rel] = date
    return StagedPlan(plan_id=plan_id, name=name, sweep=_by_bucket(sweep), as_of=as_of, objects=_by_bucket(objects, split_object))


def load_plan(path: str) -> StagedPlan:
    """Read a plan.json (local path or fsspec URL, e.g. the run dir's
    ``gs://…/plan.json``)."""
    import fsspec

    with fsspec.open(path, "r") as fh:
        return parse_plan(json.load(fh))
