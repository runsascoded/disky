"""`owner_prefix_aggs` carried across an interval-store compaction.

A scan's priced owner prefixes are keyed by the generation's tag (`site/functions/_lib/ownerTotals.ts` `indexTag`), so
`CURRENT.json` moving to a compaction's new generation would send every scoped series back to first-time pricing. A body
holds a scan's own numbers (`{v, buckets, asked, aggs}`: no stamps, group references or versions), and a compaction
answers every scan as its source does, so the rows are copied to the new tag before anything points at it.

Opt-in: `owner_carry` in the deployment's interval profile (`$INTERVAL_STORE_PROFILE`; `compact -O`), `promote -O`. When it runs:

- `preflight`, before the fold: an unreachable D1, or a generation with no rows under the tag this module writes, stops
  the job then, not hours in.
- `copy`, in `compact_store`'s `pointer` phase and in `promote_gen`, after the appended-scan check and before
  `successor.json`. A failure stops there with nothing pointed at the new generation; a rerun finds the rows it copied.
- `repeat`, once the R2 pointer has moved: the rows old isolates priced meanwhile (the R2 copy takes up to an hour). Best
  effort: a scan or a prefix without a row under the new tag prices itself on its next read (`ownerTotals.ts` `compute`),
  and a read never answers from a partial row, whose `asked` says which prefixes it lacks.
- `retire`, in the compaction job's `prune` phase (`append_runner.prune_predecessors`), before the old generation's files
  go: its row deletes run when that generation leaves the store, past its grace, never at the promote.

A compaction superseded after its copy (a scan appended meanwhile) leaves the new tag's rows: no reader asks for that
generation, and the next compaction, which takes the same name, finds them still valid.
"""
from __future__ import annotations

from collections.abc import Callable
from time import time

from . import index_footer as fx
from .static_names import err

TABLE = "owner_prefix_aggs"
#: Rows a copy statement moves; a statement D1 resets for its CPU time (7429) is retried at half, down to one.
COPY_ROWS = 16
#: Rows a delete statement removes (`index_footer.d1_delete`).
RETIRE_ROWS = 32


def tag(gen: str) -> str:
    """What the site's `indexTag` writes for an owner read of `gen`: mode `pq`, `iv:<gen>`, sliced. A generation re-cut in
    place (`INTERVAL_STORE_REV`, `@<rev>` after the name) is another tag, which `preflight` refuses."""
    return f"pq:iv:{gen}:s"


class OwnerCarry:
    """Over the deployment's D1: `$D1_DB_ID`, `$CLOUDFLARE_API_TOKEN`, `$CLOUDFLARE_ACCOUNT_ID` (the compaction job has them
    by `record_secrets`; `index_footer._creds` raises without them)."""

    def __init__(self, *, now: Callable[[], float] = time, log: Callable[[str], None] = err):
        self.now, self.log = now, log

    def query(self, sql: str) -> list[dict]:
        tok, acct = fx._creds()
        return fx._d1_query(sql, acct, tok)

    def preflight(self, gen: str) -> int:
        """`gen`'s row count under its tag. Raises when D1 doesn't answer, or holds no rows under the tag."""
        counts = {r["idx"]: r["n"] for r in self.query(f"SELECT idx, COUNT(*) AS n FROM {TABLE} GROUP BY idx;")}
        n = counts.get(tag(gen), 0)
        if not n:
            others = sorted(t for t in counts if t.startswith((f"pq:iv:{gen}@", f"pq:iv:{gen}:")))
            raise RuntimeError(f"owner carry: no {TABLE} rows under {tag(gen)}: the site's tag isn't what this carries "
                               f"(other tags of {gen}: {', '.join(others) or 'none'})")
        self.log(f"owner carry: {n} rows under {tag(gen)}")
        return n

    def copy(self, old: str, new: str) -> dict:
        """`old`'s rows under `new`'s tag: inserted, or replacing a shorter body (a row priced further under `old` since;
        a row a new-generation read priced further keeps its own, and what the replaced one lacks prices again). Paged by
        the scans listed first, each statement bounded by them, so a scan priced meanwhile is never skipped past. The old
        tag's rows are untouched. Returns `{rows: <listed>, changed: <written>}`; a rerun writes nothing."""
        src, dst = fx._q(tag(old)), fx._q(tag(new))
        scans = [r["scan"] for r in self.query(f"SELECT scan FROM {TABLE} WHERE idx = {src} ORDER BY scan;")]
        now, i, rows, changed = int(self.now()), 0, COPY_ROWS, 0
        while i < len(scans):
            j = min(i + rows, len(scans))
            lo, hi = scans[i - 1] if i else "", scans[j - 1]
            try:
                got = self.query(
                    f"INSERT INTO {TABLE} (scan, idx, body, updated_ts) SELECT scan, {dst}, body, {now} FROM {TABLE} "
                    f"WHERE idx = {src} AND scan > {fx._q(lo)} AND scan <= {fx._q(hi)} ORDER BY scan "
                    f"ON CONFLICT (scan, idx) DO UPDATE SET body = excluded.body, updated_ts = excluded.updated_ts "
                    f"WHERE length(excluded.body) > length({TABLE}.body) RETURNING scan;")
            except fx.D1CpuLimit:
                if rows <= 1:
                    raise
                rows //= 2
                continue
            changed += len(got)
            i = j
        self.log(f"owner carry: {tag(old)} → {tag(new)}: {len(scans)} rows, {changed} written")
        return {"rows": len(scans), "changed": changed}

    def repeat(self, old: str, new: str) -> dict:
        """`copy`, best effort: once the pointer has moved, a failure leaves rows that price themselves when next read,
        which is a slower read and not a wrong one. Returns `copy`'s doc, or `{error}` (logged)."""
        try:
            return self.copy(old, new)
        except (RuntimeError, OSError) as e:
            msg = f"{type(e).__name__}: {e}"
            self.log(f"owner carry: the repeat after the pointer failed ({msg}): what it would have copied prices itself when read")
            return {"error": msg}

    def retire(self, gen: str) -> int:
        """`gen`'s rows deleted (`index_footer.d1_delete`); returns how many. A failure raises, and leaves the rest for a
        rerun."""
        tok, acct = fx._creds()
        n = fx.d1_delete(TABLE, f"idx = {fx._q(tag(gen))}", acct, tok, batch=RETIRE_ROWS)
        self.log(f"owner carry: {n} rows deleted under {tag(gen)}")
        return n
