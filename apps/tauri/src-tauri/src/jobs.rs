//! `disky job NAME`: the program behind the bundled LaunchAgents
//! (`Contents/Library/LaunchAgents/com.runsascoded.disky.<NAME>.plist`).
//!
//! A bundled plist is signed and static, so it can't name a user's checkout or
//! a `~` log path. The per-user part lives in `~/.config/disk-tree/disky.json`:
//!
//! ```json
//! {"jobs": {"scan":  {"to": "r2://disk-tree/captures", "host": "m3",
//!                     "env": {"AWS_PROFILE": "m3"}, "log": "index",
//!                     "then": {"command": ["/…/.venv/bin/python", "/…/aws/submit"], "env": {"AWS_PROFILE": "r"}}},
//!           "drain": {"command": […], "log": "drain"}}}
//! ```
//!
//! A job with a `command` runs it as a child. The scan job may instead name a
//! `to` (a dir or `r2://` / `s3://` / `file://` URL): disky then captures the
//! scan root itself (`dt-capture`, in this process, so the walk is the app's
//! own under TCC) with `env` applied, and runs the optional `then` command with
//! the capture dir appended (the Batch submit, until a cloud-side trigger picks
//! up new captures; spec `rust-engine.md` phase 3).
//!
//! The job's stdout/stderr are appended to `~/Library/Logs/disk-tree/<log>.{out,err}.log`
//! (`log` defaults to the job name). An unconfigured job logs one line to
//! `disky.log` and exits 0 (the drain plist restarts only on failure).

use std::collections::BTreeMap;
use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use std::process::Command;

use serde::Deserialize;

#[derive(Deserialize, Default)]
pub struct Config {
    #[serde(default)]
    pub jobs: BTreeMap<String, Job>,
}

#[derive(Deserialize, Clone, Default)]
pub struct Job {
    #[serde(default)]
    pub command: Vec<String>,
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    pub log: Option<String>,
    /// In-process capture target (the scan job, when it has no `command`).
    pub to: Option<String>,
    /// The capture's host segment (default `DISK_TREE_HOST`, else the hostname).
    pub host: Option<String>,
    /// Run after an in-process capture, with the capture dir appended.
    pub then: Option<Step>,
}

#[derive(Deserialize, Clone, Default)]
pub struct Step {
    pub command: Vec<String>,
    #[serde(default)]
    pub env: BTreeMap<String, String>,
}

fn home() -> PathBuf {
    PathBuf::from(std::env::var_os("HOME").unwrap_or_default())
}

pub fn config_path() -> PathBuf {
    std::env::var_os("DISK_TREE_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| home().join(".config/disk-tree"))
        .join("disky.json")
}

pub fn logs_dir() -> PathBuf {
    home().join("Library/Logs/disk-tree")
}

pub fn load() -> Result<Config, String> {
    let p = config_path();
    match std::fs::read(&p) {
        Ok(bytes) => serde_json::from_slice(&bytes).map_err(|e| format!("{}: {e}", p.display())),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Config::default()),
        Err(e) => Err(format!("{}: {e}", p.display())),
    }
}

/// `(stdout, stderr)` log paths for job `name`.
pub fn log_paths(name: &str, job: Option<&Job>) -> (PathBuf, PathBuf) {
    let stem = job.and_then(|j| j.log.clone()).unwrap_or_else(|| name.to_string());
    let d = logs_dir();
    (d.join(format!("{stem}.out.log")), d.join(format!("{stem}.err.log")))
}

pub fn note(msg: &str) {
    let _ = std::fs::create_dir_all(logs_dir());
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(logs_dir().join("disky.log")) {
        let _ = writeln!(f, "{msg}");
    }
    eprintln!("{msg}");
}

/// `disky job scan --scheduled`: the scan agent's 15-minute wake. Runs the scan
/// only if a schedule slot passed since the last start, or "Scan now" set the
/// force flag; otherwise exits 0 silently. The first wake after install just
/// records a baseline.
pub fn run_scheduled_scan() -> i32 {
    use crate::settings;
    let s = settings::load();
    let mut st = settings::load_state();
    let now = settings::now();
    if !st.force {
        if st.last_start.is_none() {
            st.last_start = Some(now);
            let _ = settings::save_state(&st);
            return 0;
        }
        // A run killed mid-scan (an app reinstall, a logout) already claimed
        // its slot: retry it once at the next wake rather than wait for the
        // next slot. A retry that is itself interrupted waits.
        let retry = !st.retried && st.interrupted();
        if !settings::due(&s.times(), st.last_start, now) && !retry {
            return 0;
        }
        if retry {
            note("disky job scan: retrying an interrupted run");
        }
        st.retried = retry;
        let _ = settings::save_state(&st);
    }
    run("scan")
}

