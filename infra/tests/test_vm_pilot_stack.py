"""The `shared` stacks' VM pilot (`infra/gcp/shared/`, `infra/cf/shared/`) under Pulumi's mocks, from their committed
`Pulumi.shared.yaml`: every resource by type and name, the box's only ingress, its grants, labels and metadata, and the
tunnel's one route. No account needed.

    UV_PROJECT_ENVIRONMENT=infra/.venv uv run --project infra --with pytest pytest infra/tests/test_vm_pilot_stack.py
"""

import json
import runpy
import sys
from pathlib import Path

import pulumi
import pytest
import yaml

INFRA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(INFRA / "gcp"))
import gcp_jobs  # noqa: E402

GCP = INFRA / "gcp" / "shared"
CF = INFRA / "cf" / "shared"
PROJECT = "oa-internal-450019"
EMAIL = f"disky-vm-pilot@{PROJECT}.iam.gserviceaccount.com"
MEMBER = f"serviceAccount:{EMAIL}"
R2_SECRETS = [
    "oa-r2-source-access-key-id", "oa-r2-source-secret-access-key", "oa-r2-index-access-key-id",
    "oa-r2-index-secret-access-key", "oa-r2-d1-token", "oa-r2-site-grant", "oa-r2-slack-bot-token",
    "oa-r2-cf-analytics-token", "oa-r2-cf-pages-token",
]
HF_SECRETS = [
    "hf-usage-hf-token", "hf-usage-hf-lfs-token", "hf-usage-cf-token", "hf-usage-cf-pages-token", "hf-usage-cf-account-id",
    "hf-usage-r2-endpoint", "hf-usage-r2-access-key-id", "hf-usage-r2-secret-access-key", "hf-usage-slack-bot-token",
    "hf-usage-job-grant", "hf-usage-cf-analytics-token",
]
OWN = ["vm-pilot-tunnel-token", "vm-pilot-vmd-key-oa-r2", "vm-pilot-vmd-key-hf"]


class Mocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.resources: dict[tuple[str, str], dict] = {}
        self.calls: list[tuple[str, dict]] = []

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources[(args.typ, args.name)] = args.inputs
        state = dict(args.inputs)
        if args.typ == "gcp:serviceaccount/account:Account":
            state["email"] = f"{args.inputs['accountId']}@{args.inputs['project']}.iam.gserviceaccount.com"
        return f"{args.name}-id", state

    def call(self, args: pulumi.runtime.MockCallArgs):
        self.calls.append((args.token, dict(args.args)))
        return {"token": "fixture-token"} if args.token.endswith("getZeroTrustTunnelCloudflaredToken") else {}


def run(program: Path, project: str, monkeypatch, environ: dict[str, str] | None = None) -> tuple[Mocks, dict]:
    """`program` under mocks with its committed `Pulumi.shared.yaml`."""
    config = yaml.safe_load((program / "Pulumi.shared.yaml").read_text())["config"]
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project=project, stack="shared", preview=False)
    pulumi.runtime.set_all_config({(k if ":" in k else f"{project}:{k}"): v if isinstance(v, str) else json.dumps(v)
                                   for k, v in config.items()})
    for k, v in (environ or {}).items():
        monkeypatch.setenv(k, v)
    return mocks, runpy.run_path(str(program / "__main__.py"))


