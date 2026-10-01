//! What the menu bar says about the scheduled scan: read from the agent's
//! LaunchAgent plist (schedule, log path) and `launchctl print` (running, last
//! exit). No state of our own: launchd and the log are the record.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, SystemTime};

/// The bundled scan agent (`SMAppService`-registered; `services.rs`).
pub const SCAN_LABEL: &str = "com.runsascoded.disky.scan";
/// The hand-written agent it replaces, reported on while it's still loaded.
pub const LEGACY_SCAN_LABEL: &str = "com.runsascoded.disk-tree.index";

pub fn uid() -> u32 {
    unsafe { libc::getuid() }
}

pub fn agents_dir() -> PathBuf {
    PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join("Library/LaunchAgents")
}

pub struct Agent {
    pub label: String,
    pub out_log: Option<PathBuf>,
    /// `StartCalendarInterval` entries as (hour, minute); empty if none.
    pub schedule: Vec<(u32, u32)>,
}

fn calendar(d: &plist::Dictionary) -> Vec<(u32, u32)> {
    let entries: Vec<plist::Value> = match d.get("StartCalendarInterval") {
        Some(plist::Value::Array(a)) => a.clone(),
        Some(v @ plist::Value::Dictionary(_)) => vec![v.clone()],
        _ => vec![],
    };
    entries
        .iter()
        .filter_map(|e| {
            let e = e.as_dictionary()?;
            let get = |k: &str| e.get(k).and_then(|v| v.as_unsigned_integer()).map(|n| n as u32);
            Some((get("Hour")?, get("Minute").unwrap_or(0)))
        })
        .collect()
}

/// This bundle's `Contents/Library/LaunchAgents/<label>.plist`.
fn bundled_plist(label: &str) -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let p = exe.parent()?.join(format!("../Library/LaunchAgents/{label}.plist"));
    p.is_file().then_some(p)
}

impl Agent {
    /// The scan agent: the bundled one when launchd has it loaded, else the
    /// legacy hand-written plist (before `agentctl adopt`).
    pub fn scan() -> Option<Agent> {
        if job_state(SCAN_LABEL).loaded {
            let d = plist::Value::from_file(bundled_plist(SCAN_LABEL)?).ok()?.into_dictionary()?;
            let cfg = crate::jobs::load().unwrap_or_default();
            let (out, _) = crate::jobs::log_paths("scan", cfg.jobs.get("scan"));
            return Some(Agent { label: SCAN_LABEL.to_string(), out_log: Some(out), schedule: calendar(&d) });
        }
        let v = plist::Value::from_file(agents_dir().join(format!("{LEGACY_SCAN_LABEL}.plist"))).ok()?;
        let d = v.as_dictionary()?;
        let out_log = d.get("StandardOutPath").and_then(|v| v.as_string()).map(PathBuf::from);
        Some(Agent { label: LEGACY_SCAN_LABEL.to_string(), out_log, schedule: calendar(d) })
    }
}

pub struct JobState {
    pub loaded: bool,
    pub running: bool,
    /// `last exit code = …` verbatim ("0", "1", "(never exited)").
    pub last_exit: Option<String>,
}

pub fn job_state(label: &str) -> JobState {
    let out = Command::new("launchctl")
        .args(["print", &format!("gui/{}/{label}", uid())])
        .output();
    let Ok(out) = out else {
        return JobState { loaded: false, running: false, last_exit: None };
    };
    if !out.status.success() {
        return JobState { loaded: false, running: false, last_exit: None };
    }
    let text = String::from_utf8_lossy(&out.stdout);
    let mut running = false;
    let mut last_exit = None;
    for line in text.lines() {
        let line = line.trim();
        if line == "state = running" {
            running = true;
        }
        if let Some(rest) = line.strip_prefix("last exit code = ") {
            last_exit = Some(rest.to_string());
        }
    }
    JobState { loaded: true, running, last_exit }
}

pub fn kickstart(label: &str) -> std::io::Result<()> {
    Command::new("launchctl")
        .args(["kickstart", &format!("gui/{}/{label}", uid())])
        .status()
        .map(|_| ())
}

/// "3h ago", "12m ago", "2d ago".
pub fn ago(d: Duration) -> String {
    let s = d.as_secs();
    match s {
        0..=59 => "just now".to_string(),
        60..=3599 => format!("{}m ago", s / 60),
        3600..=172_799 => format!("{}h ago", s / 3600),
        _ => format!("{}d ago", s / 86_400),
    }
}

fn modified(p: &Path) -> Option<SystemTime> {
    std::fs::metadata(p).ok()?.modified().ok()
}

/// Local (hour, minute) now.
fn local_hm() -> (u32, u32) {
    unsafe {
        let t = libc::time(std::ptr::null_mut());
        let mut tm: libc::tm = std::mem::zeroed();
        libc::localtime_r(&t, &mut tm);
        (tm.tm_hour as u32, tm.tm_min as u32)
    }
}

/// The next scheduled (hour, minute) after now, wrapping to tomorrow's first.
pub fn next_run(schedule: &[(u32, u32)], now: (u32, u32)) -> Option<(u32, u32)> {
    let mut s = schedule.to_vec();
    s.sort();
    s.iter().copied().find(|&hm| hm > now).or_else(|| s.first().copied())
}

/// The menu's scan line, e.g. "Scanned 3h ago · next 18:00", "Scanning…",
/// "Last scan failed (exit 1) 2h ago · next 06:00", "No scan agent".
pub fn scan_line() -> String {
    let Some(agent) = Agent::scan() else {
        return "No scheduled scan".to_string();
    };
    let job = job_state(&agent.label);
    let next = next_run(&agent.schedule, local_hm())
        .map(|(h, m)| format!(" · next {h:02}:{m:02}"))
        .unwrap_or_default();
    if !job.loaded {
        return format!("Scan agent not loaded{next}");
    }
    if job.running {
        return format!("Scanning…{next}");
    }
    let when = agent
        .out_log
        .as_deref()
        .and_then(modified)
        .and_then(|t| SystemTime::now().duration_since(t).ok())
        .map(ago);
    match (job.last_exit.as_deref(), when) {
        (Some("0"), Some(w)) => format!("Scanned {w}{next}"),
        (Some(code), Some(w)) if code.parse::<i32>().is_ok() => format!("Last scan failed (exit {code}) {w}{next}"),
        (_, Some(w)) => format!("Last scan {w}{next}"),
        (_, None) => format!("Not scanned yet{next}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn next_run_wraps() {
        let s = [(18, 0), (6, 0)];
        assert_eq!(next_run(&s, (5, 59)), Some((6, 0)));
        assert_eq!(next_run(&s, (6, 0)), Some((18, 0)));
        assert_eq!(next_run(&s, (20, 30)), Some((6, 0)));
        assert_eq!(next_run(&[], (1, 0)), None);
    }

    #[test]
    fn ago_units() {
        let f = |s| ago(Duration::from_secs(s));
        assert_eq!([f(5), f(90), f(3 * 3600 + 5), f(3 * 86_400)], ["just now", "1m ago", "3h ago", "3d ago"]);
    }
}
