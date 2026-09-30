# Comms: a generic `disk_tree` notify/digest engine (seam 4)

**From:** mgu `gcs` session, 2026-09-17 — `specs/gcs-toward-union.md` §5 "Seam 4 — digest content", routed here as **DT comms roadmap item 1** (`specs/mgu-cp-2026-09-16.md`). Never gated on the gcs↔cw-s3 union; startable now. Cross-repo peer, so this is a **generalize-and-upstream** (`/cp` `handoff: spec`), not a cherry-pick: DT holds the reusable engine, each deployment keeps its own profile, and mgu eventually imports the engine from `disk_tree` in place of its local copy.

## What mgu built (the source to generalize)

`cloud/src/dt_cloud/` (gcs branch), ~900 LOC:
- `discord_api.py` (83) — thin Discord REST helpers `thrds` doesn't cover: application-emoji upload (the trend-arrow avatars), webhook introspection, guild/channel/webhook management. Pure `urllib`, no deps. **Almost entirely generic** — only `USER_AGENT` names the repo.
- `digest.py` (473) — a shape-C monthly digest: one thread per calendar month (an OP edited in place as the month progresses + one reply per scan), posted to Slack (`thrds.slack.SlackClient`) or its Discord twin (`thrds.discord.{DiscordClient,DiscordWebhookClient}`). Converge state is a per-month JSON in the data bucket. The **pure content functions** (`deg`, `op_body`, `reply`, `rows_from_meta`, `discordify`) are unit-tested; `post_digest`/`post_digest_discord` are thin side-effecting shells.
- `digest_plot.py` (135) — the 2-panel mosaic plot (plotly), hosted as an image the OP references.
- `tests/test_digest.py` (230) — content-function specs.

## The seam (mgu §5, verbatim intent)

> Both deployments share one `thrds` posting mechanism (converge state, host plot, day-keyed replies, Slack + Discord). Only the body differs: gcs reports $ cost + storage-class breakdown; cw reports % of the 1 PB quota. Factor the mechanism into an engine with a per-store **profile** (body builder + plot-panel list, selected by `Store.digest`) *if it stays a thin interpolation layer*.

So the split is **mechanism (generic, upstreamed) vs profile (per-deployment)**:

| generic engine (→ `disk_tree`) | profile (per-deployment) |
|---|---|
| converge state (per-period JSON), post/edit OP, post new replies, persist | `rows_from_meta` — turn scan metadata into the digest's row model |
| Slack + Discord adapters over `thrds` (webhook + bot clients, thread lifecycle) | `op_body` / `reply` — the message text (interpolation) |
| trend-arrow avatar upload + `<:name:id>`/`?v=REV` cache-busting (`discord_api`) | plot-panel list (which series/panels `digest_plot` draws) |
| plot hosting (render via plotly/kaleido → upload → URL) | metric semantics: gcs `$`+class-bytes; cw `%` of quota; ctbk/crashes TBD |
| period model (month/week/day keys), `deg`/threshold arrow math (shared) | prices / thresholds / units / icons base URL |

The gcs-specific bits in `digest.py` today — `PRICE` by storage class, `total_bytes`/`class_bytes` meta shape, `gcs.oa.dev`, the icons Pages URL, TiB rounding — are all **profile**, not engine.

## Proposed DT module layout

