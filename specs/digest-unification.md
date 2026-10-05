# Digest unification: one engine, two templates

Step 1 of the gcs/cw unification sequence in [`branch-layout.md`] ("Open": digests → sweep executors → `path-index`/`index-write` → identities into D1 → sheet mirror). Per [branches carry exactly their code]: the posting capability lives on `cloud` as a toolkit; each deployment's policy/config lives on its branch.

## Before

Two forks of one mechanism in `cloud/src/dt_cloud/`:

| | gcs | cw |
|---|---|---|
| module | `digest.py` (473) + `digest_plot.py` (135) | `cw_digest.py` (746, cw-s3's newer copy) + `cw_digest_plot.py` (266) |
| CLI | `dt-cloud digest [-P discord]` | `dt-cloud cw-digest [-V sender\|body] [-R -F]` |
| run by | `refs/heads/gcs:job/run.sh` (`digest -r gs://$DATA/snapshots`, then `digest -P discord …`) | `refs/heads/cw-s3:job/cw-run.sh` (`cw-digest -r gs://$DATA/snapshots/cw`) |
| replies | one per scan (date ids), headline as sender, `$/mo` body | one per UTC day (12-hourly `T` ids), `sender` or `body` variant, per-bucket `% of quota (free)` tail |
| plot | storage-class mosaic | quota sparkline + diff treemap |
| state | `digest/<YYYY-MM>.json`, Discord `digest/discord/<webhook>/…` | `digest/cw/<channel>/<variant>/<YYYY-MM>.json` |
| plot host | `gcs-usage-icons` Pages, branch `main` | same project, branch `cw` |

Duplicated: arrow math, formatters, snapshot listing + lead-in windowing, state IO, CDN wait, `wrangler pages deploy`, and the whole converge loop (OP post/edit, per-unit reply post, resumable state). `discord_api.py` was gcs-only in use.

## After

| file | lines | what |
|---|---|---|
| `digest.py` | 680 | the engine: `deg`, formatters, scan ids + `?d=` tokens (`scan_ts`, `_dlink`, `_span`), `Reply`/`Unit`, `emoji_name`/`discordify`, `list_scans`/`load_window`, `state_path`/`load_state`/`save_state`, `pages_deploy`, `converge_slack`, `redo_replies`, `converge_discord`/`post_digest_discord`, `dry_run`; `DigestConfig` + `PRESETS` + `load_config`; the `Template` protocol + `template(cfg)` |
| `digest_gcs.py` | 188 | `Gcs`: rows from `class_bytes` priced by `cfg.prices`, OP + per-scan reply |
| `digest_cw.py` | 349 | `Cw`: primary/extra buckets, `day_rows`, OP + `sender`/`body` replies, quota clauses from `cfg.buckets` |
| `digest_plot.py` | 494 | `render_tiers` (mosaic), `render_quota` (sparkline, quota optional) + `diff_tree`/`squarify`/treemap drawing, one shared style; `python -m dt_cloud.digest_plot -T gcs\|cw` |
| `discord_api.py` | 83 | unchanged (the Discord twin; any template can use it) |

Code lines moved, not grown: engine + templates 1217 vs the two digests' 1219; plots 494 vs 401 (the quota-optional path and the merged ad-hoc CLI). The duplicated converge shells are one each now; the added surface is the config.

### Template protocol

A template wraps a `DigestConfig` and supplies `load(root, month)`, `n_scans`, `op_body(data, month, plot_url)`, `units(data, variant, platform) → [Unit(key, scan, Reply)]`, `provisional(data, variant) → Unit | None` (the open day's provisional reply; see below), `render_plot(data, month, out, root)`, plus `variants`, `edited_variants` (re-edit a unit's reply when a later scan of it lands: cw `body`) and `track_scan` (posted record `{ts, scan}` vs the bare ts — gcs's existing state format, kept so live state files keep working). The engine does the rest; a third style is a third module, not a third converge loop.

The two styles are opinionated but not store-specific: `gcs` = per-scan cost/class-mix, `cw` = per-day headroom across buckets. Nothing in either reads "gcs" or "cw" except through config. (Renaming them by style, e.g. `cost` / `quota`, is a free follow-up once the job scripts pass `-T`.)

### Config surface (`DigestConfig`)

| field | gcs preset | cw preset |
|---|---|---|
| `template` | `gcs` | `cw` |
| `title` | `GCS usage` | `CoreWeave usage` |
| `site_url` | `https://gcs.oa.dev` | `https://cw-s3.oa.dev` |
| `root` | `gs://{DATA_BUCKET}/snapshots` | `gs://{DATA_BUCKET}/snapshots/cw` |
| `state` | `digest` | `digest/cw/{channel}/{variant}` |
| `discord_state` | `digest/discord/{webhook}` | same (unused today) |
| `discord_webhook_env` | `DISCORD_GCS_USAGE_WEBHOOK` | none (`-w` only) |
| `icons_base` (avatars) | `https://gcs-usage-icons.pages.dev` | same |
| `icons_dir` | `job/icons` | `job/icons-cw` |
| `plot_project` / `plot_branch` / `plot_base` | `gcs-usage-icons` / `main` / root alias | `gcs-usage-icons` / `cw` / `https://cw.gcs-usage-icons.pages.dev` |
| `variant` / `reply_hour` | `sender` / — | `sender` / `12` |
| `provisional` | — | `false` (see below) |
| `primary` | — | `$CW_BUCKET` or `marin-us-east-02a` |
| `buckets` | — | `marin-us-east-02a: {label: 02a, quota: {bytes: 910 TiB, name: 1 PB, short: 1P}}`, `hero-checkpoints: {label: hero, quota: {bytes: 100 TiB, name: 100 TiB, short: 100Ti}}` |
| `prices` | `{1: 0.02, 2: 0.01, 3: 0.004, 4: 0.0012}` | — |

Quotas are optional: a bucket without one shows raw TiB in the reply tail; a primary without one drops the OP's `· NN% of …` clauses and the plot's quota line/headroom band (y fit to the data). Secrets stay env-only (`SLACK_BOT_TOKEN`, `SLACK_CHANNEL`, `DISCORD_BOT_TOKEN`, the webhook env named by config); the config names env vars, never values.

`-C/--config FILE` (YAML/JSON) overlays the preset its `template:` names (default: `-T`'s); validated — unknown keys at any level (top, `buckets.<b>`, `buckets.<b>.quota`), a wrong-typed value, an unknown `template` or a `reply_hour` outside 0–23 raise; sizes accept `910 TiB` / `1 PB` / ints.

### Provisional daily reply (`provisional: true`, cw `sender`)

At a 6 h cadence (00/06/12/18Z) a `sender` day's reply waits for its reply scan (the first at/after `reply_hour`), because the headline is the sender name + arrow avatar, which `chat.update` can't change. With `provisional`, the day's earlier scans post ONE provisional reply instead, so the thread is never a day behind:

- **Per UTC date of the scan.** With `reply_hour` 12: the 00Z scan posts `M/D · so far` (fixed sender, neutral `:hourglass_flowing_sand:` icon — both fixed at post time, so neither can claim a trend) with the numbers so far in the body (`:arrow_degN: **<TiB> (Δ, %)** · [as of HH:MMZ](<over-time>) · <per-bucket tail>`, Δ vs the prior day's reply scan, like the final reply); the 06Z scan edits that body; the 12Z scan posts the normal final reply, then deletes the provisional. The 18Z scan edits only the OP (the day has its final reply; editing that reply's body would contradict its fixed headline), and the next date's provisional starts with that date's 00Z scan. A finished thread reads exactly as without `provisional`.
- **Missed scans.** A day whose first scan is already ≥ `reply_hour` never has a provisional. A day whose ≥ `reply_hour` scans all miss keeps its provisional until the next day's first scan: the existing stand-in rule then posts the day's last scan as its final reply, and the provisional goes (final first, then the delete, then the new day's provisional).
- **Month boundaries.** A provisional belongs to its date's month. The stand-in rule now also applies when the month is `closed` (a later month's scan exists), and the scheduled run (`converge_slack_now`, the CLI without `-m`) first re-converges the previous month whenever its state still holds a provisional — so a 10/31 that never got its 12Z scan is finalised by 11/1's first run. Otherwise the previous month is never touched.
- **State + idempotency.** The provisional's `{ts, scan}` lives under `state["provisional"][<date>]` beside `posted` (absent when none is up), saved after every post / edit / delete: a re-run with no new scan only edits the OP; a later early scan edits the provisional rather than posting another. A failed delete is logged and left in the state; the next run retries it. Turning `provisional` off deletes any that is up.
- **`body` is unaffected**: its reply is already edited as the day's scans land, so `provisional` is a no-op there (pinned: `cw-slack-body.txt` / `cw-dry-run-body.txt` are unchanged with it on). The gcs template (a reply per scan) and the Discord twin never post one.
- `--dry-run` lists the open day's provisional reply after the replies.

Goldens: `cw-slack-sender-provisional.txt` (the 12-hourly fixture), `cw-slack-provisional-6h.txt` (6-hourly across Sep → Oct, 9/30 missing its 12Z/18Z scans, then a re-run), `cw-dry-run-sender-provisional.txt`.

### CLI

`dt-cloud digest [-T gcs|cw] [-C FILE] [-P slack|discord] [-V sender|body] [-H hour] [-m YYYY-MM] [-r root] [-u url] [-i icons] [-n] [-R [-F]] [-E] [-D secs] [-c/-t/-w/-b]` — the union of both old commands. `cw-digest` is the same command with `-T cw` as its default, so `cw-run.sh` is untouched. `-V` must be one of the template's variants; `-E` is Discord-only, `-R` Slack-only.

## Proof of no behaviour change

`cloud/tests/test_digest_golden.py` (committed before the refactor, against the old modules) renders both templates over fixture snapshot trees and pins, byte for byte under `tests/fixtures/digest/`: each `--dry-run` (gcs; cw `sender` + `body`), the full fake-client call log + persisted state of gcs Slack, gcs Discord (incl. `edit_replies`), cw Slack per variant scan by scan, and cw `redo_replies` (plan + for-real). The refactor touched only the adapter lines at the top of that file; every golden held except one deliberate line, the dry-run's diagnostic header (now `--- replies (<variant>: username | body | icon) ---` for both; never posted). Both plot PNGs hashed identical before/after over the same fixtures (checked off-tree; PNG bytes depend on the matplotlib build, so not committed).

Other non-posted differences: the dry-run plot's temp filename (`<slug>-YYYYMM.png`), stderr log wording, and the plot's corner credit now derives from `site_url` (same text for the presets; follows `-u` if overridden).

## Later steps (other sessions own these branches)

- **gcs** (`job/run.sh`): nothing required — `dt-cloud digest` defaults to `-T gcs`. Optional: move the gcs preset's values into a tracked `job/digest.yml` and pass `-C job/digest.yml` (then the preset can leave `cloud`).
- **cw-s3** (`job/cw-run.sh`): nothing required — `cw-digest` keeps working. Then: switch to `dt-cloud digest -T cw [-C job/digest.yml]`, after which `cw-digest` can be deleted; cw could also gain the Discord twin (`-P discord -w …`) with no code change.
- **`cloud`** once both pass `-C`: drop the deployment presets (or keep one neutral example) — the OA values (buckets, quotas, prices, icons project) then live only on their branches.
- **Owners/users section**: neither digest has one today (gcs's owner attribution lives in the site and the retired weekly report). If wanted, it is an optional template section fed from config (on/off) + gcs's owner data — a gcs-branch input, not engine code.
- [`comms-notify.md`] describes an earlier `disk_tree.notify` engine (plotly, `DigestProfile`), deleted from `cloud` as dead code (`be4e0d1`); this spec supersedes it for the deployed digests.

[`branch-layout.md`]: branch-layout.md
[branches carry exactly their code]: branch-layout.md#2-proposed-tree
[`comms-notify.md`]: done/comms-notify.md
