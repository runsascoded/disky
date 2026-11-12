"""Read-only inventories of derived storage, for retention reviews and CS3 measurements."""
from __future__ import annotations

import json
from click import IntRange, argument, command, option

from .blobstore import Store


def audit(store: Store, prefix: str, depth: int) -> dict:
    files = store.listing(prefix)
    groups: dict[str, dict[str, int]] = {}
    for key, size in files.items():
        group = '/'.join(key.split('/')[:depth])
        entry = groups.setdefault(group, {'objects': 0, 'bytes': 0})
        entry['objects'] += 1
        entry['bytes'] += size
    return {'root': store.root, 'prefix': prefix, 'objects': len(files), 'bytes': sum(files.values()),
            'groups': dict(sorted(groups.items())), 'files': dict(sorted(files.items()))}


@command('store-audit')
@option('-d', '--depth', default=1, type=IntRange(min=1), help='Group keys by this many path components')
@option('-o', '--out', help='Complete inventory JSON, local path or URL; stdout contains the summary')
@option('-p', '--prefix', default='', help='Inventory only this prefix')
@argument('root')
def store_audit_cmd(depth: int, out: str | None, prefix: str, root: str) -> None:
    """List sizes and exact keys without reading object bodies or deleting anything."""
    result = audit(Store(root), prefix, depth)
    if out:
        from disk_tree.blobfs import write_text

        write_text(out, json.dumps(result, sort_keys=True, indent=2) + '\n')
    result.pop('files')
    print(json.dumps(result, sort_keys=True, indent=2))
