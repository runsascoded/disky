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
    spot: bool = True
    #: Stages (a Batch job's `stage` label: `anchors`, `drill`, `append`, …) that run STANDARD even when `spot`: a long
    #: single-task stage restarts from zero on each preemption, so spot's discount can cost more than it saves.
    standard_stages: tuple[str, ...] = ()
    append_tasks: int = 16
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

    def __post_init__(self):
        if self.compact_level is not None and (isinstance(self.compact_level, bool) or not isinstance(self.compact_level, int) or self.compact_level < 1):
            raise SystemExit(f"compact_level must be an integer ≥ 1 or none, not {self.compact_level!r}")

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
    "standard_stages": "STATIC_NAMES_STANDARD_STAGES",
    "append_tasks": "STATIC_NAMES_APPEND_TASKS", "drill": "STATIC_NAMES_DRILL", "drill_checkpoint": "STATIC_NAMES_DRILL_CHECKPOINT", "anchors": "STATIC_NAMES_ANCHORS", "src": "STATIC_NAMES_SRC",
    "hex_runs": "STATIC_NAMES_HEX_RUNS", "compact_level": "STATIC_NAMES_COMPACT_LEVEL",
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
    if name == "compact_level":
        return parse_compact_level(ENV[name], raw)
    if name in ("ssd_gb", "append_tasks"):
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
    return {f.name: (list(v) if isinstance(v := getattr(p, f.name), tuple) else dict(v) if isinstance(v, Mapping) else v)
            for f in fields(Profile)}


def load_profile(env: Mapping[str, str] | None = None) -> Profile:
    """`STATIC_NAMES_PROFILE` (an example's name or a JSON file), then every field's env var over it."""
    from .static_profile_examples import EXAMPLES

    env = os.environ if env is None else env
    base = Profile()
    sel = (env.get("STATIC_NAMES_PROFILE") or "").strip()
    if sel in EXAMPLES:
        base = EXAMPLES[sel]
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
