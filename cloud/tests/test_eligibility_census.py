"""Exact fixed-facet stock counts, atomic replacements and complete admission."""

from dataclasses import replace
from hashlib import sha256
from json import dumps, loads
from pathlib import Path

import pytest
from click.testing import CliRunner

from dt_cloud.eligibility_census import CONTRACT, Limits, Version, census, eligibility_census_cmd


def event(
    scan,
    path,
    size,
    count,
    owner="a",
    cls="1",
):
    return {"scan": scan, "path": path, "owner": owner, "class": cls, "bytes_delta": size, "objects_delta": count}


def capsule(tmp_path, rows, *, scope="b", scans=(0, 1, 2, 3), **overrides):
    raw = "".join(dumps(e, sort_keys=True) + "\n" for e in rows).encode()
    events_path = tmp_path / "events.jsonl"
    events_path.write_bytes(raw)
    manifest = {"contract": CONTRACT, "complete": True, "initial_state": "empty-before-first-scan", "source": "test:immutable-v1", "scope": scope, "scans": list(scans), "events": len(rows), "paths": len({e["path"] for e in rows}), "sha256": sha256(raw).hexdigest(), **overrides}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(dumps(manifest) + "\n")
    return manifest_path, events_path


def fixture_events():
    return [
        event(0, "b/Hit/zero", 0, 1),
        event(0, "b/Hit/Hit/x", 5, 1),
        event(1, "b/Hit/Hit/x", -5, -1),
        event(1, "b/Hit/Hit/x", 12, 1),
        event(2, "b/Hit/Hit/x", -12, -1),
        event(2, "b/Hit/Hit/x", 12, 1, "minor", "2"),
        event(3, "b/Hit/Hit/x", -12, -1, "minor", "2"),
    ]


def test_literal_values_and_positive_intervals(tmp_path):
    result = census(*capsule(tmp_path, fixture_events()))
    # Recursive values are not summed as independent contributions.
    assert [v for v in result.values if v.path == "b"] == [
        Version("b", "a", "1", 0, 1, 5, 2),
        Version("b", "a", "1", 1, 2, 12, 2),
        Version("b", "a", "1", 2, None, 0, 1),
        Version("b", "minor", "2", 2, 3, 12, 1),
    ]
    assert result.positive == [
        Version("b", "a", "1", 0, None, 0, 0),
        Version("b", "minor", "2", 2, 3, 0, 0),
        Version("b/Hit", "a", "1", 0, None, 0, 0),
        Version("b/Hit", "minor", "2", 2, 3, 0, 0),
        Version("b/Hit/Hit", "a", "1", 0, 2, 0, 0),
        Version("b/Hit/Hit", "minor", "2", 2, 3, 0, 0),
        Version("b/Hit/Hit/x", "a", "1", 0, 2, 0, 0),
        Version("b/Hit/Hit/x", "minor", "2", 2, 3, 0, 0),
        Version("b/Hit/zero", "a", "1", 0, None, 0, 0),
    ]
    assert result.report["structures"] == {
        "nodes": 5, "joint_uc_tuples": 2, "recursive_cells": 9,
        "all_value_versions_including_zero": 21, "event_ancestor_updates": 27,
        "atomic_own_cell_changes": 6, "atomic_own_path_changes": 5,
        "positive_values": {"rows": 15, "searchable_rows": 15, "exact_or_prefix_points": 15, "substring_character_join_points": 32},
        "positive_intervals": {"rows": 9, "searchable_rows": 9, "exact_or_prefix_points": 9, "substring_character_join_points": 20},
    }
    assert result.report["final_scope"] == {"bytes": 0, "objects": 1}
    assert {k: v for k, v in result.report["packed_terms"].items() if k.endswith("bytes")} == {
        "positive_value_payload_bytes": 720,
        "all_value_payload_bytes": 1008,
        "positive_interval_payload_bytes": 288,
        "exact_or_prefix_point_payload_bytes": 360,
        "substring_point_payload_bytes": 800,
        "shared_label_utf8_bytes": 9,
        "shared_label_offset_bytes": 40,
    }


def test_all_scans_against_independent_own_object_oracle(tmp_path):
    events = fixture_events()
    result = census(*capsule(tmp_path, events))
    # Rebuild complete snapshots directly, independently of recursive delta folds.
    own = {}
    for scan in range(4):
        for e in events:
            if e["scan"] == scan:
                key = e["path"], e["owner"], e["class"]
                b, n = own.get(key, (0, 0))
                own[key] = b + e["bytes_delta"], n + e["objects_delta"]
        for node in ("b", "b/Hit", "b/Hit/Hit", "b/Hit/Hit/x", "b/Hit/zero"):
            for owner, cls in (("a", "1"), ("minor", "2")):
                direct = [(b, n) for (p, u, c), (b, n) in own.items() if (u, c) == (owner, cls) and (p == node or p.startswith(node + "/"))]
                expected = sum(b for b, _ in direct), sum(n for _, n in direct)
                active = [v for v in result.values if (v.path, v.owner, v.cls) == (node, owner, cls) and v.start <= scan and (v.end is None or scan < v.end)]
                assert [(v.size, v.objects) for v in active] == ([expected] if expected[1] else [])
                positive = [v for v in result.positive if (v.path, v.owner, v.cls) == (node, owner, cls) and v.start <= scan and (v.end is None or scan < v.end)]
                assert len(positive) == int(expected[1] > 0)


