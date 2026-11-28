"""The `shared` stack's Cloudflare side: the VM pilot's tunnel (specs/vm-pilot.md).

- **The tunnel** (`disky-vm-pilot`, remotely managed): the box's `cloudflared` dials out with the tunnel's token (in
  Secret Manager, `vm-pilot-tunnel-token`; the box reads it at start), so the box takes no inbound connection.
- **Its one route**: `hostname` → `http://localhost:7340` (`vmd`, which listens on loopback only); anything else 404s at
  the connector. A proxied CNAME to `<tunnel id>.cfargotunnel.com` makes the hostname resolve. Nobody's page links to
  it: the sites' Functions call it, and every `/v1/*` request must carry its tenant's HMAC (`vmd`'s check, the key the
  site's `VM_KEY`); `/healthz` alone answers unsigned, with `ok`.
- **The token** is an output (secret) for the runbook to pipe into Secret Manager, never printed:
  `pulumi stack output tunnel_token --show-secrets | gcloud secrets versions add vm-pilot-tunnel-token --data-file=-`.

The account and zone ids stay out of the public repo: unless the stack config sets them, they come from
`$CLOUDFLARE_ACCOUNT_ID` and `$CF_ZONE_ID` (as hf's stack). `pulumi preview` is read-only; `up` is a human's call.
"""

import os

import pulumi
import pulumi_cloudflare as cloudflare

STACK = "shared"
ID_ENV = {"accountId": "CLOUDFLARE_ACCOUNT_ID", "zoneId": "CF_ZONE_ID"}
#: `vmd`'s listen address (specs/vm-mode.md §5's default, `deploy/vm-pilot/bootstrap.sh`'s vmd.json).
VMD = "http://localhost:7340"

if pulumi.get_stack() != STACK:
    raise ValueError(f"infra/cf/shared manages only the {STACK!r} stack, not {pulumi.get_stack()!r}")
cfg = pulumi.Config()


def config_id(key: str) -> str:
    """`accountId` / `zoneId`: the stack config's, else the environment's."""
    if (value := cfg.get(key)) is not None:
        return value
    value = os.environ.get(ID_ENV[key], "").strip()
    if not value:
        raise pulumi.RunError(f"{key}: set ${ID_ENV[key]} (kept out of the public repo), or `pulumi config set {key}`")
    return value


account_id = config_id("accountId")
zone_id = config_id("zoneId")
pilot = cfg.require_object("vmPilot")
hostname = pilot["hostname"]

tunnel = cloudflare.ZeroTrustTunnelCloudflared(
    "vm-pilot",
    account_id=account_id,
    name=pilot["tunnelName"],
    config_src="cloudflare",
)
route = cloudflare.ZeroTrustTunnelCloudflaredConfig(
    "vm-pilot",
    account_id=account_id,
    tunnel_id=tunnel.id,
    config=cloudflare.ZeroTrustTunnelCloudflaredConfigConfigArgs(
        ingresses=[
            cloudflare.ZeroTrustTunnelCloudflaredConfigConfigIngressArgs(hostname=hostname, service=VMD),
            cloudflare.ZeroTrustTunnelCloudflaredConfigConfigIngressArgs(service="http_status:404"),
        ],
    ),
)
cname = cloudflare.DnsRecord(
    "vm-pilot",
    zone_id=zone_id,
    name=hostname,
    type="CNAME",
    content=tunnel.id.apply(lambda i: f"{i}.cfargotunnel.com"),
    ttl=1,          # 1 = automatic
    proxied=True,   # a tunnel's hostname is served only through Cloudflare's proxy
    comment="disky VM pilot: vmd via Cloudflare Tunnel (specs/vm-pilot.md); HMAC per tenant, no user traffic",
)
token = cloudflare.get_zero_trust_tunnel_cloudflared_token_output(account_id=account_id, tunnel_id=tunnel.id)

pulumi.export("tunnel_id", tunnel.id)
pulumi.export("hostname", hostname)
pulumi.export("tunnel_token", pulumi.Output.secret(token.token))
