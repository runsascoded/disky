#!/usr/bin/env python
"""A `reads` sort with no rows (a store without read logs) and its sidecar, written as `dt_cloud.interval_store` writes
them: what `intervalStore.test.ts` serves in place of `g1`'s `reads` files."""
from os.path import dirname
from pathlib import Path

from dt_cloud import interval_store as ist


def main() -> None:
    out = Path(dirname(__file__)) / "reads.parquet"
    with ist._served_writer(out, ist.RD_SCHEMA, None) as w:
        w.add_key_value_metadata({"store": "interval", "version": str(ist.STORE_VERSION), "sort": "reads", "order": ist.SORTS["reads"][1], "segments": "open,dyadic", "open": str(ist.OPEN)})
    ist.write_groups(out, [], "reads", lambda rows: ist._seg_summary(rows, True))


if __name__ == "__main__":
    main()
