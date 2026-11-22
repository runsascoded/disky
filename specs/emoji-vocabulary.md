# Emoji vocabulary: semantic keys, packs, fallbacks, and an /admin page to change them

Ryan, 2026-11-21/22 (#84 in `specs/open-questions.md`). disky's Slack posts encode a lot in emoji: trend arrows (`:arrow_degN:`, ↗️➡️↘️), runway colours (`:runway-N:`, `:pool-chip-N-D:`, 🔴🟠🟡🟢), run outcomes (✅ ⚠️ ❌ ⏳ 🚫), triggers (⏰ 👤 / a starter's face), deploy results (🚀 🧪 💥 ⏹️). Today each is chosen in its own code path, a few have per-deployment overrides (`nominal.emoji`, `FLEET_EMOJI`, `deploy_emoji`), and the custom ones have no fallback: a workspace without them shows the raw `:name:` text.

## 1. Semantic keys

One registry (`cloud/src/dt_cloud/emoji_vocab.py`, mirrored for the site/fleet TS as generated JSON) of keys with a meaning and a stock default:

- `trend.<deg>` (−80…80 by 10): the trend arrow at that slope; stock: ↘️ / ➡️ / ↗️ by sign band (⬇️ ⬆️ at the ends).
- `runway.<1..8>`: a pool's runway colour, red → green; stock: 🔴🟠🟡🟢 spread over the eight.
- `pool.<hue>.<deg>`: the chip (colour × slope); stock: the `runway` colour alone.
- `run.<clean|issues|failed|running|cancelled>`: ✅ ⚠️ ❌ ⏳ 🚫.
- `trigger.<schedule|manual>`: ⏰ 👤.
- `run.<outcome>.<trigger>` (composites, #84): stock: the two keys side by side.
- `deploy.<env>.<success|failure|cancelled>`: 🚀 / 🧪 / 💥 / ⏹️.

Every place that writes an emoji asks the vocabulary for a key, never a literal.

## 2. Packs and per-deployment mapping

A **pack** maps keys to emoji: `stock` (built in), `disky` (the custom images we ship: arrows, runway chips, composites), and variants such as `circles` / `squares` / `arrows-plain` for the colour and trend keys. A deployment's mapping is: its chosen pack per key family, then per-key overrides; anything unmapped falls to `stock`. It's stored in D1 (`emoji_vocab` table, edited on `/admin`) and read by the job (digest, nominal) and the site / fleet. The existing overrides (`nominal.emoji`, `FLEET_EMOJI`, `deploy_emoji`) become per-key overrides and keep working.

## 3. Fallback

A custom emoji renders only if the workspace has it. Each writer reads the workspace's custom emoji once per run (`emoji.list`; the bots already have `emoji:read`, cached for the run) and, for a mapped name the workspace lacks, writes that key's stock emoji instead. `dt-cloud emoji check` prints which of a deployment's mapped names are missing.

## 4. The pack's images and installing them

`dt-cloud emoji build -o DIR [-p PACK]` renders a pack's images (≤ 128 KB, ≤ 256 px; folds in `job/gen-delta-arrows.py`). Installing:
- **Slack:** only Enterprise Grid can add workspace emoji by API (`admin.emoji.add`); elsewhere it's an upload at `slack.com/customize/emoji`. Ship a skill that walks it (one tab, drag the folder), then `emoji check` confirms.
- **Discord:** application emoji by API (`dt-cloud discord-emoji`, as the arrows already do), for the whole pack.

## 5. /admin page and applying a change

`/admin/emoji`: the keys grouped by family, each showing the mapped emoji as the workspace would render it (with the fallback when missing), a pack picker per family, and per-key overrides. Saving a change stores it; **applying** it to existing messages goes through thrds' bulk rewrite (`thrds/specs/bulk-rewrite.md`): find the deployment's messages that carry the old emoji, re-render each from disky's data (the digest's reapply path, the scan-run threads' converge, the fleet post), plan, show the diffs on the page (thrds' `PlanView` with disky's renderers), and dispatch the selected actions. A change that needs a new sender (an emoji in a `username` or icon) shows as a repost or as blocked, per thrds' rules (no reposting a thread head that isn't the channel's newest).

## Order

1. Fallback (§3) for today's custom names: fixes raw `:name:` text in other workspaces now.
2. The registry (§1) with the current choices as `stock`/`disky`, writers moved onto it.
3. Packs + D1 mapping + `/admin/emoji` read/edit (§2, §5 without apply).
4. Bulk apply, once thrds' bulk rewrite exists.
5. The composite images (#84's trigger × outcome), previewed at Slack's ~22 px first.
