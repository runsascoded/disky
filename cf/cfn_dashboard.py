"""CfnDashboard — the Cloudflare surface of a "Vite + CFN" disk-tree dashboard.

EXTRACTION-READY / marin-agnostic. This module carries no account ids, no zone
ids, and no store literals — only the reusable *shape*. It's written to move
verbatim to the disk-tree `cfn` reference-deploy branch (`specs/done/two-
reference-deploys.md`) and be reused by any GCS / R2 / AWS-S3-backed deployment; the
instance wiring (which stores, which account/zone) stays in the consuming
`__main__.py`. See gcs:specs/cf-iac.md.

`CfnDashboard` stands up the Cloudflare resources a dashboard needs and that its
deploy tool (`wrangler pages deploy`) does NOT own:

  Pages project shell · custom domain · apex-zone CNAME · preview-branch
  aliases · Zero Trust Access application + allow policy · D1 database ·
  optional Workers KV namespace.

THE DEPLOY/CONFIG BOUNDARY (load-bearing): `wrangler pages deploy` fills the
Pages project's *deployment_configs* — env vars, D1/KV bindings, secrets — from
`wrangler.toml` on every deploy. This component must not fight that, so the
`PagesProject` carries `ignore_changes=["deployment_configs"]`: **the component
owns the container, wrangler fills it.** Secret VALUES never enter code or state.
D1 *migrations* stay with the app (`wrangler d1 migrations apply`); only the
database resource is modeled here.

The data-plane job (a scan/snapshot pipeline) is deliberately NOT here — it's
per-cloud (GCP Batch, or a Lambda/Cloud Run elsewhere) and belongs in its own
stack. A deployment is `CfnDashboard` + a data-plane stack, not one cross-cloud
mega-component. The CF surface is the part identical across storage backends,
so it's the part worth sharing.
"""

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import pulumi
import pulumi_cloudflare as cloudflare
from pulumi import Input, ResourceOptions


@dataclass(frozen=True)
class AccessApp:
    """A Zero Trust Access application + its single allow policy.

    `uris` are the public hostnames (optionally path-scoped) the app covers;
    a dashboard may gate only an SSO hand-off path or the whole host. `include`
    is the policy's allow rule: `include_everyone` (a downstream allowlist does
    the real gating) or a list of `email_domains`. `policy_name` defaults to a
    generic label; set it to match an adopted app's existing policy name.
    """
    name: str
    uris: tuple[str, ...]
    include_everyone: bool = False
    email_domains: tuple[str, ...] = ()
    session_duration: str = "24h"
    app_launcher_visible: bool | None = None   # None = leave unset (matches a live null)
    policy_name: str | None = None


@dataclass(frozen=True)
class BranchAlias:
    """A custom hostname for one preview branch (Pages' "custom domain for a
    branch"): the hostname is added as a Pages custom domain like production's,
    but its proxied CNAME targets `<branch>.<project>.pages.dev` instead of the
    project host — that target is the whole mechanism (an unproxied record, or
    one aimed at the plain project host, lands on production). Sessions are
    per host, so a dev stack gets its own sign-in at a memorable name."""
    domain: str                       # e.g. dev.<production domain>
    branch: str                       # the Pages preview branch it aliases


@dataclass(frozen=True)
class Store:
    """One deployment's Cloudflare-side facts (all non-secret; ids are public)."""
    pages_project: str
    production_branch: str
    domain: str                       # custom domain (→ <pages_project>.pages.dev)
    d1_name: str
    # Custom hostnames for preview branches (the dev stack): each one is a
    # PagesDomain + a proxied CNAME to `<branch>.<project>.pages.dev`.
    branch_aliases: tuple[BranchAlias, ...] = ()
    # Zero Trust Access gate, or None when the deployment does its own identity
    # (gcs.oa.dev: own Google OIDC client + emailed codes — gcs:specs/done/oidc-cutover.md).
    access: AccessApp | None = None
    kv_name: str | None = None        # bind a CACHE_KV namespace, or None
    # R2 serving (specs/done/r2-serving-migration.md; the base's r2.rbw.sh
    # already serves this way): the bucket the served artifacts are published
    # to, read by the site through the `STORE_*` seam (S3-over-R2). None = the
    # deployment serves straight from its cloud store (GCS).
    r2_bucket: str | None = None
    r2_location: str = "enam"         # location hint; only honored at first create
    # Names of the R2 permission groups the publish/read token needs. Resolved
    # to ids by the API at program time through the ACCOUNT-scoped list
    # (`get_account_api_token_permission_groups_list`): the token is an
    # account-owned `AccountToken`, so an account API token (the IaC/CI token)
    # can both look these up and mint it — the user-level `ApiToken` API
    # 403s for anything but a user token. Override with explicit ids via
    # `r2_token_permission_group_ids` if the lookup is unavailable.
    r2_token_permission_groups: tuple[str, ...] = (
        "Workers R2 Storage Bucket Item Read",
        "Workers R2 Storage Bucket Item Write",
    )
    r2_token_permission_group_ids: tuple[str, ...] = ()
    # A Zero Trust service token (machine identity) plus a `non_identity` policy
    # on the Access app: lets the deployment's own data-plane job call the site
    # through Access (`CF-Access-Client-Id` / `-Secret` headers) — the warm-cache
    # stage that pre-fills the edge/KV cache after each scan. None = no machine
    # access. The client id/secret are secret outputs; the consumer hands them to
    # the job's secret store (never git). Non-expiring: an expired token would
    # silently turn the warm into a no-op; rotate by replacing the resource.
    service_token: str | None = None

    @property
    def pages_host(self) -> str:
        return f"{self.pages_project}.pages.dev"


