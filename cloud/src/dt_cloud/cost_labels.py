"""Cost-attribution labels for everything a deployment creates on GCP.

One setting, from the env: ``DISKY_LABELS`` (``k=v,k=v``, e.g.
``app=disky,deployment=gcs``). Each job adds its own ``component`` (``scan``,
``listing``, ``static-names``, ``drill``, ``anchors``, ``sweep``, …), so a
billing export groups a shared project's spend by deployment and component
(specs/cost-labels.md).

Nothing here names a deployment. Unset ``DISKY_LABELS`` = no labels at all, and
every spec stays byte-identical to an unlabeled one.

Batch: ``allocationPolicy.labels`` is what reaches the VMs, disks and GPUs a job
creates (their Compute cost lines in the billing export); the job's own
``labels`` stay on the job. ``label_batch_spec`` sets both.
"""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy

ENV = "DISKY_LABELS"
COMPONENT = "component"

# GCP label rules: ≤64 labels; keys start with a lowercase letter; keys and values are
# ≤63 chars of lowercase letters, digits, `_` and `-` (values may be empty).
KEY_RE = re.compile(r"[a-z][a-z0-9_-]{0,62}")
VALUE_RE = re.compile(r"[a-z0-9_-]{0,63}")
MAX_LABELS = 64


def parse_labels(text: str) -> dict[str, str]:
    """``"k=v,k2=v2"`` → ``{"k": "v", "k2": "v2"}``; raises on anything GCP would refuse."""
    out: dict[str, str] = {}
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"{ENV}: {item!r} is not k=v")
        k, v = (s.strip() for s in item.split("=", 1))
        check_label(k, v)
        if k in out:
            raise ValueError(f"{ENV}: duplicate key {k!r}")
        out[k] = v
    if len(out) > MAX_LABELS:
        raise ValueError(f"{ENV}: {len(out)} labels (GCP allows {MAX_LABELS})")
    return out


def check_label(key: str, value: str) -> None:
    if not KEY_RE.fullmatch(key):
        raise ValueError(f"label key {key!r}: must match {KEY_RE.pattern}")
    if not VALUE_RE.fullmatch(value):
        raise ValueError(f"label {key}={value!r}: value must match {VALUE_RE.pattern}")


def env_labels(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The deployment's labels (``$DISKY_LABELS``); ``{}`` when unset or empty."""
    return parse_labels((environ if environ is not None else os.environ).get(ENV, ""))


def cost_labels(component: str | None = None, environ: dict[str, str] | None = None) -> dict[str, str]:
    """The deployment's labels plus ``component``; ``{}`` when ``$DISKY_LABELS`` is unset (opt-in)."""
    labels = env_labels(environ)
    if labels and component:
        check_label(COMPONENT, component)
        labels[COMPONENT] = component
    return labels


def label_batch_spec(spec: dict, component: str, environ: dict[str, str] | None = None) -> dict:
    """A copy of a Batch job spec with the cost labels on the job and its allocation policy.

    A spec's own keys (e.g. ``purpose``, ``stage``) are kept; a cost label of the same key overrides
    them. Unset ``$DISKY_LABELS`` returns the spec unchanged.
    """
    labels = cost_labels(component, environ)
    if not labels:
        return spec
    out = deepcopy(spec)
    out["labels"] = {**out.get("labels", {}), **labels}
    alloc = out.setdefault("allocationPolicy", {})
    alloc["labels"] = {**alloc.get("labels", {}), **labels}
    return out


def gcloud_flag(labels: dict[str, str]) -> str:
    """``k=v,k2=v2``, for ``gcloud … --update-labels`` / ``--labels``."""
    return ",".join(f"{k}={v}" for k, v in labels.items())


def render(component: str | None, fmt: str, environ: dict[str, str] | None = None) -> str:
    labels = cost_labels(component, environ)
    return json.dumps(labels, sort_keys=True) if fmt == "json" else gcloud_flag(labels)
