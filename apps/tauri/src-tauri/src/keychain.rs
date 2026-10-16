//! The app's secrets in the login Keychain (generic passwords, one service
//! name each): the file tunnel's connector token, Pushover's keys. Through
//! `/usr/bin/security`, so an item the app writes it can read back without a
//! prompt; values go in through `security -i`'s stdin, never a process's
//! arguments, and are never logged.

use std::io::Write;
use std::process::{Command, Stdio};

/// The value stored under `service`; `None` when absent.
pub fn get(service: &str) -> Option<String> {
    let out = Command::new("/usr/bin/security").args(["find-generic-password", "-s", service, "-w"]).stderr(Stdio::null()).output().ok()?;
    let v = String::from_utf8(out.stdout).ok()?.trim().to_string();
    (out.status.success() && !v.is_empty()).then_some(v)
}

/// Store `value` under `service` (replacing any), labelled `label` in Keychain Access.
pub fn set(service: &str, label: &str, value: &str) -> Result<(), String> {
    if value.is_empty() || value.contains(char::is_whitespace) || value.contains('"') || value.contains('\\') {
        return Err("expected one token (no spaces or quotes)".into());
    }
    let account = std::env::var("USER").unwrap_or_else(|_| "disky".into());
    let mut child = Command::new("/usr/bin/security").arg("-i").stdin(Stdio::piped()).stdout(Stdio::null()).stderr(Stdio::piped()).spawn().map_err(|e| e.to_string())?;
    let line = format!("add-generic-password -U -s \"{service}\" -a \"{account}\" -l \"{label}\" -w \"{value}\"\n");
    child.stdin.take().unwrap().write_all(line.as_bytes()).map_err(|e| e.to_string())?;
    let out = child.wait_with_output().map_err(|e| e.to_string())?;
    let msg = String::from_utf8_lossy(&out.stderr).trim().to_string();
    if out.status.success() && msg.is_empty() { Ok(()) } else { Err(format!("security: {msg}")) }
}

/// Remove `service`'s item; absent is fine.
pub fn delete(service: &str) {
    let _ = Command::new("/usr/bin/security").args(["delete-generic-password", "-s", service]).stdout(Stdio::null()).stderr(Stdio::null()).status();
}
