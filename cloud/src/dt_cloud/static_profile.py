"""A deployment's static name index config (`Profile`): where its scans are, where the index goes, how its Batch jobs
run. Nothing here assumes a deployment: the generic stages read the profile, and missing config is an error naming
the variable to set.

Loaded (`load_profile`) from `STATIC_NAMES_PROFILE` — a JSON file of `Profile` fields (a deployment's own, on its
branch: `job/static-names.json`) or a named example (`static_profile_examples.EXAMPLES`: `gcs`, `cw`; illustrative, no
`gen`) — then each field's env var on top (`ENV`), so a deployment can check in its own file, start from an example, or
set every field by env.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from functools import cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .append_runner import Policy

#: The binary counter's compaction level when a profile sets none (deployments set theirs explicitly; the examples do).
#: Carries stop below level L, so a stack holds at most 2^L − 1 scans in carried runs, and a compaction into a new base
#: generation is due after about 2^L scans: at 5, 32 scans (~32 days at gcs's daily scans, ~8 at cw's 6-hourly).
COMPACT_LEVEL = 5

#: The R2 copy's credentials: Secret Manager secret names, by the env var each sets in the copy job.
R2_SECRET_VARS = {"endpoint": "R2_ENDPOINT", "key_id": "R2_ACCESS_KEY_ID", "secret": "R2_SECRET_ACCESS_KEY"}


@dataclass(frozen=True)
class Profile:
    name: str = ""
    #: Key templates of the scans' `path` sorts in the data bucket: `{id}` the scan id, `{gen}` the store generation
    #: (a scan's newest wins; a template without one counts as the oldest), e.g. `cw-l2/{id}/index/{gen}/path-index.parquet`.
    layouts: tuple[str, ...] = ()
    #: The data bucket: the scans, and the generations under `static-names/<gen>/`.
    bucket: str | None = None
    #: Intermediates (the suffix shuffle, markers, the open-version state): no soft delete, a short age-based delete rule.
    scratch: str | None = None
    #: The base generation the runs append to.
    gen: str | None = None
    #: The R2 bucket serving the index (`static-names/<gen>/` there too).
    r2_bucket: str | None = None
    #: `R2_SECRET_VARS` keys → Secret Manager secret names; `r2_endpoint` (plain) may stand in for the endpoint's.
    r2_secrets: Mapping[str, str] = field(default_factory=dict)
    r2_endpoint: str | None = None
    project: str | None = None
    region: str | None = None
    #: The job image (`dt_cloud` + `pyrmts`).
    image: str | None = None
    #: The stages' account (the data and scratch buckets); the R2 copy's (its secrets), default the stages'.
    sa: str | None = None
    r2_sa: str | None = None
    machine: str = "n2-highmem-16"
    ssd_gb: int = 750
    #: Per-stage overrides of `machine` / `ssd_gb`, by a Batch job's `stage` label (`drill`, `anchors`, `append`,
    #: `<tier>-merge`, …): the drill can take a bigger machine without upsizing the 16 append tasks.
    machines: Mapping[str, str] = field(default_factory=dict)
    ssd_gbs: Mapping[str, int] = field(default_factory=dict)
    spot: bool = True
    #: Stages (a Batch job's `stage` label: `anchors`, `drill`, `append`, …) that run STANDARD even when `spot`: a long
    #: single-task stage restarts from zero on each preemption, so spot's discount can cost more than it saves.
    standard_stages: tuple[str, ...] = ()
    append_tasks: int = 16
    #: A backlog's scans in flight at once (`runs add -c`): each stage of a scan starts once the same stage of the scan
    #: before it and its own previous stage are done, publishes in order (specs/static-backlog-pipeline.md); 1: one scan
    #: at a time.
    pipeline_depth: int = 1
    #: The compaction's drill measurement over this many tasks (`static_merge compact -D`: the compaction's own and
    #: helper Batch tasks on the `measure` stage's machine; specs/static-measure-fanout.md); 1: the compaction alone.
    measure_tasks: int = 1
    #: The compaction's base anchors' rollups over this many tasks (`static_merge compact -A`: the compaction's own and
    #: helper Batch tasks on the `anchor-shard` stage's machine; specs/static-anchors-fanout.md); 1: the compaction alone.
    anchor_tasks: int = 1
    #: The compaction's drill fold over this many tasks (`static_merge compact -L`: the compaction's own and helper Batch
    #: tasks on the `fold-shard` stage's machine, a job per wave; specs/static-fold-fanout.md); 1: the compaction alone.
    fold_tasks: int = 1
    #: Whether each run also gets the heavy-term drilldown (`drill/`, `static_drill`): needs the base generation's `drill/`.
    drill: bool = False
    #: Where the drill's tasks save each step (`static_drill build -S`: a store root, a bare bucket name meaning `gs://`), so
    #: a reclaimed spot task's retry resumes after its last saved step (specs/drill-preemption.md); unset: no checkpoints.
    drill_checkpoint: str | None = None
    #: Whether each run also gets anchored search's `names/` and `anchors/` (`static_anchors`): needs the base generation's.
    anchors: bool = False
    #: Mounted dirs copied onto the tasks' PYTHONPATH in place of the image's own code (a staged tree).
    src: tuple[str, ...] = ()
    #: The hex-run rule a new generation is built with (specs/done/static-hex-runs.md): `MIN,TAIL` (e.g. `16,8`) or `off`.
    #: Required (no default): `hex_rule()`. A generation records its rule (`scans.json` `hex_runs`); its runs follow it.
    hex_runs: str = ""
    #: The binary counter's compaction level L (≥ 1): carries stop below it, so a stack holds at most 2^L − 1 scans in
    #: carried runs and a compaction into a new base generation is due after about 2^L scans. None (JSON `null`, env
    #: `none`): unbounded, the cascade carries at every level and never asks for a compaction (≈ log2(n) runs live after
    #: n scans). Unset: `COMPACT_LEVEL`, the documented fallback; the examples set theirs explicitly.
    compact_level: int | None = COMPACT_LEVEL
    #: The compaction policy (`append_runner.Policy`; JSON `{"level": L}`, `{"every": "7d"}`, `{"every": "30s"}`,
    #: `{"geometric": 1}` or `"none"`; specs/static-compaction.md): when set, it replaces `compact_level`, which is
    #: shorthand for `{"level": L}`. `policy` is the one in force.
    compact: Policy | None = None
    #: Which copy the builder reads a generation's base and runs from in a merge and a compaction (`append_runner.READ_FROM`):
    #: `gcs` (the data bucket, in place) or `r2` (the R2 copy, mirrored locally; it must hold the builder's inputs).
    read_from: str = "gcs"
    #: Key ranges, then served sorts, the interval store's compaction folds at a time (`compact -j`: cursors of one DuckDB
    #: database sharing its memory and threads). The served sorts are each ~35 min at gcs's size, so 1 runs them in series.
    compact_jobs: int = 1
    #: The static store's compaction: its drill fold's chunks folded at a time (`static_merge compact -j`: cursors of one
    #: DuckDB database sharing its `-M` and `-p`). 0: sized from the compact machine (`static_runner.fold_jobs`).
    fold_jobs: int = 0
    #: Env var → Secret Manager secret name, for the merge and compaction jobs to record their runs on `/scans` with
    #: (`maint_runs`): `{"CLOUDFLARE_API_TOKEN": <the D1 token's secret>}`, with the submitter's `$D1_DB_ID` and
    #: `$CLOUDFLARE_ACCOUNT_ID` passed through (`append_runner.Runner.record_env`). Empty: the jobs run unrecorded.
    record_secrets: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if self.read_from not in ("gcs", "r2"):
            raise SystemExit(f"read_from must be gcs or r2, not {self.read_from!r}")
        if self.compact_level is not None and (isinstance(self.compact_level, bool) or not isinstance(self.compact_level, int) or self.compact_level < 1):
            raise SystemExit(f"compact_level must be an integer ≥ 1 or none, not {self.compact_level!r}")
        if self.compact is not None:
            from .append_runner import parse_policy

            object.__setattr__(self, "compact", parse_policy("compact", self.compact))

    @property
    def policy(self) -> Policy:
        """The compaction policy in force: `compact`, else `compact_level` as `{level: L}` (None: `none`)."""
        from .append_runner import level_policy

        return self.compact if self.compact is not None else level_policy(self.compact_level)

    def machine_for(self, stage: str) -> str:
        """`stage`'s machine: its override in `machines`, else `machine`."""
        return self.machines.get(stage) or self.machine

    def ssd_for(self, stage: str) -> int:
        """`stage`'s local SSD (GB): its override in `ssd_gbs`, else `ssd_gb`."""
        return self.ssd_gbs.get(stage) or self.ssd_gb

    def need(self, name: str):
        v = getattr(self, name)
        if v in (None, "", ()):
            raise SystemExit(f"static names: no {name} in the deployment profile: set {ENV[name]} (or STATIC_NAMES_PROFILE)")
        return v

    def hex_rule(self):
        """The profile's hex-run rule (`hex_runs.HexRule`, or None for `off`)."""
        from .hex_runs import parse_rule

        raw = self.need("hex_runs")
        try:
            return parse_rule(raw)
        except ValueError as e:
            raise SystemExit(f"static names: {ENV['hex_runs']}: {e}") from e

    def r2_env_secrets(self) -> dict[str, str]:
        """`{env var: secret name}` for the R2 copy, checked complete."""
        out = {R2_SECRET_VARS[k]: v for k, v in self.r2_secrets.items()}
        if "R2_ENDPOINT" not in out and not self.r2_endpoint:
            raise SystemExit("static names: the R2 endpoint: set R2_ENDPOINT, or name its secret in STATIC_NAMES_R2_SECRETS (endpoint=…)")
        for part, k in (("key_id", "R2_ACCESS_KEY_ID"), ("secret", "R2_SECRET_ACCESS_KEY")):
            if k not in out:
                raise SystemExit(f"static names: STATIC_NAMES_R2_SECRETS needs {part}=<secret name>")
        return out


