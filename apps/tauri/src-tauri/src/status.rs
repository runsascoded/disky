//! What the menu bar says about the scheduled scan: launchd (running?), the
//! run state `disky job scan` records (last start/end/exit), and the schedule
//! in settings.

use std::process::Command;
use std::time::Duration;

use crate::settings;

/// The bundled scan agent (`SMAppService`-registered; `services.rs`).
pub const SCAN_LABEL: &str = "com.runsascoded.disky.scan";

pub fn agents_dir() -> std::path::PathBuf {
    std::path::PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join("Library/LaunchAgents")
}

pub fn uid() -> u32 {
    unsafe { libc::getuid() }
}

pub struct JobState {
    pub loaded: bool,
    pub running: bool,
}

pub fn job_state(label: &str) -> JobState {
    let out = Command::new("launchctl").args(["print", &format!("gui/{}/{label}", uid())]).output();
    match out {
        Ok(o) if o.status.success() => {
            let text = String::from_utf8_lossy(&o.stdout);
            JobState { loaded: true, running: text.lines().any(|l| l.trim() == "state = running") }
        }
        _ => JobState { loaded: false, running: false },
    }
}

/// "Scan now": set the force flag, then wake the agent (which runs the scan
/// regardless of schedule and clears the flag).
pub fn scan_now() -> Result<(), String> {
    let mut st = settings::load_state();
    st.force = true;
    settings::save_state(&st)?;
    Command::new("launchctl")
        .args(["kickstart", &format!("gui/{}/{SCAN_LABEL}", uid())])
        .status()
        .map(|_| ())
        .map_err(|e| e.to_string())
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

/// The menu's scan line: "Scanned 3h ago · next 18:00", "Scanning…",
/// "Last scan failed (exit 1) 2h ago · next 06:00", "Scheduled scans off".
pub fn scan_line() -> String {
    let job = job_state(SCAN_LABEL);
    let s = settings::load();
    let st = settings::load_state();
    let now = settings::now();
    let scheduled = job.loaded && !s.times().is_empty();
    let next = if scheduled {
        settings::next_slot(&s.times(), now).map(|(h, m)| format!(" · next {h:02}:{m:02}")).unwrap_or_default()
    } else {
        " · scheduled scans off".to_string()
    };
    let in_flight = st.last_start.is_some_and(|start| st.last_end.is_none_or(|end| end < start));
    if job.running && in_flight {
        return match settings::load_progress() {
            Some(p) => progress_line(&p, now),
            None => format!("Scanning…{next}"),
        };
    }
    if st.force {
        return "Starting scan…".to_string();
    }
    let when = st.last_end.map(|t| ago(Duration::from_secs((now - t).max(0) as u64)));
    match (st.last_exit, when) {
        (Some(0), Some(w)) => format!("Scanned {w}{next}"),
        (Some(code), Some(w)) => format!("Last scan failed (exit {code}) {w}{next}"),
        _ => format!("Not scanned yet{next}"),
    }
}

/// `850K`, `3.2M` files.
pub fn count(n: u64) -> String {
    match n {
        0..=999 => n.to_string(),
        1_000..=999_999 => format!("{}K", n / 1_000),
        _ => format!("{:.1}M", n as f64 / 1e6),
    }
}

/// `512 MiB`, `210 GiB`, `1.2 TiB`.
pub fn bytes(n: u64) -> String {
    const GI: f64 = (1u64 << 30) as f64;
    let g = n as f64 / GI;
    if g >= 1024.0 {
        format!("{:.1} TiB", g / 1024.0)
    } else if g >= 1.0 {
        format!("{g:.0} GiB")
    } else {
        format!("{} MiB", n >> 20)
    }
}

/// A running scan: "Scanning: 3.2M files, 210 GiB (2m)", then "Captured 7.4M
/// files, 421 GiB · finishing (6m)" while the follow-on step (the ingest
/// submit) runs.
pub fn progress_line(p: &settings::Progress, now: i64) -> String {
    let secs = (now - p.since).max(0) as u64;
    let took = if secs < 60 { format!("{secs}s") } else { format!("{}m", secs / 60) };
    let (files, size) = (count(p.files), bytes(p.bytes));
    match p.phase.as_str() {
        "then" => format!("Captured {files} files, {size} · finishing ({took})"),
        _ => format!("Scanning: {files} files, {size} ({took})"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn progress_lines() {
        let p = |phase: &str, files, bytes| settings::Progress { phase: phase.into(), files, bytes, since: 1000 };
        assert_eq!(progress_line(&p("capture", 0, 0), 1005), "Scanning: 0 files, 0 MiB (5s)");
        assert_eq!(progress_line(&p("capture", 850_123, 3 << 29), 1130), "Scanning: 850K files, 2 GiB (2m)");
        assert_eq!(
            progress_line(&p("then", 7_733_883, 421 << 30), 1000 + 330),
            "Captured 7.7M files, 421 GiB · finishing (5m)"
        );
        assert_eq!(bytes(1300 << 30), "1.3 TiB");
    }

    #[test]
    fn ago_units() {
        let f = |s| ago(Duration::from_secs(s));
        assert_eq!([f(5), f(90), f(3 * 3600 + 5), f(3 * 86_400)], ["just now", "1m ago", "3h ago", "3d ago"]);
    }
}
