"""A scan's DAG run over fray's job `Client` (specs/orchestrator-abstraction.md §4): Iris (`fray.iris_backend`, on
CoreWeave) or fray's `LocalClient`. No Nextflow and no `marin.execution`: the head schedules the DAG itself, since it is
data (`scan_dag`), and each task is one fray job.

- **Scheduling**: a node's tasks are submitted once every task of its deps has succeeded (a fan-out node's tasks all
  at once, up to `limit` jobs in flight). A failed task lets the running ones finish and starts nothing new (Nextflow's
  `errorStrategy 'finish'`); a node's `retries` go to fray (`max_retries_failure`).
- **Resume**: each task that succeeded leaves a marker in the work store (`<work>/fray/<session>/<node>/<tag>.done`, the
  tag quoted); a
  later head for the same scan (the same session) skips those tasks (`cached`), as Nextflow's `-resume` does. `fresh`
  ignores them (each step still skips what its own outputs say is done).
- **A task**: `bash -c` over its env (the scan's identity, `NF_PROCESS` / `NF_TAG` / `NF_MACHINE`, the profile's env,
  the run id and profile JSON) and its calls, each through `dt-cloud secrets exec` with its bindings (`scan_head`'s
  `params`: a source's own, a step's, else the profile's), as `job/nf/prelude.nf`'s `stepScript` writes it. The script
  carries the env, since `LocalClient` runs the command as is.
- **Resources**: `scan_dag.resources` → fray's `ResourceConfig` (`cpu`, `ram`, `disk`; a GCE machine type is read as
  its vCPUs and memory, `machine_shape`).
- **The run's DAG**: `run_dag`'s own JSON (engine `fray`), with the tasks a resume found done as `cached_tasks`."""
from __future__ import annotations

import re
import shlex
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from .run_dag import Dag
from .scan_backend import Backend, Run
from .scan_dag import Node, Task, resources
from .scan_profile import Machine


def err(*a: object) -> None:
    print(*a, file=sys.stderr)


ENGINE = "fray"

#: GiB per vCPU of a GCE machine type's class (`<family>-<class>-<vcpus>`).
_GIB_PER_CPU = {"standard": 4, "highmem": 8, "highcpu": 1, "megamem": 14, "ultramem": 24}
#: The shared-core and odd shapes, by name: (vCPUs, GiB).
_SHAPES = {"e2-micro": (2, 1), "e2-small": (2, 2), "e2-medium": (2, 4)}


def machine_shape(m: Machine) -> tuple[float, str]:
    """A task's `(cpu, ram)` as fray takes them: the machine's own `cpus` / `memory` where it sets them, else its GCE
    type's (`n2-standard-8` → 8 vCPUs, 32 GiB); neither: fray's default (1 vCPU, 4 GiB)."""
    cpu: float | None = m.cpus
    ram: str | None = m.memory.replace(" ", "").lower().removesuffix("b") if m.memory else None
    if m.type and (cpu is None or ram is None):
        if m.type in _SHAPES:
            c, g = _SHAPES[m.type]
        elif (t := re.fullmatch(r"[a-z0-9]+-([a-z]+)-(\d+)", m.type)) and t.group(1) in _GIB_PER_CPU:
            c = int(t.group(2))
            g = c * _GIB_PER_CPU[t.group(1)]
        else:
            raise ValueError(f"machine type {m.type!r}: no vCPU / memory shape known (set the label's `cpus` and `memory`)")
        cpu = c if cpu is None else cpu
        ram = f"{g}g" if ram is None else ram
    return (1.0 if cpu is None else float(cpu)), (ram or "4g")


def task_label(node: Node, task: Task) -> str:
    """How the run names a task, as Nextflow's trace does: `<node>`, or `<node> (<tag>)` in a fan-out."""
    return f"{node.name} ({task.tag})" if node.fanout else node.name


def job_name(run: Run, node: Node, task: Task) -> str:
    """The task's fray job name (no spaces): `disky-<deployment>[-<store>]-<scan>-<node>[-<tag>]`, each part reduced
    to `[a-z0-9._-]`."""
    def part(s: str) -> str:
        return re.sub(r"[^a-z0-9._-]+", "-", s.lower()).strip("-")
    p = run.profile
    return "-".join(part(x) for x in ["disky", p.deployment, *([] if p.primary else [p.store]), run.scan, node.name,
                                       *([task.tag] if task.tag else [])])