#: Each field's env var.
ENV = {
    "name": "STATIC_NAMES_PROFILE", "layouts": "STATIC_NAMES_LAYOUTS", "bucket": "STATIC_NAMES_BUCKET", "scratch": "STATIC_NAMES_SCRATCH",
    "gen": "STATIC_NAMES_GEN", "r2_bucket": "R2_BUCKET", "r2_secrets": "STATIC_NAMES_R2_SECRETS", "r2_endpoint": "R2_ENDPOINT",
    "project": "GCP_PROJECT", "region": "STATIC_NAMES_REGION", "image": "STATIC_NAMES_IMAGE", "sa": "STATIC_NAMES_SA",
    "r2_sa": "STATIC_NAMES_R2_SA", "machine": "STATIC_NAMES_MACHINE", "ssd_gb": "STATIC_NAMES_SSD", "spot": "STATIC_NAMES_SPOT",
    "machines": "STATIC_NAMES_MACHINES", "ssd_gbs": "STATIC_NAMES_SSDS",
    "standard_stages": "STATIC_NAMES_STANDARD_STAGES",
    "append_tasks": "STATIC_NAMES_APPEND_TASKS", "pipeline_depth": "STATIC_NAMES_PIPELINE_DEPTH", "measure_tasks": "STATIC_NAMES_MEASURE_TASKS", "anchor_tasks": "STATIC_NAMES_ANCHOR_TASKS", "fold_tasks": "STATIC_NAMES_FOLD_TASKS", "drill": "STATIC_NAMES_DRILL", "drill_checkpoint": "STATIC_NAMES_DRILL_CHECKPOINT", "anchors": "STATIC_NAMES_ANCHORS", "src": "STATIC_NAMES_SRC",
    "hex_runs": "STATIC_NAMES_HEX_RUNS", "compact_level": "STATIC_NAMES_COMPACT_LEVEL", "compact": "STATIC_NAMES_COMPACT", "read_from": "STATIC_NAMES_READ_FROM",
    "compact_jobs": "STATIC_NAMES_COMPACT_JOBS", "fold_jobs": "STATIC_NAMES_FOLD_JOBS", "record_secrets": "STATIC_NAMES_RECORD_SECRETS",
}


