# Emoji vocabulary: semantic keys, packs, fallbacks, and an /admin page to change them

Ryan, 2026-11-21/22 (#84 in `specs/open-questions.md`). disky's Slack posts encode a lot in emoji: trend arrows (`:arrow_degN:`, ↗️➡️↘️), runway colours (`:runway-N:`, `:pool-chip-N-D:`, 🔴🟠🟡🟢), run outcomes (✅ ⚠️ ❌ ⏳ 🚫), triggers (⏰ 👤 / a starter's face), deploy results (🚀 🧪 💥 ⏹️). Before this, each was chosen in its own code path, a few had per-deployment overrides (`nominal.emoji`, `FLEET_EMOJI`, `deploy_emoji`), and the custom ones had no fallback: a workspace without them showed the raw `:name:` text.

Status: §1, §3 and §4 implemented (2026-11-22); §2 and §5 open.

## 1. Semantic keys

One registry, `cloud/src/dt_cloud/emoji_vocab.py` (`KEYS`), of keys with a meaning, a stock emoji (as characters, `stock`, and as Slack codes, `code`) and, for the `disky` pack, the custom image's Slack name (`custom`) and Discord application-emoji name (`discord`: `[A-Za-z0-9_]` only, so `arrow_deg-30` → `arrow_degm30`, `runway-star-3` → `runway_star_3`, `pool-chip-8--50` → `pool_chip_8_m50`). The TS side (fleet, site) reads the same table as `packages/pages-kit/src/emoji/vocab.json` (`dt-cloud emoji vocab > …`; `test_emoji_vocab.py` keeps them byte-equal) through `@disk-tree/pages-kit/emoji` (`code`, `stock`, `fallback`, `renders`, `workspaceCustom`).

- `trend.<deg>` (−80…80 by 10): the trend arrow at that slope; custom `arrow_deg<deg>`; stock ➡️ at 0, ↗️ / ↘️ up to ±60, ⬆️ / ⬇️ at ±70 and ±80.
- `runway.<1..8>`: a pool's runway colour, red → green; custom `runway-<n>`; stock 🔴🔴🟠🟠🟡🟡🟢🟢 (the `runway` marks' four bands: under 3 days, under ~10, under ~a month, else).
- `runway.<shape>.<1..8>` (square, diamond, triangle, star: `digest.QUOTA_SHAPES` but the circle): custom `runway-<shape>-<n>`; stock the same colour, squares 🟥🟧🟨🟩 for `square`, circles for the rest.
- `pool.<n>.<deg>`: the arrow chip (colour × slope); custom `pool-chip-<n>-<deg>`; stock the `runway.<n>` colour alone. All 136 are keys; only the 96 a writer can produce are `reachable` (a shrinking pool is only colour 1, 3 or 8: cw's `job/gen-pool-chips.py`'s set), and `emoji build` / `check` cover only those unless `-a`.
- `run.<clean|issues|failed|running|cancelled>`: ✅ ⚠️ ❌ ⏳ 🚫 (no custom image).
- `trigger.<schedule|manual|unknown>`: ⏰ 👤 ❔.
- `run.<outcome>.<schedule|manual>` (composites, #84): stock the two keys side by side; no custom image yet (Order 5).
- `deploy.<env>.<success|failure|cancelled>`: 🚀 / 🧪 / 💥 / ⏹️.

The writers ask the vocabulary for a key: the digest (`digest_quota`: trend arrows, `runway-hue` and `arrow-chip` marks, the `circles` / `runway` marks' four stock circles), the site's digest renderers (`site/src/digest/{cost,quota,quotaReply,pools}.ts`, whose OP text the job posts), nominal (`EMOJI`, `ICON`'s outcomes, `TRIGGER_EMOJI`), fleet (`DEFAULT_EMOJI`, `TRIGGER`). `site/deploy-notify` is a standalone `uv` script that can't import `dt_cloud`; its `DEFAULT_EMOJI` stays a literal, and a test holds it equal to the `deploy.*` keys. The existing overrides (`nominal.emoji`, `FLEET_EMOJI`, `deploy_emoji`) work as before, over the vocabulary's stock.

## 2. Packs and per-deployment mapping

A **pack** maps keys to emoji: `stock` (built in), `disky` (the custom images we ship: arrows, runway chips, composites), and variants such as `circles` / `squares` / `arrows-plain` for the colour and trend keys. A deployment's mapping is: its chosen pack per key family, then per-key overrides; anything unmapped falls to `stock`. It's stored in D1 (`emoji_vocab` table, edited on `/admin`) and read by the job (digest, nominal) and the site / fleet. The existing overrides (`nominal.emoji`, `FLEET_EMOJI`, `deploy_emoji`) become per-key overrides and keep working.

Learned in §1/§3: today's only pack is `disky` (`emoji_vocab.PACKS`); a key's `stock` is the stock pack. Until a deployment's mapping exists, "a deployment's mapped names" is the whole reachable `disky` pack: the digest's marks are switchable live on `/admin/slack` (`quota_marks`), so a deployment can start naming any family without a deploy. `emoji check -k` narrows it.

## 3. Fallback

A custom emoji renders only if the workspace has it. Each writer reads the workspace's custom emoji once per run and, for a vocabulary custom name the workspace lacks, writes that key's stock emoji instead (`emoji_vocab.fallback`):

- **Reading.** thrds' `SlackClient.fetch_workspace_emoji` (`emoji.list`, aliases followed, deleted ones dropped; the bots have `emoji:read`), wrapped by `emoji_vocab.workspace_custom`; TS: `workspaceCustom` over thrds' `SlackClient.api('emoji.list')`. A failure (no scope, network) logs one line and leaves the workspace unknown: custom names are written as before, the post goes out.
- **Digest (Slack).** `dt-cloud digest` wraps its thrds client in `emoji_vocab.FallbackClient`: every post's and edit's text (OP, replies, provisional, quota alert, re-applies, redos) and a custom `icon_emoji` get the fallback. It reads the workspace on the first write, so a dry run or a preview reads nothing. State and comparisons keep the unrewritten text, so a run doesn't re-edit a message because its emoji fell back.
- **Digest (Discord twin).** `discordify` now covers every vocabulary custom name (was: arrows only, raising on a missing one): an app emoji where `discord-emoji` uploaded it, else the key's stock characters.
- **Nominal threads.** The Slack transport applies the fallback to posts and edits (before the send-intent metadata is keyed by the text), and `faces` drops a manual run's starter's own emoji (`trigger_emoji`, from OA's broker, i.e. OA's workspace) where the posting workspace lacks it, so `starter` shows 👤 and the address.
- **Fleet.** The hourly post reads the workspace once (not on `?dry=1`) and shows a starter's emoji only where it renders (`renders`), else 👤.
- **Left alone.** Alerts (`scan_runs.post_alert`, `monitoring-relay`), staged-deletion cards and deploy notices write only stock emoji. `SLACK_PR_EMOJI` (site activity posts) is a custom name configured per workspace for that workspace. Overrides given as `:name:` codes (`nominal.emoji`, `FLEET_EMOJI`) aren't checked: `emoji.list` names only custom emoji, so a code it doesn't name may be a stock one; `emoji.list`'s `include_categories` would list the stock names too, if §2 needs overrides checked.

`dt-cloud emoji check [-k KEY]… [-a]` prints the pack's custom names the token's workspace (`$SLACK_BOT_TOKEN`) lacks, one `<key>\t:<name>:` a line, with the workspace and a count on stderr; exit 0 when none are missing, 1 when some are, 3 when the workspace's emoji can't be read.

## 4. The pack's images and installing them

`dt-cloud emoji build -o DIR [-p PACK] [-k KEY]… [-K FILE] [-a] [-s SHEET]` (`emoji_draw.py`; Pillow is dt-cloud's `emoji` extra) writes each selected key's `<custom>.png` (128 px, transparent, ≤ 128 KB, refused past it) into `DIR` and nothing else, so the folder is exactly what gets uploaded and Slack's dialog names each from its file. `-K` reads keys from `emoji check`'s output (`emoji check | emoji build -o DIR -K -`); `-s` writes a contact sheet (light and dark rows) outside `DIR`. It folds in `job/gen-delta-arrows.py` (removed; the arrows come out pixel-identical to `job/icons/arrows/`, which a test checks) and cw's `job/gen-pool-chips.py` (same geometry; cw drops its copy on its next merge). Nothing in the repo drew the `runway-*` emoji; `emoji_draw.runway` draws them as the site's `slack.scss` `.hue` shapes in the `RUNWAY_HUE` colours, so a workspace that already has hand-made ones keeps them (they're never replaced).

Installing:
- **Slack:** only Enterprise Grid can add workspace emoji by API (`admin.emoji.add`); elsewhere it's an upload at `https://<workspace>.slack.com/customize/emoji`, one emoji per dialog. The project skill `.claude/skills/slack-emoji-install/SKILL.md` runs `emoji check`, builds only the missing ones, opens that page in the workspace's Chrome profile, fills each dialog (file and name) and stops before each Save, refuses any other workspace, and ends with `emoji check`.
- **Discord:** `dt-cloud discord-emoji -i DIR` uploads every vocabulary PNG in `DIR` (`emoji build`'s output; default `job/icons/arrows`, the arrows) as application emoji under its Discord name; idempotent.

## 5. /admin page and applying a change

`/admin/emoji`: the keys grouped by family, each showing the mapped emoji as the workspace would render it (with the fallback when missing), a pack picker per family, and per-key overrides. Saving a change stores it; **applying** it to existing messages goes through thrds' bulk rewrite (`thrds/specs/bulk-rewrite.md`): find the deployment's messages that carry the old emoji, re-render each from disky's data (the digest's reapply path, the scan-run threads' converge, the fleet post), plan, show the diffs on the page (thrds' `PlanView` with disky's renderers), and dispatch the selected actions. A change that needs a new sender (an emoji in a `username` or icon) shows as a repost or as blocked, per thrds' rules (no reposting a thread head that isn't the channel's newest).

Learned in §3: the page can show "as the workspace would render it" with `@disk-tree/pages-kit/emoji`'s `code(key, have)` over `workspaceCustom`; messages posted while an emoji was missing carry its stock code, so applying an install to old messages is the same re-render.

## Order

1. Fallback (§3) for today's custom names: fixes raw `:name:` text in other workspaces now. Done.
2. The registry (§1) with the current choices as `stock`/`disky`, writers moved onto it. Done.
3. Packs + D1 mapping + `/admin/emoji` read/edit (§2, §5 without apply).
4. Bulk apply, once thrds' bulk rewrite exists.
5. The composite images (#84's trigger × outcome), previewed at Slack's ~22 px first.
