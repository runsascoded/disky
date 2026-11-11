# Cloudflare PR preview lifecycle

Source prototype: executable and unit-tested, **not yet a live-verified or published reusable release**. Pin the reviewed provider commit when adopting it. This composite action can be used by another repository as `Open-Athena/disky/.github/actions/cf-preview@<full-sha>`. Node 24 is installed by the action; the caller installs its pinned Wrangler from a trusted base checkout. The design, including what each mode may touch, is [the spec].

The interface is `gate`, `publish`, `retire`. `gate` checks the live PR and current collaborator permission, selects the preview mode, then returns `sha`, `base_sha`, `pr`, `target`, `mode`, and the custom URL. Automatic requests require a current write/maintain/admin PR author; `/preview [<mode>] [<full-head-sha>]` and manual dispatch require a current write collaborator as the actor. An external contributor's later commit needs another approval. `publish` takes gate's `sha` and `mode`, rechecks the captured head before deployment and before reporting success, deploys and provisions `prN.<domain>`. `retire` requires a closed PR and removes only the reserved, owned provider resources and host.

## Modes

| Mode | Deploys | Data | Status |
|---|---|---|---|
| `ui` | the PR's built static assets + this action's trusted read-only layer (`layer.mjs`) | production, read-only, through the layer | implemented (Pages) |
| `stack` | the PR's full build (front end + Functions) | a scratch D1 + R2 per PR | specified only |
| `pipeline` | a job image built from the PR's head, dispatched from GHA | a scratch store prefix | specified only |

Selection: a `/preview <mode>` comment (or the dispatch `mode` input) applies to that run; otherwise exactly one `preview:<mode>` label on the PR; otherwise `ui`. Two different mode labels, or an unknown `preview:*` label, fail the gate. `stack` and `pipeline` are refused at the gate until implemented.

