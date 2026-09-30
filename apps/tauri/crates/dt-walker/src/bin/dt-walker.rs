//! `dt-walker [--exclude PATH]... [--no-default-excludes] [--one-fs] [--private] [--stats] <root>`
//!
//! Walks `<root>` via `getattrlistbulk(2)` and writes the `gfind`-compatible
//! `%y %b %T@ %p\0` stream to stdout — a drop-in for the `gfind` subprocess
//! disk-tree shells out to. Permission errors go to stderr in gfind's format.
//! `--stats` prints a one-line summary (records, errors, Σalloc) to stderr at the end.
//! `--one-fs` (`-x`) doesn't descend into mount points, like `find -xdev`.
//! `--private` also fetches each file's APFS private size in the same bulk call
//! and adds Σprivate to `--stats` (the record stream is unchanged).

use std::io::{self, Write};
use std::os::unix::ffi::OsStrExt;
use std::process::ExitCode;

use dt_walker::{default_excludes, Walker};

fn main() -> ExitCode {
    let mut excludes: Vec<Vec<u8>> = Vec::new();
    let mut use_default_excludes = true;
    let mut stats = false;
    let mut one_fs = false;
    let mut private = false;
    let mut root: Option<Vec<u8>> = None;

    let mut args = std::env::args_os().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_bytes() {
            b"--exclude" | b"-e" => match args.next() {
                Some(p) => excludes.push(p.as_bytes().to_vec()),
                None => return usage_err("--exclude needs a PATH"),
            },
            b"--no-default-excludes" => use_default_excludes = false,
            b"--stats" => stats = true,
            b"--one-fs" | b"-x" => one_fs = true,
            b"--private" => private = true,
            b"-h" | b"--help" => {
                eprintln!(
                    "usage: dt-walker [--exclude PATH]... [--no-default-excludes] [--one-fs] [--private] [--stats] <root>"
                );
                return ExitCode::SUCCESS;
            }
            _ => {
                if root.is_some() {
                    return usage_err("multiple roots given");
                }
                root = Some(arg.as_bytes().to_vec());
            }
        }
    }

    let root = match root {
        Some(r) => r,
        None => return usage_err("missing <root>"),
    };

    if use_default_excludes {
        excludes.extend(default_excludes());
    }

    // Big buffered writer: records are tiny and frequent; syscall-per-record
    // would dominate. Lock stdout once.
    let stdout = io::stdout();
    let mut out = io::BufWriter::with_capacity(1 << 20, stdout.lock());
    let stderr = io::stderr();
    let mut err = io::BufWriter::new(stderr.lock());

    let mut walker = Walker::new(&excludes).one_fs(one_fs).private(private);
    let res = walker.walk(&root, &mut out, &mut err);

    if let Err(e) = out.flush() {
        eprintln!("dt-walker: write error: {e}");
        return ExitCode::FAILURE;
    }
    let _ = err.flush();

    if stats {
        let mib = |b: u64| b as f64 / (1u64 << 20) as f64;
        let mut line = format!(
            "dt-walker: {} records, {} errors, alloc {:.1} MiB",
            walker.records, walker.errors.count, mib(walker.alloc_bytes)
        );
        if private {
            line += &format!(", private {:.1} MiB", mib(walker.private_bytes));
        }
        if one_fs {
            line += &format!(", {} mount points skipped", walker.mounts_skipped);
        }
        eprintln!("{line}");
    }

    match res {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("dt-walker: {e}");
            ExitCode::FAILURE
        }
    }
}

fn usage_err(msg: &str) -> ExitCode {
    eprintln!("dt-walker: {msg}");
    eprintln!("usage: dt-walker [--exclude PATH]... [--no-default-excludes] [--one-fs] [--private] [--stats] <root>");
    ExitCode::FAILURE
}
