//! disk-tree native macOS app (Tauri v2 host).
//!
//! Opens a system-WKWebView window on the existing Flask+React UI. For now the
//! Python backend runs as a spawned subprocess (waitress/Flask on a loopback
//! port), exactly like the v1 pywebview app (`disk_tree.desktop`) — the window
//! loads that server's URL, so the whole `/api/*` contract works unchanged.
//!
//! The native `getattrlistbulk` walker (`dt-walker`) is compiled *into* this
//! binary (a workspace dependency), so a walk done here carries the signed app's
//! TCC identity with no child-process caveat — the core v2 win. Two ways it
//! reaches a scan:
//!   1. `native_walk_stats` command — an in-process walk (proves the walk lives
//!      in the app binary; direct streaming into aggregation is the next step).
//!   2. the spawned Python backend is pointed at the bundled `dt-walker` binary
//!      via `DISK_TREE_WALKER`, so its scans use the native walker too.
//!
//! See `specs/tauri-native-app.md`.

use std::io::Write;
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

mod agent;

use serde::Serialize;
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

/// The spawned Python backend, killed when the app exits.
struct Backend(Mutex<Option<Child>>);

#[derive(Serialize)]
struct WalkStats {
    records: u64,
    errors: u64,
}

/// Walk `root` in-process via the native getattrlistbulk walker, returning
/// counts. Demonstrates the walk running inside the signed app binary (the TCC
/// win); the record stream itself is discarded here.
#[tauri::command]
fn native_walk_stats(root: String) -> Result<WalkStats, String> {
    let excludes = dt_walker::default_excludes();
    let mut walker = dt_walker::Walker::new(&excludes);
    let mut sink = std::io::sink();
    let mut errbuf: Vec<u8> = Vec::new();
    walker
        .walk(root.as_bytes(), &mut sink, &mut errbuf)
        .map_err(|e| e.to_string())?;
    Ok(WalkStats { records: walker.records, errors: walker.errors.count })
}

/// A free loopback TCP port (bound then released — the OS won't immediately
/// reuse it for the backend we hand it to).
fn free_loopback_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .expect("bind loopback")
        .local_addr()
        .expect("local_addr")
        .port()
}

fn wait_until_up(port: u16, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if TcpStream::connect(("127.0.0.1", port)).is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    false
}

/// The bundled `dt-walker` binary, if we can find it next to our own executable
/// (how it's laid out in a bundle) — used to point the Python backend at the
/// native walker. Respects an existing `DISK_TREE_WALKER` env.
pub(crate) fn locate_walker() -> Option<PathBuf> {
    if std::env::var_os("DISK_TREE_WALKER").is_some() {
        return None; // already set by the environment; don't override
    }
    let exe = std::env::current_exe().ok()?;
    let dir = exe.parent()?;
    for cand in [dir.join("dt-walker"), dir.join("../Resources/dt-walker")] {
        if cand.is_file() {
            return Some(cand);
        }
    }
    None
}

/// Spawn the Python backend on `port`. Command: `$DISK_TREE_SERVER_CMD`
/// (space-split) or `disk-tree-server`. The v1 freezer note applies to the
/// packaged sidecar (waitress, not Flask's dev server); in dev, `disk-tree-server`
/// (Flask, honoring `PORT`) is fine.
fn spawn_backend(port: u16) -> std::io::Result<Child> {
    let cmdline = std::env::var("DISK_TREE_SERVER_CMD")
        .unwrap_or_else(|_| "disk-tree-server".to_string());
    let mut parts = cmdline.split_whitespace();
    let program = parts.next().unwrap_or("disk-tree-server");
    let mut cmd = Command::new(program);
    cmd.args(parts)
        .env("PORT", port.to_string())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());
    if let Some(walker) = locate_walker() {
        cmd.env("DISK_TREE_WALKER", walker);
    }
    cmd.spawn()
}

/// A headless mode (`agent`, `probe`) when `args` selects one: its exit code.
/// Checked before `run()`, so launchd jobs never touch Tauri/AppKit.
pub fn headless(args: &[std::ffi::OsString]) -> Option<i32> {
    agent::dispatch(args)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let port = free_loopback_port();

    tauri::Builder::default()
        .manage(Backend(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![native_walk_stats])
        .setup(move |app| {
            let child = spawn_backend(port).map_err(|e| {
                format!("failed to spawn Python backend (is `disk-tree-server` on PATH?): {e}")
            })?;
            *app.state::<Backend>().0.lock().unwrap() = Some(child);

            if !wait_until_up(port, Duration::from_secs(20)) {
                let _ = writeln!(
                    std::io::stderr(),
                    "disk-tree-app: backend did not come up on 127.0.0.1:{port}"
                );
            }

            let url = format!("http://127.0.0.1:{port}/");
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url.parse().unwrap()))
                .title("disk-tree")
                .inner_size(1200.0, 820.0)
                .min_inner_size(720.0, 480.0)
                .build()?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error building disk-tree app")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                // Reap the backend so it doesn't outlive the window.
                if let Some(mut child) = app.state::<Backend>().0.lock().unwrap().take() {
                    let _ = child.kill();
                    let _ = child.wait();
                }
            }
        });
}
