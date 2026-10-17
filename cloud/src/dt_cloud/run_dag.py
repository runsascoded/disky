"""A scan run's pipeline DAG, engine-neutral (specs/pipelines-everywhere.md "Run reports"): `{nodes: [{id, label}],
edges: [[from, to], …]}`, the steps only. A node's `id` is what the run's phases name as their `task` (Nextflow:
the process, `$NF_PROCESS`), so the site joins phases to nodes without knowing the engine. Each engine's own format
is reduced here, by the writer (`scan-run finish -D` / `scan-run dag`); the raw file stays in the run's archive."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

#: Engines with a reader here, by name (`scan_run_dags.engine`).
ENGINES = ("nextflow",)

# Nextflow's `-with-dag dag.mmd`: `v2(["list_source"])` is a process; `v0["channel.fromList"]`, `v3(( ))` (an operator)
# and `v15[" "]` (an output) are not.
_NODE = re.compile(r'^\s*(v\d+)\s*(\(\[|\(\(|\[|\()\s*"?(.*?)"?\s*(\]\)|\)\)|\]|\))\s*$')
_EDGE = re.compile(r"^\s*(v\d+)\s*-->\s*(?:\|[^|]*\|\s*)?(v\d+)\s*$")


@dataclass(frozen=True)
class Dag:
    nodes: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]

    def to_json(self) -> str:
        return json.dumps({"nodes": [{"id": n, "label": n} for n in self.nodes], "edges": [list(e) for e in self.edges]},
                          separators=(",", ":"))


def from_nextflow(mermaid: str) -> Dag:
    """Nextflow's Mermaid DAG reduced to its processes: a path through channels and operators is one edge. Nodes in
    the file's order; each process's targets sorted."""
    proc: dict[str, str] = {}
    out: dict[str, list[str]] = {}
    for line in mermaid.splitlines():
        if n := _NODE.match(line):
            if n.group(2) == "([":
                proc[n.group(1)] = n.group(3)
            continue
        if e := _EDGE.match(line):
            out.setdefault(e.group(1), []).append(e.group(2))
    edges: list[tuple[str, str]] = []
    for vid, name in proc.items():
        seen: set[str] = set()
        todo = list(out.get(vid, ()))
        to: set[str] = set()
        while todo:
            v = todo.pop(0)
            if v in seen:
                continue
            seen.add(v)
            if v in proc:
                to.add(proc[v])
            else:
                todo.extend(out.get(v, ()))
        edges += [(name, t) for t in sorted(to)]
    return Dag(tuple(proc.values()), tuple(edges))


def read(engine: str, text: str) -> Dag:
    """`text` in `engine`'s own DAG format, reduced."""
    if engine == "nextflow":
        return from_nextflow(text)
    raise ValueError(f"no DAG reader for engine {engine!r} (have: {', '.join(ENGINES)})")
