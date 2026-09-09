//! `getattrlistbulk(2)` FFI and per-entry buffer parsing.
//!
//! One syscall returns a whole directory's entries *with* their attributes,
//! amortizing the per-file path resolution that caps `gfind` (fts + lstat) and
//! `getattrlist`. See `specs/reflink-aware-sizing.md` ("Enabling perf work").
//!
//! ## Buffer layout
//!
//! We request, in canonical order:
//!   common: RETURNED_ATTRS, NAME, OBJTYPE, MODTIME
//!   file:   ALLOCSIZE
//!
//! and pass `FSOPT_PACK_INVAL_ATTRS`. The *common* group is present for every
//! entry, at these constant offsets:
//!
//! ```text
//!   offset  size  field
//!   0       4     u32       entry length (advance to next entry by this)
//!   4       20    attrset   ATTR_CMN_RETURNED_ATTRS (5 x u32)
//!   24      8     attrref   ATTR_CMN_NAME (i32 dataoffset, u32 length incl NUL)
//!   32      4     u32       ATTR_CMN_OBJTYPE (fsobj_type_t)
//!   36      16    timespec  ATTR_CMN_MODTIME (i64 sec, i64 nsec)
//!   52      8     i64       ATTR_FILE_ALLOCSIZE (bytes) — present ONLY for file
//!                           objects (see below)
//!   ...     ...   name bytes (referenced by the NAME attrref)
//! ```
//!
//! The **file** group (`ATTR_FILE_ALLOCSIZE`) is *not* packed for non-file
//! entries — measured: a directory entry omits it entirely (and
//! `FSOPT_PACK_INVAL_ATTRS` does not force it), so reading offset 52 blindly
//! yields garbage for dirs. So we consult the per-entry `returned` attribute_set
//! (its `fileattr` word, at offset 16) and only read allocsize when
//! `ATTR_FILE_ALLOCSIZE` is actually set; dirs/symlinks get 0 blocks, matching
//! `gfind %b` on APFS. We advance to the next entry by the leading `length`
//! field, so a shorter (allocsize-less) entry is handled correctly.
//!
//! getattrlist packs values contiguously with no inter-field alignment padding,
//! so the 8-byte fields land on 4-byte offsets — every read here goes through
//! `read_unaligned` to stay sound.

use std::os::raw::{c_int, c_void};

// --- attrlist constants (from <sys/attr.h>) ---
pub const ATTR_BIT_MAP_COUNT: u16 = 5;

pub const ATTR_CMN_RETURNED_ATTRS: u32 = 0x8000_0000;
pub const ATTR_CMN_NAME: u32 = 0x0000_0001;
pub const ATTR_CMN_OBJTYPE: u32 = 0x0000_0008;
pub const ATTR_CMN_MODTIME: u32 = 0x0000_0400;

pub const ATTR_FILE_ALLOCSIZE: u32 = 0x0000_0004;

/// Pack every requested attribute for every entry (zero-fill inapplicable ones)
/// so the fixed portion has a constant layout.
pub const FSOPT_PACK_INVAL_ATTRS: u64 = 0x0000_0008;

// fsobj_type_t values (from <sys/vnode.h>)
const VREG: u32 = 1;
const VDIR: u32 = 2;
const VBLK: u32 = 3;
const VCHR: u32 = 4;
const VLNK: u32 = 5;
const VSOCK: u32 = 6;
const VFIFO: u32 = 7;

#[repr(C)]
pub struct Attrlist {
    pub bitmapcount: u16,
    pub reserved: u16,
    pub commonattr: u32,
    pub volattr: u32,
    pub dirattr: u32,
    pub fileattr: u32,
    pub forkattr: u32,
}

impl Attrlist {
    /// The request we issue for every directory.
    pub fn request() -> Self {
        Attrlist {
            bitmapcount: ATTR_BIT_MAP_COUNT,
            reserved: 0,
            commonattr: ATTR_CMN_RETURNED_ATTRS
                | ATTR_CMN_NAME
                | ATTR_CMN_OBJTYPE
                | ATTR_CMN_MODTIME,
            volattr: 0,
            dirattr: 0,
            fileattr: ATTR_FILE_ALLOCSIZE,
            forkattr: 0,
        }
    }
}

