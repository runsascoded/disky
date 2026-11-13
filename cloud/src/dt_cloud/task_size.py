"""The task's CPUs and memory where its container can't read them: Fargate's microVM reports the host's CPUs in the
affinity mask and more RAM than the task's memory in `SC_PHYS_PAGES`, while its cgroup's `cpu.max` / `memory.max` read
`max`. A job definition sets `$DT_NPROC` (CPUs) and `$DT_MEM` (memory, a DuckDB size such as `16GiB`) from its own
size; unset, these fall back to what the process can see. Every step and stage that sizes threads or memory limits by
the machine reads these, so a merge on a 16 GiB task doesn't plan for the host's 31 GB (m3, 2026-11-13: a static-names
carry set DuckDB to 23GB and was OOM-killed)."""

from __future__ import annotations

import os
from collections.abc import Mapping

NPROC_ENV = "DT_NPROC"
MEM_ENV = "DT_MEM"


def task_cpus(env: Mapping[str, str] | None = None) -> int:
    """`$DT_NPROC` when set (a positive integer, else `ValueError`), else the CPUs this process may run on."""
    if v := ((os.environ if env is None else env).get(NPROC_ENV) or "").strip():
        if not v.isdigit() or int(v) < 1:
            raise ValueError(f"${NPROC_ENV}={v!r}: not a positive integer")
        return int(v)
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1


def task_ram(env: Mapping[str, str] | None = None) -> int:
    """`$DT_MEM` in bytes when set (a DuckDB size, else `ValueError`), else the machine's physical memory."""
    from .static_names import mem_bytes

    if v := ((os.environ if env is None else env).get(MEM_ENV) or "").strip():
        try:
            n = mem_bytes(v)
        except ValueError:
            n = 0
        if n < 1:
            raise ValueError(f"${MEM_ENV}={v!r}: not a memory size (e.g. 16GiB)")
        return n
    return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
