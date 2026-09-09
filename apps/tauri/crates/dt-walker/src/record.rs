//! A single walk record and its `gfind`-compatible serialization.
//!
//! `disk-tree`'s Python parser (`src/disk_tree/backends/gfind.py`) reads
//! `gfind -printf '%y %b %T@ %p\0'`: null-terminated records, fields separated
//! by single spaces, `%p` (the path) last so `split(' ', 3)` keeps spaces in
//! paths. We reproduce that byte stream exactly. Paths are raw bytes (macOS
//! paths are not guaranteed UTF-8); the Python side decodes with
//! `errors='replace'`, so we must not lossily re-encode here.

use std::io::{self, Write};

/// `%y` type character, matching GNU find's single-letter object types.
/// `f`=regular, `d`=dir, `l`=symlink, `b`/`c`=block/char device, `s`=socket,
/// `p`=fifo, `?`=unknown. The Python parser maps `f`→file, `d`→dir, and passes
/// any other letter through unchanged (so `l` stays `l`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Record<'a> {
    pub kind: u8,
    /// `%b`: 512-byte blocks allocated to the path.
    pub blocks: u64,
    /// `%T@`: mtime as integer epoch seconds. gfind prints `<sec>.<frac>`, but
    /// the parser only does `int(float(...))`, so integer seconds are identical.
    pub mtime: i64,
    /// `%p`: absolute path, raw bytes.
    pub path: &'a [u8],
}

impl<'a> Record<'a> {
    /// Write `<y> <b> <T@> <path>\0` to `out`.
    pub fn write_to<W: Write>(&self, out: &mut W) -> io::Result<()> {
        out.write_all(&[self.kind, b' '])?;
        let mut num = itoa_buf();
        out.write_all(fmt_u64(self.blocks, &mut num))?;
        out.write_all(b" ")?;
        out.write_all(fmt_i64(self.mtime, &mut num))?;
        out.write_all(b" ")?;
        out.write_all(self.path)?;
        out.write_all(&[0u8])
    }
}

/// A stack buffer big enough for any u64/i64 decimal (20 digits + sign).
#[inline]
fn itoa_buf() -> [u8; 24] {
    [0u8; 24]
}

/// Format `v` into `buf`, returning the written slice (no allocation).
#[inline]
fn fmt_u64(mut v: u64, buf: &mut [u8; 24]) -> &[u8] {
    if v == 0 {
        buf[0] = b'0';
        return &buf[..1];
    }
    let mut i = buf.len();
    while v > 0 {
        i -= 1;
        buf[i] = b'0' + (v % 10) as u8;
        v /= 10;
    }
    &buf[i..]
}

#[inline]
fn fmt_i64(v: i64, buf: &mut [u8; 24]) -> &[u8] {
    if v < 0 {
        // Two-step: format magnitude, then prepend '-'. Rare for mtimes.
        let mag = fmt_u64(v.unsigned_abs(), buf).len();
        let start = buf.len() - mag - 1;
        buf[start] = b'-';
        return &buf[start..];
    }
    fmt_u64(v as u64, buf)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ser(r: &Record) -> Vec<u8> {
        let mut v = Vec::new();
        r.write_to(&mut v).unwrap();
        v
    }

    #[test]
    fn file_record() {
        let r = Record { kind: b'f', blocks: 24, mtime: 1_700_000_000, path: b"/a/b c.txt" };
        assert_eq!(ser(&r), b"f 24 1700000000 /a/b c.txt\0".to_vec());
    }

    #[test]
    fn dir_zero_blocks() {
        let r = Record { kind: b'd', blocks: 0, mtime: 0, path: b"/" };
        assert_eq!(ser(&r), b"d 0 0 /\0".to_vec());
    }

    #[test]
    fn symlink_letter_passthrough() {
        let r = Record { kind: b'l', blocks: 0, mtime: 42, path: b"/link" };
        assert_eq!(ser(&r), b"l 0 42 /link\0".to_vec());
    }

    #[test]
    fn raw_non_utf8_path_preserved() {
        // A byte the UTF-8 decoder would replace must survive verbatim on the wire.
        let r = Record { kind: b'f', blocks: 8, mtime: 1, path: b"/x/\xff/y" };
        assert_eq!(ser(&r), b"f 8 1 /x/\xff/y\0".to_vec());
    }
}
