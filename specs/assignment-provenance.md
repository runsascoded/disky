# Assignment provenance: who says this prefix is whose, and why

Every owner the site shows comes from somewhere, and today the *somewhere* is mostly lost by the time it reaches a chip. This spec makes provenance a first-class part of every ownership fact, shows it wherever an owner is shown (one component, one code path), and adds an assigner × assignee view whose cells open the homepage filtered to that pair.

## What exists today (2026-09-08)

Two kinds of ownership fact, with very different provenance handling:

| | Assignments (ledger) | Inferred attribution (pipeline) |
|---|---|---|
| Where | D1 `actions` + `owner_prefixes` | `attr/attribution-*.parquet` → `webdata`'s prefix map → path index `usr` |
| Assigner | `actions.actor` (an email; always a person so far) | a *signal*: `user-prefix`, `artifact-record`, `manual` (`identities.yaml` `prefix_owners`), `wandb-run`, `wandb-config`, `executor-wandb` |
| Evidence | `actions.memo` (free text; the CLI's `-m`) | `AttributionRow.evidence`: `run_name~<leaf>`, `executor_name~<x>`, `config.<key>@<run_id>`, or none |
| Reaches the site as | `who`, `ts`, `memo` on `/api/actions` and the marks feed | **nothing** — the path index keeps `usr` only; `rules.json` publishes users/aliases + the manual `prefix_owners` |
| Shown | `MarkControls` ("assigned <date>"), `/user/:id` assignments tab, `/marks` feed (`assigned to X`), sweep console approver | `MarkControls` ("<pct>% · inferred"), the `Ownership` section's prose |

No automated writer of *assignments* exists: the sheet sync is read-only, the sweep executor writes only deletion tables, and every `actions` row so far has a human actor. "Iris" appears only as a source of username aliases in `identities.yaml`; the planned `iris-path` signal (`tmp/…/<user>/<job>/` path shapes, `storage-cost-attribution.md` row 3) was never built. So the honest source list is the six signals above plus people.

The W&B miner (`wandb-mine`) records `entity`, `project`, `run_id` per run, so a direct run link is available at mining time; the attribution rows drop it (evidence keeps the run name or `run_id` only).

## Model

One shape for both kinds:

```
OwnerFact {
  prefix     gs://bucket/dir/
  assignee   user id | null (cleared / explicitly nobody)
  assigner   'person:<email>' | 'wandb-run' | 'wandb-config' | 'executor-wandb'
             | 'artifact-record' | 'user-prefix' | 'rule' | 'iris-path'
  evidence   { kind, ref, url? }   — memo text; W&B `entity/project/run_id` (+ url);
                                    record path; the matched rule; the users/ segment
  ts         when it was asserted (action ts; the signal's `asof`)
}
```

"Assigner" is deliberately the same axis for a person and a pipeline signal: the heatmap and the lens treat `wandb-run` like a very prolific colleague. `rule` replaces the pipeline's `manual` name (a rule in `identities.yaml` is a person's decision, but the person isn't recorded; if we want that, the yaml gains an `assigned_by` field and the rule's assigner becomes that person).

Precedence is unchanged: an assignment (any person) beats every signal; among signals the current prefix-map order stands (`parquet rows win over manual rows`, deepest prefix wins).

## Surfaces (one component)

`<OwnerFactChip fact>`: the assignee's `UserChip` plus a small **provenance mark** — the assigner's avatar for a person, a source glyph for a signal (W&B mark, record, rule, `users/`) — and one tooltip, built by one function:

- person: *assigned by* **Avatar Name** · 2026-09-02 14:07 UTC · memo (if any) · "via CLI/API" when the memo carries the CLI's tag
- W&B: *inferred from W&B run* **name** ↗ (link to `wandb.ai/<entity>/<project>/runs/<id>`) · mined 2026-08-27 · signal `wandb-config: config.<key>`
- record: *inferred from* `path/.artifact.json` (`provenance.built_by`) ↗ (`/files/…`)
- rule: *inferred from rule* `gs://marin-*/scratch/ahmed/` in `identities.yaml` ↗
- users/: *inferred from* `gs://…/users/<seg>/`

