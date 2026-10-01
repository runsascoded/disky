"""Physical-extent mapping: which of a subtree's bytes are actually its own.

`gfind -printf '%b'` charges a file's allocated blocks to every path that links
them, so a subtree's *reported* size is an upper bound on what deleting it
frees. Hardlinks are at least visible to `stat` (`nlink > 1`); APFS **clones**
(reflinks) are not — distinct inodes, `nlink == 1`, full block count each — and
on this platform they are the dominant sharing mechanism: `uv` and `pnpm` both
populate project trees by cloning from their caches.

The only way to tell owned bytes from shared ones is to ask the filesystem
where a file physically lives. `fcntl(F_LOG2PHYS_EXT)` answers that per logical
range; two paths that map to the same device offsets share the same blocks.

Note the struct is *packed* — `u_int32_t` immediately followed by two `off_t`,
no alignment padding — which is why the format below is `=Iqq` (20 bytes) and
not the 24 bytes natural alignment would suggest.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import struct
import sys
from dataclasses import dataclass, field

F_LOG2PHYS_EXT = 65
_FMT = '=Iqq'  # l2p_flags, l2p_contigbytes, l2p_devoffset
_SZ = struct.calcsize(_FMT)

SUPPORTED = sys.platform == 'darwin'

#: (device offset, length) of one physically contiguous run.
Extent = tuple[int, int]


def file_extents(path: str, max_extents: int = 4096) -> list[Extent]:
    """Physical extents backing `path`, or [] if the file has none.

    An empty list is not an error: zero-length files have no extents, and so do
    decmpfs-compressed files, whose data lives in an xattr rather than in block
    storage.
    """
    if not SUPPORTED:
        raise NotImplementedError(f'physical extent mapping is Darwin-only (got {sys.platform})')
    fd = os.open(path, os.O_RDONLY)
    try:
        size = os.fstat(fd).st_size
        out: list[Extent] = []
        off = 0
        while off < size and len(out) < max_extents:
            res = fcntl.fcntl(fd, F_LOG2PHYS_EXT, struct.pack(_FMT, 0, size - off, off))
            _, length, phys = struct.unpack(_FMT, res[:_SZ])
            if length <= 0:
                break
            out.append((phys, length))
            off += length
        return out
    finally:
        os.close(fd)


@dataclass
class Subtree:
    """One candidate root's accounting."""
    path: str
    apparent: int = 0        # Σ st_blocks × 512 — what `du`/disk-tree report
    mapped: int = 0          # bytes we resolved to physical extents
    unmapped: int = 0        # compressed/sparse/unreadable — assumed owned
    n_files: int = 0
    n_errors: int = 0
    extents: set[Extent] = field(default_factory=set, repr=False)


@dataclass
class Reclaim:
    subtrees: list[Subtree]
    partners: list[str]
    shared: int              # candidate bytes whose extents also live in a partner
    unique: int              # candidate bytes nothing outside the set references
    partner_files: int
    shared_set: set[Extent] = field(default_factory=set, repr=False)

    @property
    def apparent(self) -> int:
        return sum(s.apparent for s in self.subtrees)


def _walk_files(root: str):
    """Regular files under `root` (no symlink following), depth-first."""
    if os.path.isfile(root):
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            p = os.path.join(dirpath, name)
            if not os.path.islink(p):
                yield p


def scan_subtree(root: str, seen_inodes: set[int]) -> Subtree:
    """Map every file under `root`, charging hardlinked inodes only once."""
    sub = Subtree(path=root)
    for p in _walk_files(root):
        try:
            st = os.lstat(p)
        except OSError:
            sub.n_errors += 1
            continue
        if st.st_ino in seen_inodes:
            continue           # a hardlink we already charged
        seen_inodes.add(st.st_ino)
        sub.n_files += 1
        sub.apparent += st.st_blocks * 512
        try:
            ex = file_extents(p)
        except OSError:
            sub.n_errors += 1
            sub.unmapped += st.st_blocks * 512
            continue
        if not ex:
            sub.unmapped += st.st_blocks * 512
            continue
        for phys, length in ex:
            sub.extents.add((phys, length))
            sub.mapped += length
    return sub


