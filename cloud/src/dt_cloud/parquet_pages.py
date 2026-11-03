"""A parquet file's page index locations, which pyarrow writes (`write_page_index=True`) but doesn't expose: each column
chunk's ColumnIndex / OffsetIndex byte ranges from the footer, and an OffsetIndex's page locations. A minimal reader of
thrift's compact protocol, the encoding of parquet's footer and indexes (parquet-format `parquet.thrift`)."""
from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import NamedTuple

# Compact-protocol type ids.
_STOP, _TRUE, _FALSE, _BYTE, _I16, _I32, _I64, _DOUBLE, _BINARY, _LIST, _SET, _MAP, _STRUCT = 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12


class _Reader:
    def __init__(self, buf: bytes):
        self.buf, self.pos = buf, 0

    def byte(self) -> int:
        b = self.buf[self.pos]
        self.pos += 1
        return b

    def varint(self) -> int:
        out = shift = 0
        while True:
            b = self.byte()
            out |= (b & 0x7F) << shift
            if not b & 0x80:
                return out
            shift += 7

    def zigzag(self) -> int:
        n = self.varint()
        return (n >> 1) ^ -(n & 1)

    def value(self, t: int):
        if t in (_TRUE, _FALSE):
            return t == _TRUE
        if t == _BYTE:
            return self.byte()
        if t in (_I16, _I32, _I64):
            return self.zigzag()
        if t == _DOUBLE:
            v = struct.unpack_from("<d", self.buf, self.pos)[0]
            self.pos += 8
            return v
        if t == _BINARY:
            n = self.varint()
            v = self.buf[self.pos:self.pos + n]
            self.pos += n
            return v
        if t in (_LIST, _SET):
            h = self.byte()
            n, et = h >> 4, h & 0x0F
            if n == 15:
                n = self.varint()
            # A bool element is one byte (1 true, else false), not a type id.
            return [self.byte() == 1 for _ in range(n)] if et in (_TRUE, _FALSE) else [self.value(et) for _ in range(n)]
        if t == _MAP:
            n = self.varint()
            if not n:
                return {}
            kv = self.byte()
            return {self.value(kv >> 4): self.value(kv & 0x0F) for _ in range(n)}
        if t == _STRUCT:
            return self.struct()
        raise ValueError(f"thrift compact: unknown type {t} at byte {self.pos}")

    def struct(self) -> dict[int, object]:
        out: dict[int, object] = {}
        fid = 0
        while True:
            h = self.byte()
            t = h & 0x0F
            if t == _STOP:
                return out
            delta = h >> 4
            fid = fid + delta if delta else self.zigzag()
            out[fid] = self.value(t)


class ChunkIndex(NamedTuple):
    """One column chunk's page index byte ranges (`*_offset`, `*_length`; 0 / 0 when it has none)."""
    column_index_offset: int
    column_index_length: int
    offset_index_offset: int
    offset_index_length: int


class PageLocation(NamedTuple):
    offset: int
    compressed_page_size: int
    first_row_index: int


def footer(path: Path) -> bytes:
    """The thrift-encoded FileMetaData at the end of `path`."""
    with open(path, "rb") as f:
        f.seek(-8, 2)
        n, magic = struct.unpack("<i4s", f.read(8))
        if magic != b"PAR1":
            raise ValueError(f"{path}: not a parquet file")
        f.seek(-8 - n, 2)
        return f.read(n)


def chunk_indexes(path: Path) -> list[list[ChunkIndex]]:
    """Per row group, per column chunk (schema order): its ColumnIndex and OffsetIndex byte ranges."""
    md = _Reader(footer(path)).struct()
    return [[ChunkIndex(c.get(6, 0), c.get(7, 0), c.get(4, 0), c.get(5, 0)) for c in rg[1]] for rg in md.get(4, [])]


def page_locations(path: Path, ix: ChunkIndex) -> list[PageLocation]:
    """A column chunk's pages, from its OffsetIndex."""
    with open(path, "rb") as f:
        f.seek(ix.offset_index_offset)
        oi = _Reader(f.read(ix.offset_index_length)).struct()
    return [PageLocation(p[1], p[2], p[3]) for p in oi[1]]


# ── Typed thrift: decoded with each value's wire type, so an edited footer re-encodes as its writer would ──

