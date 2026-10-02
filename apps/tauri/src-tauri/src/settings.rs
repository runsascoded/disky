//! User settings, in `disky.json`'s `settings` object (beside `jobs`):
//!
//! ```json
//! {"settings": {"site": "prod", "scope": "machine", "schedule": ["06:00", "18:00"]}, "jobs": {…}}
//! ```
//!
//! - `site`: `prod` (https://disk.rbw.sh), `dev` (https://dev.disk.rbw.sh), `local`
//!   (this Mac's own scans, served by the app on loopback), or a URL.
//!   `DISKY_URL` overrides.
//! - `scope`: `machine` (scan `/`, one filesystem) or `home` (`$HOME`); exported to
//!   the scan job as `DISKY_SCAN_ROOT`.
//! - `schedule`: local `HH:MM` times; the scan agent wakes every 15 min and runs
//!   when one has passed since its last run (`due`). Empty = scheduled scans off.
//!
//! Run state (last scheduled start, a "scan now" flag) is in `disky-state.json`.

use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const PROD_URL: &str = "https://disk.rbw.sh";
pub const DEV_URL: &str = "https://dev.disk.rbw.sh";
/// The app's own server (`dt_index::http`) over this Mac's local scans.
pub const LOCAL_ADDR: &str = "127.0.0.1:7792";
pub const LOCAL_URL: &str = "http://127.0.0.1:7792";

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
#[serde(default)]
pub struct Settings {
    pub site: String,
    pub scope: String,
    pub schedule: Vec<String>,
}

impl Default for Settings {
    fn default() -> Self {
        Settings { site: "prod".into(), scope: "machine".into(), schedule: vec!["06:00".into(), "18:00".into()] }
    }
}

impl Settings {
    pub fn site_url(&self) -> String {
        if let Ok(u) = std::env::var("DISKY_URL") {
            return u;
        }
        match self.site.as_str() {
            "prod" => PROD_URL.into(),
            "dev" => DEV_URL.into(),
            "local" => LOCAL_URL.into(),
            other => other.into(),
        }
    }

    pub fn scan_root(&self) -> String {
        match self.scope.as_str() {
            "home" => std::env::var("HOME").unwrap_or_else(|_| "/".into()),
            _ => "/".into(),
        }
    }

    /// Parsed `(hour, minute)` schedule entries; malformed ones are dropped.
    pub fn times(&self) -> Vec<(u32, u32)> {
        let mut t: Vec<(u32, u32)> = self.schedule.iter().filter_map(|s| parse_hm(s)).collect();
        t.sort();
        t.dedup();
        t
    }
}

pub fn parse_hm(s: &str) -> Option<(u32, u32)> {
    let (h, m) = s.trim().split_once(':')?;
    let (h, m) = (h.parse().ok()?, m.parse().ok()?);
    (h < 24 && m < 60).then_some((h, m))
}

fn read_json(p: &std::path::Path) -> Value {
    std::fs::read(p).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or(Value::Object(Default::default()))
}

fn write_json(p: &std::path::Path, v: &Value) -> Result<(), String> {
    if let Some(d) = p.parent() {
        std::fs::create_dir_all(d).map_err(|e| e.to_string())?;
    }
    let tmp = p.with_extension("json.tmp");
    std::fs::write(&tmp, serde_json::to_string_pretty(v).unwrap() + "\n").map_err(|e| e.to_string())?;
    std::fs::rename(&tmp, p).map_err(|e| e.to_string())
}

pub fn load() -> Settings {
    let v = read_json(&crate::jobs::config_path());
    v.get("settings").and_then(|s| serde_json::from_value(s.clone()).ok()).unwrap_or_default()
}

/// Whether `disky.json` has a `settings` object yet (first run when not).
pub fn exists() -> bool {
    read_json(&crate::jobs::config_path()).get("settings").is_some()
}

/// Write `s` into `disky.json`, keeping everything else (e.g. `jobs`).
pub fn save(s: &Settings) -> Result<(), String> {
    let p = crate::jobs::config_path();
    let mut v = read_json(&p);
    v.as_object_mut().ok_or("disky.json isn't an object")?.insert("settings".into(), serde_json::to_value(s).unwrap());
    write_json(&p, &v)
}

// --- run state -------------------------------------------------------------

#[derive(Serialize, Deserialize, Default, Clone, Debug, PartialEq)]
#[serde(default)]
pub struct ScanState {
    /// Epoch seconds of the last scan start (scheduled or forced).
    pub last_start: Option<i64>,
    /// "Scan now" was requested; the next agent wake runs regardless of schedule.
    pub force: bool,
    /// Epoch seconds the last scan ended, and its exit code.
    pub last_end: Option<i64>,
    pub last_exit: Option<i32>,
    /// The current run is the one automatic retry of an interrupted run.
    pub retried: bool,
}

impl ScanState {
    /// The last run didn't finish on its own: killed by a signal (exit 128+N,
    /// e.g. 143 when an app reinstall boots the agent out mid-scan), or the
    /// runner itself died before recording an end. Not running now: launchd
    /// never starts a job that is already running, so this is only asked
    /// between runs.
    pub fn interrupted(&self) -> bool {
        match (self.last_start, self.last_end, self.last_exit) {
            (_, _, Some(code)) if code >= 128 && self.last_end >= self.last_start => true,
            (Some(start), Some(end), _) => end < start,
            _ => false,
        }
    }
}

fn state_path() -> std::path::PathBuf {
    crate::jobs::config_path().with_file_name("disky-state.json")
}

pub fn load_state() -> ScanState {
    serde_json::from_value(read_json(&state_path()).get("scan").cloned().unwrap_or(Value::Null)).unwrap_or_default()
}

