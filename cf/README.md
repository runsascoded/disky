# cf — Cloudflare resources as code (shared toolkit)

`cfn_dashboard.py` is the shared `CfnDashboard` Pulumi component: the Cloudflare surface of a `site/` deployment that `wrangler pages deploy` doesn't own. That's the Pages project shell, its custom domain + CNAME, preview-branch aliases, the D1 database, an optional `CACHE_KV` namespace, an optional R2 serving bucket, and an optional Zero Trust Access gate. It carries no account ids, zone ids or store literals. Wrangler still fills each deploy's bindings and vars from that branch's `site/wrangler.toml`; Pulumi owns the container.

`cloud` carries only the component and its environment (`pyproject.toml`, `uv.lock`). Each deployment branch adds its own instance wiring next to it: a `__main__.py` (its `Store`s + account/zone config), `Pulumi.yaml` and `Pulumi.<stack>.yaml`. Those are gcs (gcs.oa.dev), cw-s3 (cw-s3.oa.dev) and m3 (disk.rbw.sh).

```bash
cd cf
uv sync
pulumi preview -s <stack>   # read-only; `up` is a human's call
```
