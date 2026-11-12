"""The site API's descriptive field names (specs/descriptive-field-names.md §3), read from a body that may still
carry the short ones.

The site and the readers here ship separately (job images, the sheet mirror), so a reader may meet either
spelling while a deployment's site and images move. Every reader takes its body through these, new names
first. Remove the short-name fallbacks (`NODE` and `node`'s rename) once every deployment's site serves the
descriptive names: the spec lists it as a follow-up.
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
