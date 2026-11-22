"""A scan's DAG (`scan_dag`) as Nextflow's `scan.nf` (specs/orchestrator-abstraction.md §2): `job/nf/prelude.nf` (the
params and helpers), then one process per node and the workflow wiring them, both rendered here. `dt-cloud scan head`
writes it into each run's launch dir. A node's process runs its task's calls (`stepScript`), takes its deps' `done`
markers as one sorted list (`gathered`: a task's input must not depend on the order its deps finished in, or `-resume`
misses its cache), and a fan-out node takes its item first (`list_source` a source of `params.sources`, `index_part`
a part id), picking that item's calls by its tag."""
from __future__ import annotations

from .scan_dag import Call, Node, ScanDag


def _str(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _call(c: Call) -> str:
    """A `stepScript` step: `'name'`, or `['name', 'arg']`."""
    return _str(c.step) if c.arg is None else f"[{_str(c.step)}, {_str(c.arg)}]"


def _calls(calls: tuple[Call, ...]) -> str:
    return "[" + ", ".join(map(_call, calls)) + "]"


#: Each fan-out kind's item: its input's name, the item's tag, and the channel of items.
_FANOUT = {
    "sources": ("src", "src.uri", "Channel.fromList(params.sources)"),
    "parts": ("part", "part", None),
}


def _script(node: Node) -> str:
    if node.fanout is None:
        return f"stepScript(task, {_calls(node.tasks[0].calls)})"
    tag = _FANOUT[node.fanout][1]
    by_tag = ", ".join(f"{_str(t.tag)}: {_calls(t.calls)}" for t in node.tasks)
    return f"stepScript(task, [{by_tag}][{tag}])"


def _list_source(node: Node) -> list[str]:
    """A source's list task (spec §7): submit attempt k runs on `src.machines[k-1]`, each but the last with a queue
    timeout, and a submit timeout retries on the next type (as `scan_head.fallback_block`'s labels). A real failure
    reruns on the same type up to `node.retries` times, then finishes. `task.attempt` counts every attempt and
    `task.submitAttempt` only the submit-timeout ones, so their difference is the failures retried so far."""
    r = node.retries
    return [
        "    machineType { src.machines ? src.machines[task.submitAttempt - 1] : (src.machine ?: params.list_default.machine) }",
        "    disk { diskOf(src.disk ?: params.list_default.disk) }",
        "    maxSubmitAwait { src.machines && task.submitAttempt < src.machines.size() ? src.queue_timeout : null }",
        f"    maxRetries params.list_retries + {r}",
        "    errorStrategy {",
        "        if( !(task.previousException?.cause instanceof nextflow.exception.ProcessSubmitTimeoutException) ) {",
        f"            if( task.attempt - task.submitAttempt >= {r} ) return 'finish'",
        '            log.warn "scan head: ${task.process} (${src.uri}) failed (exit ${task.exitStatus}); rerunning it " +',
        f'                     "(${{task.attempt - task.submitAttempt + 1}}/{r})"',
        "            return 'retry'",
        "        }",
        '        log.warn "scan head: ${task.process} queued ${src.queue_timeout} on ${task.machineType}; retrying on " +',
        "                 \"${src.machines[task.submitAttempt]}${task.submitAttempt + 1 < src.machines.size() ? '' : ' (the last: no queue timeout)'}\"",
        "        return 'retry'",
        "    }",
    ]


def process(node: Node) -> str:
    lines = [f"process {node.name} {{"]
    if node.fanout is not None:
        lines.append(f'    tag "${{{_FANOUT[node.fanout][1]}}}"')
    lines.append(f"    label {_str(node.label)}")
    if node.time:
        lines.append(f"    time {_str(node.time)}")
    if node.fanout == "sources":
        lines += _list_source(node)
    elif node.retries:
        raise ValueError(f"{node.name}: only a source fan-out's tasks rerun on failure (retries={node.retries})")
    inputs = [f"val {_FANOUT[node.fanout][0]}"] if node.fanout else []
    if node.deps:
        inputs.append("val deps")
    if inputs:
        lines.append(f"    input: {'; '.join(inputs)}")
    lines += ["    output: path 'done'", f"    script: {_script(node)}", "}"]
    return "\n".join(lines) + "\n"


def _var(name: str) -> str:
    return f"{name}_done"


def workflow(dag: ScanDag) -> str:
    lines = ["workflow {",
             "    if (!params.scan || !params.gen || !params.job_start) error \"--scan, --gen and --job_start are required "
             "(`dt-cloud scan head` sets them)\""]
    for n in dag.nodes:
        args = []
        if n.fanout == "sources":
            args.append(_FANOUT["sources"][2])
        elif n.fanout == "parts":
            args.append("Channel.fromList([" + ", ".join(_str(t.tag) for t in n.tasks) + "])")
        if n.deps:
            # a dep's every task emits once, so a short list means one failed: `gathered` emits nothing for it
            first, *rest = map(_var, n.deps)
            count = sum(len(dag[d].tasks) for d in n.deps)
            args.append(f"gathered({first}.mix({', '.join(rest)}), {count})" if rest else f"gathered({first}, {count})")
        lines.append(f"    def {_var(n.name)} = {n.name}({', '.join(args)})")
    lines.append("}")
    return "\n".join(lines) + "\n"


def body(dag: ScanDag) -> str:
    """The rendered part of `scan.nf`: the processes, then the workflow."""
    return "\n".join([*map(process, dag.nodes), workflow(dag)])


def render(dag: ScanDag, prelude: str, deployment: str) -> str:
    """The whole `scan.nf`: a generated-file line, the prelude (`job/nf/prelude.nf`), then `body`."""
    return (f"// Generated by `dt-cloud scan head` from the {deployment} profile's scan DAG (`scan_dag`): not edited by hand.\n"
            f"{prelude.rstrip()}\n\n{body(dag)}")
