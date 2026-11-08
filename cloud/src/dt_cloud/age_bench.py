"""Age API answer parity and first/repeated-request timings across a serving change.

First requests are cold only when the caller uses a fresh deployment. No query
parameter here purports to flush the Worker's metadata or row-group caches.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from statistics import median

from click import IntRange, argument, command, option

Fetch = Callable[[dict], tuple[int, object, float, str, str]]


def capture(
    cases: list[dict],
    fetch: Fetch,
    repeats: int,
    baseline: dict | None = None,
) -> dict:
    if not cases:
        raise ValueError('age benchmark needs at least one case')
    if baseline is not None and [c['query'] for c in baseline['cases']] != cases:
        raise ValueError('age benchmark cases differ from baseline')
    samples: list[list[tuple]] = [[] for _ in cases]
    for _ in range(repeats + 1):
        for query, values in zip(cases, samples):
            values.append(fetch(query))
    results = []
    for query, values in zip(cases, samples):
        first = values[0]
        ms = [round(v[2] * 1000, 3) for v in values]
        results.append({'query': query, 'answer': first[1], 'statuses': [v[0] for v in values],
                        'first_ms': ms[0], 'repeat_ms': ms[1:], 'repeat_median_ms': median(ms[1:]),
                        'stable': all(v[0] == 200 and v[1] == first[1] for v in values)})
    changed = [i for i, r in enumerate(results) if baseline is not None and r['answer'] != baseline['cases'][i]['answer']]
    return {'version': 1, 'cases': results, 'changed': changed, 'pass': not changed and all(r['stable'] for r in results)}


@command('age-bench')
@option('-b', '--baseline', type=Path, help='Earlier report: require identical answers to the same cases')
@option('-d', '--deployment', help='Deployment ID recorded for reproducibility (fresh deployment for cold timings)')
@option('-n', '--repeats', default=2, type=IntRange(min=1), help='Repeated requests after the first wave')
@option('-o', '--out', required=True, type=Path, help='Report with exact answers, statuses and every timing')
@option('-u', '--url', required=True, help='Site host; auth uses the existing SITE_TOKEN')
@argument('cases', type=Path)
def age_bench_cmd(baseline: Path | None, deployment: str | None, repeats: int, out: Path, url: str, cases: Path) -> None:
    """Capture age API cases (JSON query objects), then compare after a reader cutover. Never changes serving."""
    from .deploy import site_token
    from .diff_check import fetch

    token = site_token(None)
    result = capture(json.loads(cases.read_text()), lambda query: fetch(url, query, token, route='/api/age-pyramid'),
                     repeats, json.loads(baseline.read_text()) if baseline else None)
    result.update(url=url, deployment=deployment)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')
    for row in result['cases']:
        print(f"{json.dumps(row['query'], sort_keys=True)}: first {row['first_ms']:.1f} ms, repeat median {row['repeat_median_ms']:.1f} ms")
    print(f"{len(result['cases'])} cases, {len(result['changed'])} differing answers, {'pass' if result['pass'] else 'FAIL'}")
    if not result['pass']:
        raise SystemExit(1)
