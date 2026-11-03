"""A parquet file's page index locations, which pyarrow writes (`write_page_index=True`) but doesn't expose: each column
chunk's ColumnIndex / OffsetIndex byte ranges from the footer, and an OffsetIndex's page locations. A minimal reader of
thrift's compact protocol, the encoding of parquet's footer and indexes (parquet-format `parquet.thrift`)."""
from __future__ import annotations

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
