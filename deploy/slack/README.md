# A deployment's Slack app

`usage-bot.manifest.json` is the template for a deployment's own Slack app: the bot `dt-cloud digest` posts the usage thread as (through `thrds`), the scan job's failure alerts (`disk-tree notify`), and, where the store has staged deletions, the site's plan threads (`site/functions/_lib/stagedSlack.ts`, `/slack/actions`). Each deployment gets its own app: per-message sender overrides don't hide the app's name in Slack's Unread and Threads views, so the app itself must name the store.

To make one, copy the template to the deployment's branch (e.g. `job/slack/<deployment>-usage-bot.manifest.json`) and fill in `<Store>`, `<what>` and `<host>`. Without staged deletions, set `settings.interactivity` to `{"is_enabled": false}` and drop "staged-deletion review" from the description. Then:

1. api.slack.com/apps → *Create New App* → *From a manifest* → the workspace → paste the JSON → *Create*.
2. *Install to Workspace*.
3. Store the Bot User OAuth Token (`xoxb-…`) as the job's `SLACK_BOT_TOKEN` secret (and as the Pages secret `SLACK_BOT_TOKEN` when the site posts plan threads). Never paste it anywhere else.
4. Upload an app icon under *Basic Information* (the API can't set one).

The manifest has no comments because Slack rejects unknown top-level keys. The scopes are what thrds's `SlackClient` calls (`chat.postMessage/update/delete/getPermalink`, `conversations.history/replies/info/list`, `users.info`, `emoji.list`), plus `chat:write.customize` for the per-message sender and avatar. `chat:write.public` lets the bot post to public channels without an `/invite`; a private channel still needs one.
