"""A scan's DAG as data (specs/orchestrator-abstraction.md §1): which tasks run which steps, after which others, on
what resources. Built from the deployment's scan profile (`build`), so its options decide which nodes exist; an engine
runs it: `scan_dag_nf.render` writes it as Nextflow's `scan.nf`.

Shaped like Marin's `StepSpec` (`marin.execution`) where that costs nothing: a node has a `name`, its `deps` (names)
and its `resources`; its `tasks` are what a StepSpec's `fn` would do, each a list of `dt-cloud scan step <name> [arg]`
calls (a fan-out node has one task per item: `list_source` per source, `index_part` per part; an engine that has no
fan-out runs each as its own step, `<node> (<tag>)`).

A node's name is what the run's phases name as their `task` (`$NF_PROCESS`) and what `run_dag` draws, so it is the
same under every engine; `graph()` is the run's `run_dag.Dag`, which Nextflow's own `-with-dag` reduces to exactly.

    bucket-check, pre ─┐
    list ×source ──────┴→ [index ×part →] index ─┬→ static-names ─────────────────┐
                                 ├→ index-rows → publish-r2 → interval-store ──┼→ index-sync → post, over-time
    side ────────────────────────┘ (publish-r2's, and serving's)              │
                                 └→ index-rows ───────────────────────────────┘

- `side` steps (stage `side`: `non-live`): their own task from the start, beside `list` and `index`, which never wait
  on it; `publish-r2` does (its outputs reach R2 with the scan's), and so does everything after serving.
- `options.index.parts`: the index sharded, one `index_part` task per part, then `index` merges them.
- `post` runs the enabled post steps in registry order (`scan_steps.STEPS`), after both stores and the pointers.
- `fold`: bucket-check runs in the first source's list task, index-sync in the post task.
- `publish-r2` copies the generation as it stands, so after index-rows (the footers it writes never reached R2 when the
  two ran side by side; cw, 2026-10-30).
- On a local data dir the static index reads earlier scans from the serving store, so it waits for this one's publish.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from urllib.parse import urlparse

from .run_dag import Dag
from .scan_profile import Disk, Machine, ScanProfile

#: Each sized node's `machines` key, and the label it runs on when the profile doesn't size it.
LABELS: dict[str, tuple[str, str]] = {
    "pre": ("pre", "small"),
    "side": ("side", "small"),
    "static_names": ("static", "small"),
    "interval_store": ("interval", "big"),
    "index_part": ("index_part", "big"),
    "over_time": ("over_time", "post"),
}

#: The fan-out kinds: a node's tasks are one per source (`list_source`: each with its own machine) or per index part.
FANOUTS = ("sources", "parts")


@dataclass(frozen=True)
class Call:
    """One `dt-cloud scan step <step> [arg]`."""
    step: str
    arg: str | None = None

    def __str__(self) -> str:
        return self.step if self.arg is None else f"{self.step} {self.arg}"


@dataclass(frozen=True)
class Task:
    """One task of a node: its calls, in order. `tag` names it among a fan-out node's tasks (the source's URI, the
    part's id); "" for a node's only task."""
    calls: tuple[Call, ...]
    tag: str = ""


@dataclass(frozen=True)
class Node:
    name: str
    deps: tuple[str, ...]
    tasks: tuple[Task, ...]
    #: The resource class: a key of the profile's `machines` (or a default one the executor sizes: `small`, `big`, …).
    label: str
    fanout: str | None = None
    #: The task's time limit, where the node sets one (the label's `machines.<label>.time` otherwise).
    time: str | None = None
    #: Reruns of a task that failed (a listing is idempotent: `bulk-list -x clear`); a submit timeout on a fallback
    #: machine is never one.
    retries: int = 0

    @property
    def calls(self) -> tuple[Call, ...]:
        """Every call of every task, in task order."""
        return tuple(c for t in self.tasks for c in t.calls)


@dataclass(frozen=True)
class ScanDag:
    #: In a topological order (each node after its deps): the order the engine starts them in, and `run_dag`'s.
    nodes: tuple[Node, ...]

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for n in self.nodes:
            if n.name in seen:
                raise ValueError(f"scan DAG: node {n.name!r} twice")
            if missing := [d for d in n.deps if d not in seen]:
                raise ValueError(f"scan DAG: {n.name} needs {', '.join(missing)}, which no earlier node is")
            if n.fanout is not None and n.fanout not in FANOUTS:
                raise ValueError(f"scan DAG: {n.name}: fan-out {n.fanout!r} (one of {', '.join(FANOUTS)})")
            seen.add(n.name)

    def __getitem__(self, name: str) -> Node:
        for n in self.nodes:
            if n.name == name:
                return n
        raise KeyError(name)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(n.name for n in self.nodes)

    def graph(self) -> Dag:
        """The run's `run_dag.Dag`: the nodes in order, each one's edges to its dependents (sorted), as Nextflow's
        `-with-dag` reduces to (`run_dag.from_nextflow`)."""
        edges = [(n.name, m) for n in self.nodes for m in sorted(x.name for x in self.nodes if n.name in x.deps)]
        return Dag(self.names, tuple(edges))


def _label(profile: ScanProfile, node: str, default: str) -> str:
    key, fallback = LABELS.get(node, (default, default))
    return key if key in profile.machines else fallback


def build(profile: ScanProfile, *, reproc: bool = False) -> ScanDag:
    """The profile's scan as a DAG. `reproc` (`scan head -R`) runs from `index`: only sources adopting a listing
    (`sources[].listing`) keep a list task (its shards have no other copy on a local data dir)."""
    from .scan_steps import STEPS, index_parts

    enabled = profile.enabled()

    def on(s: str) -> bool:
        return s in enabled

    def folded(s: str) -> bool:
        return on(s) and s in profile.fold

    sources = [s for s in profile.sources if not reproc or s.listing]
    skip_list = reproc and not sources
    pre = [n for n in enabled if STEPS[n].stage == "pre" and n != "bucket-check"]
    side = [n for n in enabled if STEPS[n].stage == "side"]
    post = [n for n in enabled if STEPS[n].stage == "post" and n != "over-time"]
    parts = [pid for pid, _ in index_parts(profile)]
    nodes: list[Node] = []

    def add(name: str, deps: list[str], calls: list[Call] | None = None, *, label: str, tasks: list[Task] | None = None,
            **kw) -> str:
        nodes.append(Node(name, tuple(dict.fromkeys(deps)), tuple(tasks if tasks is not None else [Task(tuple(calls or ()))]),
                          label, **kw))
        return name

    ready: list[str] = []
    if on("bucket-check") and not folded("bucket-check") and not skip_list:
        ready.append(add("bucket_check", [], [Call("bucket-check")], label="small"))
    if pre:
        ready.append(add("pre", [], [Call(n) for n in pre], label=_label(profile, "pre", "small")))
    if not skip_list:
        tasks = [Task((*([Call("bucket-check")] if i == 0 and folded("bucket-check") else []), Call("list", s.uri)), s.uri)
                 for i, s in enumerate(sources)]
        ready.append(add("list_source", [], tasks=tasks, label="list", fanout="sources", retries=1))
    if parts:
        ready = [add("index_part", ready, tasks=[Task((Call("index", p),), p) for p in parts],
                     label=_label(profile, "index_part", "big"), fanout="parts")]
    index = add("index", ready, [Call("index")], label="big")
    sided = [add("side", [], [Call(n) for n in side], label=_label(profile, "side", "small"))] if side else []
    rows = add("index_rows", [index], [Call("index-rows")], label="small")
    pub = add("publish_r2", [rows, *sided], [Call("publish-r2")], label="medium") if on("publish-r2") else index
    static_after_publish = not urlparse(profile.data).scheme and on("publish-r2")
    st = [add("static_names", [pub if static_after_publish else index], [Call("static-names")],
              label=_label(profile, "static_names", "small"), time="6h")] if on("static-names") else []
    iv = add("interval_store", [pub], [Call("interval-store")], label=_label(profile, "interval_store", "big")) \
        if on("interval-store") else pub
    serving = [rows, *st, iv, *sided]
    synced = serving if folded("index-sync") else [add("index_sync", serving, [Call("index-sync")], label="small")]
    if post or folded("index-sync"):
        add("post", synced, [*([Call("index-sync")] if folded("index-sync") else []), *map(Call, post)], label="post")
    if on("over-time"):
        add("over_time", synced, [Call("over-time")], label=_label(profile, "over_time", "post"))
    return ScanDag(tuple(nodes))


def resources(profile: ScanProfile, node: Node, task: Task | None = None) -> Machine:
    """What one of `node`'s tasks runs on, engine-neutral: a list task's from its source (falling back to
    `machines.list`), any other's from its label's `machines` entry (empty: the executor's default); the node's own
    `time` over the label's."""
    if node.fanout == "sources":
        if task is None:
            raise ValueError(f"{node.name}: a source's task's resources are its own (pass the task)")
        s, lst = profile.source(task.tag), profile.machines.get("list") or Machine()
        disk: Disk | None = s.disk or lst.disk
        m = (Machine(s.machine, disk, fallback=s.fallback, queue_timeout=s.queue_timeout) if s.machine
             else replace(lst, disk=disk))
    else:
        m = profile.machines.get(node.label) or Machine()
    return replace(m, time=node.time) if node.time else m
