"""A bench query set: YAML data, one per deployment (gcs's lives beside its
deployment config, not in this package).

```yaml
views: ["", my-bucket, my-bucket/dir]   # default view roots ('' = the store root)
queries:
  - id: ckpt-not-eval                    # file-name-safe, unique
    q: ckpt -eval
    qs: simple                           # optional; default simple
    views: [""]                          # optional; default: the set's
    why: NOT subtraction across segments
```
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


@dataclass(frozen=True)
class Case:
    id: str
    q: str
    qs: str
    views: tuple[str, ...]
    why: str


def parse_set(doc: dict) -> list[Case]:
    default_views = tuple(doc.get("views") or [""])
    out: list[Case] = []
    seen: set[str] = set()
    for d in doc["queries"]:
        cid = d["id"]
        if not ID.match(cid) or cid in seen:
            raise ValueError(f"bad or duplicate query id: {cid!r}")
        seen.add(cid)
        views = tuple(str(v).strip("/") for v in (d.get("views") or default_views))
        out.append(Case(cid, str(d["q"]), d.get("qs", "simple"), views, str(d.get("why", "")).strip()))
    return out


def load(path: str) -> list[Case]:
    import fsspec
    import yaml

    with fsspec.open(path, "r") as f:
        return parse_set(yaml.safe_load(f))