extern "C" {
    /// `int getattrlistbulk(int dirfd, struct attrlist *alist, void *attrBuf,
    ///                      size_t attrBufSize, uint64_t options);`
    /// Returns entry count, 0 at end of directory, -1 on error (errno set).
    pub fn getattrlistbulk(
        dirfd: c_int,
        alist: *mut Attrlist,
        attr_buf: *mut c_void,
        attr_buf_size: libc::size_t,
        options: u64,
    ) -> c_int;
}

/// One parsed directory entry (borrows its name from the syscall buffer).
pub struct Entry<'a> {
    /// GNU-find `%y` letter.
    pub kind: u8,
    /// 512-byte blocks allocated (ALLOCSIZE / 512).
    pub blocks: u64,
    pub mtime_sec: i64,
    /// Entry name, raw bytes (no trailing NUL).
    pub name: &'a [u8],
    pub is_dir: bool,
}

// Offsets of the constant common-group fields within an entry.
const OFF_RETURNED_FILEATTR: usize = 16; // returned.fileattr word
const OFF_NAME_REF: usize = 24;
const OFF_OBJTYPE: usize = 32;
const OFF_MODTIME_SEC: usize = 36;
const OFF_ALLOCSIZE: usize = 52; // valid only when returned.fileattr has ALLOCSIZE

/// Parse up to `count` entries starting at `buf`, calling `f` for each.
///
/// # Safety
/// `buf` must hold `count` valid getattrlistbulk entries produced by the
/// request in [`Attrlist::request`] with `FSOPT_PACK_INVAL_ATTRS`.
pub unsafe fn parse_entries(buf: &[u8], count: usize, mut f: impl FnMut(Entry)) {
    let base = buf.as_ptr();
    let mut off = 0usize;
    for _ in 0..count {
        let entry = base.add(off);
        let length = read_u32(entry) as usize;
        let returned_fileattr = read_u32(entry.add(OFF_RETURNED_FILEATTR));

        let name_ref = entry.add(OFF_NAME_REF);
        let name_data_off = read_i32(name_ref) as isize;
        let name_len = read_u32(name_ref.add(4)) as usize;
        let objtype = read_u32(entry.add(OFF_OBJTYPE));
        let mtime_sec = read_i64(entry.add(OFF_MODTIME_SEC));

        // ALLOCSIZE is packed only for file objects; for dirs/symlinks the file
        // group is absent and offset 52 holds unrelated bytes. `%b` for those is
        // 0 on APFS anyway (empty dir inode / inline symlink target).
        let blocks = if returned_fileattr & ATTR_FILE_ALLOCSIZE != 0 {
            (read_i64(entry.add(OFF_ALLOCSIZE)).max(0) as u64) / 512
        } else {
            0
        };

        // Name bytes: offset is relative to the start of the attrref struct.
        let name_ptr = name_ref.offset(name_data_off);
        // name_len includes the trailing NUL; drop it.
        let name = std::slice::from_raw_parts(name_ptr, name_len.saturating_sub(1));

        let kind = objtype_to_kind(objtype);
        let is_dir = objtype == VDIR;
        f(Entry { kind, blocks, mtime_sec, name, is_dir });

        off += length;
    }
}

fn objtype_to_kind(t: u32) -> u8 {
    match t {
        VREG => b'f',
        VDIR => b'd',
        VLNK => b'l',
        VBLK => b'b',
        VCHR => b'c',
        VSOCK => b's',
        VFIFO => b'p',
        _ => b'?',
    }
}

#[inline]
unsafe fn read_u32(p: *const u8) -> u32 {
    (p as *const u32).read_unaligned()
}
#[inline]
unsafe fn read_i32(p: *const u8) -> i32 {
    (p as *const i32).read_unaligned()
}
#[inline]
unsafe fn read_i64(p: *const u8) -> i64 {
    (p as *const i64).read_unaligned()
}
