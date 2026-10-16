//! File previews through a Cloudflare Tunnel (spec `laptop-file-tunnel.md`
//! phase 4): with `settings.files` set, the local server answers
//! `/files/<path>` to Access-signed requests (`dt_index::files`), and while
//! `files.on` the app runs `cloudflared tunnel --no-autoupdate run` as a child,
//! restarting it when it exits.
//!
//! ```json
//! {"settings": {"files": {"on": true, "team": "<team>.cloudflareaccess.com", "aud": "<AUD tag>", "client_id": "<id>.access"}}}
//! ```
//!
//! The connector token is a secret, so it is never in `disky.json`: it lives
//! in the login Keychain (generic password, service `disky-files-tunnel`),
//! stored by `disky tunnel token` from stdin, read at each spawn, and handed
//! to the child as `TUNNEL_TOKEN` (env, not argv).

use std::io::{Read, Write};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

use crate::{jobs, settings};

pub const KEYCHAIN_SERVICE: &str = "disky-files-tunnel";
/// `cloudflared`'s metrics listener: `/ready` answers 200 once a connection to the edge is up.
const METRICS: &str = "127.0.0.1:7794";

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct FilesTunnel {
    #[serde(default)]
    pub on: bool,
    /// The Access team domain (`<team>.cloudflareaccess.com`).
    pub team: String,
    /// The Access application's AUD tag.
    pub aud: String,
    /// The service token's client id: the only identity `/files` accepts.
    pub client_id: String,
    /// The store root `/files/<path>` is relative to: `/` for the laptop store.
    #[serde(default = "root_default")]
    pub root: String,
}

fn root_default() -> String {
    "/".into()
}

/// `/files` for the local server, when configured (whether or not the tunnel runs).
pub fn server_files(s: &settings::Settings) -> Option<dt_index::files::Files> {
    let f = s.files.as_ref()?;
    let access = dt_index::access::Access::new(&f.team, &f.aud, Some(f.client_id.clone()));
    match dt_index::files::Files::new(std::path::Path::new(&f.root), access) {
        Ok(files) => Some(files.with_health(Box::new(health))),
        Err(e) => {
            jobs::note(&format!("disky: file previews root {}: {e}", f.root));
            None
        }
    }
}

/// The health check's extras (`dt_index::files`), for the site's "next scan" beside
/// "scanned …": the scan schedule (local `HH:MM`; empty while scheduled scans are
/// off) and this Mac's IANA time zone.
fn health() -> serde_json::Map<String, serde_json::Value> {
    let schedule: Vec<String> = if crate::services::agents_enabled() {
        settings::load().times().iter().map(|(h, m)| format!("{h:02}:{m:02}")).collect()
    } else {
        vec![]
    };
    let mut o = serde_json::Map::new();
    o.insert("schedule".into(), schedule.into());
    if let Some(tz) = local_tz() {
        o.insert("tz".into(), tz.into());
    }
    o
}

/// `America/New_York`, from where `/etc/localtime` points (`…/zoneinfo/America/New_York`).
fn local_tz() -> Option<String> {
    let link = std::fs::read_link("/etc/localtime").ok()?;
    let s = link.to_string_lossy();
    s.split_once("zoneinfo/").map(|(_, tz)| tz.to_string())
}

/// `cloudflared` on PATH or in Homebrew's prefixes (a GUI app's PATH is launchd's, without Homebrew).
pub fn cloudflared() -> Option<PathBuf> {
    let mut dirs: Vec<PathBuf> = std::env::var_os("PATH").map(|p| std::env::split_paths(&p).collect()).unwrap_or_default();
    dirs.extend(["/opt/homebrew/bin", "/usr/local/bin"].map(PathBuf::from));
    dirs.into_iter().map(|d| d.join("cloudflared")).find(|p| p.is_file())
}

/// Whether the Keychain holds a connector token (the value never leaves `token`).
pub fn has_token() -> bool {
    token().is_some()
}