class _TypedReader(_Reader):
    """Compact thrift as typed trees: a struct is a list of `[fid, type, value]` (a bool field's value a `bool`), a list
    `(elem type, [values])` (a bool element its raw byte). `encode` writes an unedited tree back byte for byte."""

    def tvalue(self, t: int):
        if t in (_LIST, _SET):
            h = self.byte()
            n, et = h >> 4, h & 0x0F
            if n == 15:
                n = self.varint()
            return et, [self.byte() if et in (_TRUE, _FALSE) else self.tvalue(et) for _ in range(n)]
        if t == _MAP:
            raise ValueError("thrift compact: no maps in parquet's metadata")
        if t == _STRUCT:
            return self.tstruct()
        return self.value(t)

    def tstruct(self) -> list[list]:
        out: list[list] = []
        fid = 0
        while True:
            h = self.byte()
            t = h & 0x0F
            if t == _STOP:
                return out
            delta = h >> 4
            fid = fid + delta if delta else self.zigzag()
            out.append([fid, t, self.tvalue(t)])


class _RawList(NamedTuple):
    """A list field's value given as its `n` elements of type `et` already encoded, back to back."""
    et: int
    n: int
    data: bytes


class _Writer:
    def __init__(self):
        self.buf = bytearray()

    def list_header(self, et: int, n: int) -> None:
        if n < 15:
            self.buf.append(n << 4 | et)
        else:
            self.buf.append(0xF0 | et)
            self.varint(n)

    def varint(self, n: int) -> None:
        while n > 0x7F:
            self.buf.append((n & 0x7F) | 0x80)
            n >>= 7
        self.buf.append(n)

    def zigzag(self, n: int) -> None:
        self.varint((n << 1) ^ (n >> 63))

    def value(self, t: int, v) -> None:
        if t == _BYTE:
            self.buf.append(v & 0xFF)
        elif t in (_I16, _I32, _I64):
            self.zigzag(v)
        elif t == _DOUBLE:
            self.buf += struct.pack("<d", v)
        elif t == _BINARY:
            self.varint(len(v))
            self.buf += v
        elif t in (_LIST, _SET) and isinstance(v, _RawList):
            self.list_header(v.et, v.n)
            self.buf += v.data
        elif t in (_LIST, _SET):
            et, xs = v
            self.list_header(et, len(xs))
            for x in xs:
                if et in (_TRUE, _FALSE):
                    self.buf.append(x)
                else:
                    self.value(et, x)
        elif t == _STRUCT:
            self.struct(v)
        else:
            raise ValueError(f"thrift compact: can't write type {t}")

    def struct(self, fields: list[list]) -> None:
        last = 0
        for fid, t, v in fields:
            if t in (_TRUE, _FALSE):
                t = _TRUE if v else _FALSE
            d = fid - last
            if 0 < d <= 15:
                self.buf.append(d << 4 | t)
            else:
                self.buf.append(t)
                self.zigzag(fid)
            if t not in (_TRUE, _FALSE):
                self.value(t, v)
            last = fid
        self.buf.append(_STOP)


def decode(buf: bytes) -> list[list]:
    """A thrift struct as a typed tree (`_TypedReader`)."""
    return _TypedReader(buf).tstruct()


def encode(fields: list[list]) -> bytes:
    """`decode`'s inverse: an unedited tree's bytes are the ones decoded."""
    w = _Writer()
    w.struct(fields)
    return bytes(w.buf)


def _get(fields: list[list], fid: int):
    return next((f[2] for f in fields if f[0] == fid), None)


def _shift(fields: list[list], fid: int, by: int) -> None:
    """Offset field `fid` moved `by` bytes, when set: 0 is never a page's offset (the magic is there), so it means none
    (the writer's deprecated chunk `file_offset`)."""
    for f in fields:
        if f[0] == fid and f[2]:
            f[2] += by


def _put(fields: list[list], fid: int, t: int, v) -> None:
    """Field `fid` set to `v`, kept in ascending field order (as thrift writes a struct)."""
    for f in fields:
        if f[0] == fid:
            f[1:] = [t, v]
            return
    fields.append([fid, t, v])
    fields.sort(key=lambda f: f[0])


# ── Concatenation: parts written with one writer's settings, joined as that writer would have written them ──

#: FileMetaData's `num_rows` and `row_groups`: the fields `concat`'s parts differ in.
_ROWS, _RGS = 3, 4


def _row_groups(md: list[list]) -> list[list[list]]:
    rgs = _get(md, _RGS)
    return rgs[1] if rgs else []


