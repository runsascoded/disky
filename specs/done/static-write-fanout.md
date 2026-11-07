# The drill write's fan-out

## Why

gcs's fold2 (631cbd01, n2-highmem-48): the drill write took 1156s on the fold VM, at ~4740% CPU throughout. Almost all of it was the roots (`write/long/roots` ≈ 740s, 600 files, 263 GB; `write/short/roots` ≈ 345s, 176 files, 103 GB). The rollups were one small file per kind, ~70s together. `write_indexed` is pyarrow encode + zstd at ~2.7M rows/s a core, and each output file reads only the sorted ranges it overlaps. So this is volume, not waste: more cores are the lever.

## Shape

With the measurement fanned out (`static_fanout.Fanout`, `-D`), every measured range's sorted roots are already under the fold's checkpoint, at `measured/<kind>/g<g>/sorted.parquet` (the helpers' uploads, and share 0's via `Fanout.keep`).

- `write_kind` (the `chunks` path, `roots` only) cuts the files as it always has: `_cut_counts` over the ranges' `(q, rows)`. With a `fanout` (`static_fanout.WriteFanout`, from `Fanout.writer()`), it deals the files by rows over the measurement's tasks and keeps share 0. The plan `written/plan-<n>.json` lists each file dealt away: its name, `q` bounds, and its spans' checkpoint keys (`sorted_key`).
- A helper (`static_merge write-shard`, the measurement's stage and machine) fetches its files' sorted ranges and writes each file with `_write_chunk_file`, as the fold VM does. It uploads `written/<kind>/r<i>/{file,index}.parquet`, then `done.json` (rows, bytes). A file with a `done.json` is skipped, so a retry resumes.
- The fold VM writes its share, then `collect` downloads the helpers' files into `out/` and their group index rows. The index is assembled in file order, so the drill's bytes are a single VM's (`test_a_fanned_out_drill_write_writes_the_single_vm_drill`, with a lost helper task re-dealt).
- One `WriteFanout` serves both kinds, each kind with its own re-deals (`base`).

## Cost

The helpers write ~1085s of CPU over 15 n2-highmem-32 tasks (~10 fold-VM equivalents), plus ~1 min of job start, plus ~366 GB downloaded back at ~1.3–2.3 GB/s (~3–5 min). Expected: write ~1156s → ~400s.

## Measured

gcs's race `sn-race-c001-a92e2f38-wf` (2026-10-25, the fold resumed at the drill): `write/long/roots` 432.5s (was 800), 37 files here and 563 on 15 helper tasks (229 GiB fetched back); `write/short/roots` 282.6s (was 356), 9 here and 167 on helpers (89.7 GiB). The helpers' write-shard tasks took 227–289s (long) and 113–182s (short); the copy-in ran at 0.3–2.9 GB/s, 4–23s a task. About 440s off the compaction. The plan itself is milliseconds: the "fan-out plan" stage once named the fold VM's own files' writing too (fixed: the stage is put back after `start`).

## Later

- Publish (and `compact-probe -U`) could copy the helpers' files server side from the checkpoint into the generation instead of the fold VM downloading and re-uploading them. That saves the download here, and much of the race's ~17-min upload. It needs `publish` to take files by checkpoint key; the generation dir itself must not be written before the publish (never over a generation).
- The measurement's collect could skip downloading the sorted ranges the fold VM won't write, once the per-`q` counts the cuts need come with each range's `done.json`.
