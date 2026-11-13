# cf — Cloudflare resources as code (shared toolkit)

`cfn_dashboard.py` is the shared `CfnDashboard` Pulumi component: the Cloudflare surface of a `site/` deployment that `wrangler pages deploy` doesn't own. That's the Pages project shell, its custom domain + CNAME, preview-branch aliases, the D1 database, an optional `CACHE_KV` namespace, an optional R2 serving bucket, and an optional Zero Trust Access gate. It carries no account ids, zone ids or store literals. Wrangler still fills each deploy's bindings and vars from that branch's `site/wrangler.toml`; Pulumi owns the container.

`cloud` carries only the component and its environment (`../pyproject.toml`, `../uv.lock`). Each deployment branch adds its own instance wiring next to it: a `__main__.py` (its `Store`s + account/zone config), `Pulumi.yaml` and `Pulumi.<stack>.yaml`. Those are gcs (gcs.oa.dev), cw-s3 (cw-s3.oa.dev) and m3 (disk.rbw.sh).

```bash
cd infra/cf
pulumi preview -s <stack>   # read-only; `up` is a human's call
infra/cf/run preview -s <stack>   # the same, as the IaC token: `$CF_IAC_TOKEN` (trimmed) as `CLOUDFLARE_API_TOKEN`
```

`run` is `pulumi -C infra/cf "$@"` with the Cloudflare token swapped: a deployment's direnv usually exports a D1/Pages job token as `CLOUDFLARE_API_TOKEN`, which can't manage the stack, so `run` takes `CF_IAC_TOKEN` instead (and refuses to start without it).

## cw-s3 stack

The `cw-s3` stack runs the shared program (`stack/`; `main: stack/` in `Pulumi.yaml`, backend project `gcs-usage-cf`, shared with gcs's stack) with this deployment's values in `Pulumi.cw-s3.yaml`. They declare:

- the `oa-cw-s3-usage` Pages project shell, the `cw-s3.oa.dev` custom domain + CNAME, and `dev.cw-s3.oa.dev`, a proxied CNAME onto the `dev` preview branch alias (Pages custom domains are production-only; this is the dev stack's hostname);
- the `oa-cw-s3-usage-db` D1 database (migrations stay with the app, `site/migrations/cw/`);
- the `CACHE_KV` namespace `oa-cw-s3-usage-cache`;
- the R2 serving bucket `oa-cw-s3-usage-index` and the R2 token whose S3 credentials (key id = token id, secret = sha256 of the token value; secret outputs `r2_s3_access_key_id` / `r2_s3_secret_access_key`) both the job's `dt-cloud publish-r2` and the site's `STORE_*` secrets use (`../../specs/done/r2-serving-migration.md`);
- no Zero Trust Access app: the site does its own sign-in (`../../specs/done/oidc-cutover-cw.md`).

Two scripts move outputs and secrets around without printing a value:

- `sync-secrets` reads the stack outputs once and writes them to this worktree's `.envrc`, the site's Pages secrets (production and/or preview) and Secret Manager (`cw-s3-r2-*`, for the job).
- `auth-secrets` puts the sign-in secrets (`SESSION_SECRET`, the Google client, the mail key) on the Pages project.

Backfilling R2: the job publishes every scan (`dt-cloud publish-r2`, idempotent by size + md5, so it also backfills old scans). Sippy (migrate-on-read) isn't used: it would need a GCS key in Cloudflare and make every first read cross-provider.
