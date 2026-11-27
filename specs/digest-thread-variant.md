# Digest: switch a thread's reply style from `/admin/slack`

From r2 (2026-11-27). The goal comes from Ryan: an admin toggles a month's Slack digest thread between reply styles (`quota`'s `sender`, a reply per day, and `weekly`, a reply per ISO week edited as days land) from `/admin/slack`, and back again. The style lives in D1 (mutable, set from the UI), falling back to the config file when the UI never set it. The test case is r2's November 2026 thread (#r2-usage, `C0C9J6CE8BD`): 27 `sender` replies today, which Ryan wants as `weekly`. Root reviewed the first draft. Its five changes are folded in: an R2 lease backend, a resumable `retiring` step, deletes limited to the reviewed list, gates, and `[cloud]` ownership.

## Why a setting flip isn't enough

The variant isn't a render option like `params` / `quota_marks` (`digest_settings`, which only edit existing messages). It decides which messages exist: switching means posting the new style's replies and deleting the old style's. Thread state is keyed per variant (`state: digest/{channel}/{variant}`), so a converge under a new variant finds no state for the month. It then either posts a new OP, or, with a seeded state, posts the new replies under the old ones. That happened to r2 on 2026-11-26: the image carrying `variant: weekly` had to be pinned back to `sender` so the cron wouldn't do it.

So:
- the style is per thread (channel, month), not one global value;
- a switch is a converge (post, then delete), never a value flip that the next cron interprets;
- a thread that already exists keeps the style it was posted in unless an admin switches it. The fallbacks (D1 default → config file) only choose a new month's style.

## Model

**States.** Each per-variant state file (`digest/<deploy>/<channel>/<variant>/<YYYY-MM>.json`) may carry:
- `retired_for: <variant>`: its replies are being or have been replaced by that variant's (`retiring`);
- `retired_at`: once every one of its replies is deleted (`retired`).

A month's **live** variant is the one whose state has an `op_ts` and no `retired_for`. A state with `retired_for` and no `retired_at` is **retiring**: its replies are queued for deletion (`stale`). A converge that finds a retiring state resumes its deletes first, under the reviewed-delete rule below. Two states with an `op_ts` and no `retired_for` is a conflict: the converge refuses it (exit 1, with a message naming both), never resolves it by guessing.

**The desired variant** of month M in channel C, first match wins:
1. D1 `digest_threads` row (C, M), set from `/admin/slack`;
2. M's live state's variant, if the thread exists;
3. D1 `digest_settings.variant` for the template (the deployment default, set from `/admin/slack`), for a new month;
4. the config file's `variant:` (baked into the image).

`-V` on the CLI overrides all four (a candidate: dry runs and the switch plan). The site is read with `$SITE_TOKEN`, as `live_settings`. An unreachable site leaves rules 1 and 3 out (a warning), so an existing thread keeps its live variant and never falls to the config file.

**Pairing** (Ryan, 2026-11-27: edit in place where Slack allows it). `chat.update` can't change a message's sender, so an old reply is reusable only for a new unit with the same sender (name and icon overrides, or none). `plan_replies` walks the new variant's units in thread order. Each one pairs with the next old `posted` reply whose sender matches, skipping old replies that don't. That's an edit in place: the reply keeps its ts and its place. Pairing stops at the first unit with no match; it and every unit after it are posted, since Slack only appends and a post can't land between kept replies. Old replies left unpaired (and `extra`, `stale`, provisional) are deleted. `sender` ↔ `weekly` pairs nothing (each daily reply has its own sender), and the plan says so in a note. Edit pairs count as reviewed under the same plan: the row's `edits`.

**Order around people's replies.** A post lands after every reply already in the thread. When a switch posts anything, a person's reply after the first reply it deletes would end up above the new ones. The dry run reads the thread (`conversations.replies`, bot token) and lists each such reply (`reorder`: ts, user). The site refuses to record a switch with any (409) unless the admin ticks "allow reorder", and the confirmation names what moves. Then the row's `reorder` lists exactly those replies, and the switch re-reads the thread and holds if any other person's reply would move. In-place edits never reorder.

**What a delete loses.** The same thread read gives each to-be-deleted reply's reactions, its own replies and files, all listed in the plan. Links to it from elsewhere need search, which a bot token can't do (`search.messages` needs a user token with `search:read`). Where a deployment configures one (`SLACK_SEARCH_TOKEN`, a `DIGEST_REAPPLY` secret), each reply's permalink id (`p<ts without the dot>`) is searched and the hits listed ("linked from #<channel> by <author>"). Without one, the plan says "links not checked"; it never passes silently. Without a Slack token at all, it says the thread wasn't read.

