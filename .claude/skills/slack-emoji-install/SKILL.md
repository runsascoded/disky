---
name: slack-emoji-install
description: Install disky's custom emoji pack (trend arrows, runway colors, arrow chips) into a deployment's Slack workspace — check which are missing, build just those PNGs, then walk the person through Slack's upload page in Chrome, stopping before each Save. Use when the user says "install the emoji", "the workspace is missing disky's emoji", "`emoji check` says N missing", "/slack-emoji-install", or a digest/thread shows a stock emoji where a disky one should be.
argument-hint: "[deployment, or the workspace's slack.com URL] [-k family …]"
---

# `/slack-emoji-install` — put disky's emoji pack in a Slack workspace

disky's posts name custom emoji from its vocabulary (`cloud/src/dt_cloud/emoji_vocab.py`, specs/emoji-vocabulary.md): `:arrow_degN:`, `:runway-N:` / `:runway-<shape>-N:`, `:pool-chip-N-D:`. A workspace without one gets its stock emoji instead (the writers' fallback), so nothing breaks; installing the pack makes the posts look as designed. Slack adds workspace emoji by API only on Enterprise Grid (`admin.emoji.add`); everywhere else it's the upload page, one emoji at a time. This skill does everything but the Saves.

## 1. Which workspace, which emoji

- The deployment comes from the args or the conversation (gcs, cw, hf, r2, the r2 demo, …). Use **its** Slack bot token, from its own environment (its worktree's `.envrc` / the way its digest job gets `SLACK_BOT_TOKEN`). Never another deployment's token (memory `no-borrowing-other-deploy-secrets`), and never print a token's value.
- Run, in that environment:
  ```bash
  dt-cloud emoji check > tmp/emoji-missing.tsv; echo "exit $?"
  ```
  (add `-k trend -k runway -k pool` or narrower prefixes if the args name families). Stdout: one `<key>\t:<name>:` line per missing emoji; stderr: `emoji check: <team> (<https://<workspace>.slack.com/>): N of M disky emoji missing`.
  - Exit 0: nothing missing; say so and stop.
  - Exit 3: the workspace's emoji can't be read (no `emoji:read` on the bot, bad token); report the stderr line and stop.
  - Exit 1: continue. **Write down the workspace URL from stderr**: it is the only workspace this run may touch.

## 2. Build only the missing ones

```bash
rm -rf tmp/emoji-install && dt-cloud emoji build -o tmp/emoji-install -K tmp/emoji-missing.tsv -s tmp/emoji-install-sheet.png
```

- `tmp/emoji-install/` then holds exactly the missing emoji, each `<name>.png` (128 px, transparent, ≤ 128 KB), so Slack's upload dialog fills in the right name from the file name. The contact sheet sits outside that folder; show it to the person (Read the PNG) before uploading, so they see what's going in.
- `emoji build` needs Pillow (dt-cloud's `emoji` extra). If it's missing, the root checkout's `uv sync --all-packages --all-extras --all-groups` installs it (not from a worktree: a worktree's `uv sync` repoints the shared `.venv`).

## 3. Open the workspace's emoji page

Identity-scoped, so follow CLAUDE.md's "Picking the right Chrome profile": the profile signed in to **that** workspace (OA's Slack → `openathena`, …); `chrome-profiles <query>`, `select_browser`, and say which profile/email you picked.

- `tabs_create_mcp`, then navigate to `<workspace URL>customize/emoji` (e.g. `https://test-co.slack.com/customize/emoji`).
- If it lands on a sign-in or workspace chooser: stop, and ask the person to sign in **to that workspace** in the tab. Never type credentials, never pick a workspace for them.
- Confirm the page is that workspace (the host matches the URL from step 1, and the page names the team from step 1). If it doesn't, stop: **never add emoji to another workspace**, even one the person is also in.

## 4. One emoji at a time, stopping before each Save

For each file in `tmp/emoji-install/` (in `ls` order):

1. Re-check the tab's URL host is still the workspace from step 1 (a click can land elsewhere); stop if not.
2. Click **Add Emoji** (Slack's button on that page), then set the dialog's file input to the PNG with `file_upload` (by ref from `find` / `read_page`). Don't drag through the OS.
3. Read the dialog: the name field must equal the file's basename without `.png` (`arrow_deg-30`, `runway-star-3`, `pool-chip-8--50`). Slack fills it from the file name; if it differs (Slack sometimes strips characters), fix it with `form_input`, never `computer type` (Vimium). If Slack refuses the name as taken, the workspace has a different emoji by that name: stop and report, don't rename ours.
4. Screenshot so the preview and name are visible, then **stop before Save**: tell the person "`<name>` is ready: click Save, then say next". Don't click Save yourself.
5. On "next", confirm the emoji landed (the dialog closed; the list shows the name), then the next file.

Keep a running tally (`k of n`). If the person says stop, stop; whatever is saved stays.

## 5. Confirm

```bash
dt-cloud emoji check > tmp/emoji-missing-after.tsv; echo "exit $?"
```

Exit 0 (or only the ones the person skipped, listed) is done. Report the before/after counts and anything left. Posts already in the channel keep their stock emoji until they're re-rendered; new posts and edits use the custom ones from the next run on (each run reads the workspace's emoji once).

## Discord

Discord's application emoji go up by API, no browser: `dt-cloud emoji build -o tmp/emoji-discord` (the whole pack, or `-K` a list), then `dt-cloud discord-emoji -i tmp/emoji-discord` with the deployment's `DISCORD_BOT_TOKEN` (idempotent; `-n` dry-runs). The Discord twin renders any the app lacks as stock emoji.

## Notes

- Never delete or replace an existing workspace emoji, never upload to a workspace other than the one `emoji check` named, never click Save.
- Never close the tab — it's the person's.
