//! "Connect this Mac" (spec `device-enrollment.md` phase 3; the API is m3's
//! `specs/device-api.md`): enroll with the site and upload every scan's capture
//! through presigned URLs, with no bucket key and no index on this Mac.
//!
//! Enrollment is the signed-in admin's call, so it runs in the site window:
//! the app injects a same-origin `fetch('POST /api/hosts')` and the page hands
//! the answer back as `disky://connected?n=<nonce>&s=<status>&b=<body>`, a
//! navigation the window intercepts (never a LaunchServices link). The nonce is
//! minted per attempt, so no page can forge a connection. The device token goes
//! straight to the Keychain; `disky.json` keeps only `settings.connect`.

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::{keychain, settings};

pub const KEYCHAIN_SERVICE: &str = "disky-device-token";

/// `settings.connect`: the site this Mac is enrolled with, and as what.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Connect {
    /// The site's origin, e.g. `https://disk.rbw.sh`.
    pub site: String,
    /// The host id (= its store key; the site's `/<store>` page).
    pub id: String,
    pub store: String,
}

/// This Mac's enrollment, when it has one and its token.
pub fn device() -> Option<dt_capture::device::Device> {
    let c = settings::load().connect?;
    let token = keychain::get(KEYCHAIN_SERVICE)?;
    Some(dt_capture::device::Device { site: c.site, id: c.id, token })
}

/// A host id the API accepts (`^[a-z0-9][a-z0-9-]{0,31}$`) from a computer
/// name: lowercased, runs of anything else → `-`, trimmed, ≤ 32.
pub fn host_slug(name: &str) -> String {
    let mut s = String::new();
    for c in name.chars().flat_map(char::to_lowercase) {
        if c.is_ascii_alphanumeric() {
            s.push(c);
        } else if !s.is_empty() && !s.ends_with('-') {
            s.push('-');
        }
    }
    let s: String = s.chars().take(32).collect();
    let s = s.trim_end_matches('-');
    if s.is_empty() { "mac".into() } else { s.into() }
}

/// The enrollment request body.
pub fn enroll_body(host: &str, label: &str) -> Value {
    let user = std::env::var("USER").unwrap_or_default();
    json!({
        "host": host,
        "label": label,
        "home": format!("Users/{user}"),
        "os": format!("macOS {}", os_version()),
        "version": env!("CARGO_PKG_VERSION"),
    })
}

fn os_version() -> String {
    std::process::Command::new("/usr/bin/sw_vers")
        .arg("-productVersion")
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_default()
}

/// The script the site window runs: POST the enrollment same-origin (the
/// admin's session cookie), then navigate to the callback with the answer.
pub fn enroll_script(nonce: &str, body: &Value) -> String {
    let body = serde_json::to_string(&body.to_string()).unwrap();
    let nonce = serde_json::to_string(nonce).unwrap();
    format!(
        "(async () => {{ let s = 0, b = ''; try {{ const r = await fetch('/api/hosts', {{method: 'POST', credentials: 'same-origin', headers: {{'content-type': 'application/json'}}, body: {body}}}); s = r.status; b = await r.text(); }} catch (e) {{ b = String(e); }} location.href = 'disky://connected?n=' + encodeURIComponent({nonce}) + '&s=' + s + '&b=' + encodeURIComponent(b); }})();"
    )
}

/// What a `disky://connected` callback says, given the nonce we're waiting on.
#[derive(Debug, PartialEq)]
pub enum Enrolled {
    Ok { id: String, store: String, token: String },
    Failed(String),
}

pub fn parse_callback(url: &tauri::Url, nonce: Option<&str>) -> Result<Enrolled, String> {
    let q: std::collections::HashMap<_, _> = url.query_pairs().collect();
    match (nonce, q.get("n")) {
        (Some(want), Some(got)) if want == got.as_ref() => {}
        _ => return Err("a connect callback without a pending connection".into()),
    }
    let status: u16 = q.get("s").and_then(|s| s.parse().ok()).unwrap_or(0);
    let body = q.get("b").map(|b| b.to_string()).unwrap_or_default();
    let v: Value = serde_json::from_str(&body).unwrap_or(Value::Null);
    let field = |k: &str| v.get(k).and_then(Value::as_str).map(str::to_string);
    Ok(match (status, field("id"), field("store"), field("token")) {
        (200 | 201, Some(id), Some(store), Some(token)) => Enrolled::Ok { id, store, token },
        (0, ..) => Enrolled::Failed(format!("couldn't reach the site: {body}")),
        (401 | 403, ..) => Enrolled::Failed("sign in to the site as an admin in the disky window, then Connect again".into()),
        (409, ..) => Enrolled::Failed(format!("that name is taken by a host that can't be re-enrolled ({})", field("error").unwrap_or_default())),
        (s, ..) => Enrolled::Failed(format!("the site answered {s}: {}", field("error").unwrap_or(body))),
    })
}