def parse_compact_level(var: str, raw: str) -> int | None:
    """`raw` (env var or option `var`) as a compaction level: an integer ≥ 1, or `none` (unbounded: None)."""
    if raw.strip().lower() == "none":
        return None
    try:
        v = int(raw)
    except ValueError as e:
        raise SystemExit(f"{var}: {raw!r} is not an integer ≥ 1 or none") from e
    if v < 1:
        raise SystemExit(f"{var}: {raw!r} is not an integer ≥ 1 or none")
    return v


def level_arg(level: int | None) -> str:
    """A compaction level as a CLI argument (`-L`): `none` when unbounded."""
    return "none" if level is None else str(level)


def _parse(name: str, raw: str):
    if name in ("layouts", "src", "standard_stages"):
        return tuple(s.strip() for s in raw.split(",") if s.strip())
    if name == "r2_secrets":
        out = {}
        for part in filter(None, (p.strip() for p in raw.split(","))):
            k, _, v = part.partition("=")
            if k.strip() not in R2_SECRET_VARS or not v.strip():
                raise SystemExit(f"STATIC_NAMES_R2_SECRETS: {part!r} is not endpoint=|key_id=|secret=<secret name>")
            out[k.strip()] = v.strip()
        return out
    if name in ("machines", "ssd_gbs", "record_secrets"):
        out = {}
        for part in filter(None, (p.strip() for p in raw.split(","))):
            k, _, v = part.partition("=")
            if not k.strip() or not v.strip():
                raise SystemExit(f"{ENV[name]}: {part!r} is not " + ("<VAR>=<secret name>" if name == "record_secrets" else "<stage>=<value>"))
            out[k.strip()] = int(v) if name == "ssd_gbs" else v.strip()
        return out
    if name == "compact_level":
        return parse_compact_level(ENV[name], raw)
    if name == "compact":
        from .append_runner import parse_policy

        return parse_policy(ENV[name], raw)
    if name in ("ssd_gb", "append_tasks", "pipeline_depth", "measure_tasks", "anchor_tasks", "fold_tasks", "compact_jobs", "fold_jobs"):
        return int(raw)
    if name in ("spot", "drill", "anchors"):
        return raw.strip() not in ("0", "false", "")
    return raw.strip()


