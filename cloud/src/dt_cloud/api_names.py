"""The site API's descriptive field names (specs/descriptive-field-names.md §3), read from a body that may still
carry the short ones.

The site and the readers here ship separately (job images, the sheet mirror), so a reader may meet either
spelling while a deployment's site and images move. Every reader takes its body through these, new names
first. Remove the short-name fallbacks (`NODE`, `ROW`, `TOTALS` and the renames) once every deployment's site serves
the descriptive names: the spec lists it as a follow-up.
"""
from __future__ import annotations

#: A map node's short keys (`/api/subtree`'s `tree`, before the rename) → the descriptive ones. `d` (the epoch
#: day of `t`) has no new name: the node carries `mtime` alone.
NODE = {
    "n": "name", "k": "kind", "b": "bytes", "o": "objects", "t": "mtime", "a": "last_read", "us": "owners",
    "cb": "class_bytes", "c": "children", "f": "folded", "m": "matched", "pv": "provenance", "src": "store", "at": "scan",
}


def node(n: dict) -> dict:
    """A map node and its children in the descriptive names: as is when it has them, else renamed from the short
    ones (`d` dropped)."""
    if "name" in n:
        return n
    out = {NODE.get(k, k): v for k, v in n.items() if k != "d"}
    if "matched" in out:
        out["matched"] = True  # the short form's `m: 1`
    if "children" in out:
        out["children"] = [node(c) for c in out["children"]]
    return out


def tree(body: dict) -> dict:
    """An `/api/subtree` body's `tree`, in the descriptive names."""
    return node(body["tree"])


#: A diff row's short keys (`/api/diff`'s `rows`, before the rename) → the descriptive ones.
ROW = {
    "p": "path", "d": "depth", "k": "kind", "s": "status", "a": "bytes_before", "b": "bytes_after",
    "oa": "objects_before", "ob": "objects_after", "x": "expanded", "l": "lookup", "src": "store",
}
#: A diff body's short totals → the descriptive ones.
TOTALS = {"total_a": "bytes_before", "total_b": "bytes_after", "objects_a": "objects_before", "objects_b": "objects_after"}


def diff(body: dict) -> dict:
    """An `/api/diff` body in the descriptive names: its totals and every row (as is when it has them)."""
    if not any(k in body for k in TOTALS):
        return body
    out = {TOTALS.get(k, k): v for k, v in body.items()}
    out["rows"] = [{ROW.get(k, k): v for k, v in r.items()} for r in body.get("rows", [])]
    return out
