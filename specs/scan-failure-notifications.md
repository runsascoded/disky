# Scan failure notifications (macOS first, push as the fallback)

From the m3 session, 2026-10-14. Ryan: a failed scan should notify him; a macOS notification "sounds slick", but it needs a fallback when it can't be shown (the app isn't running, notifications aren't permitted). Pushover and his private Discord server are the fallback channels.

## What already exists (on `m3`, `[local]`, landing on `local`)

- `disk-tree notify [-c CHANNELS] [-f] [-l LABEL] [-p PRIORITY] [-t TITLE] [-u URL] MESSAGE` (`src/disk_tree/notify/push.py`). Channels: `pushover` (`PUSHOVER_TOKEN` + `PUSHOVER_USER`) and `discord` (`DISCORD_NOTIFY_WEBHOOK`), configured by env. `DISK_TREE_NOTIFY` / `-c` orders them, and `-f` stops at the first that succeeds. Exit 2 = nothing configured, 1 = every attempt failed.
- The cloud ingest (`infra/aws/ingest.sh`) calls it when the ingest fails, so ingest failures already reach Pushover/Discord without the laptop.

## What the app should add

1. **Laptop-side failures notify.** Scan job (`disky job scan`) failures: the capture exits non-zero, the walk is refused (no Full Disk Access), the R2 upload fails, or a slot is missed because the job couldn't start. Also drain job failures.
2. **macOS first.** Post a `UNUserNotificationCenter` notification (the `objc2-user-notifications` crate is already in `Cargo.lock`). Clicking it opens the run, or the menu's log view.
3. **Fallback.** When `getNotificationSettings` says the app isn't authorized (or the post errors), run `disk-tree notify -f` with the same title and message. Its env comes from the same place the drain job's `CLOUDFLARE_API_TOKEN` does (`direnv export json` of the m3 worktree), so no app config holds a secret.
4. **Ask once.** Request notification authorization the first time a scan job runs. The menu shows the state ("Notifications: on / off — failures go to Pushover").

## Not the app's job

- Ingest failures (cloud): `ingest.sh` already pushes those. The app may also surface them (it could poll `/api/scan-runs`), but nothing depends on that.
- A scan that never happens because the laptop is asleep or off: nothing on the laptop can see that. A cloud watchdog (a cron on the capture-trigger Worker that flags "no succeeded ingest in > 14 h") is the m3/local session's to build.
