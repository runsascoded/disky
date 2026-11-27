"""`static_measure.range_index_sql`: the `q` range a value falls in, as `q_range_sql` bounds them (`[lo, hi)`)."""
from __future__ import annotations

import duckdb
import pytest

from dt_cloud import static_measure as sme


@pytest.mark.parametrize("cuts", [[], ["b"], ["b", "d"], ["b", "d", "f"], ["b", "d", "f", "h", "j"]])
def test_range_index_is_the_cuts_at_or_below(cuts: list[str]):
    values = ["", "a", "b", "bb", "c", "d", "e", "f", "g", "h", "i", "j", "k", "é"]
    con = duckdb.connect()
    got = [r[0] for r in con.execute(f"SELECT {sme.range_index_sql('v', cuts)} FROM (SELECT unnest(?) AS v)", [values]).fetchall()]
    bounds = [None, *cuts, None]
    # the range whose `q_range_sql` admits the value: exactly one
    want = [[i for i, (lo, hi) in enumerate(zip(bounds[:-1], bounds[1:]))
             if con.execute(f"SELECT {sme.q_range_sql('v', lo, hi)} FROM (SELECT ? AS v)", [v]).fetchone()[0]] for v in values]
    assert [[g] for g in got] == want
