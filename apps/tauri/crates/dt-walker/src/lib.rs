//! Native macOS directory walker built on `getattrlistbulk(2)`, emitting the
//! exact `gfind -printf '%y %b %T@ %p\0'` byte stream that disk-tree's Python
//! indexer parses. A drop-in replacement for the `gfind` subprocess that (a)
//! makes TCC attribution unambiguous when compiled into the signed app binary,
//! (b) removes the `findutils` dependency, and (c) is the throughput lever above
//! the APFS metadata-lock ceiling. See `specs/tauri-native-app.md`.

pub mod attrlist;
pub mod record;

use std::io::{self, Write};
use std::os::raw::c_void;

use attrlist::{getattrlistbulk, Attrlist, Request};
pub use record::Record;

/// Where walked records go: the gfind-format byte stream ([`Walker::walk`]) or
/// a callback ([`Walker::walk_records`], e.g. the in-process capture).
pub trait Sink {
    fn record(&mut self, r: &Record) -> io::Result<()>;
}

/// `%y %b %T@ %p\0` to a writer.
struct WriteSink<'w, W: Write>(&'w mut W);

impl<W: Write> Sink for WriteSink<'_, W> {
    fn record(&mut self, r: &Record) -> io::Result<()> {
        r.write_to(self.0)
    }
}

impl<F: FnMut(&Record) -> io::Result<()>> Sink for F {
    fn record(&mut self, r: &Record) -> io::Result<()> {
        self(r)
    }
}

/// Directories proxying to cloud services (macOS File Provider); walking them
/// blocks on network I/O, so we prune them — mirroring `local.py`'s
/// `CLOUDSTORAGE_PATHS` default excludes.
pub fn default_excludes() -> Vec<Vec<u8>> {
    let mut out = vec![b"/Library/CloudStorage".to_vec()];
    if let Some(home) = std::env::var_os("HOME") {
        use std::os::unix::ffi::OsStrExt;
        let mut p = home.as_bytes().to_vec();
        p.extend_from_slice(b"/Library/CloudStorage");
        out.push(p);
    }
    out
}

/// Running error tally, matching `ErrorCollector` on the Python side.
#[derive(Default)]
pub struct ErrorStats {
    pub count: u64,
    pub paths: Vec<Vec<u8>>,
    max_paths: usize,
}

impl ErrorStats {
    pub fn new() -> Self {
        ErrorStats { count: 0, paths: Vec::new(), max_paths: 100 }
    }
    fn add(&mut self, path: &[u8]) {
        self.count += 1;
        if self.paths.len() < self.max_paths {
            self.paths.push(path.to_vec());
        }
    }
}

pub struct Walker<'a> {
    excludes: &'a [Vec<u8>],
    buf: Vec<u8>,
    request: Request,
    /// Don't descend into mount points (`find -xdev`); set via [`Walker::one_fs`].
    one_fs: bool,
    pub records: u64,
    pub errors: ErrorStats,
    /// Σ allocated bytes over files (`%b` × 512).
    pub alloc_bytes: u64,
    /// Σ `ATTR_CMNEXT_PRIVATESIZE` over files, when [`Walker::private`] is on.
    pub private_bytes: u64,
    /// Mount points skipped under [`Walker::one_fs`].
    pub mounts_skipped: u64,
}

impl<'a> Walker<'a> {
    pub fn new(excludes: &'a [Vec<u8>]) -> Self {
        Walker {
            excludes,
            // 256 KiB batches: big enough to amortize the syscall, small enough
            // to stay in cache and bound memory.
            buf: vec![0u8; 256 * 1024],
            request: Request::default(),
            one_fs: false,
            records: 0,
            errors: ErrorStats::new(),
            alloc_bytes: 0,
            private_bytes: 0,
            mounts_skipped: 0,
        }
    }

    /// Stay on the root's filesystem: mount points are emitted (as gfind
    /// `-xdev` does) but not descended into.
    pub fn one_fs(mut self, on: bool) -> Self {
        self.one_fs = on;
        self.request.mount_status = on;
        self
    }

    /// Also fetch each file's APFS private size (same syscall, one more
    /// attribute), summed into `private_bytes`. The record stream is unchanged.
    pub fn private(mut self, on: bool) -> Self {
        self.request.private = on;
        self
    }

    /// Walk `root`, writing gfind-format records to `out` and permission
    /// errors to `err`.
    pub fn walk<W: Write, E: Write>(&mut self, root: &[u8], out: &mut W, err: &mut E) -> io::Result<()> {
        self.walk_into(root, &mut WriteSink(out), err)
    }

    /// Walk `root`, handing each record to `f` (paths are borrowed for the call).
    pub fn walk_records<F: FnMut(&Record) -> io::Result<()>, E: Write>(
        &mut self,
        root: &[u8],
        mut f: F,
        err: &mut E,
    ) -> io::Result<()> {
        self.walk_into(root, &mut f, err)
    }

    /// Walk `root` (an absolute path, no trailing slash except "/"), writing
    /// records to `out` and permission errors to `err`.
    fn walk_into<S: Sink + ?Sized, E: Write>(
        &mut self,
        root: &[u8],
        out: &mut S,
        err: &mut E,
    ) -> io::Result<()> {
        let root = normalize_root(root);
        // The root entry itself (gfind/find emits the starting point first).
        match lstat_record(&root) {
            Some((kind, blocks, mtime, is_dir)) => {
                self.emit(Record { kind, blocks, mtime, path: &root }, out)?;
                if is_dir {
                    // DFS stack of directories still to visit.
                    let mut stack: Vec<Vec<u8>> = vec![root];
                    while let Some(dir) = stack.pop() {
                        self.walk_dir(&dir, &mut stack, out, err)?;
                    }
                }
            }
            None => {
                report_errno(&root, err, &mut self.errors);
            }
        }
        Ok(())
    }

