"""`dt-cloud vm-pack`: VM mode's pack writer (specs/vm-mode.md §6).

After the interval store's append, the store's current generation and its newest manifest become one slim pack
(`fsb pack-slim`, `bench/full-scan/`), published next to the store:

    <store>/vm-pack/<gen>/<manifest stem>.fsb     the pack (its header records `{gen, manifest}`: the pins vmd checks)
    <store>/vm-pack/CURRENT.json                  {gen, manifest, key, bytes, sha256, scans, written}, written last

`build` is idempotent: when `CURRENT.json` already names the generation and manifest, it does nothing. Older packs
beyond the newest `keep` are deleted (the serving VM may still be mapping the previous one).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from functools import partial
from pathlib import Path

from click import IntRange, Path as PathType, argument, group, option

from .append_runner import current, manifest_keys
from .blobstore import Store, store_url

err = partial(print, file=sys.stderr)

#: The interval store's prefix in a store (the site's `interval-store/CURRENT.json`).
STORE_PREFIX = "interval-store"
#: Where packs go, beside it.
PREFIX = "vm-pack"
POINTER = f"{PREFIX}/CURRENT.json"


def newest(store: Store) -> tuple[str, str | None]:
    """`(generation, newest manifest's file name or None)`: `interval-store/CURRENT.json`'s generation and its newest
    manifest (by scan, then revision), as the site's `newestManifest` picks it."""
    gen = current(store, STORE_PREFIX)
    if gen is None:
        raise FileNotFoundError(f"{store.uri(STORE_PREFIX)}: no CURRENT.json")
    if not store.exists(f"{STORE_PREFIX}/{gen}/scans.json"):
        raise FileNotFoundError(f"{store.uri(f'{STORE_PREFIX}/{gen}')}: no scans.json")
    g = f"{STORE_PREFIX}/{gen}/"
    ms = manifest_keys([k.removeprefix(g) for k in store.listing(f"{g}manifests")])
    return gen, (ms[-1].removeprefix("manifests/") if ms else None)


def sorts(store: Store, gen: str, manifest: str | None) -> list[str]:
    """The keys `fsb` reads, relative to the generation: `scans.json`, the manifest, the base's `path` sort, then each
    of the manifest's runs' `path` sorts."""
    keys = ["scans.json", "served/path.parquet"]
    if manifest:
        keys.insert(1, f"manifests/{manifest}")
        m = store.read_json(f"{STORE_PREFIX}/{gen}/manifests/{manifest}")
        keys += [f"{r['key']}/served/path.parquet" for r in m["runs"]]
    return keys


def pack_key(gen: str, manifest: str | None) -> str:
    stem = manifest.removesuffix(".json") if manifest else "base"
    return f"{PREFIX}/{gen}/{stem}.fsb"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while b := f.read(1 << 24):
            h.update(b)
    return h.hexdigest()


def fsb_path(given: str | None) -> str:
    """`fsb`: `--fsb`, `$FSB`, or on `PATH`."""
    p = given or os.environ.get("FSB") or shutil.which("fsb")
    if not p:
        raise FileNotFoundError("fsb not found (--fsb, $FSB, or PATH): the job image needs bench/full-scan's `fsb`")
    return p


def build(root: str, work: Path, fsb: str, *, keep: int = 2, force: bool = False) -> dict:
    """Write and publish the pack of `root`'s current generation and newest manifest; the pointer's content (with
    `"skipped": true` when it already named them)."""
    store = Store(store_url(root))
    gen, manifest = newest(store)
    if not force and store.exists(POINTER):
        cur = store.read_json(POINTER)
        if cur.get("gen") == gen and cur.get("manifest") == (manifest or ""):
            err(f"vm-pack: {POINTER} already holds {gen} / {manifest}")
            return {**cur, "skipped": True}
    dest = work / "gen" / gen
    if dest.exists():
        shutil.rmtree(dest)
    t0 = time.monotonic()
    for k in sorts(store, gen, manifest):
        (dest / k).parent.mkdir(parents=True, exist_ok=True)
        store.get(f"{STORE_PREFIX}/{gen}/{k}", dest / k)
    mirror_s = time.monotonic() - t0
    out = work / "pack.fsb"
    t1 = time.monotonic()
    r = subprocess.run([fsb, "pack-slim", str(dest), "-o", str(out)], stdout=subprocess.PIPE, text=True, check=True)
    info = json.loads(r.stdout.strip().splitlines()[-1])
    pack_s = time.monotonic() - t1
    key = pack_key(gen, manifest)
    doc = {"gen": gen, "manifest": manifest or "", "key": key, "bytes": out.stat().st_size, "sha256": sha256(out),
           "scans": info["scans"], "written": int(time.time()), "mirror_s": round(mirror_s, 1), "pack_s": round(pack_s, 1),
           "peak_rss_gb": info.get("load", {}).get("peak_rss_gb")}
    store.put(out, key)
    store.write_text(POINTER, json.dumps(doc) + "\n")
    out.unlink()
    shutil.rmtree(dest)
    pruned = prune(store, keep, key)
    err(f"vm-pack: {key} ({doc['bytes']:,} B, {doc['scans']} scans; mirror {mirror_s:.0f} s, pack {pack_s:.0f} s), pruned {pruned}")
    return doc


def prune(store: Store, keep: int, current_key: str) -> list[str]:
    """Delete packs beyond the newest `keep` (by when they were written), never the current one."""
    packs = sorted(((t, k) for k, t in store.mtimes(PREFIX).items() if k.endswith(".fsb")), reverse=True)
    old = [k for _, k in packs[keep:] if k != current_key]
    if old:
        store.delete(old)
    return old


@group("vm-pack")
def cli() -> None:
    """VM mode's packs (specs/vm-mode.md §6)."""


@cli.command("build")
@option("-f", "--fsb", help="The `fsb` binary (default `$FSB`, then PATH)")
@option("-F", "--force", is_flag=True, help="Write even when the pointer already names this generation and manifest")
@option("-k", "--keep", type=IntRange(1), default=2, show_default=True, help="Packs to keep under vm-pack/")
@option("-o", "--store", "root", required=True, help="The store (a dir or a gs:// / s3:// / r2:// URL) holding interval-store/")
@option("-w", "--work", type=PathType(file_okay=False, path_type=Path), default=Path("work/vm-pack"), show_default=True, help="Local work dir (the mirrored generation and the pack: ~1.3x the pack's size)")
def build_cmd(fsb: str | None, force: bool, keep: int, root: str, work: Path) -> None:
    """Write the slim pack of the store's current interval-store generation and newest manifest, and publish it under
    vm-pack/ (the pack, then `vm-pack/CURRENT.json`). Prints the pointer's JSON."""
    work.mkdir(parents=True, exist_ok=True)
    print(json.dumps(build(root, work, fsb_path(fsb), keep=keep, force=force)))


@cli.command("current")
@argument("root")
def current_cmd(root: str) -> None:
    """Print ROOT's `vm-pack/CURRENT.json` (nothing when there is none)."""
    store = Store(store_url(root))
    if store.exists(POINTER):
        print(json.dumps(store.read_json(POINTER)))