`src/disk_tree/notify/`:
- `discord_api.py` — port of mgu's verbatim, with `USER_AGENT` parameterized (default names disk-tree). Generic, no profile coupling.
- `digest.py` — the **engine**: `Digest` (converge state, OP/reply lifecycle, Slack+Discord via `thrds`), `post(profile, rows, state_store)` shell, the shared `deg`/threshold arrow math, `discordify`. Takes a `DigestProfile`.
- `profile.py` — the `DigestProfile` `Protocol`: `rows_from_meta(dated_meta) -> list[Row]`, `op_body(rows) -> str`, `reply(row) -> (sender, body, avatar)`, `plot_panels() -> list[...]`, plus the period key. A reference profile (bytes-over-time, no cloud pricing) ships so DT has a runnable default and a test fixture.
- `plot.py` — `digest_plot` generalized to a profile-supplied panel list (DT already has `plotly` + `kaleido`).
- CLI: `disk-tree digest` (mirrors mgu's `dt-cloud digest [-P discord]`) — reads a bucket's scans, builds rows via the configured profile, converges the thread. Wire under the existing `cli/` group.

## Config

A `digest:` block per bucket in `~/.config/disk-tree/buckets.yml` (or `defaults`):
- `profile:` — selects the `DigestProfile` (built-in name or import path).
- `slack:` / `discord:` — channel + the **env var names** holding webhook URL / bot token (values never inlined, per the secrets rule — the module reads `os.environ`).
- `period:` — `month` (default) | `week` | `day`.
- `state:` — where the converge JSON lives (defaults beside the scans, `r2://…`/local via `blobfs`).

## Dependency

`thrds` (provides `thrds.slack.SlackClient`, `thrds.discord.*`) as a **`notify` optional extra**, not a core dep — the desktop/core install stays lean; only a deployment that posts digests pulls it. `discord_api` itself needs only stdlib. Guard the `thrds` import so `disk-tree` runs without the extra and errors only when `digest` is invoked without it.

## Staging

1. **`discord_api` port** — self-contained, stdlib-only, unit-testable in isolation. **Done** (`9833200`): `src/disk_tree/notify/discord_api.py` + 7 specs, `USER_AGENT` parameterized, `_request -> Any` (Discord collections return arrays), errors never leak the webhook token.
2. **Engine + `DigestProfile` Protocol + reference profile + `plot`** — the mechanism, with mgu's content functions as the shape to preserve; port `test_digest.py`'s pure-function specs against the reference profile. **Done**:
   - `notify/digest.py` — the engine: shared arrow/emoji math (`deg`, `THRESH`, `emoji_name`, `discordify`), the `Period` model (`month`/`week`/`day` keys) + `select_window` (lead-in windowing), converge-state IO (via `blobfs`, so `r2://`/local both work), and the two profile-driven lifecycles `converge_slack` / `converge_discord` + the real-client `post_digest_discord` shell (guarded `thrds` import).
   - `notify/profile.py` — the `DigestProfile` `Protocol` (opaque `Row`) + the reference `BytesProfile` (bytes-over-time monthly digest, no cloud pricing; `icons_base=None` degrades to plain senders).
   - `notify/plot.py` — `render_bytes`, the total-TiB-over-time panel, **plotly + kaleido** (already core deps — no matplotlib, unlike mgu's `digest_plot`).
   - `tests/test_digest.py` — 24 specs: the arrow/emoji/period/window/state-path engine functions, the `BytesProfile` content (rows/reply/op_body, exact strings), and both converge lifecycles against fake Slack/Discord clients (fresh → incremental → idempotent, backfill edit-replies).
   - `pyproject.toml` — `notify` extra = `thrds`.
3. **CLI `disk-tree digest` + `digest:` config** — wire to a bucket's scans. **Done**:
   - `notify/sources.py` — the row *source* (decoupled from the profile, which now only maps meta→rows): `scan_totals(uri)` reads disk-tree's own `Scan` DB (a bucket's scans over time, latest-per-day), `rows_from_totals` windows them to the period (pure, unit-tested), `scan_rows` ties them together.
   - `notify/profile.py` `build_profile(spec)` — config-block → profile factory (built-in `bytes`, or an `pkg.mod:Class` import path for a deployment's own).
   - `cli/digest.py` — `disk-tree digest [BUCKET] [-p slack|discord] [-m YYYY-MM] [-n]`: resolve the bucket's `digest:` block, build the profile, load the period's rows from the `Scan` DB, and converge via the engine shells. `--dry-run` builds rows + renders the plot + prints the OP body without posting or reading any secret. Secrets are named by env var (`*_env`) and read from `os.environ` at post time, never inlined.
   - `digest:` block on `BucketCfg` (a passthrough field `load_config` accepts and `sync` ignores).
   - `post_digest_slack` added beside `post_digest_discord`; both now take pre-loaded `rows` (source ≠ engine). `tests/test_cli_digest.py` — an end-to-end `--dry-run` (import a bucket at three dated TiB-scale totals → exact OP body) + config-error paths.
4. **mgu adopts** — mgu's gcs `digest.py` becomes a `DigestProfile` importing DT's engine; its `discord_api` deletes in favour of DT's. Handled by the mgu session once the engine lands here (spec round-trip). *(Next — theirs.)*

## Deferred (not blocking mgu adoption)

- **Slack plot hosting.** Discord attaches the PNG; Slack needs a hosted image URL, which disk-tree has no default host for — `post_digest_slack` takes an optional `plot_url` (from the `slack.plot_url` config), else posts text-only. A deployment that wants the Slack plot hosts it (gcs uses its Pages project).
- **Import-path profiles.** `build_profile` accepts `pkg.mod:Class` but only `bytes` ships; gcs's `$`+class-tier profile lands with its adoption (stage 4).

## Open questions — resolved in stage 2

- **Row model generality.** ✅ `Row` is opaque: the engine reads only `row.date` (for per-scan reply keying) and the profile's `op_body`/`reply`/`render_plot` output. The `DigestProfile` Protocol owns the row type (`BytesRow` for the reference; gcs's `Scan` for its future profile).
- **Avatar/emoji hosting.** ✅ `BytesProfile(icons_base=None)` degrades — `reply` returns an empty avatar, `op_avatar` is empty — so a deployment without an icons host posts plain senders.
- **Plot stack.** ✅ plotly + kaleido (DT core), not mgu's matplotlib — no new dependency. The reference panel is a single total-line; a richer profile (gcs's stacked storage-class tiers) supplies its own `render_plot`.
- **`thrds` API surface.** The engine leans on `SlackClient.{post,edit}` and `DiscordWebhookClient.{post,edit}` / `DiscordClient.create_thread` (mirrors mgu; fakes in the tests pin the exact call shapes). A `Poster` Protocol for a Slack-only deployment is deferred until one needs it.