def task_script(run: Run, node: Node, task: Task, params: Mapping, machine: str = "") -> str:
    """The task's `bash -c` script: its env, then each call through `dt-cloud secrets exec` with its bindings
    (`params`: `scan_head.params`'s `sources`, `step_secrets`, `secrets`, `secrets_project`, `env`)."""
    from .scan_head import _bindings
    from .scan_steps import PROFILE_JSON_ENV, profile_env_value

    env = {"SNAP_ID": run.scan, "SCAN_ID": run.scan, "GEN": run.gen, "DT_JOB_START": run.job_start,
           "NF_PROCESS": node.name, "NF_TAG": task.tag, "NF_MACHINE": machine, **params["env"],
           "DT_RUN_ID": run.run_id, PROFILE_JSON_ENV: profile_env_value(run.profile_json)}
    lines = [f"export {k}={shlex.quote(str(v))}" for k, v in env.items()]
    proj = ["-p", params["secrets_project"]] if params["secrets_project"] else []
    by_source = {s["uri"]: s.get("secrets") for s in params["sources"]}
    for c in task.calls:
        src = by_source.get(c.arg) if c.step == "list" else None
        sec = src if src is not None else params["step_secrets"].get(c.step, params["secrets"])
        argv = ["dt-cloud", "secrets", "exec", *proj, *(a for b in sec for a in ("-s", b)), "--",
                "dt-cloud", "scan", "step", c.step, *([c.arg] if c.arg is not None else [])]
        lines.append(shlex.join(argv))
    return "set -e\n" + "\n".join(lines) + "\n"


def fray_request(name: str, script: str, cpu: float, ram: str, disk: str, image: str | None, retries: int):
    """A task as a fray `JobRequest` (fray imported here: only this backend needs it)."""
    from fray.types import Entrypoint, JobRequest, ResourceConfig

    return JobRequest(name=name, entrypoint=Entrypoint.from_binary("bash", ["-c", script]),
                      resources=ResourceConfig(cpu=cpu, ram=ram, disk=disk, image=image), max_retries_failure=retries)


@dataclass
class _Task:
    node: Node
    task: Task
    label: str
    state: str = "waiting"   # waiting | cached | running | succeeded | failed | stopped
    handle: object = None
    error: str | None = None