Used everywhere an owner is shown: `MarkControls` owner row, treemap hover box, `/user/:id` (the assignments tab and the estate table's owner column), `/users`, `/marks` feed rows, the sweep console's top-owner and approver cells. The `share` (e.g. `41%`) stays a separate suffix, as now.

## Data changes

### Phase 1 — assignments only (no pipeline change)

- `/api/actions` already returns `who`, `ts`, `memo` per owner prefix. Add `via` derived from the memo tag the CLI writes (`gcs-usage mark` sets `memo` = `cli: …`; the sheet or any future bot would tag itself the same way).
- **`/api/assignments`**: the assigner × assignee matrix. For every live `owner_prefixes` row, bytes at the latest scan come from the same claims fold `/api/marks/totals` already computes per user (`mark_totals.claims`); group by `(actor, owner)` → `{ cells: [{ by, to, bytes, prefixes }], scan, head }`. Cached alongside `mark_totals`.
- **`/assignments`** page: heatmap, assigners as rows, assignees as columns (both sorted by bytes), cell color by bytes (log scale), cell text `Ti · n`. Row/column headers are `UserChip`s; hovering a cell lists its biggest prefixes; clicking opens `/?o=<assignee>&by=<assigner>`.
- **Homepage `?by=<assigner>` lens**: with `?o=<user>`, restricts the owner lens to the claimed regions whose action actor is `by` (the region list `view.ts` already builds from the claims fold; the fold gains `who` per region and filters). Without `?o=`, `?by=` alone shows everything that assigner assigned, to anyone (colour by assignee as the owner lens does today). Inferred attribution is excluded under `?by=` (a person never "assigned" inferred bytes) until Phase 2 gives signals the same axis.
- `OwnerFactChip` for assignments on every surface above; the sweep console's approver avatar (already shipped) becomes an instance of it.

### Phase 2 — inferred provenance

- **Path index gains `src` per `(path, usr)` slice**: the signal id (dictionary-encoded int; `deepest_lookup` already returns `(user, source)`), plus `ev` on rows whose `path` *is* the attributing prefix (evidence string; null elsewhere — the ancestor walk finds it, as `AttrIndex` does today). Size cost: one small int column on every slice, one short string on ~200k rows; the group manifests are unaffected.
- **Attribution rows carry a link**: `evidence` becomes structured for W&B (`entity/project/run_id`), so the chip can link the run; `wandb_attr` has the fields at hand.
- `/api/resolve` and `/api/subtree` expose `src`/`ev` for the top owner; the chip renders signal provenance from them.
- `/api/assignments` gains signal rows (`wandb-run`, `wandb-config`, …) from a per-source rollup `webdata` writes into `meta.json` (`by_source: { <signal>: { <user>: bytes } }`), so the heatmap's top rows are the pipeline's signals and the human rows sit below; `?by=wandb-run` filters slices by `src`.

### Phase 3 — `iris-path` signal

The path-shape rule from `storage-cost-attribution.md` (`tmp/…/<user>/<job>/…`), as an `AttributionRow` source with the job id as evidence. Only worth doing once Phase 2 makes the provenance visible; otherwise it's another invisible guess.

## Non-goals

- Changing precedence or the vote model.
- Group/team ownership (excised 2026-09-06; stays out).
- Backfilling `evidence` for past W&B mining runs; the next mine writes the new shape.

## Open questions

1. Should `rule` assignments record the person who added the rule (`assigned_by:` in `identities.yaml`)? Cheap, and it makes every human decision attributable.
2. `?by=` with neither `?o=` nor a person (`?by=wandb-run`) needs Phase 2; until then the UI hides signal rows in the heatmap rather than linking to an empty view.
3. Heatmap size: today ~20 actors × ~30 assignees; fine as a table. If signals add rows it stays small.
