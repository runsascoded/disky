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
        return format!("Scanning…{next}");
    }
    let when = st.last_end.map(|t| ago(Duration::from_secs((now - t).max(0) as u64)));
    match (st.last_exit, when) {
        (Some(0), Some(w)) => format!("Scanned {w}{next}"),
        (Some(code), Some(w)) => format!("Last scan failed (exit {code}) {w}{next}"),
        _ => format!("Not scanned yet{next}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ago_units() {
        let f = |s| ago(Duration::from_secs(s));
        assert_eq!([f(5), f(90), f(3 * 3600 + 5), f(3 * 86_400)], ["just now", "1m ago", "3h ago", "3d ago"]);
    }
}
