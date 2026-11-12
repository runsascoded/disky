"""Coordinated GC of published generation predecessors, across every serving/building copy.

Unlike the manual `store-gc` sweep, an automatic run only removes generations whose forwarding chain
reaches every copy's current generation. Unknown directories, candidates and explicit pins stay.
"""
from __future__ import annotations

import json
import time
from contextlib import ExitStack
from typing import Callable
from uuid import uuid4

from click import Choice, IntRange, command, option

from . import append_runner as ar
from .blobstore import Store
from .static_names import err
from .store_gen_gc import GRACE_S, gen_dirs


def snapshot(stores: list[Store], prefix: str) -> tuple[str, dict]:
    """All copies have the same current base, its scan roster and its completed publication record."""
    pointers = [s.read_json(f'{prefix}/{ar.CURRENT}') for s in stores]
    if any(p != pointers[0] for p in pointers[1:]):
        raise RuntimeError(f'{prefix}: current pointers disagree across copies; nothing collected')
    gen = pointers[0]['gen']
    marker = f'{prefix}/{gen}/scans.json'
    scans = [s.read_json(marker) for s in stores]
    if any(d != scans[0] for d in scans[1:]):
        raise RuntimeError(f'{prefix}/{gen}: scan rosters disagree across copies; nothing collected')
    records = []
    for s in stores:
        key = f'{prefix}/{gen}/{ar.COMPACTION}'
        records.append(s.read_json(key) if s.exists(key) else None)
    if any(d != records[0] for d in records[1:]):
        raise RuntimeError(f'{prefix}/{gen}: publication records disagree across copies; nothing collected')
    expected = (records[0] or {}).get('files', {})
    if not expected:
        raise RuntimeError(f'{prefix}/{gen}: no completed output inventory; nothing collected')
    listings = [s.listing(f'{prefix}/{gen}') for s in stores]
    for rel in expected:
        key = f'{prefix}/{gen}/{rel}'
        sizes = [d.get(key) for d in listings]
        if sizes[0] is None or any(n != sizes[0] for n in sizes[1:]):
            raise RuntimeError(f'{key}: published inventory missing or sizes disagree; nothing collected')
    # Other lineages may have named pointers in the same bucket (for example a meta store).
    named = [{k: s.read_json(k) for k in s.listing(prefix)
              if k.count('/') == 1 and k.endswith('CURRENT.json')} for s in stores]
    return gen, {'pointer': pointers[0], 'scans': scans[0], 'publication': records[0], 'named': named}


def plan(
    stores: list[Store],
    prefix: str,
    pins: list[str],
    *,
    now: float,
    grace_s: float = GRACE_S,
) -> tuple[dict, list[dict]]:
    current, state = snapshot(stores, prefix)
    inventories = [gen_dirs(s, prefix) for s in stores]
    names = sorted(set().union(*(d.keys() for d in inventories)))
    protected = {current, *pins}
    for pointers in state['named']:
        protected.update(p['gen'] for p in pointers.values())
    for s, inventory in zip(stores, inventories):
        for gen, files in inventory.items():
            marker = f'{prefix}/{gen}/{ar.CANDIDATE}'
            if marker in files:
                source = s.read_json(marker)['from']
                forwarding = f'{prefix}/{source}/{ar.SUCCESSOR}'
                if not s.exists(forwarding) or s.read_json(forwarding)['gen'] != gen:
                    protected.update((gen, source))
        for pin in list(protected):
            if s.exists(f'{prefix}/{pin}/{ar.SUCCESSOR}'):
                protected.add(ar.chain(s, prefix, pin)[-1])
    records = []
    for gen in names:
        files = [{k: n for k, n in d.get(gen, {}).items() if k != f'{prefix}/{gen}/{ar.SUCCESSOR}'} for d in inventories]
        if not any(files):
            continue
        reason = 'current' if gen == current else 'pinned or candidate input' if gen in protected else None
        cutovers = []
        if reason is None:
            for s in stores:
                chain = ar.chain(s, prefix, gen)
                if len(chain) < 2 or chain[-1] != current:
                    reason = 'no published forwarding chain to current in every copy'
                    break
                if not state['publication']:
                    reason = 'current generation has no completed publication record'
                    break
                for old, new in zip(chain, chain[1:]):
                    marker = f'{prefix}/{new}/{ar.COMPACTION}'
                    if s.exists(marker) and s.read_json(marker).get('from') != old:
                        reason = 'successor publication not verified'
                        break
                    if old == gen:
                        cutovers.extend(s.mtimes(f'{prefix}/{old}').values())
                        if s.exists(marker):
                            cutovers.append(s.mtimes(f'{prefix}/{new}').get(marker, now))
                if reason:
                    break
        retired_at = max(cutovers) if cutovers else None
        if reason is None and now - retired_at < grace_s:
            reason = 'retention grace after publication or last predecessor write'
        records.append({'gen': gen, 'action': 'keep' if reason else 'delete', 'reason': reason,
                        'retired_at': retired_at, 'copies': [
                            {'store': s.root, 'objects': len(f), 'bytes': sum(f.values())} for s, f in zip(stores, files)
                        ], '_files': files})
    return state, records