def partner_extents(roots: list[str], want: set[Extent]) -> tuple[set[Extent], int]:
    """Extents under `roots` that also appear in `want` (the candidate set).

    Only intersecting extents are retained — the caches are far larger than any
    candidate set, and holding all of their extents would cost more memory than
    the answer is worth.
    """
    hits: set[Extent] = set()
    n = 0
    for root in roots:
        for p in _walk_files(root):
            n += 1
            try:
                ex = file_extents(p)
            except OSError:
                continue
            for e in ex:
                if e in want:
                    hits.add(e)
    return hits, n


def default_partners() -> list[str]:
    """Caches known to populate project trees by cloning."""
    home = os.path.expanduser('~')
    cache = os.environ.get('XDG_CACHE_HOME') or os.path.join(home, '.cache')
    candidates = [
        os.path.join(cache, 'uv'),
        os.path.join(home, 'Library', 'pnpm', 'store'),
        os.path.join(home, 'Library', 'Caches', 'pnpm'),
    ]
    return [p for p in candidates if os.path.isdir(p)]


def measure(roots: list[str], partners: list[str] | None = None) -> Reclaim:
    """How much deleting `roots` would actually free, given `partners` remain."""
    partners = default_partners() if partners is None else partners
    seen: set[int] = set()
    subtrees = [scan_subtree(r, seen) for r in roots]
    want: set[Extent] = set()
    for s in subtrees:
        want |= s.extents
    hits, n_partner = partner_extents(partners, want)
    shared = sum(length for _, length in hits)
    mapped = sum(length for _, length in want)
    unmapped = sum(s.unmapped for s in subtrees)
    return Reclaim(
        subtrees=subtrees,
        partners=partners,
        shared=shared,
        shared_set=hits,
        unique=mapped - shared + unmapped,
        partner_files=n_partner,
    )


# --- Exclusive size via getattrlist(ATTR_CMNEXT_PRIVATESIZE) -----------------
#
# `F_LOG2PHYS_EXT` above answers "which physical blocks back this file" and needs
# an open() per file. For the common question — "how many of a file's bytes are
# NOT shared with any other file on the volume" (i.e. what deleting it frees) —
# APFS exposes that directly, without opening the file, via getattrlist's
# extended-common `ATTR_CMNEXT_PRIVATESIZE`. Paired with `ATTR_FILE_ALLOCSIZE`
# (the per-path allocation `du`/gfind report) it yields apparent-vs-exclusive in
# one cheap call, ~32K files/s single-threaded on APFS (measured 2026-08-29).


_ATTR_BIT_MAP_COUNT = 5
_ATTR_FILE_ALLOCSIZE = 0x00000004
_ATTR_CMNEXT_PRIVATESIZE = 0x00000008   # NB: 0x8, not the 0x4000 some old refs cite
_FSOPT_ATTR_CMN_EXTENDED = 0x00000020
_ATTR_FMT = '=Iqq'                        # u_int32 length, off_t alloc, off_t priv (packed)
_ATTR_BUFLEN = 32


class _attrlist(ctypes.Structure):
    _fields_ = [
        ('bitmapcount', ctypes.c_ushort), ('reserved', ctypes.c_ushort),
        ('commonattr', ctypes.c_uint), ('volattr', ctypes.c_uint),
        ('dirattr', ctypes.c_uint), ('fileattr', ctypes.c_uint), ('forkattr', ctypes.c_uint),
    ]


_libc = None


def _getattrlist():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL('libc.dylib', use_errno=True)
        _libc.getattrlist.argtypes = [
            ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ulong,
        ]
        _libc.getattrlist.restype = ctypes.c_int
    return _libc.getattrlist


