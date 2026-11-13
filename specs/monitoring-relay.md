# Monitoring alerts through the deployment's own bot

Cloud Monitoring alert policies (`CronAlerts`, and any later ones) reach a deployment's Slack the way every other disky alert does: its own bot, through `disk-tree notify`, created entirely by the stack. No console OAuth step, no Google Slack app.

## Why

Monitoring's Slack notification channel needs an OAuth grant made in the console, so Pulumi can't create it: each deployment's alerts would hang on a hand-made resource, posted by a different app than its other alerts. Every job already posts with the deployment's bot (`SLACK_BOT_TOKEN` + `SLACK_ALERT_CHANNEL`, `disk_tree.notify.push`); the relay puts Monitoring on the same path.

## Design (GCP: `MonitoringRelay`, `infra/gcp/gcp_jobs.py`)

```
alert policy ──▶ notification channel (type pubsub) ──▶ topic ──push (OIDC)──▶ Cloud Run service ──▶ disk-tree notify ──▶ Slack
```

- **Topic** `<name>`, published to by the project's Monitoring notification agent (`service-<number>@gcp-sa-monitoring-notification.iam.gserviceaccount.com`, `roles/pubsub.publisher` on the topic). GCP creates that agent with the project's first Pub/Sub channel, so the grant depends on the channel.
- **Notification channel** of type `pubsub`, `labels.topic` = the topic. `.channel` is its id, for `CronAlerts(channels=[…])` or any alert policy.
- **Service** `<name>` (Cloud Run, the deployment's job image) runs `dt-cloud monitoring-relay serve -c slack` as its own account (`account_id`). That account can read the `secret_env` secrets (the bot token) and nothing else. It scales to zero between alerts and runs at most one instance.
- **Push subscription** to the service's URL with an OIDC token minted as that same account: `run.invoker` on the service, which Cloud Run checks, so the service admits no one else. The Pub/Sub service agent holds `serviceAccountTokenCreator` on the account to mint the token. The component also enables `pubsub.googleapis.com`, abandoned on destroy.
- **Delivery**: the service answers 204 once a channel took the notice and 500 when none did. Pub/Sub then redelivers, backing off from 10 s to 10 min, for up to a day (`message_retention_duration`). A body that isn't a Monitoring notification is posted raw, never dropped, so a schema change shows up in Slack.

Cloud Run pins the digest `:latest` pointed to when the revision was made. A rebuilt image reaches the relay through a new revision (`gcloud run deploy <name> --image <image>`); `pulumi up` alone doesn't redeploy it, since the image string hasn't changed. The relay's code (`dt_cloud/monitoring_relay.py`, `disk_tree/notify/push.py`) changes rarely.

### Message

`dt-cloud monitoring-relay render FILE` prints what the relay would post for a notification's JSON (`-s` posts it instead, as a by-hand test of the channel). An open incident:

```
*⚠️ gcs-usage-snapshot-submit failed*
a failed execution · opened 2026-11-11 07:05 UTC
A run of the Cloud Run job `gcs-usage-snapshot-submit` failed, so today's Batch job may not exist. Its logs say why; re-run it once fixed (a no-op if the job exists): `gcloud run jobs execute gcs-usage-snapshot-submit --region &lt;region&gt; --project …`
<https://console.cloud.google.com/monitoring/alerting/incidents/…|incident>
```

A closed incident:

```
*✅ resolved: gcs-usage-snapshot-submit failed*
a failed execution · open 2026-11-11 07:05 → 07:35 UTC (30m)
<…|incident>
```

A repeated notification (`renotify`) is titled "(still open)". The text is Slack mrkdwn: `&`, `<`, `>` are escaped, and Markdown links in the policy's documentation become Slack links.

`notify` posts top-level, so a "resolved" line is its own message, not a reply in the open incident's thread. Threading it would take state: the open message's `ts` per incident id, kept in the service, a bucket or D1, or found again through `conversations.history`, which needs a scope the bots don't have.

### Not covered

- Monitoring failing to publish (the agent's grant removed, the topic deleted) loses the alert silently. Monitoring logs its own delivery failures, but nothing alerts on them.
- A relay that never answers (a bad image, a missing secret) is retried for a day, then dropped. `serve` exits at start-up when its channel has no credentials, so the revision fails to become ready and the `up` that made it fails.

## Rollout (gcs as the example)

A deployment opts in with one stack config value. On gcs, `alertRelay: true` creates the relay and adds its channel to the daily scan's `CronAlerts`. `alertChannel` (a console-made channel's id) still works beside it, so both can run while the relay is checked.

1. Rebuild the job image from a checkout that contains the relay (`job/build.sh`). The service's first revision runs `dt-cloud monitoring-relay serve`, which must already be in `:latest`.
2. From `infra/gcp`: `pulumi config set -s gcs alertRelay true`, then `pulumi up -s gcs`.
3. Check it end to end. Publish a fixture to the topic (`gcloud pubsub topics publish gcs-alert-relay --message "$(cat incident.json)" --project <project>`) and see it posted in #gcs-admin. The service's logs show `monitoring-relay: message <id>: …: slack: sent`.
4. Then `pulumi config rm -s gcs alertChannel` and `pulumi up -s gcs`, and delete the console channel by hand.

## Other clouds

**AWS** (cw, m3): CloudWatch alarm → SNS topic → Lambda → notify. The alarm's `AlarmActions` / `OKActions` name the topic, and the Lambda subscribes to it. The SNS message carries the alarm JSON (`AlarmName`, `NewStateValue` `ALARM` | `OK`, `NewStateReason`, `AlarmDescription`, `StateChangeTime`), which maps onto `render`'s fields: `ALARM` is open, `OK` is resolved, and the description is the documentation. The Lambda image or zip needs only the stdlib `disk_tree.notify.push` plus a renderer for the alarm shape. The bot token comes from Secrets Manager or SSM, read by the function's role. It is the same shape as the GCP relay: a topic the alerting service publishes to, a subscriber holding only the bot token, all in the stack. SNS retries Lambda deliveries itself, with an optional DLQ. Not built yet; a `render_cloudwatch` beside `render` would share `notice_for`'s fallback.

**Cloudflare** (the r2 demo) has no alerting service to relay. A Cloudflare deployment's jobs have to report their own failures (`disk-tree notify` from the job, as gcs's submitter and steps do), and nothing reports a job that never ran. The r2 demo's daily ingest is a GitHub Actions cron, and today its only failure signal is GitHub's own failed-run email.