def collect(
    stores: list[Store],
    prefix: str,
    pins: list[str],
    *,
    now: float,
    grace_s: float = GRACE_S,
    execute: bool = False,
    check: Callable[[], None] = lambda: None,
    audit: Callable[[dict], None] | None = None,
) -> list[dict]:
    state, records = plan(stores, prefix, pins, now=now, grace_s=grace_s)
    if audit is not None:
        # Persist the exact plan before the first delete; a failed/partial execution can be reconstructed.
        audit({'state': state, 'generations': records, 'execute': execute, 'at': now, 'grace_s': grace_s})
    for rec in records:
        files = rec.pop('_files')
        if execute and rec['action'] == 'delete':
            err(json.dumps({'generation': rec}, sort_keys=True))
            check()
            if snapshot(stores, prefix)[1] != state:
                raise RuntimeError(f'{prefix}: publication changed during GC; stopped before deleting {rec["gen"]}')
            for s, expected in zip(stores, files):
                actual = {k: n for k, n in s.listing(f'{prefix}/{rec["gen"]}').items() if k != f'{prefix}/{rec["gen"]}/{ar.SUCCESSOR}'}
                if actual != expected:
                    raise RuntimeError(f'{s.root}/{prefix}/{rec["gen"]}: inventory changed during GC')
            for s, f, counts in zip(stores, files, rec['copies']):
                check()
                counts['deleted'] = s.delete(sorted(f))
                err(f'generation-gc {s.root}/{prefix}/{rec["gen"]}: deleted {counts["deleted"]} objects ({counts["bytes"]} B)')
        err(f'generation-gc {prefix}/{rec["gen"]}: {rec["action"]}' + (f' ({rec["reason"]})' if rec['reason'] else ''))
    return records


@command('generation-gc')
@option('-c', '--copy', 'copies', multiple=True, help='Additional copy that must agree before any predecessor is deleted')
@option('-G', '--grace', 'grace_s', default=GRACE_S, type=IntRange(min=0), help='Seconds after cutover/last write to retain predecessors (default 3 days)')
@option('-k', '--keep', 'pins', multiple=True, required=True, help='Every pinned generation in every environment, including secondary stores')
@option('-o', '--out', 'root', required=True, help='Primary store URL or local directory')
@option('-s', '--scratch', help='GCS scratch bucket holding the builders’ coordinator and compaction leases; required to execute')
@option('-t', '--kind', type=Choice(['static-names', 'interval-store']), required=True)
@option('-x', '--execute', is_flag=True, help='Delete verified predecessors (default: plan only)')
def command_cmd(
    copies: tuple[str, ...],
    grace_s: int,
    pins: tuple[str, ...],
    root: str,
    scratch: str | None,
    kind: str,
    execute: bool,
) -> None:
    if execute and not scratch:
        raise ValueError('executing generation GC requires --scratch to coordinate with builders')
    stores = [Store(r) for r in dict.fromkeys((root, *copies))]
    audit_key = f'{kind}/gc/{int(time.time())}-{uuid4().hex}/plan.json' if execute else None

    def audit(doc: dict) -> None:
        Store(f'gs://{scratch}').write_text(audit_key, json.dumps(doc, indent=1) + '\n')
        err(f'generation-gc audit: gs://{scratch}/{audit_key}')

    with ExitStack() as stack:
        checks = []
        if execute:
            checks.append(stack.enter_context(ar.gcs_coordinator_lease(scratch, kind)))
            lease = stack.enter_context(ar.compaction_lease(ar.GcsLeaseFile(scratch, f'{kind}/{ar.COMPACT_LEASE}'), 'generation-gc'))
            checks.append(lease.check)
        def check_leases() -> None:
            for check in checks:
                check()

        records = collect(stores, kind, list(pins), now=time.time(), grace_s=grace_s, execute=execute,
                          check=check_leases, audit=audit if execute else None)
    print(json.dumps({'kind': kind, 'execute': execute, 'audit': f'gs://{scratch}/{audit_key}' if execute else None,
                      'generations': records}, indent=1))