/// The connector token from the Keychain; `None` when absent. Never logged.
fn token() -> Option<String> {
    let out = Command::new("/usr/bin/security").args(["find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"]).stderr(Stdio::null()).output().ok()?;
    let t = String::from_utf8(out.stdout).ok()?.trim().to_string();
    (out.status.success() && !t.is_empty()).then_some(t)
}

/// Store the token in the Keychain (replacing any), through `security -i`'s
/// stdin so it never appears in a process's arguments.
fn store_token(t: &str) -> Result<(), String> {
    if t.is_empty() || t.contains(char::is_whitespace) || t.contains('"') {
        return Err("expected one token on stdin".into());
    }
    let account = std::env::var("USER").unwrap_or_else(|_| "disky".into());
    let mut child = Command::new("/usr/bin/security").arg("-i").stdin(Stdio::piped()).stdout(Stdio::null()).stderr(Stdio::piped()).spawn().map_err(|e| e.to_string())?;
    let line = format!("add-generic-password -U -s {KEYCHAIN_SERVICE} -a \"{account}\" -l \"disky file previews tunnel\" -w \"{t}\"\n");
    child.stdin.take().unwrap().write_all(line.as_bytes()).map_err(|e| e.to_string())?;
    let out = child.wait_with_output().map_err(|e| e.to_string())?;
    let msg = String::from_utf8_lossy(&out.stderr).trim().to_string();
    if out.status.success() && msg.is_empty() { Ok(()) } else { Err(format!("security: {msg}")) }
}

fn pid_path() -> PathBuf {
    jobs::config_path().with_file_name("disky-tunnel.pid")
}

/// A `cloudflared` a previous app instance left running (killed without its exit hook): stop it.
fn reap_stale() {
    let Some(pid) = std::fs::read_to_string(pid_path()).ok().and_then(|s| s.trim().parse::<i32>().ok()) else { return };
    let comm = Command::new("/bin/ps").args(["-p", &pid.to_string(), "-o", "comm="]).output().map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string()).unwrap_or_default();
    if comm.ends_with("cloudflared") {
        unsafe { libc::kill(pid, libc::SIGTERM) };
        jobs::note(&format!("disky: stopped a stale cloudflared ({pid})"));
    }
    let _ = std::fs::remove_file(pid_path());
}

/// Whether `cloudflared` reports a live edge connection (`GET /ready` → 200).
fn ready() -> bool {
    let Ok(mut s) = std::net::TcpStream::connect_timeout(&METRICS.parse().unwrap(), Duration::from_millis(300)) else { return false };
    let _ = s.set_read_timeout(Some(Duration::from_secs(1)));
    if s.write_all(b"GET /ready HTTP/1.0\r\nHost: localhost\r\n\r\n").is_err() {
        return false;
    }
    let mut head = [0u8; 12];
    s.read_exact(&mut head).is_ok() && &head[9..12] == b"200"
}

#[derive(Clone, Debug, PartialEq)]
pub enum State {
    NotConfigured,
    Off,
    NoCloudflared,
    NoToken,
    Starting,
    Connected,
    /// Exited; restarting after a backoff.
    Retrying(String),
}

/// The menu's line for `state`.
pub fn menu_line(state: &State) -> String {
    match state {
        State::NotConfigured => "File previews: not set up".into(),
        State::Off => "File previews".into(),
        State::NoCloudflared => "File previews: cloudflared not found".into(),
        State::NoToken => "File previews: no tunnel token (disky tunnel token)".into(),
        State::Starting => "File previews: connecting…".into(),
        State::Connected => "File previews: connected".into(),
        State::Retrying(why) => format!("File previews: {why}, retrying"),
    }
}

/// The supervised child and what the menu shows.
pub struct Tunnel {
    child: Mutex<Option<Child>>,
    state: Mutex<State>,
    backoff: Mutex<(Duration, Option<Instant>)>,
    /// Epoch seconds the current connection came up.
    since: Mutex<Option<i64>>,
}

