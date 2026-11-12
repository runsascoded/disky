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
