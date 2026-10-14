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

## Implemented (`tauri-native-app`, 2026-10-14)

`src-tauri/src/notify.rs`, wired into `jobs.rs` and the menu (`lib.rs`).

- **What alerts.**
  - A scan job that exits with an error. Its message is the error log's last line, e.g. the `dt-capture` upload error.
  - A drain job that exits with an error, at most once per 6 h, since launchd restarts it.
  - A job that can't start: bad config, unopenable logs, a failed lock, an empty command.
  - A scan that starts without Full Disk Access, at most daily. The walk "succeeds" but misses every protected folder.
  - A retry of an interrupted run that is itself interrupted, once per run.
  - A scheduled slot that hasn't started an hour later. The app's 20 s refresh loop watches for this, since the scan job can't report its own absence. The hour is time *awake*: `Instant` stops while the Mac sleeps, so a slot slept through is the next wake's to run, not a miss.
  - Not alerted: exits by signal (128+N), which are the interruption the retry handles.
- **Route.**
  - When macOS allows disky to post, a `UNUserNotificationCenter` notification, keyed by alert, so a repeat replaces the previous one. A click opens the log, or the Full Disk Access pane (`userInfo.open`, read by the app's delegate).
  - **Deviation:** macOS refuses this to any app without a Developer ID. The self-signed m3 build and the ad-hoc pilot both get "Notifications are not allowed for this application" (`UNErrorDomain` 1), status Denied, without ever prompting.
  - So the fallback is a banner through AppleScript (`display notification`, posted as Script Editor; a click opens Script Editor) **plus** the push fallback when configured. That banner was delivered on m3 (2026-10-14).
  - The push fallback is the optional `"notify": {"command": [...]}` in `disky.json`, run with `-t TITLE MESSAGE` appended.
  - Native notifications start working once the Developer ID build lands; nothing to change.
- **Asking.**
  - Asked when "Scan now" runs from the menu or Settings, and at launch while scheduled scans are on. Only while the status is "not determined"; the jobs themselves never prompt.
  - The menu line shows the route: "Failure alerts: notifications ✓", "allow notifications…" (click asks), "banner + push", or "banner only (no push)".
- **Throttle.** `disky-alerts.json` holds `{key: last sent}`. Entries older than 30 days are dropped.
- **Testing.** `disky alert TITLE MESSAGE` sends a test alert by the same path, and `disky alert ask` requests permission and prints the answer.

Verified on m3 with the installed self-signed build:
- Permission is Denied without a prompt, and the route falls back to the banner.
- A banner was delivered (the usernoted `record`).
- A temp-config drain job exiting 3 alerted with its stderr's last line; a second failure within 6 h was suppressed.
- A temp-config `notify` command ran, and its exit-2 "no channel configured" was logged.

### Left for m3 (deployment)

- The m3 env has `PUSHOVER_USER` but neither `PUSHOVER_TOKEN` nor `DISCORD_NOTIFY_WEBHOOK`, so `disk-tree notify` has no channel yet.
- Once one is set, the real `~/.config/disk-tree/disky.json` needs:

  ```json
  "notify": {"command": ["/opt/homebrew/bin/direnv", "exec", "/Users/ryan/c/disky/wt/m3", "/Users/ryan/c/disky/wt/m3/.venv/bin/disk-tree", "notify", "-f"]}
  ```

  Not added yet: it changes the real config.
