from dt_cloud import api_names


def test_node_keeps_a_descriptive_node():
    n = {"name": "b1", "kind": "dir", "bytes": 5, "objects": 2, "children": [{"name": "x", "kind": "file", "bytes": 5, "objects": 1}]}
    assert api_names.node(n) is n


def test_node_renames_a_short_node_and_its_children():
    short = {
        "n": "all buckets", "k": "dir", "b": 30, "o": 4, "d": 20000, "t": 1728000000, "a": 19990,
        "us": [["ann", 20]], "cb": {"2": 10}, "pv": ["rule", None, "b1"],
        "c": [
            {"n": "b1", "k": "dir", "b": 20, "o": 3, "m": 1, "src": "gcs", "at": "2026-11-12"},
            {"n": "(other)", "k": "dir", "b": 10, "o": 1, "f": 7},
        ],
    }
    assert api_names.node(short) == {
        "name": "all buckets", "kind": "dir", "bytes": 30, "objects": 4, "mtime": 1728000000, "last_read": 19990,
        "owners": [["ann", 20]], "class_bytes": {"2": 10}, "provenance": ["rule", None, "b1"],
        "children": [
            {"name": "b1", "kind": "dir", "bytes": 20, "objects": 3, "matched": True, "store": "gcs", "scan": "2026-11-12"},
            {"name": "(other)", "kind": "dir", "bytes": 10, "objects": 1, "folded": 7},
        ],
    }


def test_tree_reads_a_body():
    assert api_names.tree({"tree": {"n": "r", "b": 1, "o": 1}}) == {"name": "r", "bytes": 1, "objects": 1}


def test_diff_keeps_a_descriptive_body():
    d = {"bytes_before": 1, "bytes_after": 2, "objects_before": 1, "objects_after": 1, "rows": [{"path": "m", "bytes_before": 1}]}
    assert api_names.diff(d) is d


def test_diff_renames_a_short_body_and_its_rows():
    short = {
        "prev": "A", "curr": "B", "total_a": 10, "total_b": 30, "objects_a": 1, "objects_b": 3, "tier": "descent",
        "rows": [
            {"p": "m", "d": 1, "k": "dir", "s": "changed", "a": 10, "b": 30, "oa": 1, "ob": 3, "x": True, "src": "gcs"},
            {"p": "m/x", "d": 2, "k": "file", "s": "added", "a": 0, "b": 20, "oa": 0, "ob": 2, "l": 2},
        ],
    }
    assert api_names.diff(short) == {
        "prev": "A", "curr": "B", "bytes_before": 10, "bytes_after": 30, "objects_before": 1, "objects_after": 3, "tier": "descent",
        "rows": [
            {"path": "m", "depth": 1, "kind": "dir", "status": "changed", "bytes_before": 10, "bytes_after": 30, "objects_before": 1,
             "objects_after": 3, "expanded": True, "store": "gcs"},
            {"path": "m/x", "depth": 2, "kind": "file", "status": "added", "bytes_before": 0, "bytes_after": 20, "objects_before": 0,
             "objects_after": 2, "lookup": 2},
        ],
    }