@pulumi.runtime.test
def test_gcp_box(monkeypatch):
    seen: dict = {}
    # The stack transform needs the engine's callbacks, which mocks don't serve: record the labels it would apply.
    monkeypatch.setattr(gcp_jobs, "labeled_provider", lambda name, **kw: seen.update(name=name, **kw))
    mocks, g = run(GCP, "disky-shared-gcp", monkeypatch)
    box = g["box"]

    def check(_: object) -> None:
        assert seen == {"name": "shared-labeled", "project": PROJECT, "region": "us-west1",
                        "labels": {"app": "disky", "deployment": "shared"}}
        by_type: dict[str, list[str]] = {}
        for typ, name in mocks.resources:
            by_type.setdefault(typ, []).append(name)
        assert {t: sorted(n) for t, n in by_type.items()} == {
            "disky:gcp:VmBox": ["vm-pilot"],
            "disky:gcp:JobAccount": ["vm-pilot"],
            "gcp:serviceaccount/account:Account": ["vm-pilot"],
            "gcp:projects/iAMMember:IAMMember": ["vm-pilot-logging.logWriter"],
            "disky:gcp:Secrets": ["vm-pilot-secrets"],
            "gcp:secretmanager/secret:Secret": sorted(OWN),
            "gcp:secretmanager/secretIamMember:SecretIamMember": sorted(
                [f"{s}-accessor" for s in OWN] + [f"vm-pilot-oa-r2-{s}" for s in R2_SECRETS] + [f"vm-pilot-hf-{s}" for s in HF_SECRETS]),
            "gcp:storage/bucketIAMMember:BucketIAMMember": ["vm-pilot-hf-oa-hf-usage-data", "vm-pilot-oa-r2-oa-r2-usage-data"],
            "gcp:artifactregistry/repositoryIamMember:RepositoryIamMember": ["vm-pilot-hf-hf-usage", "vm-pilot-oa-r2-oa-r2-usage"],
            "gcp:compute/network:Network": ["vm-pilot"],
            "gcp:compute/subnetwork:Subnetwork": ["vm-pilot"],
            "gcp:compute/firewall:Firewall": ["vm-pilot-iap-ssh"],
            "gcp:compute/router:Router": ["vm-pilot"],
            "gcp:compute/routerNat:RouterNat": ["vm-pilot"],
            "gcp:compute/disk:Disk": ["vm-pilot-data"],
            "gcp:compute/instance:Instance": ["vm-pilot"],
        }
        r = mocks.resources
        assert r[("gcp:projects/iAMMember:IAMMember", "vm-pilot-logging.logWriter")] == {
            "project": PROJECT, "role": "roles/logging.logWriter", "member": MEMBER}
        # Every secret grant is `secretAccessor` for the box's account, on exactly its own and its tenants' secrets.
        grants = [v for (t, _), v in r.items() if t == "gcp:secretmanager/secretIamMember:SecretIamMember"]
        assert sorted((v["secretId"], v["role"], v["member"]) for v in grants) == sorted(
            (s, "roles/secretmanager.secretAccessor", MEMBER) for s in OWN + R2_SECRETS + HF_SECRETS)
        assert {r[("gcp:secretmanager/secret:Secret", s)]["labels"]["component"] for s in OWN} == {"vm-pilot"}
        assert sorted((v["bucket"], v["role"], v["member"]) for (t, _), v in r.items() if t == "gcp:storage/bucketIAMMember:BucketIAMMember") == [
            ("oa-hf-usage-data", "roles/storage.objectAdmin", MEMBER), ("oa-r2-usage-data", "roles/storage.objectAdmin", MEMBER)]
        assert sorted((v["location"], v["repository"], v["role"]) for (t, _), v in r.items()
                      if t == "gcp:artifactregistry/repositoryIamMember:RepositoryIamMember") == [
            ("us-central1", "hf-usage", "roles/artifactregistry.reader"), ("us-west1", "oa-r2-usage", "roles/artifactregistry.reader")]
        # No ingress but IAP's SSH: one rule, on its own VPC (no default-network rule reaches it).
        fw = r[("gcp:compute/firewall:Firewall", "vm-pilot-iap-ssh")]
        assert {k: fw[k] for k in ("name", "direction", "sourceRanges", "targetServiceAccounts", "allows")} == {
            "name": "disky-vm-pilot-iap-ssh", "direction": "INGRESS", "sourceRanges": ["35.235.240.0/20"], "targetServiceAccounts": [EMAIL],
            "allows": [{"protocol": "tcp", "ports": ["22"]}]}
        assert r[("gcp:compute/network:Network", "vm-pilot")]["autoCreateSubnetworks"] is False
        sub = r[("gcp:compute/subnetwork:Subnetwork", "vm-pilot")]
        assert (sub["ipCidrRange"], sub["privateIpGoogleAccess"], sub["region"]) == ("10.64.0.0/24", True, "us-west1")
        disk = r[("gcp:compute/disk:Disk", "vm-pilot-data")]
        assert {k: disk[k] for k in ("name", "zone", "type", "size", "labels")} == {
            "name": "disky-vm-pilot-data", "zone": "us-west1-b", "type": "pd-balanced", "size": 50, "labels": {"component": "vm-pilot"}}
        vm = r[("gcp:compute/instance:Instance", "vm-pilot")]
        assert {k: vm[k] for k in ("name", "machineType", "zone", "labels", "networkInterfaces", "serviceAccount", "attachedDisks")} == {
            "name": "disky-vm-pilot",
            "machineType": "e2-standard-4",
            "zone": "us-west1-b",
            "labels": {"component": "vm-pilot"},
            # no `accessConfigs`: no external address
            "networkInterfaces": [{"subnetwork": "vm-pilot-id"}],
            "serviceAccount": {"email": EMAIL, "scopes": ["https://www.googleapis.com/auth/cloud-platform"]},
            "attachedDisks": [{"source": "vm-pilot-data-id", "deviceName": "disky-data"}],
        }
        assert vm["bootDisk"]["initializeParams"] == {"image": "debian-cloud/debian-12", "size": 20, "type": "pd-balanced",
                                                       "labels": {"component": "vm-pilot"}}
        md = vm["metadata"]
        assert md["startup-script"] == (INFRA.parent / "deploy/vm-pilot/bootstrap.sh").read_text()
        assert {k: v for k, v in md.items() if k not in ("startup-script", "disky-tenants")} == {
            "enable-oslogin": "TRUE",
            "block-project-ssh-keys": "TRUE",
            "disky-repo": "https://github.com/Open-Athena/disky.git",
            "disky-ref": "cloud",
            "disky-tunnel-secret": "vm-pilot-tunnel-token",
            "disky-registries": "us-central1-docker.pkg.dev,us-west1-docker.pkg.dev",
            "disky-scan-memory-max": "8G",
            "disky-serve-memory-min": "3G",
        }
        assert json.loads(md["disky-tenants"]) == [
            {"name": "oa-r2", "vmd_key_secret": "vm-pilot-vmd-key-oa-r2", "store": "r2://oa-r2-usage-index",
             "pack_env": {"AWS_DEFAULT_REGION": "auto",
                          "DISKY_R2_ENDPOINT_URL": "https://74981a43be0de7712369306c7b19133d.r2.cloudflarestorage.com"},
             "pack_secrets": {"AWS_ACCESS_KEY_ID": "oa-r2-index-access-key-id",
                              "AWS_SECRET_ACCESS_KEY": "oa-r2-index-secret-access-key"}},
            {"name": "hf", "vmd_key_secret": "vm-pilot-vmd-key-hf", "store": "r2://oa-hf-usage-index",
             "pack_env": {"AWS_DEFAULT_REGION": "auto"},
             "pack_secrets": {"AWS_ACCESS_KEY_ID": "hf-usage-r2-access-key-id",
                              "AWS_SECRET_ACCESS_KEY": "hf-usage-r2-secret-access-key",
                              "DISKY_R2_ENDPOINT_URL": "hf-usage-r2-endpoint"}},
        ]

    return pulumi.Output.all(box.instance.id, box.nat.id, *[s.id for s in box.secrets.grants.values()]).apply(check)