def _chunks(md: list[list]) -> list[list[list]]:
    return [cc for rg in _row_groups(md) for cc in _get(rg, 1)[1]]


def _copy_range(src: int, dst: int, n: int, at: int, to: int, chunk: int = 1 << 24) -> None:
    """`n` bytes of file descriptor `src` from `at` to `dst` at `to` (in the kernel where it can)."""
    while n:
        if hasattr(os, "copy_file_range"):
            k = os.copy_file_range(src, dst, min(chunk, n), at, to)
        else:
            k = os.pwrite(dst, os.pread(src, min(chunk, n), at), to)
        if not k:
            raise ValueError(f"short copy: {n} bytes left at {at}")
        n, at, to = n - k, at + k, to + k


def _span(ranges: list[tuple[int, int]], what: str, path: str) -> tuple[int, int]:
    """The byte range `ranges` (`(offset, length)`, in chunk order) cover, refused unless back to back."""
    for (o, n), (o2, _) in zip(ranges, ranges[1:]):
        if o + n != o2:
            raise ValueError(f"{path}: its {what}es aren't back to back in chunk order")
    return (ranges[0][0], ranges[-1][0] + ranges[-1][1]) if ranges else (0, 0)


class PartLayout(NamedTuple):
    """A `concat` part's layout: its row groups' bytes `[4, data_end)`, its ColumnIndexes' `ci` and OffsetIndexes' `oi`
    byte ranges (each back to back, in chunk order), its footer's fields but `num_rows` / `row_groups` (`meta`), and its
    counts."""
    data_end: int
    ci: tuple[int, int]
    oi: tuple[int, int]
    meta: list
    rows: int
    groups: int


def part_layout(path: str) -> PartLayout:
    """`concat`'s first phase: a part's layout, refused unless its bytes are the magic, its row groups, then its
    ColumnIndexes, OffsetIndexes and footer, nothing else (no bloom filters)."""
    tail = footer(Path(path))
    md = decode(tail)
    ccs = _chunks(md)
    ci = _span([(_get(cc, 6), _get(cc, 7)) for cc in ccs if _get(cc, 6) is not None], "ColumnIndex", path)
    oi = _span([(_get(cc, 4), _get(cc, 5)) for cc in ccs if _get(cc, 4) is not None], "OffsetIndex", path)
    end = Path(path).stat().st_size - 8 - len(tail)
    if oi == (0, 0):
        data_end = end if ci == (0, 0) else -1
    else:
        data_end = ci[0] if ci != (0, 0) else oi[0]
        if oi[1] != end or (ci != (0, 0) and ci[1] != oi[0]):
            data_end = -1
    if data_end < 4:
        raise ValueError(f"{path}: not its row groups, then ColumnIndexes, then OffsetIndexes, then its footer")
    return PartLayout(data_end, ci, oi, [f for f in md if f[0] not in (_ROWS, _RGS)], _get(md, _ROWS), len(_row_groups(md)))


def part_page_index(path: str, out: str, lay: PartLayout, by: int, ci_to: int, oi_tmp: str) -> list[int]:
    """`concat`'s second phase, for one part: its row groups and ColumnIndexes copied into `out` (at `4 + by` and
    `ci_to`: a ColumnIndex holds no offsets), its OffsetIndexes, their page offsets moved `by` bytes, re-encoded to
    `oi_tmp`. Returns their lengths."""
    src, dst = os.open(path, os.O_RDONLY), os.open(out, os.O_WRONLY)
    try:
        _copy_range(src, dst, lay.data_end - 4, 4, 4 + by)
        _copy_range(src, dst, lay.ci[1] - lay.ci[0], lay.ci[0], ci_to)
        buf = os.pread(src, lay.oi[1] - lay.oi[0], lay.oi[0])
    finally:
        os.close(src)
        os.close(dst)
    lens: list[int] = []
    w = _Writer()
    r = _TypedReader(buf)
    while r.pos < len(buf):
        o = r.tstruct()
        for loc in _get(o, 1)[1]:
            _shift(loc, 1, by)
        n = len(w.buf)
        w.struct(o)
        lens.append(len(w.buf) - n)
    with open(oi_tmp, "wb") as f:
        f.write(w.buf)
    return lens


