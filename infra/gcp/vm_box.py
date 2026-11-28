"""A standing VM that serves and scans for several deployments (specs/vm-pilot.md "The box").

The shape and none of the names: a stack program (`shared/__main__.py`, the VM pilot's) instantiates it.

- **Its own account**, holding only what the box's work needs: log writing, `secretAccessor` on the box's own secrets
  (the tunnel token, each tenant's `vmd` key) and on each tenant's scan secrets (other stacks' containers, granted
  here additively), `objectAdmin` on each tenant's data buckets, read on each tenant's image repositories. The local
  job backend runs every tenant's jobs with these credentials (specs/vm-pilot.md "Identity").
- **No ingress.** A VPC of its own (so the default network's `0.0.0.0/0` SSH / RDP / ICMP rules never reach it), whose
  only ingress rule is IAP's TCP forwarding to port 22. No external address: egress through a Cloud NAT on the VPC's one
  subnet, Google APIs through Private Google Access. Serving reaches the box over its outbound Cloudflare Tunnel.
- **A data disk** (packs, job state, Docker's images, the checkout) separate from the boot disk, so a replaced
  instance keeps it.
- **A startup script** run at every boot (the tracked `deploy/vm-pilot/bootstrap.sh`), configured by the instance's
  non-secret metadata; secrets are read from Secret Manager on the box, never put in metadata.

Labels: every labelable resource carries `component` beside the provider's default `app` / `deployment`; networks,
firewall rules, routers, NAT and IAM members take none in GCP.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import pulumi
import pulumi_gcp as gcp
from pulumi import ComponentResource, ResourceOptions

from gcp_jobs import Adopt, JobAccount, Secrets, grant_bucket, grant_secret

#: IAP's TCP-forwarding range: the one ingress source (`gcloud compute ssh --tunnel-through-iap`).
IAP_RANGE = "35.235.240.0/20"
#: The data disk's device name: `/dev/disk/by-id/google-<this>` on the box (`bootstrap.sh`).
DATA_DEVICE = "disky-data"


@dataclass(frozen=True)
class Repository:
    """An Artifact Registry repository a tenant's job images come from."""

    location: str
    name: str
    project: str | None = None


@dataclass(frozen=True)
class Tenant:
    """A deployment the box serves and scans for: `name` is `vmd`'s tenant and the local job backend's (a job spec's
    `deployment` label)."""

    name: str
    #: Existing Secret Manager ids (the tenant's own stack's) its jobs and its pack sync read: `secretAccessor` on each.
    secrets: tuple[str, ...] = ()
    #: Buckets its jobs read and write (its data store, Nextflow's work dir): `objectAdmin` on each.
    buckets: tuple[str, ...] = ()
    #: Repositories its job images come from: `artifactregistry.reader` on each.
    repositories: tuple[Repository, ...] = ()


@dataclass(frozen=True)
class Box:
    account_id: str
    zone: str
    machine_type: str
    cidr: str
    boot_disk_gb: int = 20
    data_disk_gb: int = 50
    disk_type: str = "pd-balanced"
    image: str = "debian-cloud/debian-12"
    tenants: tuple[Tenant, ...] = ()
    #: The box's own secret containers (created here, values added by hand) → their purpose, for the description.
    own_secrets: Mapping[str, str] = field(default_factory=dict)