pub fn save_state(st: &ScanState) -> Result<(), String> {
    let p = state_path();
    let mut v = read_json(&p);
    v.as_object_mut().ok_or("disky-state.json isn't an object")?.insert("scan".into(), serde_json::to_value(st).unwrap());
    write_json(&p, &v)
}

/// A running in-process scan's progress (`disky-progress.json`), for the menu:
/// written by the scan job as it goes, removed when it ends.
#[derive(Serialize, Deserialize, Default, Clone, Debug, PartialEq)]
#[serde(default)]
pub struct Progress {
    /// `capture`, then `then` (the follow-on step, e.g. the ingest submit).
    pub phase: String,
    pub files: u64,
    pub bytes: u64,
    /// Epoch seconds the scan started.
    pub since: i64,
}

fn progress_path() -> std::path::PathBuf {
    crate::jobs::config_path().with_file_name("disky-progress.json")
}

pub fn load_progress() -> Option<Progress> {
    serde_json::from_slice(&std::fs::read(progress_path()).ok()?).ok()
}

pub fn save_progress(p: &Progress) {
    let _ = write_json(&progress_path(), &serde_json::to_value(p).unwrap());
}

pub fn clear_progress() {
    let _ = std::fs::remove_file(progress_path());
}

pub fn now() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)
}

/// Local broken-down time for epoch seconds `t`: (days since epoch in local
/// time, hour, minute), via `localtime_r`.
fn local(t: i64) -> (i64, u32, u32, i64) {
    unsafe {
        let tt = t as libc::time_t;
        let mut tm: libc::tm = std::mem::zeroed();
        libc::localtime_r(&tt, &mut tm);
        // seconds since local midnight
        let since_midnight = (tm.tm_hour as i64) * 3600 + (tm.tm_min as i64) * 60 + tm.tm_sec as i64;
        (t - since_midnight, tm.tm_hour as u32, tm.tm_min as u32, since_midnight)
    }
}

/// The most recent scheduled instant ≤ `now` (epoch seconds), looking back two
/// days; `None` for an empty schedule. Ignores DST's skipped/doubled hour.
pub fn last_slot(times: &[(u32, u32)], now: i64) -> Option<i64> {
    let (midnight, _, _, _) = local(now);
    (0..=2)
        .flat_map(|d| times.iter().map(move |&(h, m)| midnight - d * 86_400 + (h as i64) * 3600 + (m as i64) * 60))
        .filter(|&t| t <= now)
        .max()
}

/// The next scheduled `(hour, minute)` after now, wrapping to tomorrow.
pub fn next_slot(times: &[(u32, u32)], now: i64) -> Option<(u32, u32)> {
    let (_, h, m, _) = local(now);
    times.iter().copied().find(|&hm| hm > (h, m)).or_else(|| times.first().copied())
}

/// Whether a scheduled scan should start now: a slot has passed since the last
/// start. With no recorded start, not due (the first slot after install runs).
pub fn due(times: &[(u32, u32)], last_start: Option<i64>, now: i64) -> bool {
    match (last_slot(times, now), last_start) {
        (Some(slot), Some(last)) => slot > last,
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn midnight_today() -> i64 {
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs() as i64;
        local(now).0
    }

    #[test]
    fn parses_times() {
        assert_eq!(["06:00", "18:30", " 7:05 ", "24:00", "6", "ab:cd"].map(parse_hm), [Some((6, 0)), Some((18, 30)), Some((7, 5)), None, None, None]);
    }

    #[test]
    fn due_after_a_slot_passes() {
        let m = midnight_today();
        let t = [(6, 0), (18, 0)];
        let at = |h: i64, min: i64| m + h * 3600 + min * 60;
        // Last start 06:00:30; at 17:59 nothing new is due, at 18:00 it is.
        assert!(!due(&t, Some(at(6, 0) + 30), at(17, 59)));
        assert!(due(&t, Some(at(6, 0) + 30), at(18, 0)));
        // Asleep across both slots: one catch-up run on wake.
        assert!(due(&t, Some(at(6, 0) - 86_400), at(19, 15)));
        // Never ran: not due (no surprise scan at install); empty schedule: never.
        assert!(!due(&t, None, at(19, 15)));
        assert!(!due(&[], Some(0), at(19, 15)));
    }

    #[test]
    fn interrupted_runs() {
        let st = |last_start, last_end, last_exit| ScanState { last_start, last_end, last_exit, ..Default::default() };
        assert_eq!(
            [
                st(None, None, None),              // fresh install
                st(Some(100), None, None),         // baseline recorded, never ran
                st(Some(100), Some(200), Some(0)), // finished
                st(Some(100), Some(200), Some(1)), // failed on its own: no retry
                st(Some(100), Some(200), Some(143)), // SIGTERM'd (bootout)
                st(Some(300), Some(200), Some(0)), // runner died mid-run
            ]
            .map(|s| s.interrupted()),
            [false, false, false, false, true, true],
        );
    }

    #[test]
    fn next_slot_wraps() {
        let m = midnight_today();
        let t = [(6, 0), (18, 0)];
        assert_eq!(next_slot(&t, m + 5 * 3600), Some((6, 0)));
        assert_eq!(next_slot(&t, m + 6 * 3600), Some((18, 0)));
        assert_eq!(next_slot(&t, m + 20 * 3600), Some((6, 0)));
    }

    #[test]
    fn settings_round_trip_keeps_defaults() {
        let s: Settings = serde_json::from_str(r#"{"site": "dev"}"#).unwrap();
        assert_eq!(s, Settings { site: "dev".into(), ..Settings::default() });
        assert_eq!(s.times(), [(6, 0), (18, 0)]);
    }
}