def alloc_and_private(path: str) -> tuple[int, int]:
    """(allocated, private) bytes for `path` — apparent size and the portion
    exclusive to this file (freed if it were deleted). Equal when nothing is
    shared; private is 0 for a file whose every block is cloned/shared.
    """
    if not SUPPORTED:
        raise NotImplementedError(f'ATTR_CMNEXT_PRIVATESIZE is Darwin-only (got {sys.platform})')
    al = _attrlist(_ATTR_BIT_MAP_COUNT, 0, 0, 0, 0, _ATTR_FILE_ALLOCSIZE, _ATTR_CMNEXT_PRIVATESIZE)
    buf = ctypes.create_string_buffer(_ATTR_BUFLEN)
    rc = _getattrlist()(os.fsencode(path), ctypes.byref(al), buf, _ATTR_BUFLEN, _FSOPT_ATTR_CMN_EXTENDED)
    if rc != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e), path)
    _, alloc, priv = struct.unpack(_ATTR_FMT, buf.raw[:struct.calcsize(_ATTR_FMT)])
    return alloc, priv


@dataclass
class Overcount:
    """Apparent-vs-physical accounting for a subtree."""
    path: str
    apparent: int = 0        # Σ allocated (what du / disk-tree report)
    exclusive: int = 0       # Σ private — bytes only this subtree's files hold
    n_files: int = 0
    n_errors: int = 0

    @property
    def shared(self) -> int:
        """Bytes shared with files elsewhere on the volume (the overcount)."""
        return self.apparent - self.exclusive


def measure_overcount(root: str, seen_inodes: set[int] | None = None) -> Overcount:
    """Walk `root`, summing apparent and exclusive (private) bytes.

    Hardlinked inodes are counted once. `shared` = apparent − exclusive is what
    disk-tree's per-path sizes overstate for this subtree, i.e. bytes it shares
    (via clone or hardlink) with files outside it *or* duplicated within it.
    """
    seen = set() if seen_inodes is None else seen_inodes
    oc = Overcount(path=root)
    for p in _walk_files(root):
        try:
            st = os.lstat(p)
        except OSError:
            oc.n_errors += 1
            continue
        if st.st_ino in seen:
            continue
        seen.add(st.st_ino)
        try:
            alloc, priv = alloc_and_private(p)
        except OSError:
            oc.n_errors += 1
            continue
        oc.n_files += 1
        oc.apparent += alloc
        oc.exclusive += priv
    return oc


# --- Exact per-subtree reclaim via extent → LCA attribution ------------------
#
# `alloc_and_private` answers "delete this file, keep everything else". The
# question users actually ask — "how much would `rm -rf D` free, for a directory
# D?" — is context-dependent: a block cloned only between two files *inside* D is
# freed by removing D, though it is not private to either file. Spec:
# specs/reflink-aware-sizing.md.
#
# Model: attribute each physical extent to the lowest common ancestor directory
# of every file that references it; then reclaim(D) = Σ extents whose LCA is at
# or below D. Because each extent's bytes live at its LCA, the value rolls up the
# tree exactly like `size` does.

from collections import defaultdict as _defaultdict


def _lca(a: tuple[str, ...], b: tuple[str, ...]) -> tuple[str, ...]:
    out = []
    for x, y in zip(a, b):
        if x != y:
            break
        out.append(x)
    return tuple(out)


