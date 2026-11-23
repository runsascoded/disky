# Scan threads: status as the sender (icon + name), green/red streak threads

Status: implemented on `cloud` (2026-11-22), default on for every deployment (`nominal.senders`); not yet deployed anywhere. Ryan's design and the points it left open are as written; "As built" after them is the final behavior, and "Settled in the implementation" answers each open point.

From the r2 session, 2026-11-21: Ryan's design for `#<deploy>-admin`'s scan thread (`nominal`) and the deploy notices. Every message today repeats "R2 Usage Bot" + the app icon, which tells the reader nothing, and the status is an emoji at the start of the text. Slack fixes a message's `username` / `icon_url` when it's posted (`chat.update` can't change them), so the sender has to carry only what never changes for that message.

## Design (Ryan's)

1. **Threads are streaks of one kind.** A clean (green) streak's head and a failing (red) streak's head each post with a fixed sender:
   - icon: a ✅ / ❌ image (any image works: a badge, a mosaic),
   - name: the kind, e.g. `OA R2 scans · clean` / `OA R2 scans · failing` (no counts, nothing that changes; ≤ 80 chars).
   The head's text is still edited each run (count, since, latest, stores, "ended by …"), but drops the leading `✅ OA R2 scans:` that the sender now says.
2. **A scan that starts** posts a ⏳ reply (⏳ sender icon) in the current thread.
3. **A scan that finishes** deletes its ⏳ reply, then:
   - **same kind as the thread** (clean run in a green thread, failed run in a red one): re-posts it as a finished reply whose sender icon is the outcome (✅ / ❌; optionally the starter's face with the outcome as a badge, as `_lib/badgedAvatar.ts` does for staged deletions), so the text loses the inline status emoji and `:ryan:` face;
   - **kind flipped**: nothing more in the old thread. The run's canonical message is the new thread's head (the other kind), which closes the old thread as today (`closed_head`).
4. **Step alerts** (grouped) stay replies, posted once in their final state, so their icon can be the alert's kind (⚠️ / ❌).
5. **Deploy notices** (`deploy_notify`, the `OA-R2 dev deployment succeeded` thread): the status emoji (✅ / 🚀 / 💥) moves from the name into the icon; the name keeps the words.

## With quiet greens (#85, Ryan 2026-11-22: quiet greens on by default, every deployment)

#85 came after this design: a clean thread posts its head and one rolling reply (`trail`), then runs only edit them (`nominal.quiet`, default on); a red notice or unclean run posts as before. The two compose:

- **Senders everywhere they post:** a green head (✅ icon, `… · clean`), a red head (❌, `… · failing`), the rolling reply (a fixed "runs" sender, the app's icon / `… · runs`: its text changes, its sender can't), alerts (⚠️ / ❌), deploy notices (emoji in the icon).
- **Green threads under `quiet` (the default):** no ⏳ reply and no per-run re-post (§2–3 above); a run edits the head and the rolling reply only. A run that flips the kind posts the new red head (§3, "kind flipped").
- **Red threads, and green ones with `quiet: false`:** §2–3 as written: a ⏳ reply, deleted and re-posted as the finished reply with the outcome as its icon.
- **Deploy notices under `deploy_quiet`:** the green head is edited (its sender stays 🚀 / 🧪); reds post with their own sender.

## Things to settle in the implementation

- **`thread_url`** (`scan_runs.thread_url`, just added): set when the finished reply is posted (the ⏳ reply's link dies with it); for a flipped run, the new head's permalink.
- **Delete + re-post under the thread lease**, so a `batch-events` converge and the job's own end can't both re-post. A head that dies without its end (cancel, lost VM) is converged by `batch-events serve`, which must do the same delete + re-post. Deleting a reply with no replies of its own leaves no tombstone.
- **Icon hosting:** `icon_url` must be public. Options: the deployment's icons Pages project (`icons_base`), or the site's public `/a/<sha256>.<ext>` (`public_avatars`) for faces with badges.
- **Recovery / adoption** (`adopt`, `owns`, `find`): messages become `bot_message`s with per-message usernames; ownership checks must still recognize them (bot id, not username).
- **Every deployment** gets it (parity); the r2 thread in `#r2-admin` is a fine first one to look at.

## As built

**Senders** (`nominal.Config`, `senders: true` by default; `false`: every post the app's, as before). Every post in the scan thread carries a name and an icon (`chat.postMessage` `username` + `icon_emoji` or `icon_url`, the bot's `chat:write.customize`), fixed once posted:

| post | name | icon |
| --- | --- | --- |
| a clean thread's head | `<title> · clean` | ✅ `:white_check_mark:` |
| a failure thread's head | `<title> · failing` (opened by a failed run), `<title> · phase issues` (opened by a run with phase issues) | ❌ `:x:` / ⚠️ `:warning:` |
| a quiet clean thread's rolling reply | `<title> · runs` | the app's |
| a run's reply (red threads; `quiet: false`) | `<stamp> · <duration> · <outcome>`, e.g. `11/21 19:31Z · 115m 54s · phase issues: static-names` | the outcome's: ✅ ⚠️ ❌ 🚫 |
| a run's ⏳ reply | `<stamp> · running` | ⏳ `:hourglass_flowing_sand:` |
| a step alert (`push.Notice.icon`) | the app's | ⚠️ `:warning:` / ❌ `:x:` |

Names are plain text, ≤ 80 characters: a head's clips the title (`…`), a run reply's the outcome. `<outcome>` is `short`: `clean`, `failed in <phase>`, `phase issues: <phases>`, `cancelled`; a failure's error stays in the text. A head's name is its kind and never changes; its icon is the outcome of the run that opened it (a failure thread opened by a run with phase issues says ⚠️, as its head text does).

**Texts.** A head posted with a sender drops its leading status emoji and title (`lead`): `**3 consecutive clean runs**`, `**1 consecutive failed run (ended)**`; the rest is unchanged. A head posted before senders (state without `head_sender`, or an adopted one) keeps its full text on every edit, since its sender is the app. A run reply's first line moved into its name (Ryan, 2026-11-22), so its text is what a name can't hold (`record`): the trigger (`:alarm_clock:`, or the starter's face/👤), `[record](<scan record>)`, who started it when no face shows, and a failure's error; folded alert lines follow, as before. The rolling reply's lines keep their status emoji (one reply lists many runs), and the canvas keeps the full head line.

**Icons.** Stock emoji codes (`ICON`); `nominal.emoji` overrides become icons when given as a `:name:` code or as a character with a known code (`CODES`); any other character keeps the stock icon (give it as a code). `nominal.icons` (`{kind: https URL}`) uses images instead, per kind. `nominal.badges` is the run reply's icon hook: an https URL template over `{outcome}` (`clean`, `issues`, `failed`, `cancelled`, `running`) and `{trigger}` (`schedule`, `manual`, `unknown`), for pre-made images with the trigger as a corner badge (❌ + ⏰); unset, the outcome's icon alone. All images must be publicly fetchable.

**Quiet greens** (`quiet: true`, the default): as "With quiet greens" below. A green thread posts its head and rolling reply, each with its sender, and runs only edit them: no ⏳ reply, no per-run post. A ⏳ reply left in a quiet thread from before it was quiet is deleted when its run ends there (the rolling reply lists the run).

**Red threads, and `quiet: false`.** A run starting posts its ⏳ reply. When it ends:
- in the thread it was posted in (same kind): the ⏳ reply is deleted, then its reply posted under its outcome's sender (`run_post`);
- elsewhere (the kind flipped, or under `clean` grouping a failure closed the thread): the ⏳ reply is deleted and nothing more is posted in the old thread; the run is the new thread's (its head; in a red thread its reply too, carrying its folded alert lines);
- cancelled: the ⏳ reply is deleted and a 🚫 reply posted where it stood.

**Step alerts.** `scan-run`'s alerts carry `icon` `:x:` / `:warning:` (`push.Notice.icon`; titles keep their emoji, which the other channels show). A grouped alert whose priority rises (a failure joining a warning's message) is posted again under ❌ with every line, and the ⚠️ message deleted (a delete that fails leaves it beside the new one); a lower notice joining is an edit, as before. Without `icon` nothing changes.

**Deploy notices** (`site/deploy-notify`). The emoji moved from the name into the icon: `username` `HF dev deployment succeeded`, `icon_emoji` `:test_tube:` (`ICON_CODES`: 🚀 🧪 💥 ⏹️ ✅ ❌ 🧫 🔥 🛳️ 🎉 ⚠️). A `deploy_emoji` character without a code leads the name as before. `SENDER` matches a name with or without a leading emoji, so threads posted before keep running; the review output (`render`, `send` without `-x`) still prints the emoji first (`display`). Under `deploy_quiet` a green edits its head, whose sender stays 🚀 / 🧪.

## Settled in the implementation

- **`thread_url`** (`link_threads`): a run's reply link moves to its finished reply when that is posted (the ⏳ reply's ts is gone from state). The run that opened a thread by changing its kind (`state.opener`) links the new head. A run whose ⏳ reply was deleted with no reply in a thread links its 🚫 reply or, under `clean` grouping, its top-level alert (`state.relink`, kept across thread resets, at most 10 unwritten, cleared once written).
- **Delete + re-post under the lease.** All of it runs in `converge`, which `run` calls under the coordinator lease, whether from the job's own start/finish report or `batch-events serve`'s converge of a head that died. The ⏳ reply is deleted first and its removal persisted; the re-post goes through the usual persisted send intent (`pending`, now carrying its sender), so a lost response is recovered by its metadata, never posted twice. A delete repeated after an interrupted report is harmless (`message_not_found` is benign). A reply with no replies of its own leaves no tombstone.
- **Ownership** (`owns`, `find`, `adopt`, `alert_messages`): by the bot's `user` or `bot_id`, never its name, so `bot_message`s posted under per-message usernames stay ours. `head_sender` records the sender a head was posted with; an adopted head has none, so it keeps its full text.
- **Icon hosting:** none needed by default (`icon_emoji`). `icons` / `badges` take any public URL (the deployment's `icons_base`).
- **Parity:** default on, no deployment defaults. Each deployment's admin bot needs `chat:write.customize` (both manifests in `deploy/slack/` have it); without it Slack silently drops the sender, and a new head's text then lacks its title: turn `senders` off for such a bot until it is reinstalled.

## Follow-ups

- **The starter's face as the badge** (Ryan, 2026-11-22: ❌ + the starter's face for a manual run). `badges` takes static per-outcome-and-trigger images only. A face needs a hosted image per person and outcome: a site endpoint that mints `/a/<sha>.png` with `_lib/badgedAvatar.ts` and stores it in `public_avatars`, as staged-deletion posts do, which the job calls (with its grant) before posting a manual run's reply. That is a site route, a gate row and a job call, not a config change; left for its own spec. `run_sender` is the one place it would plug in.
- **Alert senders' names** are the app's; only their icons say their kind.

<!-- Claude-Session: https://claude.ai/code/session_01MJgqTbCPPaPpxRYa9ujuVB -->
<!-- Claude-Session: https://claude.ai/code/session_019krTiwBTwBWLFiX3NU1CM1 -->