    fn walk_dir<S: Sink + ?Sized, E: Write>(
        &mut self,
        dir: &[u8],
        stack: &mut Vec<Vec<u8>>,
        out: &mut S,
        err: &mut E,
    ) -> io::Result<()> {
        let cpath = match cstring(dir) {
            Some(c) => c,
            None => return Ok(()), // interior NUL — can't happen for a real path
        };
        let fd = unsafe { libc::open(cpath.as_ptr(), libc::O_RDONLY) };
        if fd < 0 {
            report_errno(dir, err, &mut self.errors);
            return Ok(());
        }

        let mut alist = Attrlist::request(self.request);
        let options = self.request.options();
        loop {
            let n = unsafe {
                getattrlistbulk(
                    fd,
                    &mut alist,
                    self.buf.as_mut_ptr() as *mut c_void,
                    self.buf.len(),
                    options,
                )
            };
            if n < 0 {
                report_errno(dir, err, &mut self.errors);
                break;
            }
            if n == 0 {
                break; // end of directory
            }

            // Collect children first (the parse borrows `self.buf`; emitting and
            // pushing need `&mut self`, so we can't do both inside the closure).
            let mut children: Vec<(Vec<u8>, u8, u64, i64, bool)> = Vec::with_capacity(n as usize);
            let (mut private, mut skipped) = (0u64, 0u64);
            let one_fs = self.one_fs;
            unsafe {
                attrlist::parse_entries(&self.buf, n as usize, |e| {
                    let path = join(dir, e.name);
                    private += e.private.unwrap_or(0);
                    let descend = e.is_dir && !(one_fs && e.mount_point);
                    skipped += (e.is_dir && !descend) as u64;
                    children.push((path, e.kind, e.blocks, e.mtime_sec, descend));
                });
            }
            self.private_bytes += private;
            self.mounts_skipped += skipped;
            for (path, kind, blocks, mut mtime, descend) in children {
                // A mount point's bulk attributes describe the *covered* dir
                // (e.g. the sealed system's build date); `lstat` sees the
                // mounted root, as gfind does. Rare, so the extra call is free.
                if kind == b'd' && !descend {
                    if let Some((_, _, m, _)) = lstat_record(&path) {
                        mtime = m;
                    }
                }
                self.alloc_bytes += if kind == b'f' { blocks * 512 } else { 0 };
                self.emit(Record { kind, blocks, mtime, path: &path }, out)?;
                if descend && !self.is_excluded(&path) {
                    stack.push(path);
                }
            }
        }

        unsafe { libc::close(fd) };
        Ok(())
    }

    fn is_excluded(&self, path: &[u8]) -> bool {
        self.excludes.iter().any(|e| e.as_slice() == path)
    }

    #[inline]
    fn emit<S: Sink + ?Sized>(&mut self, r: Record, out: &mut S) -> io::Result<()> {
        out.record(&r)?;
        self.records += 1;
        Ok(())
    }
}

/// Strip a trailing slash unless the path is exactly "/".
fn normalize_root(root: &[u8]) -> Vec<u8> {
    if root.len() > 1 && root.ends_with(b"/") {
        root[..root.len() - 1].to_vec()
    } else {
        root.to_vec()
    }
}

/// `dir + '/' + name`, special-casing the filesystem root so we get `/name`.
fn join(dir: &[u8], name: &[u8]) -> Vec<u8> {
    let mut p = Vec::with_capacity(dir.len() + 1 + name.len());
    if dir == b"/" {
        p.push(b'/');
    } else {
        p.extend_from_slice(dir);
        p.push(b'/');
    }
    p.extend_from_slice(name);
    p
}

fn cstring(path: &[u8]) -> Option<std::ffi::CString> {
    std::ffi::CString::new(path).ok()
}

/// lstat the root to reproduce gfind's record for the starting point.
fn lstat_record(path: &[u8]) -> Option<(u8, u64, i64, bool)> {
    let cpath = cstring(path)?;
    let mut st: libc::stat = unsafe { std::mem::zeroed() };
    if unsafe { libc::lstat(cpath.as_ptr(), &mut st) } != 0 {
        return None;
    }
    let fmt = st.st_mode & libc::S_IFMT;
    let (kind, is_dir) = match fmt {
        libc::S_IFREG => (b'f', false),
        libc::S_IFDIR => (b'd', true),
        libc::S_IFLNK => (b'l', false),
        libc::S_IFBLK => (b'b', false),
        libc::S_IFCHR => (b'c', false),
        libc::S_IFSOCK => (b's', false),
        libc::S_IFIFO => (b'p', false),
        _ => (b'?', false),
    };
    // st_blocks is already in 512-byte units — exactly gfind's %b.
    Some((kind, st.st_blocks.max(0) as u64, st.st_mtime as i64, is_dir))
}

/// Emit a gfind-style `Permission denied` line (matched by the Python parser's
/// `PERMISSION_DENIED_RE`) for EACCES/EPERM; a generic line otherwise. Either
/// way the path counts as one error and the walk continues.
fn report_errno<E: Write>(path: &[u8], err: &mut E, stats: &mut ErrorStats) {
    let e = io::Error::last_os_error();
    let raw = e.raw_os_error().unwrap_or(0);
    stats.add(path);
    let _ = err.write_all(b"dt-walker: '");
    let _ = err.write_all(path);
    if raw == libc::EACCES || raw == libc::EPERM {
        let _ = err.write_all(b"': Permission denied\n");
    } else {
        let _ = err.write_all(b"': ");
        let _ = err.write_all(e.to_string().as_bytes());
        let _ = err.write_all(b"\n");
    }
}
