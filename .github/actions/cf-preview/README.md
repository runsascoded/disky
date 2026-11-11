# Cloudflare PR preview lifecycle

Source prototype: executable and unit-tested, **not yet a live-verified or published reusable release**. Pin the reviewed provider commit when adopting it. This composite action can be used by another repository as `Open-Athena/disky/.github/actions/cf-preview@<full-sha>`. Node 24 is installed by the action; the caller installs its pinned Wrangler from a trusted base checkout.

The interface is `gate`, `publish`, `retire`. `gate` checks the live PR and current collaborator permission, then returns `sha`, `base_sha`, `pr`, `target`, and the custom URL. Automatic requests require a current write/maintain/admin PR author; `/preview` or `/preview <full-head-sha>` and manual dispatch require a current write collaborator as the actor. An external contributor's later commit needs another approval. `publish` rechecks the captured head before deployment and before reporting success, deploys the built artifact and provisions `prN.<domain>`. `retire` requires a closed PR and removes only the reserved, owned provider resources and host.

A small caller workflow is shown in [the example]. It has three jobs: a trusted gate, a credential-free build, and a trusted publisher. PR source/config is never checked out or installed in the credentialed publisher. Provide `sha` from gate, not an input editable by the PR. The caller's build must retain the complete static/Worker artifact, including WASM. A `pull_request_target` workflow is safe only while it respects this separation. Its privileged configuration comes from the accepted base, never from the built artifact. Do not interpolate comment bodies, branch names or PR titles into shell commands.

The component deliberately does not choose build instructions, data bindings, auth policy or runtime secrets for the caller. A deployed preview can use every binding and runtime secret it receives. Existing Disky dev sites share production D1/R2; whether to do that for PR code remains an explicit review item. Workers templates must be a dedicated preview template, with declared preview bindings and paths relative to the artifact; unsupported stateful fields fail explicitly. Production routes, cron and build hooks are not copied. Native Workers Previews require Wrangler 4.135.0 or newer; the locally inspected CLI is 4.150.0. This provider feature is in open beta and remains subject to a disposable live pilot.

## Trusted configuration

A Pages target:

```json
{
  "repository": "your-org/your-repo",
  "targets": {
    "main": {
      "provider": "pages",
      "account_id": "<CF account ID>",
      "zone_id": "<zone ID>",
      "project": "your-existing-pages-project",
      "domain": "preview.example.org",
      "working_directory": "site",
      "wrangler": ["pnpm", "exec", "wrangler"],
      "verification": { "path": "/__revision" }
    }
  }
}
```

Place this in the trusted repository as `.github/cf-preview.json`. Paths are relative to that file, so a site at repo-root `site/` uses `working_directory: "../site"` from `.github/cf-preview.json`; the schematic above assumes the config is at repository root. The target key is the permitted PR base branch. Disky can use `gcs`, `cw`, `hf`, `r2`; a shared source PR must explicitly select a preview target rather than inherit an unrelated production config.

A Workers target uses `provider: "workers"`, an existing parent Worker named by `project`, and `worker_template`:

```json
{
  "main": "edge/preview-build/worker.js",
  "compatibility_date": "2026-11-10",
  "assets": { "directory": "www/dist", "binding": "ASSETS" },
  "d1_databases": [{ "binding": "DB", "database_id": "<explicit preview DB ID>" }],
  "vars": { "PUBLIC_ORIGIN": "<caller-defined preview origin>" }
}
```

The template is an object in the trusted target configuration, not a path in the PR artifact. The action creates the native named Preview `prN` under that existing parent, pins its account, and supplies `CF_PREVIEW_OWNER`. It writes a temporary JSON config with `no_bundle: true` and uploads using `wrangler preview --name prN --worker-name <project> --ignore-base-config --tag <sha> --message <owner>`. Bindings go into the generated `previews` block; assets and compatibility settings stay at the top level. The action does not create a parent Worker or change its production routing. Ignoring server Base configuration prevents unexpected bindings/shared secrets from being copied into a new Preview. Runtime secrets therefore need explicit caller provisioning; existing Preview secrets are not deleted by an update. Paths stay inside the built artifact. A caller whose app bakes canonical URLs into its frontend should build with gate's `url` output; an app requiring a per-host runtime origin should put its explicitly generated preview origin in the trusted template, not reuse production's URL. Runtime secret installation is a separate caller responsibility.

