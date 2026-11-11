"""Bounded, offline joint owner/class stock eligibility census; no query index."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from hashlib import sha256
from json import dumps, loads
from pathlib import Path
from resource import RUSAGE_SELF, getrusage
import sys
from time import monotonic

from click import ClickException, IntRange, argument, command, option
from click import Path as ClickPath


CONTRACT = "joint-uc-stock-events-v1"
Cell = tuple[str, str, str]
Vector = tuple[int, int]


@dataclass(frozen=True)
class Limits:
    input_bytes: int = 64 << 20
    events: int = 100_000
    paths: int = 50_000
    scans: int = 16
    depth: int = 64
    ancestor_updates: int = 2_000_000
    cells: int = 250_000
    value_versions: int = 1_000_000
    seconds: int = 300
    rss_bytes: int = 8 << 30


def require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer")
    return value


def canonical(path: object, *, root: bool = False) -> str:
    if not isinstance(path, str):
        raise ValueError("path must be text")
    require((root and path == "") or all(p not in ("", ".", "..") for p in path.split("/")), "path must have canonical nonempty slash-separated segments")
    return path


@dataclass(frozen=True)
class Version:
    path: str
    owner: str
    cls: str
    start: int
    end: int | None
    size: int
    objects: int


@dataclass
class Census:
    report: dict
    values: list[Version]
    positive: list[Version]
    resources: dict


def census(
    manifest_path: Path,
    events_path: Path,
    limits: Limits = Limits(),
) -> Census:
    """Read one admitted capsule, fold each scan atomically, then count structures.

    `positive` uses Version only for its identity/validity; size/objects are zero
    because eligibility intervals carry no magnitudes. No history is sampled.
    """
    started = monotonic()
    cpu_start = getrusage(RUSAGE_SELF)

    def rss_bytes() -> int:
        # macOS reports bytes, Linux KiB. This is process lifetime high water.
        return int(getrusage(RUSAGE_SELF).ru_maxrss) * (1 if sys.platform == "darwin" else 1024)

    def budget() -> None:
        require(monotonic() - started <= limits.seconds, "deadline exceeded")
        require(rss_bytes() <= limits.rss_bytes, "process peak RSS exceeds admission")

    require(all(type(v) is int and v > 0 for v in asdict(limits).values()), "limits must be positive integers")
    with manifest_path.open("rb") as stream:
        raw_manifest = stream.read(65_537)
    require(len(raw_manifest) <= 65_536, "manifest exceeds 65536 bytes")
    manifest = loads(raw_manifest)
    require(type(manifest) is dict and set(manifest) == {"contract", "complete", "initial_state", "source", "scope", "scans", "events", "paths", "sha256"}, "invalid manifest fields")
    require(manifest["contract"] == CONTRACT and manifest["complete"] is True and manifest["initial_state"] == "empty-before-first-scan", "complete stock-event capsule required")
    require(isinstance(manifest["source"], str) and bool(manifest["source"]), "source identity required")
    scope = canonical(manifest["scope"], root=True)
    scans = manifest["scans"]
    require(type(scans) is list and 0 < len(scans) <= limits.scans, "scan count exceeds admission")
    require(all(type(s) is int and -(1 << 63) <= s < (1 << 63) for s in scans) and scans == sorted(set(scans)), "scans must be strictly increasing Int64 integers")
    expected_events = integer(manifest["events"], "events")
    expected_paths = integer(manifest["paths"], "paths")
    require(0 <= expected_events <= limits.events and 0 <= expected_paths <= limits.paths, "declared input exceeds admission")
    digest = manifest["sha256"]
    require(isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest), "invalid sha256")
    require(events_path.stat().st_size <= limits.input_bytes, "input bytes exceed admission")

    # own states are retained only while live. A path can occupy one joint cell.
    own: dict[str, dict[tuple[str, str], Vector]] = {}
    state: dict[Cell, Vector] = {}
    history: dict[Cell, list[tuple[int, int, int]]] = {}
    paths: set[str] = set()
    nodes: set[str] = {scope}
    pending: dict[Cell, Vector] = {}
    stamp: int | None = None
    event_count = input_bytes = ancestor_updates = value_changes = 0
    own_cell_changes = own_path_changes = 0
    hasher = sha256()
    scope_depth = len(scope.split("/")) if scope else 0

    def ancestors(path: str) -> list[str]:
        parts = path.split("/")
        return ["/".join(parts[:d]) for d in range(scope_depth, len(parts) + 1)]

    def flush(t: int) -> None:
        nonlocal value_changes, own_cell_changes, own_path_changes
        rolled: dict[Cell, Vector] = defaultdict(lambda: (0, 0))
        touched: set[str] = set()
        changed_paths: set[str] = set()
        for (path, owner, cls), (db, dn) in pending.items():
            budget()
            by_tuple = own.setdefault(path, {})
            b, n = by_tuple.get((owner, cls), (0, 0))
            old_own = b, n
            b, n = b + db, n + dn
            require(n in (0, 1) and 0 <= b <= (1 << 63) - 1 and (n != 0 or b == 0), f"invalid own stock at {t}: {path}")
            if n:
                by_tuple[owner, cls] = b, n
            else:
                by_tuple.pop((owner, cls), None)
            touched.add(path)
            if (b, n) != old_own:
                own_cell_changes += 1
                changed_paths.add(path)
            for node in ancestors(path):
                key = node, owner, cls
                require(len(rolled) < limits.cells or key in rolled, "joint cells exceed admission")
                rb, rn = rolled[key]
                rolled[key] = rb + db, rn + dn
        for path in touched:
            require(sum(n for _, n in own[path].values()) <= 1, f"multiple live joint cells at {t}: {path}")
            if not own[path]:
                del own[path]
        own_path_changes += len(changed_paths)
        for key, (db, dn) in rolled.items():
            budget()
            old = state.get(key, (0, 0))
            new = old[0] + db, old[1] + dn
            require(0 <= new[0] <= (1 << 63) - 1 and 0 <= new[1] <= (1 << 63) - 1 and (new[1] != 0 or new[0] == 0), "invalid recursive stock")
            if new == old:
                continue
            require(len(history) < limits.cells or key in history, "joint cells exceed admission")
            value_changes += 1
            require(value_changes <= limits.value_versions, "value versions exceed admission")
            history.setdefault(key, []).append((t, *new))
            state[key] = new
        pending.clear()

    with events_path.open("rb") as stream:
        while raw := stream.readline(min(limits.input_bytes - input_bytes + 1, 1 << 20)):
            budget()
            input_bytes += len(raw)
            require(input_bytes <= limits.input_bytes, "input bytes exceed admission")
            require(raw.endswith(b"\n"), "events require newline-terminated rows of at most 1 MiB")
            hasher.update(raw)
            event_count += 1
            require(event_count <= expected_events, "event count exceeds declared input")
            row = loads(raw)
            require(type(row) is dict and set(row) == {"scan", "path", "owner", "class", "bytes_delta", "objects_delta"}, "invalid event fields")
            t = integer(row["scan"], "scan")
            require(t in scans and (stamp is None or t >= stamp), "events must be ordered at declared scans")
            if stamp is not None and t != stamp:
                flush(stamp)
            stamp = t
            path = canonical(row["path"])
            require(not scope or path == scope or path.startswith(scope + "/"), "event outside complete scope")
            require(len(path.split("/")) <= limits.depth, "path depth exceeds admission")
            require(isinstance(row["owner"], str) and isinstance(row["class"], str) and bool(row["class"]), "owner/class must be text; class must be nonempty")
            db = integer(row["bytes_delta"], "bytes_delta")
            dn = integer(row["objects_delta"], "objects_delta")
            require(-(1 << 63) <= db < (1 << 63) and dn in (-1, 0, 1), "invalid own delta")
            paths.add(path)
            require(len(paths) <= expected_paths, "path count exceeds declared input")
            chain = ancestors(path)
            ancestor_updates += len(chain)
            require(ancestor_updates <= limits.ancestor_updates, "ancestor updates exceed admission")
            nodes.update(chain)
            require(len(nodes) <= limits.cells, "tree nodes exceed admission")
            key = path, row["owner"], row["class"]
            b, n = pending.get(key, (0, 0))
            pending[key] = b + db, n + dn
    require(event_count == expected_events and len(paths) == expected_paths, "input counts disagree with manifest")
    require(hasher.hexdigest() == digest, "input sha256 disagrees with manifest")
    if stamp is not None:
        flush(stamp)

    values: list[Version] = []
    positive: list[Version] = []
    for (path, owner, cls), changes in sorted(history.items()):
        budget()
        opened = None
        for i, (t, b, n) in enumerate(changes):
            end = changes[i + 1][0] if i + 1 < len(changes) else None
            if n:
                values.append(Version(path, owner, cls, t, end, b, n))
                if opened is None:
                    opened = t
            elif opened is not None:
                positive.append(Version(path, owner, cls, opened, t, 0, 0))
                opened = None
        if opened is not None:
            positive.append(Version(path, owner, cls, opened, None, 0, 0))

    def points(rows: list[Version]) -> dict:
        searchable = [r for r in rows if r.path]
        chars = sum(len(r.path.rsplit("/", 1)[-1].lower()) for r in searchable)
        return {"rows": len(rows), "searchable_rows": len(searchable), "exact_or_prefix_points": len(searchable), "substring_character_join_points": chars}

    vp, ip = points(values), points(positive)
    labels = {node.rsplit("/", 1)[-1].lower() for node in nodes if node}
    tuples = {(r.owner, r.cls) for r in values}
    # Explicit elementary payloads only: UInt64 node/tuple IDs + Int64 bounds,
    # values add Int64 byte/count channels; text/topology and access indexes extra.
    packed = {
        "assumption": "fixed 64-bit node/tuple IDs and signed validity/value channels; payload terms only, not an encoded index or range-tree estimate",
        "positive_value_payload_bytes": len(values) * 48,
        "all_value_payload_bytes": value_changes * 48,
        "positive_interval_payload_bytes": len(positive) * 32,
        "exact_or_prefix_point_payload_bytes": ip["exact_or_prefix_points"] * 40,
        "substring_point_payload_bytes": ip["substring_character_join_points"] * 40,
        "shared_label_utf8_bytes": sum(len(s.encode("utf-8")) for s in labels),
        "shared_label_offset_bytes": (len(labels) + 1) * 8,
        "excluded": ["tree topology and path/tuple dictionaries", "coordinate directories and access indexes", "range-tree replication", "allocator/build memory", "compression and file metadata"],
    }
    final = [v for k, v in state.items() if k[0] == scope]
    report = {
        "contract": CONTRACT,
        "source": manifest["source"],
        "scope": scope,
        "scans": scans,
        "manifest_sha256": sha256(raw_manifest).hexdigest(),
        "events_sha256": digest,
        "coverage": "producer-asserted complete scope; verified bytes/counts and stock consistency, not independent source completeness",
        "limits": asdict(limits),
        "input": {"bytes": input_bytes, "events": event_count, "own_paths": len(paths)},
        "structures": {"nodes": len(nodes), "joint_uc_tuples": len(tuples), "recursive_cells": len(history), "all_value_versions_including_zero": value_changes, "event_ancestor_updates": ancestor_updates, "atomic_own_cell_changes": own_cell_changes, "atomic_own_path_changes": own_path_changes, "positive_values": vp, "positive_intervals": ip},
        "final_scope": {"bytes": sum(b for b, _ in final), "objects": sum(n for _, n in final)},
        "packed_terms": packed,
        "unsupported": ["physical or membership churn", "live ownership/deletion masks", "object-version selection", "Boolean query plans", "successor queries and index performance"],
    }
    budget()
    usage = getrusage(RUSAGE_SELF)
    resources = {
        "wall_seconds": monotonic() - started,
        "process_user_seconds": usage.ru_utime - cpu_start.ru_utime,
        "process_system_seconds": usage.ru_stime - cpu_start.ru_stime,
        "process_lifetime_peak_rss_bytes": rss_bytes(),
        "threads": 1,
        "work_disk_bytes_written": 0,
        "note": "RSS includes interpreter/imports and earlier process work; cooperative guards, not OS isolation",
    }
    return Census(report, values, positive, resources)


@command("eligibility-census")
@option("-b", "--input-bytes", type=IntRange(min=1), default=64 << 20, show_default=True)
@option("-c", "--cells", type=IntRange(min=1), default=250_000, show_default=True)
@option("-d", "--depth", type=IntRange(min=1), default=64, show_default=True)
@option("-e", "--events", type=IntRange(min=1), default=100_000, show_default=True)
@option("-m", "--rss-bytes", type=IntRange(min=1), default=8 << 30, show_default=True)
@option("-p", "--paths", type=IntRange(min=1), default=50_000, show_default=True)
@option("-s", "--scans", type=IntRange(min=1), default=16, show_default=True)
@option("-t", "--seconds", type=IntRange(min=1), default=300, show_default=True)
@option("-u", "--ancestor-updates", type=IntRange(min=1), default=2_000_000, show_default=True)
@option("-v", "--value-versions", type=IntRange(min=1), default=1_000_000, show_default=True)
@argument("manifest", type=ClickPath(path_type=Path, exists=True, dir_okay=False))
@argument("input_events", type=ClickPath(path_type=Path, exists=True, dir_okay=False))
def eligibility_census_cmd(
    input_bytes: int,
    cells: int,
    depth: int,
    events: int,
    rss_bytes: int,
    paths: int,
    scans: int,
    seconds: int,
    ancestor_updates: int,
    value_versions: int,
    manifest: Path,
    input_events: Path,
) -> None:
    """Count joint U/C recursive stock eligibility from one complete pinned capsule.

    No downloads or writes. See cloud/docs/eligibility-census.md for the schema.
    Budget/admission failures print no successful report.
    """
    try:
        result = census(manifest, input_events, Limits(input_bytes, events, paths, scans, depth, ancestor_updates, cells, value_versions, seconds, rss_bytes))
    except (ValueError, OSError) as error:
        raise ClickException(str(error)) from error
    print(dumps(result.report, sort_keys=True, indent=2))
    print("eligibility-census resources: " + dumps(result.resources, sort_keys=True), file=sys.stderr)
