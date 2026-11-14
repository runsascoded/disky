from dataclasses import replace

from dt_cloud import digest as E
from dt_cloud import digest_cw as D

TIB = 1024 ** 4
CFG = E.DigestConfig(root="unused", state="unused", template="cw", title="T", site_url="https://x.test", primary="main", quota_marks="arrow-chip", reply_lines=True,
                     buckets={"main": E.Bucket("main", E.Quota(100*TIB, "100 TiB", "100Ti")),
                              "west": E.Bucket("04a", E.Quota(100*TIB, "100 TiB", "100Ti")), "east": E.Bucket("01a")})
DAY = D.DayRow(date="2026-09-23", scan="2026-09-23T1201", tb=50, dtb=0, hours=24, since=None, extra={"west": 0, "east": .1})
MAIN = ":pool-chip-8-0: [main](https://x.test/main?d=2609231201#over-time): 50.0% of 100Ti (50.0 Ti free)"
WEST = ":pool-chip-8-0: [04a](https://x.test/west?d=2609231201#over-time): 0%"
EAST = "[01a](https://x.test/east?d=2609231201#over-time): 0 Ti"


def test_quiet_entries_share_a_line_without_combining_quotas():
    assert D._tail(DAY, CFG).splitlines() == [MAIN, f"{WEST} · {EAST}"]


def test_low_info_lines_share_whatever_their_size_or_change():
    big = "[01a](https://x.test/east?d=2609231201#over-time): 66 Ti"
    assert D._tail(replace(DAY, extra={"west": 0, "east": 66}), CFG).splitlines() == [MAIN, f"{WEST} · {big}"]
    assert D._tail(replace(DAY, dextra={"west": -3}), CFG).splitlines() == [MAIN, f"{WEST} · {EAST}"]


def test_a_headroom_clause_keeps_its_own_line():
    west = ":pool-chip-8-0: [04a](https://x.test/west?d=2609231201#over-time): 30.0% of 100Ti (70.0 Ti free)"
    assert D._tail(replace(DAY, extra={"west": 30, "east": .1}), CFG).splitlines() == [MAIN, west, EAST]


def test_missing_bucket_does_not_appear_as_empty():
    assert D._tail(replace(DAY, extra={"west": 0}), CFG).splitlines() == [MAIN, WEST]


def test_single_line_format_retains_its_order():
    assert D._tail(DAY, replace(CFG, reply_lines=False)) == f"{MAIN} · {WEST} · {EAST}"