def part_row_groups(path: str, out: str, by: int, ci_by: int, oi_tmp: str, oi_to: int, oi_lens: list[int], ordinal: int) -> bytes:
    """`concat`'s third phase, for one part: its OffsetIndexes (`oi_tmp`) written into `out` at `oi_to`, and its row
    groups encoded back to back, every offset moved — the row group's and each chunk's `file_offset` and page offsets by
    `by`, its ColumnIndex by `ci_by`, its OffsetIndex to where it now is — and their `ordinal`s (when written) from
    `ordinal`."""
    md = decode(footer(Path(path)))
    with open(oi_tmp, "rb") as f:
        buf = f.read()
    fd = os.open(out, os.O_WRONLY)
    try:
        os.pwrite(fd, buf, oi_to)
    finally:
        os.close(fd)
    w = _Writer()
    lens = iter(oi_lens)
    for i, rg in enumerate(_row_groups(md)):
        _shift(rg, 5, by)
        if _get(rg, 7) is not None:
            _put(rg, 7, _I16, ordinal + i)
        for cc in _get(rg, 1)[1]:
            _shift(cc, 2, by)
            m = _get(cc, 3)
            for fid in (9, 10, 11):
                _shift(m, fid, by)
            _shift(cc, 6, ci_by)
            if _get(cc, 4) is not None:
                n = next(lens)
                _put(cc, 4, _I64, oi_to)
                _put(cc, 5, _I32, n)
                oi_to += n
        w.struct(rg)
    return bytes(w.buf)


def _serial_map(fn, *its) -> list:
    return list(map(fn, *its))


def concat(parts: list[Path], out: Path, *, drop: bool = False, map_=_serial_map) -> dict:
    """One parquet file from `parts` (one writer's settings, schema and key-value metadata; only their row groups
    differ), laid out as one writer of all their row groups leaves it, so its bytes are that writer's: the magic, every
    part's row groups back to back, every chunk's ColumnIndex (row group, then column order), every OffsetIndex, the
    footer. The footer is the first part's with every part's row groups, each moved by its part's position: the row
    group's `file_offset` (and `ordinal`), each chunk's `file_offset` and page offsets (`data_page_offset`,
    `index_page_offset`, `dictionary_page_offset`), its page index's new locations, and its OffsetIndex's page
    locations; `num_rows` the sum. Three phases over the parts, each `map_(fn, *iterables)` (a process pool's `map` runs
    them in parallel; the default runs them in turn): `part_layout`, `part_page_index`, `part_row_groups`; then the
    footer. A part's footer is only decoded within its own phase. `drop`: the parts deleted once joined. Returns
    `{rows, groups, bytes}`."""
    names = [str(p) for p in parts]
    lays = list(map_(part_layout, names))
    if any(x.meta != lays[0].meta for x in lays):
        raise ValueError(f"{out}: the parts' schemas or metadata differ")
    bys, ci_tos, at = [], [], 4
    for x in lays:
        bys.append(at - 4)
        at += x.data_end - 4
    for x in lays:
        ci_tos.append(at)
        at += x.ci[1] - x.ci[0]
    with open(out, "wb") as f:
        f.write(b"PAR1")
    k = len(parts)
    oi_tmps = [f"{out}.oi{i}" for i in range(k)]
    try:
        oi_lens = list(map_(part_page_index, names, [str(out)] * k, lays, bys, ci_tos, oi_tmps))
        oi_tos = []
        for lens in oi_lens:
            oi_tos.append(at)
            at += sum(lens)
        ordinals = [sum(x.groups for x in lays[:i]) for i in range(k)]
        rgs = list(map_(part_row_groups, names, [str(out)] * k, bys, [c - x.ci[0] for c, x in zip(ci_tos, lays)], oi_tmps,
                        oi_tos, oi_lens, ordinals))
    finally:
        for t in oi_tmps:
            Path(t).unlink(missing_ok=True)
    rows, groups = sum(x.rows for x in lays), sum(x.groups for x in lays)
    md = [list(f) for f in lays[0].meta]
    _put(md, _ROWS, _I64, rows)
    _put(md, _RGS, _LIST, _RawList(_STRUCT, groups, b"".join(rgs)))
    tail = encode(md)
    with open(out, "r+b") as f:
        f.seek(at)
        f.write(tail + struct.pack("<i", len(tail)) + b"PAR1")
        f.truncate()
        size = f.tell()
    if drop:
        for p in parts:
            p.unlink()
    return {"rows": rows, "groups": groups, "bytes": size}
