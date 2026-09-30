"""`apfs.build_container`: diskutil plists + `mount` output → the container a
path lives on, volumes biggest-first with mount points and snapshots."""

from disk_tree.apfs import Container, Snapshot, Volume, build_container, device_mounts

G = 2 ** 30

INFO = {'APFSContainerReference': 'disk3', 'DeviceIdentifier': 'disk3s1s1'}
APFS_LIST = {'Containers': [
    {'ContainerReference': 'disk1', 'CapacityCeiling': G, 'CapacityFree': G, 'Volumes': []},
    {'ContainerReference': 'disk3', 'CapacityCeiling': 460 * G, 'CapacityFree': 23 * G, 'Volumes': [
        {'DeviceIdentifier': 'disk3s1', 'Name': 'Macintosh HD', 'Roles': ['System'], 'CapacityInUse': 13 * G},
        {'DeviceIdentifier': 'disk3s2', 'Name': 'Preboot', 'Roles': ['Preboot'], 'CapacityInUse': 20 * G},
        {'DeviceIdentifier': 'disk3s5', 'Name': 'Data', 'Roles': ['Data'], 'CapacityInUse': 387 * G},
        {'DeviceIdentifier': 'disk3s6', 'Name': 'VM', 'Roles': ['VM'], 'CapacityInUse': 12 * G},
        {'DeviceIdentifier': 'disk3s7', 'Name': 'Spare', 'Roles': [], 'CapacityInUse': 0},
    ]},
]}
SNAPSHOTS = {
    'disk3s1': {'Snapshots': [
        {'SnapshotName': 'com.apple.os.update-A', 'SnapshotXID': 1, 'Purgeable': False, 'LimitingContainerShrink': True},
        {'SnapshotName': 'com.apple.os.update-MSUPrepareUpdate', 'SnapshotXID': 2, 'Purgeable': False, 'LimitingContainerShrink': False},
    ]},
    'disk3s5': {'Snapshots': []},
}
MOUNT = """\
/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)
devfs on /dev (devfs, local, nobrowse)
/dev/disk3s6 on /System/Volumes/VM (apfs, local, noexec, journaled, noatime, nobrowse)
/dev/disk3s2 on /System/Volumes/Preboot (apfs, local, journaled, nobrowse)
/dev/disk3s5 on /System/Volumes/Data (apfs, local, journaled, nobrowse, protect, root data)
/dev/disk3s1 on /System/Volumes/Update/mnt1 (apfs, sealed, local, journaled, nobrowse)
map auto_home on /System/Volumes/Data/home (autofs, automounted, nobrowse)
"""


def test_device_mounts():
    assert device_mounts(MOUNT) == {
        'disk3s1s1': '/',
        'disk3s6': '/System/Volumes/VM',
        'disk3s2': '/System/Volumes/Preboot',
        'disk3s5': '/System/Volumes/Data',
        'disk3s1': '/System/Volumes/Update/mnt1',
    }


def test_build_container():
    c = build_container(INFO, APFS_LIST, SNAPSHOTS, device_mounts(MOUNT))
    assert c == Container('disk3', 460 * G, 23 * G, [
        Volume('disk3s5', 'Data', ['Data'], 387 * G, '/System/Volumes/Data'),
        Volume('disk3s2', 'Preboot', ['Preboot'], 20 * G, '/System/Volumes/Preboot'),
        Volume('disk3s1', 'Macintosh HD', ['System'], 13 * G, '/', [
            Snapshot('com.apple.os.update-A', 1, False, True),
            Snapshot('com.apple.os.update-MSUPrepareUpdate', 2, False, False),
        ]),
        Volume('disk3s6', 'VM', ['VM'], 12 * G, '/System/Volumes/VM'),
        Volume('disk3s7', 'Spare', [], 0, None),
    ])
    assert c.used == 437 * G
    assert c.to_json()['used'] == 437 * G