def reclaimable_by_dir(root: str) -> tuple[dict[str, int], int]:
    """Map `root`'s files and return (reclaimable, n_errors).

    `reclaimable[relpath]` is the rolled-up bytes that `rm -rf <relpath>` frees:
    physical blocks referenced only by files at or below it. Paths are relative
    to `root` in disk-tree's scheme (root itself is '.'). Rolls up, so
    `reclaimable['.']` is the whole tree's physical footprint.

    Exactness depends on coverage: only referencers *under `root`* are seen, so a
    block cloned from outside (e.g. `~/.cache/uv` when `root` is a single
    project) looks root-owned and is counted — an over-count. The result is exact
    when `root` contains every sharing partner (a home/full scan including the
    caches); for a narrow subtree it is an upper bound.
    """
    if not SUPPORTED:
        raise NotImplementedError(f'extent mapping is Darwin-only (got {sys.platform})')

    # inode → (its extents, LCA of every dir that links it). Extents are per
    # inode (hardlinks share them); folding the inode's own paths first makes
    # hardlinks correct before clones fold across inodes below.
    inode_dirs: dict[int, tuple[str, ...]] = {}
    inode_first: dict[int, str] = {}
    inode_blocks: dict[int, int] = {}
    n_errors = 0
    for p in _walk_files(root):
        try:
            st = os.lstat(p)
        except OSError:
            n_errors += 1
            continue
        rel = os.path.relpath(os.path.dirname(p), root)
        parts = () if rel == '.' else tuple(rel.split(os.sep))
        ino = st.st_ino
        if ino in inode_dirs:
            inode_dirs[ino] = _lca(inode_dirs[ino], parts)
        else:
            inode_dirs[ino] = parts
            inode_first[ino] = p
            inode_blocks[ino] = st.st_blocks * 512

    extent_lca: dict[int, list] = {}   # phys → [lca_parts, length]
    local: dict[tuple[str, ...], int] = _defaultdict(int)
    for ino, parts in inode_dirs.items():
        try:
            ex = file_extents(inode_first[ino])
        except OSError:
            ex = []
            n_errors += 1
        if not ex:
            # No mappable extents (compressed/empty): bytes are wholly owned by
            # this inode, attributed to the LCA of its own links.
            local[parts] += inode_blocks[ino]
            continue
        for phys, length in ex:
            if phys in extent_lca:
                extent_lca[phys][0] = _lca(extent_lca[phys][0], parts)
            else:
                extent_lca[phys] = [parts, length]

    for parts, length in extent_lca.values():
        local[tuple(parts)] += length

    # Roll each dir's local total up through its ancestors (inclusive of root ()).
    recl: dict[tuple[str, ...], int] = _defaultdict(int)
    for parts, val in local.items():
        for i in range(len(parts) + 1):
            recl[parts[:i]] += val

    return ({(os.sep.join(p) if p else '.'): v for p, v in recl.items()}, n_errors)


# --- Reclaim sidecar: <blob-stem>.reclaim.parquet ---------------------------
#
# A second parquet next to a scan's blob, holding one row per directory:
#   path         str    directory, in the scan's relative scheme ('.' = root)
#   reclaimable  int64  bytes `rm -rf path` frees (extent-LCA rollup)
# Written only by `index -x` (needs the live filesystem). Stale if older than
# its blob, which a hybrid in-place delete rewrites.

RECLAIM_SUFFIX = '.reclaim.parquet'


def reclaim_sidecar_path(blob_path: str) -> str:
    from os.path import splitext
    stem, ext = splitext(blob_path)
    return (stem if ext == '.parquet' else blob_path) + RECLAIM_SUFFIX


def write_reclaim_sidecar(blob_path: str, reclaimable: dict[str, int]) -> str:
    import pandas as pd
    out = reclaim_sidecar_path(blob_path)
    df = pd.DataFrame(
        sorted(reclaimable.items()), columns=['path', 'reclaimable'],
    ).astype({'reclaimable': 'int64'})
    df.to_parquet(out, index=False)
    return out


def read_reclaim_sidecar(blob_path: str) -> dict[str, int] | None:
    """Fresh reclaim map for `blob_path`, or None if absent/stale."""
    import pandas as pd
    out = reclaim_sidecar_path(blob_path)
    if not os.path.exists(out) or os.path.getmtime(out) < os.path.getmtime(blob_path):
        return None
    df = pd.read_parquet(out)
    return dict(zip(df['path'], df['reclaimable'].astype(int)))