`ui` never deploys the PR's backend and holds no write binding. The publish step copies the PR's static build, replaces any `_worker.js` / `_routes.json` it carries with the layer (inlined from the trusted action checkout, with this publish's settings) and routes every path through it. The layer:

- answers `GET`/`HEAD` only; every other method is a 405 everywhere,
- serves only `prN.<domain>` (the `pages.dev` aliases, which Cloudflare Access does not cover, get a 421),
- forwards reads of `forward` paths (default `/api/`, `/data/`, `/v1/files/`) to the production `upstream`, except `deny` (default `/api/auth/`, `/api/token`) unless the exact path is in `allow` (default `/api/auth/whoami`); encoded separators and `?key=` are refused,
- forwards only `accept`, `accept-language`, `if-modified-since`, `if-none-match`, `range`, plus `Authorization: Bearer <read grant>`; the viewer's cookies and any `Authorization` the page sends stay behind; upstream `Set-Cookie` is dropped, responses are `private, no-store`, and same-origin redirects are made relative,
- serves everything else from the PR's assets, with `x-robots-tag: noindex` and `x-cf-preview-revision: <sha>`,
- answers `/__preview/revision` with the captured SHA (the publish probe; the one path open without sign-in),
- refuses to run (503) with any binding but `ASSETS`, or without its read grant on a gated deployment.

Viewers: a gated `ui` preview admits only a Cloudflare Access user, verified by the layer itself (RS256 assertion against the team's keys, the application's AUD tag, issuer, expiry) and then checked against the trusted `emails` / `domains` list. `viewers: "public"` (a public deployment, e.g. the r2 demo) skips the gate and forwards without credentials.

## Trusted configuration

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
      "modes": {
        "ui": {
          "project": "your-site-ui-previews",
          "upstream": "https://app.example.org",
          "working_directory": "..",
          "wrangler": ["node", "site/node_modules/wrangler/bin/wrangler.js"],
          "viewers": {
            "access": { "team_domain": "<team>.cloudflareaccess.com", "aud": "<Access application AUD tag>" },
            "domains": ["example.org"],
            "emails": []
          }
        }
      }
    }
  }
}
```

Place this in the trusted repository as `.github/cf-preview.json`. Paths are relative to that file. The target key is the permitted PR base branch. Disky can use `gcs`, `cw`, `hf`, `r2`; a shared source PR must explicitly select a preview target rather than inherit an unrelated production config. `modes.ui` overrides `project`, `domain`, `wrangler` and `working_directory`, and the publish probe is always the layer's `/__preview/revision` (the target's own `verification` would read production's build). `forward`, `deny` and `allow` may override the defaults above.

`modes.ui.project` is a **dedicated** Pages project, never the deployment's own (whose preview environment, on Disky, is the dev stack with production bindings). Before uploading, publish reads its preview configuration and refuses any binding (D1, R2, KV, services, Durable Objects, queues, anything that is not a plain setting) and any variable except `CF_PREVIEW_READ_TOKEN` as a secret (none at all when public). Wrangler runs in `working_directory`, which must hold no `functions/` and no `wrangler.{toml,json,jsonc}`: either would put the base branch's backend or production bindings into the upload. Run the trusted Wrangler by path from there, as above.

## Credentials and domains

- Gate: repository read token only. Publish: PR comment + Deployments Write for the job's `GITHUB_TOKEN`; scoped Pages deployment token; a **separate DNS Edit token for the configured zone**. Retire: the same. DNS authority is zone-wide; Cloudflare does not turn the component's `prN` namespace checks into provider-side per-record permission restrictions.
- `ui` read grant: a production grant with the deployment's base scope (never `admin`), minted once by a production admin and set as the dedicated project's preview secret `CF_PREVIEW_READ_TOKEN`. It reaches only the layer; the browser never sees it. Rotate it by minting a new grant and revoking the old.
- `ui` viewer gate: one Cloudflare Access application on `*.<domain>` (the policy names the reviewers), plus a Bypass application for `*.<domain>/__preview/revision` so the publish probe gets through. The layer re-checks the assertion and its own allow list, so a missing or misconfigured Access app fails closed.
- Secrets belong only in the publish/retire job's protected environment, not in the build. Secret values never appear in action argv, receipts or comments.

Pages uses a reserved `pr-N` branch, a proxied CNAME to that branch's `pages.dev` alias, and Pages Custom Domain registration. The component waits a bounded interval for valid TLS and the captured full SHA on the custom origin. Redirects, another revision and mere reachability cannot pass.

The full-build adapters (`publishPages` with the caller's bindings, native Workers Previews via `publishWorker`, `wrangler preview --ignore-base-config`, Wrangler ≥ 4.135.0) remain in `preview.mjs` as `stack`'s building blocks; no mode reaches them yet.

## Cleanup and its boundaries

Reserve the declared `prN.<domain>` and Pages `pr-N` branches for this tool. Pages uploads carry the exact ownership marker (which names the mode's project) in deployment metadata; DNS carries it as a comment. A pre-existing unowned domain, DNS record or deployment causes a refusal, not a takeover. Production branch/canonical deployment checks run before deletes. Lists are fully paginated. Retry after partial deletion is supported when ownership remains provable; an externally removed proof requires reconciliation. `retire` removes every implemented mode's resources (today `ui`'s), whatever the labels say at close.

After a PR is retargeted, call `retire` for **every configured target it could have used**, not just its final base. Caller concurrency should serialize all publish/close operations for that PR; a closed-state recheck prevents a new publish after closure. If Cloudflare refuses deletion of a branch's last payload, report the failure and retain the reconciliation receipt; do not claim complete retirement.

Receipts identify repository, target, mode, PR, SHA, provider, resource IDs, the layer's upstream and viewer gate, and the GitHub run. A bot comment is updated rather than appended on every successful rebuild; close marks it retired. Provider/DNS failures and missing ownership remain job failures. No existing deployments are cleaned by merely adding this action's source to a repository.

A small caller workflow is shown in [the example]: a trusted gate, a credential-free build, and a trusted publisher. PR source/config is never checked out or installed in the credentialed publisher. Provide `sha` and `mode` from gate, not inputs editable by the PR. Do not interpolate comment bodies, branch names or PR titles into shell commands.

Run `node --test .github/actions/cf-preview/preview.test.mjs` for the dependency-free tests: mode selection, the layer's forwarding and refusals, the Access gate, the `ui` project/directory checks and staging, both full-build adapters, authorization and lifecycle guards. Remaining acceptance: a real disposable `ui` preview's create/update/retire/retry, DNS/SSL, the Access applications, and source publication.

[the spec]: ../../../specs/cf-pr-previews.md
[the example]: examples/preview.yml
