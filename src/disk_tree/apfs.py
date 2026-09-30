"""The APFS container a path lives on: its volumes, their usage, and snapshots.

A scan walks one tree; the disk holds more. On a Mac the boot container also
carries the sealed System volume, Preboot (OS update staging), VM (swap),
Recovery and Update volumes, plus snapshots — space no walk of `~` (or even
of `/`) can attribute. `diskutil` reports each volume's `CapacityInUse`, which
lets a UI draw the whole container: walked tree + other volumes + free.

Per-snapshot sizes aren't available from APFS without a diff; snapshots are
listed with their flags (`purgeable`, `limits_shrink`) only.
"""

from __future__ import annotations

import plistlib
import re
import subprocess
from dataclasses import asdict, dataclass, field

_DEV_MOUNT_RE = re.compile(r'^/dev/(?P<dev>\S+) on (?P<path>/.*?)(?: type \S+)? \([^()]*\)$')


@dataclass
class Snapshot:
    name: str
    xid: int
    purgeable: bool
    limits_shrink: bool


@dataclass
class Volume:
    device: str
    name: str
    roles: list[str]
    used: int
    mount: str | None
    snapshots: list[Snapshot] = field(default_factory=list)


@dataclass
class Container:
    device: str
    capacity: int
    free: int
    volumes: list[Volume]

    @property
    def used(self) -> int:
        return self.capacity - self.free

    def to_json(self) -> dict:
        return {**asdict(self), 'used': self.used}


def device_mounts(mount_output: str) -> dict[str, str]:
    """`/dev/<dev>` → mount point, from `mount` output. A sealed System volume
    mounts as its snapshot (`disk3s1s1`); `build_container` matches both."""
    return {m['dev']: m['path'] for line in mount_output.splitlines() if (m := _DEV_MOUNT_RE.match(line))}


def build_container(
    info: dict,
    apfs_list: dict,
    snapshots: dict[str, dict],
    mounts: dict[str, str],
) -> Container:
    """Assemble a `Container` from `diskutil info -plist PATH`, `diskutil apfs
    list -plist`, `diskutil apfs listSnapshots -plist <vol>` per volume device,
    and `device_mounts`. Pure, for tests."""
    ref = info['APFSContainerReference']
    c = next(c for c in apfs_list['Containers'] if c['ContainerReference'] == ref)
    volumes = []
    for v in c['Volumes']:
        dev = v['DeviceIdentifier']
        # The volume itself or its snapshot (`disk3s1s1` is the sealed System
        # volume at `/`; `disk3s1` itself may also be mounted for an update).
        # The shortest mount path is the one people know it by.
        cands = [p for d, p in mounts.items() if d == dev or d.startswith(dev + 's')]
        mount = min(cands, key=len) if cands else None
        snaps = [
            Snapshot(s['SnapshotName'], s['SnapshotXID'], s['Purgeable'], s['LimitingContainerShrink'])
            for s in snapshots.get(dev, {}).get('Snapshots', [])
        ]
        volumes.append(Volume(dev, v['Name'], list(v.get('Roles', [])), v['CapacityInUse'], mount, snaps))
    volumes.sort(key=lambda v: -v.used)
    return Container(ref, c['CapacityCeiling'], c['CapacityFree'], volumes)


def container_for(path: str = '/') -> Container:
    """The live APFS container holding `path` (macOS; raises elsewhere)."""
    info = plistlib.loads(subprocess.run(['diskutil', 'info', '-plist', path], check=True, capture_output=True).stdout)
    if 'APFSContainerReference' not in info:
        raise ValueError(f'{path} is not on an APFS volume')
    apfs_list = plistlib.loads(subprocess.run(['diskutil', 'apfs', 'list', '-plist'], check=True, capture_output=True).stdout)
    ref = info['APFSContainerReference']
    devs = [v['DeviceIdentifier'] for c in apfs_list['Containers'] if c['ContainerReference'] == ref for v in c['Volumes']]
    snapshots = {}
    for dev in devs:
        r = subprocess.run(['diskutil', 'apfs', 'listSnapshots', '-plist', dev], capture_output=True)
        if r.returncode == 0:  # a locked or unmountable volume has no listing; not an error
            snapshots[dev] = plistlib.loads(r.stdout)
    mounts = device_mounts(subprocess.run(['mount'], check=True, capture_output=True, text=True).stdout)
    return build_container(info, apfs_list, snapshots, mounts)
