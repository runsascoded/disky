# disky @ Open Athena

**This branch deploys the public docs and staff-only fleet dashboard at [disky.oa.dev]. Each storage dashboard has its own deployment branch below.**

[disky] maps where storage goes: scheduled scans of a store's buckets, then a treemap site with sizes over time, diffs between scans, owners and read recency. Open Athena runs four deployments, one per storage provider. Each has its own branch in this repo, and each branch merges the shared base, [`runsascoded/disky@cloud`][cloud].

<p>
<a href="https://gcs.oa.dev"><img alt="gcs.oa.dev: the public preview (names and sizes omitted)" src="https://github.com/Open-Athena/disky/raw/gcs/site/public/og.jpg" width="49%"></a>
<a href="https://cw.oa.dev"><img alt="cw.oa.dev: the latest scan's card" src="https://cw.oa.dev/og/map.png?policy=full&amp;r=8" width="49%"></a>
<a href="https://hf.oa.dev"><img alt="hf.oa.dev: the latest scan's card" src="https://hf.oa.dev/og/map.png?policy=full&amp;r=8" width="49%"></a>
<a href="https://r2.oa.dev"><img alt="r2.oa.dev: the latest scan's card" src="https://r2.oa.dev/og/map.png?policy=full&amp;r=8" width="49%"></a>
</p>

*Each card is the site's own link preview, drawn from its latest scan (gcs shows its static public preview: its cards are for signed-in viewers).*

| branch | site | scans |
|---|---|---|
| [`gcs`] | [gcs.oa.dev] | Marin's GCS buckets (`marin-*`), with per-user ownership, read recency from access logs, and staged deletion |
| [`cw`] | [cw.oa.dev] | Marin's CoreWeave (CAIOS) S3 buckets, with zone quotas and lifecycle rules |
| [`hf`] | [hf.oa.dev] | the `open-athena` Hugging Face org: its buckets, models, datasets and spaces, including the history its storage bill counts |
| [`r2`] | [r2.oa.dev] | Open Athena's Cloudflare R2 buckets, including disky's own indexes |

The sites are Open-Athena-gated. Each branch's README says how its scan runs, what it changes from `cloud`, and where its infrastructure lives.

## What's on this branch

- **`.github/workflows/health.yml`**: the hourly serving check across all four sites. It lives here because GitHub runs `schedule:` workflows only from a repo's default branch.
- **Shared code from `cloud`**, including `fleet/`: public docs and a staff-only fleet page over the four deployments. `fleet/wrangler.toml` is this deployment's configuration; broker client and source tokens are encrypted Pages secrets.
- **`CI`**: tests the shared code before publishing. The front-door deployment workflow builds and checks preview before production; `FLEET_DEPLOY_ENABLED=1` enables it after credentials are configured.
- **The hourly fleet post** (`fleet-update.yml`, `FLEET_POST_ENABLED=1`) is not enabled until source tokens, sign-in and the first post are verified; the existing healthcheck keeps its own schedule.

## Elsewhere

- **The shared base:** [`runsascoded/disky`][upstream], branch [`cloud`][cloud]: the scanning engine, the `dt-cloud` overlay, the site, and the specs.
- **The public demo:** [r2.rbw.sh], built from the same base.

[disky]: https://github.com/runsascoded/disky
[upstream]: https://github.com/runsascoded/disky
[cloud]: https://github.com/runsascoded/disky/tree/cloud
[`gcs`]: https://github.com/Open-Athena/disky/tree/gcs
[`cw`]: https://github.com/Open-Athena/disky/tree/cw
[`hf`]: https://github.com/Open-Athena/disky/tree/hf
[`r2`]: https://github.com/Open-Athena/disky/tree/r2
[gcs.oa.dev]: https://gcs.oa.dev
[cw.oa.dev]: https://cw.oa.dev
[hf.oa.dev]: https://hf.oa.dev
[r2.oa.dev]: https://r2.oa.dev
[r2.rbw.sh]: https://r2.rbw.sh
[disky.oa.dev]: https://disky.oa.dev