@dataclass
class FrayBackend(Backend):
    """`client`: a fray `Client` (`submit(JobRequest) → JobHandle` with `status()`, `terminate()`, `logs()`). `request`
    builds a task's job request (`fray_request`'s signature). `poll`: seconds between status checks."""
    client: object
    request: Callable = fray_request
    limit: int = 20
    poll: float = 5.0
    engine = ENGINE
    tasks: list[_Task] = field(default_factory=list)

    def _store(self, run: Run):
        from .blobstore import Store

        return Store(run.profile.work)

    def _marker(self, run: Run, t: _Task) -> str:
        """The task's done marker in the work store: its tag quoted whole (a source's URI is one key segment)."""
        return f"fray/{run.session}/{t.node.name}/{quote(t.task.tag, safe='') or '_'}.done"

    def prepare(self, run: Run) -> list[str]:
        from .scan_head import params

        run.launch.mkdir(parents=True, exist_ok=True)
        self.params = params(run.profile, run.profile_json, run.scan, run.gen, run.job_start, run.run_id, run.session,
                             run.env, reproc=run.reproc, fresh=run.fresh, dag=run.dag)
        self.tasks = [_Task(n, t, task_label(n, t)) for n in run.dag.nodes for t in n.tasks]
        self.stop = threading.Event()
        self.rc: int | None = None
        self.reported: tuple[str, ...] | None = None
        self._write_dag(run)
        return ["fray", "run", run.session, f"({len(self.tasks)} tasks in {len(run.dag.nodes)} nodes)"]

    def _cached(self) -> tuple[str, ...]:
        return tuple(sorted(t.label for t in self.tasks if t.state == "cached"))

    def _write_dag(self, run: Run) -> Path:
        g = run.dag.graph()
        path = self.dag_file(run)
        path.write_text(Dag(g.nodes, g.edges, self._cached()).to_json() + "\n")
        return path

    def store_preview(self, run: Run) -> None:
        from .scan_head import _record
        from .scan_runs import dag_cmd

        try:
            _record(dag_cmd, ["-E", ENGINE, run.run_id, str(self._write_dag(run))])
        except Exception as e:  # noqa: BLE001 — a view, never the run
            err(f"scan head: DAG preview failed: {type(e).__name__}: {e}")

    def start(self, run: Run) -> None:
        if not run.fresh:
            store = self._store(run)
            for t in self.tasks:
                if store.exists(self._marker(run, t)):
                    t.state = "cached"
            self._write_dag(run)
        self.thread = threading.Thread(target=self._schedule, args=(run,), name="fray-scan", daemon=True)
        self.thread.start()

    def _submit(self, run: Run, t: _Task) -> None:
        m = resources(run.profile, t.node, t.task if t.node.fanout == "sources" else None)
        cpu, ram = machine_shape(m)
        disk = f"{m.disk.request}g" if m.disk else "64g"
        script = task_script(run, t.node, t.task, self.params, m.type or "")
        req = self.request(job_name(run, t.node, t.task), script, cpu, ram, disk,
                           run.image or run.profile.executor.image, t.node.retries)
        t.handle = self.client.submit(req)
        t.state = "running"
        err(f"scan head: submitted {t.label} ({cpu:g} cpu, {ram}, {disk})")

    def _settle(self, run: Run, t: _Task) -> None:
        status = str(t.handle.status()).lower().rsplit(".", 1)[-1]
        if status in ("pending", "running"):
            return
        if status == "succeeded":
            t.state = "succeeded"
            self._store(run).write_text(self._marker(run, t), f"{t.handle.job_id}\n")
        else:
            t.state = "stopped" if status == "stopped" else "failed"
            t.error = status
        err(f"scan head: {t.label} {t.state}")

    def _schedule(self, run: Run) -> None:
        by_node: dict[str, list[_Task]] = {}
        for t in self.tasks:
            by_node.setdefault(t.node.name, []).append(t)
        done = ("succeeded", "cached")
        try:
            while True:
                for t in self.tasks:
                    if t.state == "running":
                        self._settle(run, t)
                failed = any(t.state in ("failed", "stopped") for t in self.tasks)
                running = [t for t in self.tasks if t.state == "running"]
                if not failed and not self.stop.is_set():
                    for t in self.tasks:
                        if len(running) >= self.limit:
                            break
                        if t.state == "waiting" and all(d.state in done for dep in t.node.deps for d in by_node[dep]):
                            self._submit(run, t)
                            running.append(t)
                if not running:
                    break
                time.sleep(self.poll)
            self.rc = 0 if all(t.state in done for t in self.tasks) else 1
        except Exception as e:  # noqa: BLE001 — the head must still finish the record
            err(f"scan head: fray scheduler failed: {type(e).__name__}: {e}")
            self.rc = 1

    def wait(self, timeout: float) -> int | None:
        self.thread.join(timeout)
        return None if self.thread.is_alive() else self.rc

    def progress(self, run: Run) -> Path | None:
        cached = self._cached()
        if cached == self.reported:
            return None
        self.reported = cached
        return self._write_dag(run)

    def interrupt(self) -> None:
        self.stop.set()
        for t in self.tasks:
            if t.state == "running":
                t.handle.terminate()

    def cancel(self, run: Run) -> None:
        for t in self.tasks:
            if t.state == "running":
                t.handle.terminate()

    def error(self, run: Run, rc: int) -> str:
        first = next((t for t in self.tasks if t.state in ("failed", "stopped")), None)
        return f"fray exit {rc}" + (f": {first.label} {first.state}" if first else "") + f" (session {run.session})"

    def finish(self, run: Run, rc: int, signal: int | None) -> None:
        self._write_dag(run)
        for t in self.tasks:
            if t.state in ("failed", "stopped") and t.handle is not None:
                try:
                    err(f"--- task {t.label} {t.state}")
                    for line in t.handle.logs():
                        err(line)
                except Exception as e:  # noqa: BLE001 — logs are a view of the run
                    err(f"--- task {t.label}: no logs ({type(e).__name__}: {e})")

    def cost(self, run: Run) -> list[str]:
        return []

    def dag_file(self, run: Run) -> Path:
        return run.launch / "dag.json"