class CfnDashboard(pulumi.ComponentResource):
    """Stand up a dashboard's Cloudflare surface. Exposes the created resources
    as attributes so the caller can `pulumi.export` whichever it wants."""

    def __init__(
        self,
        name: str,
        *,
        account_id: Input[str],
        zone_id: Input[str],           # apex zone holding the custom-domain CNAME
        store: Store,
        import_ids: Mapping[str, str] | None = None,
        opts: ResourceOptions | None = None,
    ):
        """`import_ids` adopts pre-existing (hand-built) resources into this
        component instead of creating them: a `{key: cloudflare-import-id}` map
        over the keys `pages`, `domain`, `cname`, `d1`, `kv`, `access-policy`,
        `access-app`, `r2`, `r2-token`, `service-token`, `service-policy`, and
        `domain:<branch>` / `cname:<branch>` per branch alias. Set it
        (via config) for the first `up` on a live account,
        then clear it once the stack is authoritative. Absent → normal create.
        """
        super().__init__("oa:cfn:CfnDashboard", name, None, opts)
        imports = dict(import_ids or {})

        def child(
            key: str,
            ignore: list[str] | None = None,
            depends_on: list[pulumi.Resource] | None = None,
        ) -> ResourceOptions:
            """parent=self, plus `import_=<id>` when this key is being adopted,
            and any `ignore_changes` (for live-owned legacy/free-text fields the
            component doesn't manage)."""
            return ResourceOptions(
                parent=self,
                import_=imports.get(key) or None,
                ignore_changes=ignore,
                depends_on=depends_on,
            )

        # Pages project — container only; wrangler owns deployment_configs.
        self.pages = cloudflare.PagesProject(
            f"{name}-pages",
            account_id=account_id,
            name=store.pages_project,
            production_branch=store.production_branch,
            build_config=cloudflare.PagesProjectBuildConfigArgs(
                # `site/deploy` uploads a prebuilt `dist/`; CF runs no build. Only
                # destination_dir is set — build_command/root_dir stay unset to
                # match a wrangler-created project (empty strings would diff).
                destination_dir="dist",
            ),
            opts=child("pages", ignore=["deployment_configs"]),
        )

        # Custom domain + the apex-zone CNAME it resolves through.
        self.domain = cloudflare.PagesDomain(
            f"{name}-domain",
            account_id=account_id,
            project_name=self.pages.name,
            name=store.domain,
            opts=child("domain"),
        )
        self.cname = cloudflare.DnsRecord(
            f"{name}-cname",
            zone_id=zone_id,
            name=store.domain,
            type="CNAME",
            content=store.pages_host,
            ttl=1,          # 1 = automatic
            proxied=True,   # orange-cloud: required for the Access gate to see requests
            # `comment` is human free-text on the record; leave it to whoever set it.
            opts=child("cname", ignore=["comment"]),
        )

        # Preview-branch aliases: the same PagesDomain + CNAME pair, with the
        # CNAME aimed at the branch's pages.dev host. The record goes first so
        # activating the domain never has to invent one aimed at production.
        self.branch_cnames: dict[str, cloudflare.DnsRecord] = {}
        self.branch_domains: dict[str, cloudflare.PagesDomain] = {}
        for alias in store.branch_aliases:
            cname = cloudflare.DnsRecord(
                f"{name}-{alias.branch}-cname",
                zone_id=zone_id,
                name=alias.domain,
                type="CNAME",
                content=f"{alias.branch}.{store.pages_host}",
                ttl=1,
                proxied=True,   # required: an unproxied alias routes to production
                opts=child(f"cname:{alias.branch}", ignore=["comment"]),
            )
            self.branch_cnames[alias.branch] = cname
            self.branch_domains[alias.branch] = cloudflare.PagesDomain(
                f"{name}-{alias.branch}-domain",
                account_id=account_id,
                project_name=self.pages.name,
                name=alias.domain,
                opts=child(f"domain:{alias.branch}", depends_on=[cname]),
            )

        # D1 database (container only; migrations stay with the app).
        self.d1 = cloudflare.D1Database(
            f"{name}-d1",
            account_id=account_id,
            name=store.d1_name,
            # Match the server default so import→preview is clean (not a null diff).
            read_replication=cloudflare.D1DatabaseReadReplicationArgs(mode="disabled"),
            opts=child("d1"),
        )

        # Optional KV cache namespace.
        self.kv = None
        if store.kv_name:
            self.kv = cloudflare.WorkersKvNamespace(
                f"{name}-cache-kv",
                account_id=account_id,
                title=store.kv_name,
                opts=child("kv"),
            )

        # Zero Trust Access: application + its single allow policy — only for a
        # deployment that gates at the edge (`store.access` set). NOTE
        # (pulumi-cloudflare 6.21): a hand-built app carrying both `destinations`
        # and the legacy `self_hosted_domains` (CF auto-mirrors them) cannot be
        # imported — the provider rejects the combination at input validation,
        # before `ignore_changes` can help. Create such apps from here instead.
        self.access_policy = None
        self.access_app = None
        self.service_token = None
        self.service_policy = None
        if store.access is not None:
            self._access(name, account_id, store, child)

        # R2 serving bucket + the API token whose S3-API credentials the publish
        # step (`dt-cloud publish-r2`) and the site's `STORE_*` seam use. R2's
        # S3 credentials derive from an API token: access key id = the token's
        # id, secret = sha256(token value) — so the token IS the credential and
        # nothing hand-minted needs to leave the console. Both outputs are
        # secrets in state; values never enter git (the site takes them as
        # `wrangler pages secret put`, the job from Secret Manager).
        self.r2 = None
        self.r2_token = None
        if store.r2_bucket:
            self.r2 = cloudflare.R2Bucket(
                f"{name}-r2",
                account_id=account_id,
                name=store.r2_bucket,
                location=store.r2_location,
                opts=child("r2"),
            )
            group_ids: list[Input[str]] = list(store.r2_token_permission_group_ids) or [
                cloudflare.get_account_api_token_permission_groups_list_output(
                    account_id=account_id, name=g,
                ).apply(lambda r, g=g: _only_permission_group(r, g))
                for g in store.r2_token_permission_groups
            ]
            # Account-owned (`/accounts/<id>/tokens`), not a user token: it
            # belongs to the account, not to whoever ran `up`, and an account
            # API token can create it. R2's S3 credentials derive the same way
            # (access key id = token id, secret = sha256 of the value).
            self.r2_token = cloudflare.AccountToken(
                f"{name}-r2-token",
                account_id=account_id,
                name=f"{store.pages_project} r2 publish+read",
                policies=[
                    cloudflare.AccountTokenPolicyArgs(
                        effect="allow",
                        permission_groups=[
                            cloudflare.AccountTokenPolicyPermissionGroupArgs(id=gid) for gid in group_ids
                        ],
                        # Scoped to this account's R2 buckets; bucket-level
                        # scoping (`com.cloudflare.edge.r2.bucket.<acct>_default_<name>`)
                        # is a tighter follow-up once the bucket id is known.
                        # v6 takes the resource map as a JSON string.
                        resources=pulumi.Output.from_input(account_id).apply(
                            lambda a: json.dumps({f"com.cloudflare.api.account.{a}": "*"})
                        ),
                    )
                ],
                # The provider reads `policies` back in a shape it never
                # matches to the inputs (permission-group ids only vs the
                # API's full group records), so a live token diffs on every
                # preview. The policy is set once at create; re-key by
                # replacing the token, not by editing this.
                opts=child("r2-token", ["policies"]),
            )

        outputs: dict[str, object] = {
            "pages_project": self.pages.name,
            "custom_domain": self.domain.name,
            "d1_database": self.d1.name,
        }
        if self.access_app is not None:
            outputs["access_app_id"] = self.access_app.id
            outputs["access_app_aud"] = self.access_app.aud
        if self.kv is not None:
            outputs["cache_kv"] = self.kv.id
        if self.service_token is not None:
            outputs["service_token_client_id"] = pulumi.Output.secret(self.service_token.client_id)
            outputs["service_token_client_secret"] = pulumi.Output.secret(self.service_token.client_secret)
        if self.r2 is not None and self.r2_token is not None:
            outputs["r2_bucket"] = self.r2.name
            outputs["r2_s3_access_key_id"] = pulumi.Output.secret(self.r2_token.id)
            outputs["r2_s3_secret_access_key"] = pulumi.Output.secret(
                self.r2_token.value.apply(lambda v: hashlib.sha256(v.encode()).hexdigest())
            )
        self.register_outputs(outputs)

    def _access(
        self,
        name: str,
        account_id: Input[str],
        store: Store,
        child: Callable[..., ResourceOptions],
    ) -> None:
        """The Access application + its allow policy (+ optional service-token
        policy) for an edge-gated deployment. Sets `access_policy`, `access_app`,
        `service_token`, `service_policy`."""
        access = store.access
        assert access is not None
        if access.include_everyone:
            includes = [
                cloudflare.ZeroTrustAccessPolicyIncludeArgs(
                    everyone=cloudflare.ZeroTrustAccessPolicyIncludeEveryoneArgs()
                )
            ]
        else:
            includes = [
                cloudflare.ZeroTrustAccessPolicyIncludeArgs(
                    email_domain=cloudflare.ZeroTrustAccessPolicyIncludeEmailDomainArgs(domain=d)
                )
                for d in access.email_domains
            ]
        self.access_policy = cloudflare.ZeroTrustAccessPolicy(
            f"{name}-access-policy",
            account_id=account_id,
            name=access.policy_name or f"{access.name} — allow",
            decision="allow",
            includes=includes,
            opts=child("access-policy"),
        )
        app_policies = [
            cloudflare.ZeroTrustAccessApplicationPolicyArgs(
                id=self.access_policy.id, precedence=1
            )
        ]
        # Optional machine identity: a service token + the `non_identity` policy
        # that admits it (Access evaluates it before the human allow rule).
        if store.service_token:
            self.service_token = cloudflare.ZeroTrustAccessServiceToken(
                f"{name}-service-token",
                account_id=account_id,
                name=store.service_token,
                duration="forever",
                opts=child("service-token"),
            )
            self.service_policy = cloudflare.ZeroTrustAccessPolicy(
                f"{name}-service-policy",
                account_id=account_id,
                name=f"{store.service_token} — service token",
                decision="non_identity",
                includes=[
                    cloudflare.ZeroTrustAccessPolicyIncludeArgs(
                        service_token=cloudflare.ZeroTrustAccessPolicyIncludeServiceTokenArgs(
                            token_id=self.service_token.id
                        )
                    )
                ],
                opts=child("service-policy"),
            )
            app_policies.append(
                cloudflare.ZeroTrustAccessApplicationPolicyArgs(
                    id=self.service_policy.id, precedence=2
                )
            )
        access_app_kwargs = dict(
            account_id=account_id,
            name=access.name,
            type="self_hosted",
            destinations=[
                cloudflare.ZeroTrustAccessApplicationDestinationArgs(type="public", uri=uri)
                for uri in access.uris
            ],
            session_duration=access.session_duration,
            http_only_cookie_attribute=True,
            policies=app_policies,
        )
        # Only assert app_launcher_visible when the store sets it (None → unset,
        # which matches a live null rather than diffing null→false).
        if access.app_launcher_visible is not None:
            access_app_kwargs["app_launcher_visible"] = access.app_launcher_visible
        self.access_app = cloudflare.ZeroTrustAccessApplication(
            f"{name}-access-app",
            # `domain` / `self_hosted_domains` are legacy mirrors of `destinations`;
            # keep the imported values rather than nulling them.
            opts=child("access-app", ignore=["domain", "self_hosted_domains"]),
            **access_app_kwargs,
        )


def _only_permission_group(result: object, name: str) -> str:
    """The single permission-group id matching `name`; loud when the lookup
    returns none (a token without the account-level list permission can't —
    pass explicit ids via `Store.r2_token_permission_group_ids` instead)."""
    results = list(getattr(result, "results", None) or [])
    ids = [getattr(r, "id", None) or r.get("id") for r in results]
    if len(ids) != 1:
        raise pulumi.RunError(
            f"expected exactly one API-token permission group named {name!r}, got {len(ids)}; "
            "set Store.r2_token_permission_group_ids explicitly"
        )
    return ids[0]
