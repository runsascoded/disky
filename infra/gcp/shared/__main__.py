"""The `shared` stack: GCP resources labelled `deployment=shared`, owned by the root session.

Today one thing: the VM pilot's box (specs/vm-pilot.md, `vm_box.VmBox`), from the `vmPilot` config object. Its own
Pulumi project (`disky-shared-gcp`), so it never meets a deployment branch's `infra/gcp/Pulumi.yaml` on a merge.

Secret values are never managed: the stack makes the box's containers (the tunnel token, each tenant's `vmd` key) and
its accessor grants; the values go in with `gcloud secrets versions add` (specs/vm-pilot.md "Runbook").

`pulumi preview` is read-only; `up` is a human's call.
"""

import json
import re
import sys
from pathlib import Path

import pulumi

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gcp_jobs import labeled_provider, parse_labels  # noqa: E402
from vm_box import Box, VmBox, tenants_from_config  # noqa: E402

STACK = "shared"
ROOT = Path(__file__).resolve().parents[3]
BOOTSTRAP = ROOT / "deploy/vm-pilot/bootstrap.sh"
SHA = re.compile(r"[0-9a-f]{40}")

if pulumi.get_stack() != STACK:
    raise ValueError(f"infra/gcp/shared manages only the {STACK!r} stack, not {pulumi.get_stack()!r}")
cfg = pulumi.Config()
gcp_cfg = pulumi.Config("gcp")
project = gcp_cfg.require("project")
region = gcp_cfg.require("region")
# The stack's cost labels (specs/done/cost-labels.md), from its config rather than `$DISKY_LABELS`: the root session's
# shell is no deployment's. Required, so an `up` can't strip them.
labels = parse_labels(cfg.require("labels"))
if labels.get("deployment") != STACK or "app" not in labels:
    raise ValueError(f"labels: {cfg.require('labels')!r} needs app= and deployment={STACK}")
labeled_provider(f"{STACK}-labeled", project=project, region=region, labels=labels)

pilot = cfg.require_object("vmPilot")
tenants = tenants_from_config(pilot["tenants"])
if not pilot["zone"].startswith(f"{region}-"):
    raise ValueError(f"vmPilot.zone {pilot['zone']!r} isn't in gcp:region {region!r}")
names = [t.name for t in tenants]
if len(set(names)) != len(names):
    raise ValueError(f"vmPilot.tenants: duplicate names in {names}")
if not SHA.fullmatch(pilot["ref"]):
    pulumi.log.warn(f"vmPilot.ref {pilot['ref']!r} is not a full sha: each boot checks out whatever it names then")
own = {pilot["tunnelSecret"]: "the Cloudflare Tunnel's connector token (cloudflared)"}
own.update({t["vmdKeySecret"]: f"vmd's HMAC key for tenant {t['name']} (= its site's VM_KEY)" for t in pilot["tenants"]})

# The box's own config, read by `bootstrap.sh` from the metadata server: names only, never a secret's value. The pack
# sync reads `pack_secrets` as the tenant's `job_account` (impersonated), never as the box.
box_tenants = [
    {
        "name": t["name"],
        "job_account": t["jobAccount"],
        "vmd_key_secret": t["vmdKeySecret"],
        "store": t["pack"]["store"],
        "pack_env": t["pack"].get("env") or {},
        "pack_secrets": t["pack"].get("secrets") or {},
    }
    for t in pilot["tenants"]
]
box = VmBox(
    "vm-pilot",
    project=project,
    region=region,
    box=Box(
        account_id=pilot["accountId"],
        zone=pilot["zone"],
        machine_type=pilot["machineType"],
        cidr=pilot["cidr"],
        boot_disk_gb=int(pilot.get("bootDiskGb", 20)),
        data_disk_gb=int(pilot.get("dataDiskGb", 50)),
        tenants=tenants,
        own_secrets=own,
    ),
    component="vm-pilot",
    startup_script=BOOTSTRAP.read_text(),
    metadata={
        "disky-repo": pilot["repo"],
        "disky-ref": pilot["ref"],
        "disky-tunnel-secret": pilot["tunnelSecret"],
        "disky-tenants": json.dumps(box_tenants, sort_keys=True),
        # The hosts whose pulls go through the job's own token (`docker-credential-disky`): the tenants' job images
        "disky-registries": ",".join(sorted(pilot["registries"])),
        "disky-scan-memory-max": str(pilot.get("scanMemoryMax", "8G")),
        "disky-serve-memory-min": str(pilot.get("serveMemoryMin", "3G")),
    },
)

pulumi.export("vm_pilot_account", box.account.email)
pulumi.export("vm_pilot_instance", box.instance.name)
pulumi.export("vm_pilot_zone", pilot["zone"])
pulumi.export("vm_pilot_secrets", sorted(own))
