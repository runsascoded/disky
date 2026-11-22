"""The engine under `dt-cloud scan head` (specs/orchestrator-abstraction.md §3): the head stamps the scan, records the
run (start, phases before it, heartbeat, finish) and handles a TERM; a backend runs the scan's DAG (`scan_dag`). It
submits it, waits, cancels, resumes a scan's earlier run (a task whose outputs are marked done isn't redone), and says
what the run cost and which step failed first. The steps record their own phases (`scan-run`), whatever runs them.

The run record, phases, alerts and `run_dag` keep their shapes under every backend; only the DAG's format differs, by
`engine` (`run_dag.ENGINES`). The first backend is Nextflow (`scan_head.NextflowBackend`)."""
from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .scan_dag import ScanDag
from .scan_profile import ScanProfile


@dataclass(frozen=True)
class Run:
    """One scan's run, as the head hands it to its backend."""
    profile: ScanProfile
    #: The profile as JSON (every task's `$DT_SCAN_PROFILE_JSON`).
    profile_json: str
    dag: ScanDag
    scan: str
    gen: str
    job_start: str
    #: The run every task records into ("" when the run isn't recorded).
    run_id: str
    #: The scan's stable run identity: a re-run of the head for the same scan resumes under it.
    session: str
    #: Where the backend keeps the run's own files.
    launch: Path
    env: dict[str, str]
    now: dt.datetime
    reproc: bool = False
    #: Every task reruns (each step still skips what its own outputs say is done).
    fresh: bool = False
    #: The image the tasks run (the head's own), over the profile's `executor.image`.
    image: str | None = None
    #: Where `run_id` came from: `DT_RUN_ID`, `BATCH_JOB_UID`, or `unrecorded` (the head made one up, or none).
    run_id_source: str = "unrecorded"


class Backend(ABC):
    #: The run's DAG format (`run_dag.ENGINES`, `scan_run_dags.engine`).
    engine: ClassVar[str]

    @abstractmethod
    def prepare(self, run: Run) -> list[str]:
        """Write whatever the run needs before it starts; return its command line, for the log (a dry run stops here)."""

    @abstractmethod
    def store_preview(self, run: Run) -> None:
        """Store the run's DAG before any task runs (`scan-run dag`), so /scans draws the pipeline while it runs. Never
        fails the run."""

    @abstractmethod
    def start(self, run: Run) -> None:
        """Submit the DAG; return once it is running."""

    @abstractmethod
    def wait(self, timeout: float) -> int | None:
        """The run's exit status once it has ended, else None after `timeout` seconds."""

    @abstractmethod
    def progress(self, run: Run) -> Path | None:
        """The run's DAG file (in `engine`'s format), when what it says (e.g. which tasks a resume found done) changed
        since the last call; else None."""

    @abstractmethod
    def interrupt(self) -> None:
        """The head got a TERM / INT: stop the run (`wait` then returns)."""

    @abstractmethod
    def cancel(self, run: Run) -> None:
        """After an interrupted run ends: cancel whatever task it left running."""

    @abstractmethod
    def error(self, run: Run, rc: int) -> str:
        """A failed run's one-line reason: the first failed step, where known."""

    @abstractmethod
    def finish(self, run: Run, rc: int, signal: int | None) -> None:
        """After the run: its logs, reports and evidence, published. Never fails the run."""

    @abstractmethod
    def cost(self, run: Run) -> list[str]:
        """`scan-run finish`'s cost arguments (`-l KEY=VALUE`: the labels its tasks' jobs carry)."""

    @abstractmethod
    def dag_file(self, run: Run) -> Path:
        """The run's final DAG file, in `engine`'s format (stored by `scan-run finish -D` when it exists)."""