@pulumi.runtime.test
def test_cf_tunnel(monkeypatch):
    mocks, g = run(CF, "disky-shared-cf", monkeypatch, {"CLOUDFLARE_ACCOUNT_ID": "acct", "CF_ZONE_ID": "zone"})

    def check(_: object) -> None:
        assert mocks.resources == {
            ("cloudflare:index/zeroTrustTunnelCloudflared:ZeroTrustTunnelCloudflared", "vm-pilot"): {
                "accountId": "acct", "name": "disky-vm-pilot", "configSrc": "cloudflare"},
            ("cloudflare:index/zeroTrustTunnelCloudflaredConfig:ZeroTrustTunnelCloudflaredConfig", "vm-pilot"): {
                "accountId": "acct", "tunnelId": "vm-pilot-id",
                "config": {"ingresses": [{"hostname": "vmd-pilot.oa.dev", "service": "http://localhost:7340"},
                                         {"service": "http_status:404"}]}},
            ("cloudflare:index/dnsRecord:DnsRecord", "vm-pilot"): {
                "zoneId": "zone", "name": "vmd-pilot.oa.dev", "type": "CNAME", "content": "vm-pilot-id.cfargotunnel.com",
                "ttl": 1, "proxied": True,
                "comment": "disky VM pilot: vmd via Cloudflare Tunnel (specs/vm-pilot.md); HMAC per tenant, no user traffic"},
        }
        assert mocks.calls == [("cloudflare:index/getZeroTrustTunnelCloudflaredToken:getZeroTrustTunnelCloudflaredToken",
                                {"accountId": "acct", "tunnelId": "vm-pilot-id"})]

    return pulumi.Output.all(g["route"].id, g["cname"].id, g["token"].token).apply(check)


def test_cf_needs_its_ids(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CF_ZONE_ID", raising=False)
    with pytest.raises(pulumi.RunError) as e:
        run(CF, "disky-shared-cf", monkeypatch)
    assert str(e.value) == "accountId: set $CLOUDFLARE_ACCOUNT_ID (kept out of the public repo), or `pulumi config set accountId`"