class VmBox(ComponentResource):
    def __init__(
        self,
        name: str,
        *,
        project: str,
        region: str,
        box: Box,
        component: str,
        startup_script: str,
        metadata: Mapping[str, pulumi.Input[str]],
        opts: ResourceOptions | None = None,
    ):
        super().__init__("disky:gcp:VmBox", name, None, opts)
        labels = {"component": component}
        child = ResourceOptions(parent=self)
        adopt = Adopt(False)
        self.account = JobAccount(
            name,
            project=project,
            account_id=box.account_id,
            display_name=f"disky {name}",
            description=f"The {name} VM: serves vmd and runs its tenants' scans ({', '.join(t.name for t in box.tenants)})",
            roles=["roles/logging.logWriter"],
            acts_as_self=False,
            adopt=adopt,
            opts=child,
        )
        member, email = self.account.member, self.account.email_literal
        self.secrets = Secrets(
            f"{name}-secrets",
            project=project,
            secrets={sid: labels for sid in box.own_secrets},
            accessor=member,
            accessor_email=email,
            adopt=adopt,
            opts=child,
        )
        grants: list[pulumi.Resource] = [self.secrets]
        for t in box.tenants:
            for sid in t.secrets:
                grants.append(grant_secret(f"{name}-{t.name}-{sid}", project=project, secret_id=sid, member=member,
                                           member_email=email, adopt=adopt, parent=self))
            for b in t.buckets:
                grants.append(grant_bucket(f"{name}-{t.name}-{b}", bucket=b, role="roles/storage.objectAdmin",
                                           member=member, member_email=email, adopt=adopt, parent=self))
            for r in t.repositories:
                grants.append(gcp.artifactregistry.RepositoryIamMember(
                    f"{name}-{t.name}-{r.name}",
                    project=r.project or project,
                    location=r.location,
                    repository=r.name,
                    role="roles/artifactregistry.reader",
                    member=member,
                    opts=child,
                ))

        base = box.account_id  # every GCP name: the account's, `disky-<name>`
        self.network = gcp.compute.Network(
            name,
            name=base,
            project=project,
            auto_create_subnetworks=False,
            description=f"The {name} VM's own VPC: no ingress but IAP SSH",
            opts=child,
        )
        self.subnet = gcp.compute.Subnetwork(
            name,
            name=base,
            project=project,
            region=region,
            network=self.network.id,
            ip_cidr_range=box.cidr,
            private_ip_google_access=True,
            opts=child,
        )
        self.iap_ssh = gcp.compute.Firewall(
            f"{name}-iap-ssh",
            name=f"{base}-iap-ssh",
            project=project,
            network=self.network.id,
            direction="INGRESS",
            priority=1000,
            source_ranges=[IAP_RANGE],
            target_service_accounts=[self.account.email],
            allows=[gcp.compute.FirewallAllowArgs(protocol="tcp", ports=["22"])],
            description="IAP TCP forwarding to SSH only; everything else is the VPC's implied deny",
            opts=child,
        )
        self.router = gcp.compute.Router(name, name=base, project=project, region=region, network=self.network.id, opts=child)
        self.nat = gcp.compute.RouterNat(
            name,
            name=base,
            project=project,
            region=region,
            router=self.router.name,
            nat_ip_allocate_option="AUTO_ONLY",
            source_subnetwork_ip_ranges_to_nat="LIST_OF_SUBNETWORKS",
            subnetworks=[gcp.compute.RouterNatSubnetworkArgs(name=self.subnet.id, source_ip_ranges_to_nats=["ALL_IP_RANGES"])],
            log_config=gcp.compute.RouterNatLogConfigArgs(enable=True, filter="ERRORS_ONLY"),
            opts=child,
        )

        self.data_disk = gcp.compute.Disk(
            f"{name}-data",
            name=f"{base}-data",
            project=project,
            zone=box.zone,
            type=box.disk_type,
            size=box.data_disk_gb,
            labels=labels,
            description=f"The {name} VM's packs, job state, images and checkout",
            opts=child,
        )
        self.instance = gcp.compute.Instance(
            name,
            name=base,
            project=project,
            zone=box.zone,
            machine_type=box.machine_type,
            boot_disk=gcp.compute.InstanceBootDiskArgs(
                initialize_params=gcp.compute.InstanceBootDiskInitializeParamsArgs(
                    image=box.image, size=box.boot_disk_gb, type=box.disk_type, labels=labels,
                ),
            ),
            attached_disks=[gcp.compute.InstanceAttachedDiskArgs(source=self.data_disk.id, device_name=DATA_DEVICE)],
            network_interfaces=[gcp.compute.InstanceNetworkInterfaceArgs(subnetwork=self.subnet.id)],
            service_account=gcp.compute.InstanceServiceAccountArgs(
                email=self.account.email, scopes=["https://www.googleapis.com/auth/cloud-platform"],
            ),
            # `startup-script` as a metadata key (not `metadata_startup_script`, which replaces the instance on a change):
            # an edit lands in place and runs at the next boot or `google_metadata_script_runner startup`.
            metadata={
                "enable-oslogin": "TRUE",
                "block-project-ssh-keys": "TRUE",
                "startup-script": startup_script,
                **metadata,
            },
            shielded_instance_config=gcp.compute.InstanceShieldedInstanceConfigArgs(
                enable_secure_boot=True, enable_vtpm=True, enable_integrity_monitoring=True,
            ),
            scheduling=gcp.compute.InstanceSchedulingArgs(automatic_restart=True, on_host_maintenance="MIGRATE"),
            labels=labels,
            allow_stopping_for_update=True,
            description=f"disky {name}: vmd (serve.slice) + the local job backend (scan.slice); specs/vm-pilot.md",
            # Its first boot reads secrets and pulls images: the grants and the NAT come first.
            opts=ResourceOptions(parent=self, depends_on=[*grants, self.nat, self.iap_ssh]),
        )
        self.register_outputs({"email": self.account.email, "instance": self.instance.name})


def tenants_from_config(items: Sequence[Mapping]) -> tuple[Tenant, ...]:
    """The stack config's `tenants` list (`{name, secrets, buckets, repositories: [{location, name, project?}]}`)."""
    return tuple(
        Tenant(
            name=t["name"],
            secrets=tuple(t.get("secrets") or ()),
            buckets=tuple(t.get("buckets") or ()),
            repositories=tuple(Repository(**r) for r in t.get("repositories") or ()),
        )
        for t in items
    )
