# A deployment's Slack app

Every deployment talks to Slack through one bot of its own, made from [`usage-bot.manifest.json`](usage-bot.manifest.json). That bot posts:
- the monthly usage digest (`dt-cloud digest`, through `thrds`), in `#<deploy>-usage`;
- the per-scan run thread, its pinned head and the channel canvas (`nominal`), and the scan job's failure alerts (`disk-tree notify`), in `#<deploy>-admin`;
- the site's activity posts (`site/functions/_lib/activity.ts`) and, where the store has staged deletions, its plan threads with their buttons (`_lib/stagedSlack.ts`, `/slack/actions`), in `#<deploy>-admin`;
- release notices (`site/deploy-notify`, from OA's release controller), in `#<deploy>-admin`.

Each deployment gets its own app, not a shared one: per-message sender overrides don't hide the app's name in Slack's Unread and Threads views, so the app itself must name the store.

## Setting one up

1. **Channels.** Create `#<deploy>-usage` (the digest, nothing else) and `#<deploy>-admin` (threads, alerts, activity, notices). Public channels need no invite: the bot posts with `chat:write.public` and joins by itself (`channels:join`) before pinning or reading a thread. A private channel needs `/invite @<Store> Usage Bot`.
2. **Manifest.** Copy the template to the deployment's branch (e.g. `job/slack/<deployment>-usage-bot.manifest.json`) and fill in `<Store>`, `<what>` and `<host>`. Without staged deletions, set `settings.interactivity` to `{"is_enabled": false}` and drop "staged-deletion review" from the description.
3. **Create.** api.slack.com/apps → *Create New App* → *From a manifest* → the workspace → paste the JSON → *Create*, then *Install to Workspace*.
4. **Icon.** Under *Basic Information* → *Display Information* (the API can't set one), upload `icons/<key>.png`: the store's mark (the site's favicon, `scripts/marks/`) with its tiles filling the frame (`avatar`; Slack rounds the corners) at 1024 px, written by `node scripts/marks/build.mjs` for `gcs`, `cw`, `hf` and `r2` (and `disky`, the fleet bot). Add a key there for a new deployment.
5. **Token.** Store the Bot User OAuth Token (`xoxb-…`) in each place below that the deployment uses, and nowhere else.

| Where | Name | What it posts |
|---|---|---|
| the scan job's secrets (the deployment's secret store) | `SLACK_BOT_TOKEN` | digest, run thread, pin, canvas, alerts |
| the site's Pages secrets (prod and preview) | `SLACK_BOT_TOKEN`, with the var `SLACK_ADMIN_CHANNEL` (`#<deploy>-admin`'s id) | activity posts, staged-plan threads |
| the site's Pages secrets, with staged deletions | `SLACK_SIGNING_SECRET` (*Basic Information* → *Signing Secret*) | verifies the plan buttons at `/slack/actions` |
| OA's release environments `<deploy>-dev` and `<deploy>-prod` (GitHub) | `SLACK_DEPLOY_BOT_TOKEN` | release notices, to `admin_channel` in `deploy/oa/sites.json` |

Unset, each of those posts nothing and fails nothing. The job's digest and channel ids live in its own config (`job/digest.yml`, the scan-run profile's `nominal` block).

## Scopes

The manifest has no comments because Slack rejects unknown top-level keys, so here is what each scope is for:

| Scope | Used for |
|---|---|
| `chat:write` | posting and editing every message above (`chat.postMessage`, `chat.update`, `chat.delete`, `chat.getPermalink`) |
| `chat:write.customize` | each message's own sender name and face (digest replies, activity posts, the scan thread's status senders, alert icons, deploy notices) |
| `chat:write.public` | posting to a public channel the bot hasn't joined |
| `channels:history`, `groups:history` | reading threads back: the digest converging its replies, a run's alerts found again and grouped into one message, the run thread (`conversations.history`, `conversations.replies`) |
| `channels:read`, `groups:read` | channel lookups (`conversations.info`, `conversations.list`), the channel's canvas id |
| `users:read` | `users.info`, for people's names and faces |
| `users:read.email` | matching people by email: a plan button's clicker checked against the deployment's admins (`users.info`), and the faces of people the site names (`users.lookupByEmail`) |
| `emoji:read` | the workspace's custom emoji (`emoji.list`) |
| `channels:join` | joining `#<deploy>-admin` / `#<deploy>-usage` itself (`conversations.join`): pinning and reading a thread's replies need membership |
| `pins:write` | pinning the open run thread's head and the current month's digest thread, and unpinning the ones they replaced |
| `canvases:write` | the `#<deploy>-admin` canvas, the deployment's at-a-glance status (`conversations.canvases.create`, `canvases.edit`) |

A missing pin, join or canvas scope never fails a job: the run thread and digest log a warning and retry next run.

## Changing scopes

Paste the updated manifest under *App Manifest*, then *Reinstall to Workspace*. The bot token normally stays the same; check the job still posts. To see what an installed token actually has, call `auth.test` with it: the response's `x-oauth-scopes` header lists the granted scopes.