impl Tunnel {
    pub fn new() -> Self {
        reap_stale();
        Tunnel { child: Mutex::new(None), state: Mutex::new(State::NotConfigured), backoff: Mutex::new((Duration::from_secs(5), None)), since: Mutex::new(None) }
    }

    pub fn state(&self) -> State {
        self.state.lock().unwrap().clone()
    }

    pub fn connected_since(&self) -> Option<i64> {
        *self.since.lock().unwrap()
    }

    fn set(&self, s: State) {
        let mut since = self.since.lock().unwrap();
        match (&s, *since) {
            (State::Connected, None) => *since = Some(settings::now()),
            (State::Connected, Some(_)) => {}
            _ => *since = None,
        }
        *self.state.lock().unwrap() = s;
    }

    pub fn stop(&self) {
        if let Some(mut c) = self.child.lock().unwrap().take() {
            unsafe { libc::kill(c.id() as i32, libc::SIGTERM) };
            let deadline = Instant::now() + Duration::from_secs(3);
            while Instant::now() < deadline && matches!(c.try_wait(), Ok(None)) {
                std::thread::sleep(Duration::from_millis(100));
            }
            let _ = c.kill();
            let _ = c.wait();
            let _ = std::fs::remove_file(pid_path());
        }
    }

    /// One supervision step (every few seconds): match the child to the settings.
    pub fn tick(&self) {
        let s = settings::load();
        if !s.files.as_ref().is_some_and(|f| f.on) {
            self.stop();
            return self.set(if s.files.is_some() { State::Off } else { State::NotConfigured });
        }
        let mut guard = self.child.lock().unwrap();
        if let Some(c) = guard.as_mut() {
            match c.try_wait() {
                Ok(None) => {
                    drop(guard);
                    let up = ready();
                    if up {
                        *self.backoff.lock().unwrap() = (Duration::from_secs(5), None);
                    }
                    return self.set(if up { State::Connected } else { State::Starting });
                }
                Ok(Some(status)) => {
                    jobs::note(&format!("disky: cloudflared exited ({status})"));
                    *guard = None;
                    let _ = std::fs::remove_file(pid_path());
                    let mut b = self.backoff.lock().unwrap();
                    b.1 = Some(Instant::now() + b.0);
                    b.0 = (b.0 * 2).min(Duration::from_secs(300));
                    drop(b);
                    drop(guard);
                    self.set(State::Retrying(format!("cloudflared exited ({})", status.code().map_or("signal".into(), |c| c.to_string()))));
                    return;
                }
                Err(e) => {
                    jobs::note(&format!("disky: cloudflared wait: {e}"));
                    return;
                }
            }
        }
        if self.backoff.lock().unwrap().1.is_some_and(|t| Instant::now() < t) {
            return;
        }
        let Some(bin) = cloudflared() else {
            drop(guard);
            return self.set(State::NoCloudflared);
        };
        let Some(token) = token() else {
            drop(guard);
            return self.set(State::NoToken);
        };
        let log = std::fs::OpenOptions::new().create(true).append(true).open(log_path());
        let mut cmd = Command::new(&bin);
        cmd.args(["tunnel", "--no-autoupdate", "--metrics", METRICS, "run"]).env("TUNNEL_TOKEN", token).stdin(Stdio::null());
        if let Ok(l) = log {
            if let Ok(l2) = l.try_clone() {
                cmd.stdout(l2);
            }
            cmd.stderr(l);
        }
        match cmd.spawn() {
            Ok(c) => {
                let _ = std::fs::write(pid_path(), c.id().to_string());
                jobs::note(&format!("disky: started cloudflared ({}) for file previews", c.id()));
                *guard = Some(c);
                drop(guard);
                self.set(State::Starting);
            }
            Err(e) => {
                drop(guard);
                jobs::note(&format!("disky: can't start {}: {e}", bin.display()));
                self.set(State::Retrying(format!("can't start cloudflared: {e}")));
                let mut b = self.backoff.lock().unwrap();
                b.1 = Some(Instant::now() + b.0);
                b.0 = (b.0 * 2).min(Duration::from_secs(300));
            }
        }
    }
}