def from_mapping(d: Mapping) -> Profile:
    """A profile from a mapping of its fields (a JSON file, an example); unknown keys are an error."""
    known = {f.name for f in fields(Profile)}
    if bad := sorted(set(d) - known):
        raise SystemExit(f"static names profile: unknown fields {bad}")
    d = dict(d)
    for k in ("layouts", "src", "standard_stages"):
        if k in d:
            d[k] = tuple(d[k])
    return Profile(**d)


def to_mapping(p: Profile) -> dict:
    """`p` as a JSON-able mapping of every field: what a deployment checks in as its profile file (`from_mapping`'s
    inverse)."""
    from .append_runner import Policy

    return {f.name: (list(v) if isinstance(v := getattr(p, f.name), tuple) else dict(v) if isinstance(v, Mapping) else
                     v.to_json() if isinstance(v, Policy) else v)
            for f in fields(Profile)}


def load_profile(env: Mapping[str, str] | None = None) -> Profile:
    """`STATIC_NAMES_PROFILE` (an example's name, a JSON file, or the JSON itself), then every field's env var over it."""
    from .static_profile_examples import EXAMPLES

    env = os.environ if env is None else env
    base = Profile()
    sel = (env.get("STATIC_NAMES_PROFILE") or "").strip()
    if sel in EXAMPLES:
        base = EXAMPLES[sel]
    elif sel.startswith("{"):
        # inline (`to_mapping`'s JSON): what the runner hands a compaction job that submits jobs of its own
        base = from_mapping(json.loads(sel))
    elif sel:
        if not os.path.exists(sel):
            raise SystemExit(f"STATIC_NAMES_PROFILE: {sel!r} is neither an example ({', '.join(sorted(EXAMPLES))}) nor a file")
        with open(sel) as fh:
            base = from_mapping({"name": os.path.basename(sel), **json.load(fh)})
    over = {k: _parse(k, env[v]) for k, v in ENV.items() if k != "name" and (env.get(v) or "").strip()}
    return replace(base, **over)


@cache
def profile() -> Profile:
    """The process's profile (`load_profile` over the environment, once)."""
    return load_profile()


def data_bucket() -> str:
    return profile().need("bucket")


def scratch_bucket() -> str:
    return profile().need("scratch")


def layouts() -> tuple[str, ...]:
    return profile().need("layouts")