**Converge.** desired = live, or no live state (a new month): today's converge under desired. desired ≠ live, a **switch**:
1. Check: every reply of the live state (`posted`, `extra`, `stale`, the provisional) is in the row's reviewed `deletes` ∪ `edits`. Anything else refuses the switch before any write. The run then converges the live variant as usual (the day's reply still posts) and exits 1 with "switch to <to> pending: <n> replies not in the reviewed dry run; re-run it" (the digest step is soft: an alert, not a failed scan).
2. Seed the desired variant's state from the live one: `op_ts`, `pinned`, `prev_url` / `next_url`, `variant`; `posted` empty. A retired state of the desired variant is overwritten.
3. Read the thread: a person's reply that would move and isn't in the row's `reorder` holds the switch (as step 1).
4. Edit the pairs in place (recorded in the new state's `posted` under the old ts), then converge desired: post the rest (the OP is edited as on every run).
5. Save the new state, then mark the old one retiring: `retired_for = desired`, `stale` = its replies' ts not kept by an edit, `posted` / `extra` / `provisional` cleared.
6. Delete `stale`, one at a time, recording each. A failed delete stays in `stale` for the next converge, which resumes it (step 1's check covers resumed deletes too: they must be in the row's `deletes`).
7. Empty `stale` → `retired_at`.

A failure before step 5 leaves the old state live and the new one with a partial `posted`: the next converge finishes posting (desired ≠ live → switch again, check, seed keeps the partial `posted`). Duplicates, never a loss. The cron finishes a switch the admin confirmed; it deletes only reviewed ts.

Switching back (`weekly` → `sender`) is the same path over the retired `sender` state. The OP's text is variant-independent today. If a template's OP ever differs by variant, step 3's OP edit carries it.

`_prev_op`, `relinks` and `converge_slack_now`'s previous-month check read the month's live state, whatever its variant, not the same-variant path (`digest.py` `_prev_op`, `relinks`; they take `variant` today).

**Lease.** A converge, a switch and `-R` hold the thread's lease for the month: `<state dir>/<YYYY-MM>.lease.json`, the state template formatted with `variant` left out. It is held via `append_runner.coordinator_lease` on a `LeaseFile` for the state's store: GCS (`GcsLeaseFile`), R2/S3 (a new `S3LeaseFile`: conditional `PutObject` with `If-None-Match: *` to take and `If-Match: <etag>` to renew; delete re-reads and compares the ETag, then deletes with `If-Match`), or local. Holder `$DT_JOB_NAME` with `$DT_JOB_REGION`, so `job_ended` can take an ended holder's lease at once. The reapply job sets both (`reapplySpec`). A busy lease is a skip ("busy: held by <holder>"), and the next run converges.

## D1

Recipe `site/migrations/digest-thread-variant.sql`, `schema.sql` regenerated (`specs/done/d1-migrations.md`):

```sql
CREATE TABLE digest_threads (
  channel    TEXT NOT NULL,
  month      TEXT NOT NULL,         -- YYYY-MM
  variant    TEXT NOT NULL,
  deletes    TEXT NOT NULL,         -- JSON [ts…]: the reviewed dry run's replies to delete
  plan_job   TEXT NOT NULL,         -- the dry run (`digest_reapply.job_id`) they came from
  updated_by TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (channel, month)
);
ALTER TABLE digest_settings ADD COLUMN variant TEXT;  -- NULL: the config file's
```

Then recipe `site/migrations/digest-thread-switch.sql`: `digest_threads.edits` and `.reorder` (JSON [ts…]).

Each write logs old and new to `admin_edits`, as `putSettings` does. A D1 without a recipe reads its columns as NULL and its table as no row (`_lib/digestRecipe.ts`), and a write that needs it is a 503 naming it. So the code ships everywhere ahead of each deployment's recipe apply.

## API

- `GET /api/digest-threads?channel=&month=` → `{thread: row | null}` (viewer: the job reads it with its grant). A path-restricted grant is refused (403): the row names a channel and the digest's message ids, which a restricted link has no business with (`gates.test.ts` `RESTRICTED`).
- `PUT /api/digest-threads {channel, month, variant, plan_job, allow_reorder?}` (admin). The server reads the dry run's switch plan `gs://<data>/digest/reapply/<plan_job>.switch.json` and stores the row from it. It refuses (409) when the plan is for another channel, month or variant, is older than the thread's last switch, or would move people's replies without `allow_reorder`. `deletes`, `edits` and `reorder` always come from the plan file, never from the request.
- `digest_settings` PUT/GET carry `variant` (nullable). Changing it never changes an existing thread (rule 2); the page says so beside the control.
- `POST /api/digest-reapply` gains `variant` (a candidate) → `-V`. A whole-thread dry run with a candidate variant writes its switch plan (`-j` → `<job>.switch.json`).

**Switch plan** (`digest -n -V <to> -j PATH` on a thread whose live variant differs):

```json
{"digest_switch": 1, "month": "2026-11", "channel": "C0C9J6CE8BD", "from": "sender", "to": "weekly",
 "edit": [{"key": "…", "ts": "…", "to": "…", "scan": "…", "was": "…", "headline": "…"}],
 "post": [{"key": "wk-2026-10-26", "scan": "…", "headline": "…"}],
 "delete": [{"key": "2026-11-01", "ts": "1793519629.118359", "reactions": [{"name": "eyes", "count": 2}], "replies": 0, "files": 0,
             "links": [{"channel": "general", "author": "…", "permalink": "…"}]}],
 "reorder": [{"ts": "…", "user": "U…"}],
 "thread_read": true, "links_checked": false, "notes": ["no reply is edited in place: …"],
 "at": "<ISO time>"}
```

`links` is null when not checked. A plan from before pairing reads as no edits and no reorders.

## `/admin/slack`

On the thread card:
- **Status:** the live style and, where set, the desired one, plus anything a switch left mid-way (retiring: N deletes pending).
- **Picker:** picking a different style runs the dry run (`digest-reapply` POST, `target: thread`, `dry_run: true`, `variant: <new>`). Once it finishes, the page shows its plan: its notes; edits in place (old → new headline); posts; deletes in full, each with what it loses; "links not checked" / "thread not read" when so; and the people's replies it would move, with an "allow reorder" box that the Switch button needs.
- **Switch:** a confirmation that says deletes can't be undone, then:
  1. PUT `digest_threads` with `plan_job`;
  2. POST the real run (`target: thread`, `dry_run: false`, no candidate: it reads the row).

The deployment default for new months (sender | weekly | the config file's) is a field in the existing settings form.

## Tests (CI; never on the laptop)

- Python (`cloud/tests/test_digest_switch.py`):
  - precedence: row > live > default > file, `-V` over all, an unreachable site;
  - switch: seed, post, retiring, delete, `retired_at`;
  - a failed post leaves the old state live, and the next run finishes it;
  - a failed delete is resumed;
  - an unreviewed reply refuses the switch before any write;
  - switching back over a retired state;
  - two live states refused;
  - `_prev_op` / relink across a switched month;
  - the switch plan JSON and its text, with and without a search token and a Slack token;
  - pairing: matching senders, mismatched senders, leftovers each way, a skipped old reply;
  - an in-place switch (every reply edited, nothing posted or deleted);
  - a person's reply that would move: the plan lists it, the switch holds, and an allowed reorder goes ahead;
  - `S3LeaseFile` over a fake client (take, busy, renew by ETag, delete).
- TS:
  - `digest-threads` GET/PUT gates, with a `gates.test.ts` row including a path-restricted grant;
  - PUT validation from the plan file (the 409s, including an unallowed reorder), `edits` / `reorder` stored, `admin_edits`;
  - a D1 before either recipe, and between them;
  - `digest-reapply` passing `variant` → `-V` and `-j` → `.switch.json`, and `DT_JOB_NAME` / `DT_JOB_REGION` in the spec;
  - `digest-settings` `variant`.

## Review in thrds' `PlanView` (phase 2)

Phase 1 reviews the switch plan on the page. Phase 2 reviews and applies message by message in `PlanView`, which needs thrds plan actions `post` and `delete`, spec'd at `~/c/thrds/specs/rewrite-post-delete.md`. The page's apply then also needs to write the digest state (today's apply writes none, `specs/done/digest-rewrite-review.md` §Digest state). That's phase 2's open design point.

## Ownership and rollout

The code is `[cloud]` (digest.py, `append_runner`'s `S3LeaseFile`, the functions, the page), built on r2 and sent to root as shas. Steps 1 and 2 are r2's `[r2]` steps.
1. Delete r2's seeded copies `digest/r2/C0C9J6CE8BD/weekly/2026-10.json` and `…/weekly/2026-11.json`. r2 wrote them on 2026-11-26 for a hand-run re-thread; with no `retired_for`, each would be a second live state. These are the only objects deleted, and Ryan approves the list first.
2. Apply the recipe to r2's D1 (Ryan's go). Ship. With `job/digest.yml` back at `variant: weekly`, nothing changes for November (rule 2: live = `sender`), and December starts weekly unless the default is set otherwise.
3. Ryan switches November on `/admin/slack`: dry run → review (5 weekly to post, every sender reply to delete) → Switch. Then he switches a test DM thread (the reapply `channel` field) back and forth.
4. Every other deployment by parity (cw, gcs, hf; each D1 recipe apply with Ryan's go in that session): no change to any thread until an admin switches one.
