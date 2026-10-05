"""A laptop deployment's AWS ingest, from config alone: one `BatchIngest`.

Every laptop deployment runs this same program; only its `Pulumi.<stack>.yaml`
differs (`Pulumi.stack.example.yaml` lists every key). A deployment's
`infra/aws/Pulumi.yaml` points here with `main: stack/` (`Pulumi.yaml.example`).

The laptop only walks (`disk-tree capture` → layer-1 shards in R2); the
component owns what runs after: the arm64 ingest image built on CodeBuild, the
Fargate Spot queue, the job definition, and the capture trigger's IAM user.
Secrets are created empty and filled out of band, so values never enter state.
"""
import sys
from pathlib import Path

import pulumi

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from batch_ingest import IngestSpec, BatchIngest  # noqa: E402

cfg = pulumi.Config()
REPO_ROOT = Path(__file__).resolve().parents[3]

ingest = BatchIngest(
    "ingest",
    IngestSpec(
        prefix=cfg.require("prefix"),
        repo_root=REPO_ROOT,
        sources=tuple(cfg.require_object("sources")),
        dockerfile=cfg.require("dockerfile"),
        vcpu=cfg.get("vcpu") or "4",
        memory_mib=cfg.get("memory_mib") or "16384",
        ephemeral_gib=cfg.get_int("ephemeral_gib") or 50,
        max_vcpus=cfg.get_int("max_vcpus") or 16,
        timeout_s=cfg.get_int("timeout_s") or 2 * 3600,
        env=cfg.get_object("env") or {},
        secrets=cfg.get_object("secrets") or {},
    ),
    # True for a stack that declared these resources inline before the component.
    moved_from_root=cfg.get_bool("moved_from_root") or False,
)

pulumi.export("queue", ingest.queue.name)
pulumi.export("job_definition", ingest.job_def.name)
pulumi.export("image", ingest.image)
pulumi.export("log_group", ingest.log_group.name)
pulumi.export("capture_trigger_user", ingest.trigger_user.name)