/// Toggle `files.on` (the menu item); `Err` when not set up.
pub fn toggle() -> Result<bool, String> {
    let on = !settings::load().files.is_some_and(|f| f.on);
    set_on(on).map(|_| on)
}

/// Set `files.on` (the menu, the Settings window); `Err` when not set up.
pub fn set_on(on: bool) -> Result<(), String> {
    let mut s = settings::load();
    s.files.as_mut().ok_or("file previews aren't set up (disky tunnel set …)")?.on = on;
    settings::save(&s)
}

pub fn log_path() -> PathBuf {
    jobs::logs_dir().join("cloudflared.log")
}

/// `disky tunnel …`: set up and inspect file previews from a shell.
pub fn cli(args: &[String]) -> i32 {
    let usage = || {
        eprintln!("usage: disky tunnel set TEAM AUD CLIENT_ID [ROOT] | token (reads stdin) | on | off | status");
        2
    };
    let mut s = settings::load();
    // The app's next supervision tick (≤ 20 s) starts or stops `cloudflared`.
    let save = |s: &settings::Settings, done: &str| match settings::save(s) {
        Ok(()) => {
            println!("{done}");
            0
        }
        Err(e) => {
            eprintln!("disky tunnel: {e}");
            1
        }
    };
    match args.iter().map(String::as_str).collect::<Vec<_>>().as_slice() {
        ["set", team, aud, client_id, rest @ ..] if rest.len() <= 1 => {
            let on = s.files.as_ref().is_some_and(|f| f.on);
            s.files = Some(FilesTunnel { on, team: team.to_string(), aud: aud.to_string(), client_id: client_id.to_string(), root: rest.first().map_or_else(root_default, |r| r.to_string()) });
            save(&s, &format!("file previews set up for {team} (root {}); {}", s.files.as_ref().unwrap().root, if on { "on" } else { "off: disky tunnel on" }))
        }
        ["token"] => {
            let mut t = String::new();
            if let Err(e) = std::io::stdin().read_to_string(&mut t) {
                eprintln!("disky tunnel token: {e}");
                return 1;
            }
            match store_token(t.trim()) {
                Ok(()) => {
                    println!("stored in the Keychain ({KEYCHAIN_SERVICE})");
                    0
                }
                Err(e) => {
                    eprintln!("disky tunnel token: {e}");
                    1
                }
            }
        }
        [v @ ("on" | "off")] => match s.files.as_mut() {
            Some(f) => {
                f.on = *v == "on";
                let done = if f.on { "file previews on: the app starts cloudflared within 20 s" } else { "file previews off: the app stops cloudflared within 20 s" };
                save(&s, done)
            }
            None => {
                eprintln!("disky tunnel: not set up (disky tunnel set TEAM AUD CLIENT_ID)");
                1
            }
        },
        ["status"] => {
            println!("settings: {}", s.files.as_ref().map_or("none".into(), |f| serde_json::to_string(f).unwrap()));
            println!("cloudflared: {}", cloudflared().map_or("not found".into(), |p| p.display().to_string()));
            println!("token: {}", if token().is_some() { "in Keychain" } else { "missing" });
            println!("connected: {}", ready());
            0
        }
        _ => usage(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn settings_shape() {
        let f: FilesTunnel = serde_json::from_str(r#"{"team": "t.cloudflareaccess.com", "aud": "a", "client_id": "c.access"}"#).unwrap();
        assert_eq!(f, FilesTunnel { on: false, team: "t.cloudflareaccess.com".into(), aud: "a".into(), client_id: "c.access".into(), root: "/".into() });
    }

    #[test]
    fn menu_lines() {
        assert_eq!(
            [State::NotConfigured, State::Off, State::Connected, State::Retrying("cloudflared exited (1)".into())].iter().map(menu_line).collect::<Vec<_>>(),
            ["File previews: not set up", "File previews", "File previews: connected", "File previews: cloudflared exited (1), retrying"],
        );
    }
}
