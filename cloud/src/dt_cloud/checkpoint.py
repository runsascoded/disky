"""Resumable steps for a long single-task job (a spot Batch task reclaimed and retried with the same args): each step's
output — DuckDB tables, a JSON doc, or an append-only series of row parts — saved under `<root>/<prefix>/` on a
`blobstore.Store` (a local dir or a `gs://` / `r2://` / `s3://` URL), so a rerun skips what is saved and resumes after it
(specs/drill-preemption.md).

- **Steps** (`save` / `load`): a step's tables (parquet, written with their DuckDB column types so a load restores them
  exactly: `HUGEINT`s ride as text, which parquet would otherwise take as a double) and doc; its `done.json`, written last,
  is the step's completeness marker.
- **Parts** (`save_part` / `parts`): a loop's output so far, in order, with how many of its items it covers: each part's
  rows, then `progress.json` (`{done, parts}`), so a rerun loads the parts and continues at item `done`.

A checkpoint is the code's that saved it (`code`, `code_version()`: a hash of this package's source): opened by other
code (a rebuilt image, after a bug fix), it is cleared rather than resumed, so a step a buggy build saved never reaches the
fixed build's output. `close` logs the checkpoint's own cost (`saves`, `resumes`: count and seconds).

The layout is the checkpoint's own (nothing reads it but a rerun); `clear` deletes it once the job's output is published.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from time import monotonic

import pyarrow as pa
import pyarrow.parquet as pq

from .blobstore import Store
from .static_names import err, q

#: Types parquet does not round-trip exactly (DuckDB writes `HUGEINT` as a double): saved as text, cast back on load.
TEXT_TYPES = ("HUGEINT", "UHUGEINT")
#: A loop's parts are saved at most this often (seconds): a reclaimed task loses at most this much of the loop.
PART_INTERVAL_S = 300.0


def code_version(pkg: Path = Path(__file__).parent) -> str:
    """A hash of the package's Python source (every `*.py` under `pkg`, by relative path): what a checkpoint is keyed by."""
    h = hashlib.sha256()
    for f in sorted(pkg.rglob("*.py")):
        h.update(f.relative_to(pkg).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()[:16]


class Checkpoint:
    """Steps' outputs under `Store(root)`'s `prefix/`, staged through a local dir under `work` (default: the system's
    temp dir; a big step's tables pass through it). `on_save(name)` (tests: an interruption) is called after each step's
    marker or part's progress is written, with its name relative to the root checkpoint (`<sub>/<step>`, a part
    `<step>#<n>`). `code`: the version this checkpoint belongs to (`code.json`); one another version saved is cleared."""

    def __init__(self, root: str, prefix: str, *, every_s: float = PART_INTERVAL_S, log: Callable[[str], None] = err,
                 on_save: Callable[[str], None] | None = None, work: str | Path | None = None, code: str | None = None):
        self.store, self.prefix, self.every_s, self.log, self.on_save = Store(root), prefix.strip("/"), every_s, log, on_save
        if work:
            Path(work).mkdir(parents=True, exist_ok=True)
        self._tmp = Path(tempfile.mkdtemp(prefix="ckpt-", dir=work))
        self._label = ""
        # Shared with `sub` checkpoints (the same dict): the whole checkpoint's cost, logged by `close`.
        self.stats = {"saves": 0, "save_s": 0.0, "resumes": 0, "resume_s": 0.0}
        if code:
            self._claim(code)

    def _claim(self, code: str) -> None:
        """Clear what other code saved (or a checkpoint without a `code.json`); then mark this one `code`'s."""
        key = self.key("code.json")
        have = self.store.read_json(key)["code"] if self.store.exists(key) else None
        if have != code and self.store.listing(self.prefix):
            self.log(f"checkpoint {self.store.uri(self.prefix)}: saved by code {have or '(unversioned)'}, this is {code}: not resumed")
            self.clear()
        if have != code:
            self.store.write_text(key, json.dumps({"code": code}) + "\n")

    def _count(self, kind: str, t0: float) -> None:
        self.stats[f"{kind}s"] += 1
        self.stats[f"{kind}_s"] += monotonic() - t0

    def __repr__(self) -> str:
        return f"Checkpoint({self.store.uri(self.prefix)!r})"

    def key(self, rel: str) -> str:
        return f"{self.prefix}/{rel}"

    def sub(self, name: str) -> Checkpoint:
        """The checkpoint of a sub-job (e.g. one kind of a drill) under `prefix/name/`."""
        c = Checkpoint.__new__(Checkpoint)
        c.__dict__.update(self.__dict__)
        c.prefix, c._label = f"{self.prefix}/{name}", f"{self._label}{name}/"
        return c

    def _saved(self, name: str) -> None:
        if self.on_save:
            self.on_save(f"{self._label}{name}")

    # ── Tables ──

    def _put_table(self, con, table: str, key: str) -> list[list[str]]:
        cols = con.execute(f"SELECT column_name, column_type FROM (DESCRIBE {table})").fetchall()
        sel = ", ".join(f'"{c}"::VARCHAR AS "{c}"' if t in TEXT_TYPES else f'"{c}"' for c, t in cols)
        local = self._tmp / "put.parquet"
        con.execute(f"COPY (SELECT {sel} FROM {table}) TO {q(str(local))} (FORMAT parquet, COMPRESSION zstd)")
        self.store.put(local, self.key(key))
        local.unlink()
        return [[c, t] for c, t in cols]

    def _get_table(self, con, table: str, key: str, cols: list[list[str]]) -> None:
        local = self._tmp / "get.parquet"
        self.store.get(self.key(key), local)
        con.execute(f"DROP TABLE IF EXISTS {table}")
        con.execute(f"CREATE TABLE {table} ({', '.join(f'{chr(34)}{c}{chr(34)} {t}' for c, t in cols)})")
        con.execute(f"""INSERT INTO {table} SELECT {', '.join(f'CAST("{c}" AS {t})' for c, t in cols)} FROM read_parquet({q(str(local))})""")
        local.unlink()

    # ── Steps ──

    def done(self, step: str) -> bool:
        return self.store.exists(self.key(f"{step}/done.json"))

    def save(self, con, step: str, tables: tuple[str, ...] | list[str] = (), doc: dict | None = None) -> None:
        """Step `step`'s `tables` (DuckDB) and `doc`; the marker last."""
        t0 = monotonic()
        schemas = {t: self._put_table(con, t, f"{step}/{t}.parquet") for t in tables}
        self.store.write_text(self.key(f"{step}/done.json"), json.dumps({"tables": schemas, "doc": doc}) + "\n")
        self.log(f"checkpoint {self.key(step)}: saved {', '.join(tables) or 'doc'} in {monotonic() - t0:.0f}s")
        self._count("save", t0)
        self._saved(step)

    def load(self, con, step: str) -> dict | None:
        """Step `step`'s tables restored (replacing any of those names) and its doc."""
        t0 = monotonic()
        m = self.store.read_json(self.key(f"{step}/done.json"))
        for t, cols in m["tables"].items():
            self._get_table(con, t, f"{step}/{t}.parquet", cols)
        self.log(f"checkpoint {self.key(step)}: resumed from {', '.join(m['tables']) or 'doc'} in {monotonic() - t0:.0f}s")
        self._count("resume", t0)
        return m["doc"]

    # ── Files ──

    def save_files(self, step: str, local: Path, rels: list[str], doc: dict | None = None) -> None:
        """Files `local/<rel>` (a sub-job's finished output) and `doc`; the marker last."""
        t0 = monotonic()
        for r in rels:
            self.store.put(local / r, self.key(f"{step}/files/{r}"))
        self.store.write_text(self.key(f"{step}/done.json"), json.dumps({"tables": {}, "files": rels, "doc": doc}) + "\n")
        self.log(f"checkpoint {self.key(step)}: saved {len(rels)} files")
        self._count("save", t0)
        self._saved(step)

    def load_files(self, step: str, local: Path) -> dict | None:
        t0 = monotonic()
        m = self.store.read_json(self.key(f"{step}/done.json"))
        for r in m["files"]:
            self.store.get(self.key(f"{step}/files/{r}"), local / r)
        self.log(f"checkpoint {self.key(step)}: resumed {len(m['files'])} files")
        self._count("resume", t0)
        return m["doc"]

    # ── Parts: a loop's output so far ──

    def parts(self, step: str) -> tuple[int, int, pa.Table | None]:
        """`(done, parts, rows)`: how many items the saved parts cover, how many parts, and their rows (None: no part saved)."""
        if not self.store.exists(self.key(f"{step}/progress.json")):
            return 0, 0, None
        t0 = monotonic()
        p = self.store.read_json(self.key(f"{step}/progress.json"))
        tables = []
        for i in range(p["parts"]):
            local = self._tmp / "part.parquet"
            self.store.get(self.key(f"{step}/part-{i:05d}.parquet"), local)
            tables.append(pq.read_table(local))
            local.unlink()
        self.log(f"checkpoint {self.key(step)}: resumed at item {p['done']:,} ({p['parts']} parts)")
        self._count("resume", t0)
        return p["done"], p["parts"], pa.concat_tables(tables) if tables else None

    def save_part(self, step: str, rows: pa.Table, done: int, n_parts: int) -> int:
        """Part `n_parts` (`rows`, the loop's output since the last part), then the progress (`done` items); returns the
        part count."""
        t0 = monotonic()
        local = self._tmp / "part.parquet"
        pq.write_table(rows, local, compression="zstd")
        self.store.put(local, self.key(f"{step}/part-{n_parts:05d}.parquet"))
        local.unlink()
        self.store.write_text(self.key(f"{step}/progress.json"), json.dumps({"done": done, "parts": n_parts + 1}) + "\n")
        self._count("save", t0)
        self._saved(f"{step}#{n_parts}")
        return n_parts + 1

    def clear(self) -> int:
        """Delete every saved step (the job's output is published); returns the files deleted."""
        n = self.store.delete(self.store.listing(self.prefix))
        if n:
            self.log(f"checkpoint {self.store.uri(self.prefix)}: cleared {n} files")
        return n

    def close(self) -> None:
        s = self.stats
        self.log(f"checkpoint {self.store.uri(self.prefix)}: total {s['saves']} saves in {s['save_s']:.0f}s, "
                 f"{s['resumes']} resumes in {s['resume_s']:.0f}s")
        shutil.rmtree(self._tmp, ignore_errors=True)


class Parts:
    """A loop's resumable output: `start` items already done (their rows `loaded`), `add(rows, done)` after each item, a
    part saved when `every_s` has passed since the last (or `flush`, at the loop's end). Without a checkpoint, nothing."""

    def __init__(self, ck: Checkpoint | None, step: str, schema: pa.Schema):
        self.ck, self.step, self.schema = ck, step, schema
        self.start, self.n_parts, self.loaded = ck.parts(step) if ck else (0, 0, None)
        self.saved = self.start
        self.pending: list[pa.Table] = []
        self.t = monotonic()

    def add(self, rows: pa.Table | list[tuple], done: int) -> None:
        if self.ck is None:
            return
        if isinstance(rows, list):
            rows = pa.table([pa.array(col, f.type) for col, f in zip(zip(*rows), self.schema)], schema=self.schema) if rows else self.schema.empty_table()
        self.pending.append(rows.cast(self.schema))
        if monotonic() - self.t >= self.ck.every_s:
            self.flush(done)

    def flush(self, done: int) -> None:
        """A part of the rows since the last (unless no item was done since)."""
        if self.ck is None or done == self.saved:
            return
        rows = pa.concat_tables(self.pending) if self.pending else self.schema.empty_table()
        self.n_parts = self.ck.save_part(self.step, rows, done, self.n_parts)
        self.pending, self.t, self.saved = [], monotonic(), done
