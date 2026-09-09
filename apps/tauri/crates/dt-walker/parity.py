#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["click"]
# ///
"""Benchmark and parity-check `dt-walker` against `gfind`.

Runs both over the same root (the `%y %b %T@ %p\\0` stream), times each, then
compares the two record sets modulo ordering. `%T@` is compared as
`int(float(...))` — exactly what disk-tree's Python parser keeps — so gfind's
fractional seconds don't count as a mismatch.

    ./parity.py /some/dir
    ./parity.py --walker ../../target/release/dt-walker ~/some/large/subtree
"""
from __future__ import annotations

import subprocess
import sys
import time
from collections import namedtuple

from click import argument, command, option

Rec = namedtuple("Rec", "kind blocks mtime")


def parse(stream: bytes) -> dict[bytes, Rec]:
    """`%y %b %T@ %p\\0` → {path_bytes: Rec}. mtime truncated to int like the
    Python parser (`int(float(%T@))`)."""
    out: dict[bytes, Rec] = {}
    for record in stream.split(b"\0"):
        if not record:
            continue
        parts = record.split(b" ", 3)
        if len(parts) < 4:
            continue
        kind, blocks, mtime, path = parts
        out[path] = Rec(kind, int(blocks), int(float(mtime)))
    return out


def run(cmd: list[str]) -> tuple[bytes, float, bytes]:
    t0 = time.monotonic()
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return p.stdout, time.monotonic() - t0, p.stderr


@command()
@option("-n", "--max-mismatches", default=20, help="Max per-category mismatches to print")
@option("-w", "--walker", default=None, help="Path to dt-walker binary")
@argument("root")
def main(max_mismatches: int, walker: str | None, root: str):
    from pathlib import Path

    if walker is None:
        walker = str(Path(__file__).parent / "target" / "release" / "dt-walker")
        if not Path(walker).exists():
            # workspace target dir
            walker = str(Path(__file__).parents[2] / "target" / "release" / "dt-walker")

    print(f"root:   {root}", file=sys.stderr)
    print(f"walker: {walker}", file=sys.stderr)

    g_out, g_dt, _ = run(["gfind", root, "-printf", r"%y %b %T@ %p\0"])
    w_out, w_dt, w_err = run([walker, "--no-default-excludes", root])

    g = parse(g_out)
    w = parse(w_out)

    print(f"\ngfind:     {len(g):>9,} records   {g_dt:7.3f}s", file=sys.stderr)
    speedup = g_dt / w_dt if w_dt else float("inf")
    print(f"dt-walker: {len(w):>9,} records   {w_dt:7.3f}s   ({speedup:.2f}x)", file=sys.stderr)

    g_paths, w_paths = set(g), set(w)
    only_g = g_paths - w_paths
    only_w = w_paths - g_paths
    common = g_paths & w_paths

    kind_mm = [(p, g[p].kind, w[p].kind) for p in common if g[p].kind != w[p].kind]
    block_mm = [(p, g[p].blocks, w[p].blocks) for p in common if g[p].blocks != w[p].blocks]
    mtime_mm = [(p, g[p].mtime, w[p].mtime) for p in common if g[p].mtime != w[p].mtime]

    def show(label: str, items: list):
        print(f"\n{label}: {len(items)}", file=sys.stderr)
        for row in items[:max_mismatches]:
            print(f"  {row}", file=sys.stderr)

    show("only in gfind", sorted(only_g))
    show("only in dt-walker", sorted(only_w))
    show("kind mismatches", kind_mm)
    show("block (%b) mismatches", block_mm)
    show("mtime mismatches", mtime_mm)

    total_block_gap = sum(gb - wb for _, gb, wb in block_mm)
    if block_mm:
        print(f"\nΣ(gfind - walker) blocks over mismatches: {total_block_gap} "
              f"({total_block_gap * 512 / 1e6:.1f} MB)", file=sys.stderr)

    ok = not (only_g or only_w or kind_mm or block_mm or mtime_mm)
    print(f"\n{'PARITY OK' if ok else 'PARITY MISMATCH'}", file=sys.stderr)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
