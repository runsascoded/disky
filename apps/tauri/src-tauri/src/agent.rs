//! Headless modes of the app binary, dispatched on `argv[1]` before any Tauri
//! (or AppKit) initialization, so they never show a window or a Dock icon.
//!
//! - `disky agent [--] CMD [ARGS…]` — run CMD as a *child* and exit
//!   with its status. For LaunchAgents: when launchd execs this binary, TCC
//!   treats the signed `disky.app` as the responsible process for the job
//!   and everything it spawns, so one Full Disk Access grant to the app covers
//!   the scan (`gfind`/`dt-walker`) under a venv `python`. The child is
//!   spawned, never `exec`ed: an `exec` would replace this image with the
//!   interpreter, and the job's responsible code would become `python3.x`
//!   again, which is exactly the fragile grant this replaces. When the
//!   bundle carries `dt-walker` and `DISK_TREE_WALKER` is unset, the child
//!   gets it, so disk-tree scans under the agent use the native walker.
//! - `disky job NAME` — the bundled LaunchAgents' program: run job NAME from
//!   `~/.config/disk-tree/disky.json` the same way (`jobs.rs`).
//! - `disky agents register|unregister|status`, `disky login-item on|off|status`
//!   — `SMAppService` registration (`services.rs`).
//! - `disky probe` — try reading TCC-protected locations *in this
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
        Some("job") => Some(match args.get(2).and_then(|a| a.to_str()) {
            Some(name) => crate::jobs::run(name),
            None => {
                eprintln!("usage: disky job NAME");
                2
            }
        }),
        Some(kind @ ("agents" | "login-item")) => {
            Some(crate::services::cli(
                kind,
                args.get(2).and_then(|a| a.to_str()),
                args.get(3).and_then(|a| a.to_str()),
            ))
        }
        _ => None,
    }
}

fn agent(rest: &[OsString]) -> i32 {
    let rest = match rest.first().and_then(|a| a.to_str()) {
        Some("--") => &rest[1..],
        _ => rest,
    };
    let Some((program, args)) = rest.split_first() else {
        eprintln!("usage: disky agent [--] CMD [ARGS…]");
        return 2;
    };
    let mut cmd = Command::new(program);
    cmd.args(args);
    run_child(cmd)
}

/// Spawn `cmd` (with the bundled walker as `DISK_TREE_WALKER` when unset),
/// forward TERM/INT/HUP to it, and return its exit status (128+signal when
/// killed). Shared by `agent` and `job`.
pub fn run_child(mut cmd: Command) -> i32 {
    if let Some(walker) = crate::locate_walker() {
        cmd.env("DISK_TREE_WALKER", walker);
    }
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("disky: can't spawn {:?}: {e}", cmd.get_program());
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
            eprintln!("disky: wait failed: {e}");
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

/// `(path, verdict)` per probe target; verdict `ok N` / `absent` / `denied` / `error …`.
pub fn probe_results() -> Vec<(PathBuf, String)> {
    probe_targets()
        .into_iter()
        .map(|path| {
            let verdict = match std::fs::read_dir(&path) {
                Ok(entries) => format!("ok {}", entries.count()),
                Err(e) if e.kind() == ErrorKind::NotFound => "absent".to_string(),
                Err(e) if e.kind() == ErrorKind::PermissionDenied => "denied".to_string(),
                Err(e) => format!("error {e}"),
            };
            (path, verdict)
        })
        .collect()
}

/// Whether this process can read every present TCC-protected probe dir.
pub fn has_full_disk_access() -> bool {
    probe_results().iter().all(|(_, v)| v.starts_with("ok") || v == "absent")
}

fn probe() -> i32 {
    let mut denied = 0;
    for (path, verdict) in probe_results() {
        if !(verdict.starts_with("ok") || verdict == "absent") {
            denied += 1;
        }
        println!("{verdict}\t{}", path.display());
    }
    if denied == 0 { 0 } else { 3 }
}
