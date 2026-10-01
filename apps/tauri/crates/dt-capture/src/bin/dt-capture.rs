//! `dt-capture [-o] [-n ROWS] [-C] -t DIR|URL ROOT` — the Rust `disk-tree capture`
//! (a local dir, or `r2://` / `s3://` / `file://`). Prints the capture dir on stdout, a summary on stderr, as
//! the Python CLI does.

use std::process::ExitCode;

fn usage(msg: &str) -> ExitCode {
    eprintln!("dt-capture: {msg}\nusage: dt-capture [-o|--one-fs] [-n|--batch-rows N] [-C|--no-container] -t|--to DIR|URL ROOT");
    ExitCode::from(2)
}

/// `836855` → `836,855` (Python's `f"{n:,}"`).
fn thousands(n: u64) -> String {
    let s = n.to_string();
    let mut out = String::new();
    for (i, c) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    out
}

fn main() -> ExitCode {
    let (mut to, mut root, mut one_fs, mut container, mut batch_rows) = (None, None, false, true, 200_000usize);
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "-o" | "--one-fs" => one_fs = true,
            "-C" | "--no-container" => container = false,
            "-t" | "--to" => to = args.next(),
            "-n" | "--batch-rows" => match args.next().and_then(|n| n.parse().ok()) {
                Some(n) => batch_rows = n,
                None => return usage("--batch-rows needs a number"),
            },
            "-h" | "--help" => return usage("help"),
            _ if root.is_none() => root = Some(a),
            _ => return usage("one ROOT only"),
        }
    }
    let (Some(to), Some(root)) = (to, root) else { return usage("need -t DIR and ROOT") };
    let to = match dt_capture::target::Target::parse(&to) {
        Ok(t) => t,
        Err(e) => {
            eprintln!("dt-capture: {e}");
            return ExitCode::FAILURE;
        }
    };
    let opts = dt_capture::Opts {
        root,
        to,
        host: dt_capture::host(),
        batch_rows,
        one_fs,
        container: container && cfg!(target_os = "macos"),
    };
    match dt_capture::capture(&opts) {
        Ok(s) => {
            let tail = if s.error_count > 0 { format!(", {} permission errors", s.error_count) } else { String::new() };
            eprintln!("{}: {} files in {} shard(s) → {}{tail}", dt_capture::normalize_root(&opts.root), thousands(s.n_rows), s.n_shards, s.dir);
            println!("{}", s.dir);
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("dt-capture: {e}");
            ExitCode::FAILURE
        }
    }
}
