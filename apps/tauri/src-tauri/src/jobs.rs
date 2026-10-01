//! `disky job NAME`: the program behind the bundled LaunchAgents
//! (`Contents/Library/LaunchAgents/com.runsascoded.disky.<NAME>.plist`).
//!
//! A bundled plist is signed and static, so it can't name a user's checkout or
//! a `~` log path. The per-user part lives in `~/.config/disk-tree/disky.json`:
//!
//! ```json
//! {"jobs": {"scan":  {"command": ["/…/.venv/bin/python", "/…/aws/laptop-scan"],
//!                     "env": {"PATH": "/opt/homebrew/bin:/usr/bin:/bin"}, "log": "index"},
//!           "drain": {"command": […], "log": "drain"}}}
//! ```
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

#[derive(Deserialize, Clone)]
pub struct Job {
    pub command: Vec<String>,
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    pub log: Option<String>,
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
        if !settings::due(&s.times(), st.last_start, now) {
            return 0;
        }
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
    let Some((program, args)) = job.command.split_first() else {
        note(&format!("disky job {name}: empty command"));
        return 1;
    };
    let (out, err) = log_paths(name, Some(job));
    let _ = std::fs::create_dir_all(logs_dir());
    let open = |p: &PathBuf| OpenOptions::new().create(true).append(true).open(p);
    let (Ok(out_f), Ok(err_f)) = (open(&out), open(&err)) else {
        note(&format!("disky job {name}: can't open {} / {}", out.display(), err.display()));
        return 1;
    };
    let mut cmd = Command::new(program);
    cmd.args(args).envs(&job.env).stdout(out_f).stderr(err_f);
    if name != "scan" {
        return crate::agent::run_child(cmd);
    }
    use crate::settings;
    cmd.env("DISKY_SCAN_ROOT", settings::load().scan_root());
    let mut st = settings::load_state();
    st.last_start = Some(settings::now());
    st.force = false;
    let _ = settings::save_state(&st);
    let code = crate::agent::run_child(cmd);
    let mut st = settings::load_state();
    st.last_end = Some(settings::now());
    st.last_exit = Some(code);
    let _ = settings::save_state(&st);
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
    }
}
