"""Adopt opaque object versions without assigning current tokens to historical objects."""
from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
from click import IntRange, command, option

from . import interval_compact as ic
from . import interval_store as ist
from . import tree_mem as tm
from .append_runner import latest_key
from .blobstore import Store, mirror, upload_new
from .interval_local import publish_order, publish_tier, reads
from .static_local import _check_gen, workdir

RECORD = 'object-version-migration.json'


def migrate(store: Store, gen: str, target: str, work: Path, *, ranges_gen: str | None = None,
            threads: int = 4, mem: str = "4GB", rg_rows: int = ist.SERVED_RG) -> dict:
    """Fold into a new generation: existing GCS generations become exact strings, unknown historical identities stay NULL.

    Neither the source nor CURRENT moves. A future scan's first known token opens its own interval, including when
    size and mtime match an older unknown version. No raw listing or historical per-scan index is needed.
    """
    source_prefix, prefix = f"{ist.PREFIX}/{gen}", f"{ist.PREFIX}/{target}"
    if store.listing(prefix):
        raise ValueError(f"{prefix}: refusing to overwrite a generation")
    source = work / "source"
    mirror(store, source_prefix, source, keep=reads)
    if not (source / "scans.json").exists():
        raise ValueError(f"{source_prefix}: missing scans.json")
    if not (source / "ranges.json").exists():
        if ranges_gen is None:
            raise ValueError(f"{source_prefix}: ranges are borrowed; supply --ranges-gen")
        mirror(store, f"{ist.PREFIX}/{ranges_gen}", source,
               keep=lambda key: key == "ranges.json" or key.split("/")[0] in ist.SUB_SCHEMA)
    base = json.loads((source / "scans.json").read_text())
    if isinstance(ist.has_generation(base), pa.Field):
        raise ValueError(f"{source_prefix}: already carries opaque object versions")
    manifest = latest_key([p.relative_to(source).as_posix() for p in (source / "manifests").glob("*.json")])
    runs = json.loads((source / manifest).read_text())["runs"] if manifest else []
    from .interval_level import LEVEL_DOC, leveled, record_level

    if not leveled(base, source) and any((source / r['key'] / LEVEL_DOC).is_file() for r in runs):
        # A legacy base may lack the optional sort while its newer runs have it.
        # The fold rebuilds it over the complete history in the new generation.
        (source / 'scans.json').write_text(json.dumps(record_level(base)) + '\n')
    out = work / "output"
    doc, rows, _, _ = ic.fold_gen(source, out, work / "fold", gen=gen, runs=runs, threads=threads, mem=mem,
                                 rg_rows=rg_rows, identity=ist.OBJECT_VERSION_FIELD)
    record = {"from": gen, "gen": target, "manifest": manifest, "scans": len(doc["scans"]),
              "object_version": doc["object_version"], "rows": rows}
    (out / RECORD).write_text(json.dumps(record, indent=1) + "\n")
    upload_new(store, out, prefix, {}, publish_order, tier=publish_tier)
    return record


@command("migrate-object-version")
@option("-g", "--gen", required=True, help="Source generation (left unchanged)")
@option("-M", "--mem", default="4GB", help="Memory budget of the whole process tree (`tree_mem`): DuckDB gets what this process, the cut's workers and the Arrow buffers leave")
@option("-o", "--out", "store_root", required=True, help="Generation store, a dir or URL")
@option("-O", "--to-gen", required=True, help="New generation; must not exist")
@option("-p", "--threads", default=4, type=IntRange(min=1))
@option("-R", "--ranges-gen", help="Source's borrowed range generation, if any")
@option("-r", "--rg-rows", default=ist.SERVED_RG, type=IntRange(min=1))
@option("-w", "--work", help="Local work directory")
def migrate_cmd(gen, mem, store_root, to_gen, threads, ranges_gen, rg_rows, work) -> None:
    """Adopt the shared opaque version field; preserve unknowns and leave CURRENT for a verified cutover."""
    _check_gen(gen)
    _check_gen(to_gen)
    with tm.watch(mem, f"migrate-object-version {gen}"), workdir(work) as w:
        print(json.dumps(migrate(Store(store_root), gen, to_gen, w, ranges_gen=ranges_gen,
                                  threads=threads, mem=mem, rg_rows=rg_rows), indent=1))