pub fn run(name: &str) -> i32 {
    let cfg = match load() {
        Ok(c) => c,
        Err(e) => {
            note(&format!("disky job {name}: bad config: {e}"));
            return 1;
        }
    };
    let Some(job) = cfg.jobs.get(name) else {
        note(&format!("disky job {name}: not configured in {} — nothing to run", config_path().display()));
        return 0;
    };
    let (out, err) = log_paths(name, Some(job));
    let _ = std::fs::create_dir_all(logs_dir());
    let open = |p: &PathBuf| OpenOptions::new().create(true).append(true).open(p);
    let (Ok(out_f), Ok(err_f)) = (open(&out), open(&err)) else {
        note(&format!("disky job {name}: can't open {} / {}", out.display(), err.display()));
        return 1;
    };
    let in_process = name == "scan" && job.command.is_empty() && job.to.is_some();
    let cmd = if in_process {
        None
    } else {
        let Some((program, args)) = job.command.split_first() else {
            note(&format!("disky job {name}: empty command"));
            return 1;
        };
        let mut cmd = Command::new(program);
        cmd.args(args).envs(&job.env).stdout(out_f.try_clone().unwrap()).stderr(err_f.try_clone().unwrap());
        Some(cmd)
    };
    if name != "scan" {
        return crate::agent::run_child(cmd.unwrap());
    }
    use crate::settings;
    let root = settings::load().scan_root();
    let mut st = settings::load_state();
    st.last_start = Some(settings::now());
    st.force = false;
    let _ = settings::save_state(&st);
    let code = match cmd {
        Some(mut cmd) => {
            cmd.env("DISKY_SCAN_ROOT", &root);
            crate::agent::run_child(cmd)
        }
        None => capture(job, &root, out_f, err_f),
    };
    settings::clear_progress();
    let mut st = settings::load_state();
    st.last_end = Some(settings::now());
    st.last_exit = Some(code);
    let _ = settings::save_state(&st);
    code
}

/// `[2026-10-01T12:00:00Z] msg`, as `aws/laptop-scan` logs.
fn stamped(msg: &str) -> String {
    let (_, iso) = dt_capture::stamps(crate::settings::now(), 0);
    format!("[{}Z] {msg}", &iso[..19])
}

/// The in-process scan: capture `root` to `job.to` (with `job.env` applied to
/// this process, where the credential lookup reads it), then `job.then`.
fn capture(job: &Job, root: &str, mut out: std::fs::File, mut err: std::fs::File) -> i32 {
    for (k, v) in &job.env {
        std::env::set_var(k, v);
    }
    let to = job.to.as_deref().unwrap_or_default();
    let target = match dt_capture::target::Target::parse(to) {
        Ok(t) => t,
        Err(e) => {
            let _ = writeln!(err, "{}", stamped(&format!("dt-capture: {e}")));
            return 1;
        }
    };
    use crate::settings::{save_progress, Progress};
    let since = crate::settings::now();
    let opts = dt_capture::Opts {
        root: root.into(),
        to: target,
        host: job.host.clone().unwrap_or_else(dt_capture::host),
        batch_rows: 200_000,
        // `/` stays on one filesystem: the Data volume is reached through its
        // firmlinks once, not walked again at `/System/Volumes/Data`.
        one_fs: root == "/",
        container: cfg!(target_os = "macos"),
        progress: Some(Box::new(move |files, bytes| save_progress(&Progress { phase: "capture".into(), files, bytes, since }))),
    };
    save_progress(&Progress { phase: "capture".into(), files: 0, bytes: 0, since });
    let _ = writeln!(out, "{}", stamped(&format!("capture {root} → {to}")));
    let s = match dt_capture::capture(&opts) {
        Ok(s) => s,
        Err(e) => {
            let _ = writeln!(err, "{}", stamped(&format!("dt-capture: {e}")));
            return 1;
        }
    };
    let _ = writeln!(
        out,
        "{}",
        stamped(&format!("captured {} ({} files, {} shards, {} errors)", s.dir, s.n_rows, s.n_shards, s.error_count))
    );
    let Some(then) = &job.then else { return 0 };
    let Some((program, args)) = then.command.split_first() else { return 0 };
    save_progress(&Progress { phase: "then".into(), files: s.n_rows, bytes: s.n_bytes, since });
    let mut cmd = Command::new(program);
    cmd.args(args).arg(&s.dir).envs(&then.env).stdout(out.try_clone().unwrap()).stderr(err.try_clone().unwrap());
    let code = crate::agent::run_child(cmd);
    let _ = writeln!(out, "{}", stamped(&format!("then {program}: exit {code}")));
    code
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_jobs_and_log_paths() {
        let cfg: Config = serde_json::from_str(
            r#"{"jobs": {"scan": {"command": ["/py", "/laptop-scan"], "env": {"PATH": "/opt/homebrew/bin"}, "log": "index"},
                         "drain": {"command": ["/py", "/laptop-drain"]}}}"#,
        )
        .unwrap();
        let scan = &cfg.jobs["scan"];
        assert_eq!(scan.command, ["/py", "/laptop-scan"]);
        assert_eq!(scan.env.get("PATH").map(String::as_str), Some("/opt/homebrew/bin"));
        let names = |(o, e): (PathBuf, PathBuf)| {
            (o.file_name().unwrap().to_string_lossy().into_owned(), e.file_name().unwrap().to_string_lossy().into_owned())
        };
        assert_eq!(names(log_paths("scan", Some(scan))), ("index.out.log".into(), "index.err.log".into()));
        assert_eq!(names(log_paths("drain", cfg.jobs.get("drain"))), ("drain.out.log".into(), "drain.err.log".into()));
        assert!(serde_json::from_str::<Config>("{}").unwrap().jobs.is_empty());
        let cfg: Config = serde_json::from_str(
            r#"{"jobs": {"scan": {"to": "r2://disk-tree/captures", "host": "m3", "env": {"AWS_PROFILE": "m3"},
                                  "then": {"command": ["/py", "/submit"], "env": {"AWS_PROFILE": "r"}}}}}"#,
        )
        .unwrap();
        let scan = &cfg.jobs["scan"];
        assert!(scan.command.is_empty());
        assert_eq!((scan.to.as_deref(), scan.host.as_deref()), (Some("r2://disk-tree/captures"), Some("m3")));
        let then = scan.then.as_ref().unwrap();
        assert_eq!(then.command, ["/py", "/submit"]);
        assert_eq!(then.env.get("AWS_PROFILE").map(String::as_str), Some("r"));
    }
}