def test_atomic_same_stamp_order_and_object_folder_twin(tmp_path):
    events = [event(0, "b/x", 5, 1), event(0, "b/x/zero", 0, 1), event(1, "b/x", 12, 1), event(1, "b/x", -5, -1)]
    result = census(*capsule(tmp_path, events))
    assert [v for v in result.values if v.path == "b/x"] == [Version("b/x", "a", "1", 0, 1, 5, 2), Version("b/x", "a", "1", 1, None, 12, 2)]
    assert [v for v in result.positive if v.path == "b/x"] == [Version("b/x", "a", "1", 0, None, 0, 0)]


@pytest.mark.parametrize("events,message", [
    ([event(0, "b/x", 1, 0)], "invalid own stock at 0: b/x"),
    ([event(0, "b/x", 1, 1), event(0, "b/x", 1, 1, "other")], "multiple live joint cells at 0: b/x"),
    ([event(1, "b/x", 1, 1), event(0, "b/x", -1, -1)], "events must be ordered at declared scans"),
    ([event(4, "b/x", 1, 1)], "events must be ordered at declared scans"),
    ([event(0, "bad/x", 1, 1)], "event outside complete scope"),
    ([event(0, "b//x", 1, 1)], "path must have canonical nonempty slash-separated segments"),
])
def test_invalid_capsule_refused(tmp_path, events, message):
    with pytest.raises(ValueError) as exc:
        census(*capsule(tmp_path, events))
    assert str(exc.value) == message


@pytest.mark.parametrize("overrides,message", [
    ({"complete": False}, "complete stock-event capsule required"),
    ({"sha256": "0" * 64}, "input sha256 disagrees with manifest"),
    ({"events": 2}, "input counts disagree with manifest"),
    ({"paths": 2}, "input counts disagree with manifest"),
    ({"scans": [0, 0]}, "scans must be strictly increasing Int64 integers"),
])
def test_manifest_refusals(tmp_path, overrides, message):
    with pytest.raises(ValueError) as exc:
        census(*capsule(tmp_path, [event(0, "b/x", 1, 1)], **overrides))
    assert str(exc.value) == message


@pytest.mark.parametrize("limits,message", [
    (replace(Limits(), input_bytes=1), "input bytes exceed admission"),
    (replace(Limits(), scans=1), "scan count exceeds admission"),
    (replace(Limits(), ancestor_updates=1), "ancestor updates exceed admission"),
    (replace(Limits(), cells=1), "tree nodes exceed admission"),
    (replace(Limits(), depth=1), "path depth exceeds admission"),
    (replace(Limits(), value_versions=1), "value versions exceed admission"),
    (replace(Limits(), rss_bytes=1), "process peak RSS exceeds admission"),
])
def test_limits_refuse_whole_scope(tmp_path, limits, message):
    with pytest.raises(ValueError) as exc:
        census(*capsule(tmp_path, [event(0, "b/x", 1, 1)]), limits)
    assert str(exc.value) == message


def test_cli_exact_report_and_no_success_on_failure(tmp_path):
    paths = capsule(tmp_path, [event(0, "b/x", 0, 1)])
    runner = CliRunner()
    first = runner.invoke(eligibility_census_cmd, [str(p) for p in paths])
    second = runner.invoke(eligibility_census_cmd, [str(p) for p in paths])
    assert first.exit_code == second.exit_code == 0
    assert first.stdout == second.stdout == dumps(census(*paths).report, sort_keys=True, indent=2) + "\n"
    assert loads(first.stdout)["final_scope"] == {"bytes": 0, "objects": 1}
    refused = runner.invoke(eligibility_census_cmd, ["-b", "1", *map(str, paths)])
    assert (refused.exit_code, refused.output) == (1, "Error: input bytes exceed admission\n")


def test_checked_in_adversarial_capsule():
    fixture = Path(__file__).parent / "fixtures" / "eligibility-census"
    result = census(fixture / "manifest.json", fixture / "events.jsonl")
    assert result.report["structures"] == {
        "nodes": 5, "joint_uc_tuples": 2, "recursive_cells": 9,
        "all_value_versions_including_zero": 21, "event_ancestor_updates": 27,
        "atomic_own_cell_changes": 6, "atomic_own_path_changes": 5,
        "positive_values": {"rows": 15, "searchable_rows": 15, "exact_or_prefix_points": 15, "substring_character_join_points": 32},
        "positive_intervals": {"rows": 9, "searchable_rows": 9, "exact_or_prefix_points": 9, "substring_character_join_points": 20},
    }


def test_empty_complete_scope(tmp_path):
    result = census(*capsule(tmp_path, [], scope=""))
    assert result.values == result.positive == []
    assert result.report["structures"] == {
        "nodes": 1, "joint_uc_tuples": 0, "recursive_cells": 0,
        "all_value_versions_including_zero": 0, "event_ancestor_updates": 0,
        "atomic_own_cell_changes": 0, "atomic_own_path_changes": 0,
        "positive_values": {"rows": 0, "searchable_rows": 0, "exact_or_prefix_points": 0, "substring_character_join_points": 0},
        "positive_intervals": {"rows": 0, "searchable_rows": 0, "exact_or_prefix_points": 0, "substring_character_join_points": 0},
    }
    assert result.report["final_scope"] == {"bytes": 0, "objects": 0}


def test_deadline_refusal(tmp_path, monkeypatch):
    paths = capsule(tmp_path, [event(0, "b/x", 1, 1)])
    ticks = iter([0, 2])
    monkeypatch.setattr("dt_cloud.eligibility_census.monotonic", lambda: next(ticks))
    with pytest.raises(ValueError) as exc:
        census(*paths, replace(Limits(), seconds=1))
    assert str(exc.value) == "deadline exceeded"
