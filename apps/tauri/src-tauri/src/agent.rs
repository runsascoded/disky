//! Headless modes of the app binary, dispatched on `argv[1]` before any Tauri
//! (or AppKit) initialization, so they never show a window or a Dock icon.
//!
//! - `disk-tree-app agent [--] CMD [ARGS…]` — run CMD as a *child* and exit
//!   with its status. For LaunchAgents: when launchd execs this binary, TCC
//!   treats the signed `disk-tree.app` as the responsible process for the job
//!   and everything it spawns, so one Full Disk Access grant to the app covers
//!   the scan (`gfind`/`dt-walker`) under a venv `python`. The child is
//!   spawned, never `exec`ed: an `exec` would replace this image with the
//!   interpreter, and the job's responsible code would become `python3.x`
//!   again, which is exactly the fragile grant this replaces. When the
//!   bundle carries `dt-walker` and `DISK_TREE_WALKER` is unset, the child
//!   gets it, so disk-tree scans under the agent use the native walker.
//! - `disk-tree-app probe` — try reading TCC-protected locations *in this
//!   process* and print one line per location (`ok` / `denied` / `absent`).
//!   Exit 0 when every present location was readable, 3 otherwise. Run it via
//!   launchd to check the app's Full Disk Access grant.
//!
//! See `specs/tauri-native-app.md` ("Scheduled scans").

use std::ffi::OsString;
use std::io::ErrorKind;
use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicI32, Ordering};

/// The running child's pid, for the signal forwarder (0 = none yet).
static CHILD: AtomicI32 = AtomicI32::new(0);

extern "C" fn forward(sig: libc::c_int) {
    let pid = CHILD.load(Ordering::SeqCst);
    if pid > 0 {
        // kill(2) is async-signal-safe.
        unsafe { libc::kill(pid, sig) };
    }
}

/// `Some(exit code)` when `args` selects a headless mode, `None` for the GUI.
pub fn dispatch(args: &[OsString]) -> Option<i32> {
    match args.get(1).and_then(|a| a.to_str()) {
        Some("agent") => Some(agent(&args[2..])),
        Some("probe") => Some(probe()),
        _ => None,
    }
}

fn agent(rest: &[OsString]) -> i32 {
    let rest = match rest.first().and_then(|a| a.to_str()) {
        Some("--") => &rest[1..],
        _ => rest,
    };
    let Some((program, args)) = rest.split_first() else {
        eprintln!("usage: disk-tree-app agent [--] CMD [ARGS…]");
        return 2;
    };
    let mut cmd = Command::new(program);
    cmd.args(args);
    if let Some(walker) = crate::locate_walker() {
        cmd.env("DISK_TREE_WALKER", walker);
    }
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("disk-tree-app agent: can't spawn {program:?}: {e}");
            return 127;
        }
    };
    CHILD.store(child.id() as i32, Ordering::SeqCst);
    // launchd stops a job with SIGTERM to the job's pid (then SIGKILL after
    // ExitTimeOut); pass TERM/INT/HUP on so the child can clean up.
    for sig in [libc::SIGTERM, libc::SIGINT, libc::SIGHUP] {
        unsafe { libc::signal(sig, forward as extern "C" fn(libc::c_int) as libc::sighandler_t) };
    }
    match child.wait() {
        Ok(status) => {
            use std::os::unix::process::ExitStatusExt;
            match (status.code(), status.signal()) {
                (Some(code), _) => code,
                (None, Some(sig)) => 128 + sig,
                (None, None) => 1,
            }
        }
        Err(e) => {
            eprintln!("disk-tree-app agent: wait failed: {e}");
            1
        }
    }
}

/// Locations macOS guards with TCC, each readable only with Full Disk Access
/// (or a per-category grant). `~/.Trash` is the drainer's; the rest are the
/// `~/Library` trees an FDA-less scan silently skips.
fn probe_targets() -> Vec<PathBuf> {
    let home = PathBuf::from(std::env::var_os("HOME").unwrap_or_default());
    [
        ".Trash",
        "Library/Mail",
        "Library/Messages",
        "Library/Safari",
        "Library/Application Support/com.apple.TCC",
        "Library/Containers/com.apple.stocks",
    ]
    .iter()
    .map(|rel| home.join(rel))
    .collect()
}

fn probe() -> i32 {
    let mut denied = 0;
    for path in probe_targets() {
        let verdict = match std::fs::read_dir(&path) {
            Ok(entries) => format!("ok {}", entries.count()),
            Err(e) if e.kind() == ErrorKind::NotFound => "absent".to_string(),
            Err(e) if e.kind() == ErrorKind::PermissionDenied => {
                denied += 1;
                "denied".to_string()
            }
            Err(e) => {
                denied += 1;
                format!("error {e}")
            }
        };
        println!("{verdict}\t{}", path.display());
    }
    if denied == 0 { 0 } else { 3 }
}