## Credentials and domains

- Gate: repository read token only. Publish: PR comment + Deployments Write for the job's `GITHUB_TOKEN`; scoped provider deployment token. Cleanup: repository read and the provider credentials; this implementation also records its result in GitHub and updates the existing bot comment.
- Pages: the configured account's Pages Edit/Write token, plus a **separate DNS Edit token for the configured zone**. DNS authority is zone-wide; Cloudflare does not turn the component's `prN` namespace checks into provider-side per-record permission restrictions.
- Workers: the caller's scoped Worker Preview deployment authority. Enable the parent's custom domain for Previews once, outside the per-PR lifecycle; publish verifies that setting rather than altering it. The exact policy must be live-verified for the caller before activation; this prototype does not claim a Pages-only token can deploy Workers.
- Secrets belong only in the publish/retire job's protected environment, not in the build. Secret values never appear in action argv, receipts or comments.

Pages uses a reserved `pr-N` branch, a proxied CNAME to that branch's `pages.dev` alias, and Pages Custom Domain registration. Workers uses native named Previews, with the parent custom domain already configured with `previews_enabled: true`. Cloudflare manages the shared wildcard DNS/certificate once; closing a PR deletes its native Preview and deployments, while the parent and wildcard baseline remain. The component waits a bounded interval for valid TLS and the captured full SHA on the custom origin. Configure `verification.path` to return exactly that SHA as text, or also set `verification.header` to read the first whitespace-separated header token (Disky uses `/api/health` and `x-disky-build`). The caller must stamp its build accordingly. Redirects, another revision and mere reachability cannot pass. This proves source routing, not authenticated application/data acceptance. Broker/OAuth callback registration and noindex/privacy remain caller policy and must be exercised in the real pilot.

## Cleanup and its boundaries

Reserve the declared `prN.<domain>`, Pages `pr-N` branches or native Workers `prN` Preview names for this tool. Pages uploads carry the exact ownership marker in deployment metadata; DNS carries it as a comment; native Worker deployments carry the exact ownership message annotation and source SHA tag (plus the runtime ownership binding). A pre-existing unowned domain, DNS record, Worker or selected deployment causes a refusal, not a takeover. Production branch/canonical deployment checks run before deletes. Lists are fully paginated. Retry after partial deletion is supported when ownership remains provable; an externally removed proof requires reconciliation.

After a PR is retargeted, call `retire` for **every configured target it could have used**, not just its final base. Closed-state validation permits cleaning a previous target, but the same namespace and ownership rules still apply. Caller concurrency should serialize all publish/close operations for that PR; a closed-state recheck prevents a new publish after closure. If Cloudflare refuses deletion of a branch's last payload, report the failure and retain the reconciliation receipt; do not claim complete retirement. The real pilot must explicitly test that API behavior.

Receipts identify repository, target, PR, SHA, provider, resource IDs, and GitHub run. A bot comment is updated rather than appended on every successful rebuild; close marks it retired. Provider/DNS failures and missing ownership remain job failures. No existing deployments are cleaned by merely adding this action's source to a repository.

Native Workers Previews are documented in [configuration] and [custom domains]. Pages aliases follow [custom branch aliases].

Run `node --test .github/actions/cf-preview/preview.test.mjs` for dependency-free tests of both adapters, authorization and lifecycle guards. Remaining acceptance: real create/update/retire/retry, source/build correctness, DNS/SSL, auth, explicit data-policy approval and source publication.

[the example]: examples/preview.yml

[configuration]: https://developers.cloudflare.com/workers/previews/configuration/
[custom domains]: https://developers.cloudflare.com/workers/previews/custom-domains/
[custom branch aliases]: https://developers.cloudflare.com/pages/how-to/custom-branch-aliases/
