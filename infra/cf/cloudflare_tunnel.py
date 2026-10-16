"""`CloudflareTunnel`: a local machine's files, reachable only by the site's Function.

A named, remotely configured tunnel (`cloudflared tunnel run --token …` on the
machine dials out; nothing listens publicly) whose one ingress maps `hostname`
to the machine's loopback file server (`dt-serve /files`). The hostname sits
behind an Access application whose only policy admits a service token, so no
browser login path exists: the Function sends `CF-Access-Client-Id` / `-Secret`,
Access injects `Cf-Access-Jwt-Assertion`, and `dt-serve` verifies it against the
application's `aud` (spec `laptop-file-tunnel.md`).

Outputs the machine needs (`tunnel_token`) and the Function needs (`client_id`,
`client_secret`, the URL), plus `aud` for `dt-serve --access-aud`.
"""
from __future__ import annotations

import pulumi
import pulumi_cloudflare as cloudflare


class CloudflareTunnel(pulumi.ComponentResource):
    tunnel: cloudflare.ZeroTrustTunnelCloudflared
    config: cloudflare.ZeroTrustTunnelCloudflaredConfig
    cname: cloudflare.DnsRecord
    service_token: cloudflare.ZeroTrustAccessServiceToken
    policy: cloudflare.ZeroTrustAccessPolicy
    app: cloudflare.ZeroTrustAccessApplication
    tunnel_token: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        account_id: str,
        zone_id: str,
        hostname: str,
        service: str,
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__("disky:cf:CloudflareTunnel", name, None, opts)
        child = pulumi.ResourceOptions(parent=self)

        self.tunnel = cloudflare.ZeroTrustTunnelCloudflared(
            f"{name}-tunnel",
            account_id=account_id,
            name=name,
            config_src="cloudflare",
            opts=child,
        )
        self.config = cloudflare.ZeroTrustTunnelCloudflaredConfig(
            f"{name}-config",
            account_id=account_id,
            tunnel_id=self.tunnel.id,
            config=cloudflare.ZeroTrustTunnelCloudflaredConfigConfigArgs(
                ingresses=[
                    cloudflare.ZeroTrustTunnelCloudflaredConfigConfigIngressArgs(hostname=hostname, service=service),
                    cloudflare.ZeroTrustTunnelCloudflaredConfigConfigIngressArgs(service="http_status:404"),
                ],
            ),
            opts=child,
        )
        self.cname = cloudflare.DnsRecord(
            f"{name}-cname",
            zone_id=zone_id,
            name=hostname,
            type="CNAME",
            content=self.tunnel.id.apply(lambda i: f"{i}.cfargotunnel.com"),
            ttl=1,
            proxied=True,
            opts=child,
        )
        self.service_token = cloudflare.ZeroTrustAccessServiceToken(
            f"{name}-service-token",
            account_id=account_id,
            name=f"{name} — site Function",
            duration="forever",
            opts=child,
        )
        self.policy = cloudflare.ZeroTrustAccessPolicy(
            f"{name}-policy",
            account_id=account_id,
            name=f"{name} — service token only",
            decision="non_identity",
            includes=[
                cloudflare.ZeroTrustAccessPolicyIncludeArgs(
                    service_token=cloudflare.ZeroTrustAccessPolicyIncludeServiceTokenArgs(token_id=self.service_token.id),
                ),
            ],
            opts=child,
        )
        self.app = cloudflare.ZeroTrustAccessApplication(
            f"{name}-app",
            account_id=account_id,
            name=name,
            type="self_hosted",
            destinations=[cloudflare.ZeroTrustAccessApplicationDestinationArgs(type="public", uri=hostname)],
            app_launcher_visible=False,
            session_duration="24h",
            http_only_cookie_attribute=True,
            policies=[cloudflare.ZeroTrustAccessApplicationPolicyArgs(id=self.policy.id, precedence=1)],
            opts=child,
        )
        self.tunnel_token = pulumi.Output.secret(
            cloudflare.get_zero_trust_tunnel_cloudflared_token_output(account_id=account_id, tunnel_id=self.tunnel.id).token
        )
        self.register_outputs({"tunnel_id": self.tunnel.id, "aud": self.app.aud})