/// Save a successful enrollment: the token to the Keychain, the rest to `disky.json`.
pub fn save(site: &str, id: String, store: String, token: &str) -> Result<(), String> {
    keychain::set(KEYCHAIN_SERVICE, "disky device token", token)?;
    let mut s = settings::load();
    s.connect = Some(Connect { site: site.into(), id, store });
    settings::save(&s)
}

/// The site's view of this host: `GET /api/hosts/<id>`.
pub fn status() -> Result<Value, String> {
    let d = device().ok_or("not connected")?;
    let r = ureq::get(&format!("{}/api/hosts/{}", d.site, d.id))
        .set("Authorization", &format!("Bearer {}", d.token))
        .timeout(std::time::Duration::from_secs(10))
        .call();
    match r {
        Ok(r) => serde_json::from_str(&r.into_string().map_err(|e| e.to_string())?).map_err(|e| e.to_string()),
        Err(ureq::Error::Status(401, _)) => Err("the site refused this Mac's token (revoked): disconnect and connect again".into()),
        Err(e) => Err(e.to_string()),
    }
}

/// Revoke the token at the site (best effort: offline still disconnects
/// here), then forget it. Returns the site's answer or why it failed.
pub fn disconnect() -> Result<String, String> {
    let revoked = match device() {
        Some(d) => match ureq::delete(&format!("{}/api/hosts/{}", d.site, d.id))
            .set("Authorization", &format!("Bearer {}", d.token))
            .timeout(std::time::Duration::from_secs(10))
            .call()
        {
            Ok(_) | Err(ureq::Error::Status(401, _)) => "revoked at the site".to_string(),
            Err(e) => format!("not revoked at the site ({e}); revoke it there"),
        },
        None => "nothing to revoke".to_string(),
    };
    keychain::delete(KEYCHAIN_SERVICE);
    let mut s = settings::load();
    s.connect = None;
    settings::save(&s)?;
    Ok(revoked)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn host_slugs() {
        assert_eq!(
            [host_slug("Ryan's MacBook Air"), host_slug("m1"), host_slug("--Büro  Mac--"), host_slug("!!!"), host_slug(&"x".repeat(40))],
            ["ryan-s-macbook-air", "m1", "b-ro-mac", "mac", &"x".repeat(32)],
        );
    }

    fn cb(q: &str) -> tauri::Url {
        format!("disky://connected?{q}").parse().unwrap()
    }

    #[test]
    fn callbacks() {
        let ok = cb("n=abc&s=201&b=%7B%22id%22%3A%22m1%22%2C%22store%22%3A%22m1%22%2C%22token%22%3A%22t%22%7D");
        assert_eq!(parse_callback(&ok, Some("abc")), Ok(Enrolled::Ok { id: "m1".into(), store: "m1".into(), token: "t".into() }));
        assert_eq!(parse_callback(&ok, Some("other")), Err("a connect callback without a pending connection".into()));
        assert_eq!(parse_callback(&ok, None), Err("a connect callback without a pending connection".into()));
        assert_eq!(
            parse_callback(&cb("n=abc&s=403&b=%7B%7D"), Some("abc")),
            Ok(Enrolled::Failed("sign in to the site as an admin in the disky window, then Connect again".into()))
        );
        assert_eq!(
            parse_callback(&cb("n=abc&s=400&b=%7B%22error%22%3A%22bad%20host%22%7D"), Some("abc")),
            Ok(Enrolled::Failed("the site answered 400: bad host".into()))
        );
    }

    #[test]
    fn script_quotes_its_inputs() {
        assert_eq!(
            enroll_script("n'1", &json!({"host": "a\"b"})),
            r#"(async () => { let s = 0, b = ''; try { const r = await fetch('/api/hosts', {method: 'POST', credentials: 'same-origin', headers: {'content-type': 'application/json'}, body: "{\"host\":\"a\\\"b\"}"}); s = r.status; b = await r.text(); } catch (e) { b = String(e); } location.href = 'disky://connected?n=' + encodeURIComponent("n'1") + '&s=' + s + '&b=' + encodeURIComponent(b); })();"#,
        );
    }
}
