//! "Connect this Mac" (spec `device-enrollment.md` phase 3; the API is m3's
//! `specs/device-api.md`): enroll with the site and upload every scan's capture
//! through presigned URLs, with no bucket key and no index on this Mac.
//!
//! Enrollment is the signed-in admin's call: the app sends `POST /api/hosts`
//! itself, with the site window's session cookie and the site's `Origin` (the
//! route's same-origin check), so the device token never passes through page
//! script or a URL. It goes straight to the Keychain; `disky.json` keeps only
//! `settings.connect`.

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

/// `POST <site>/api/hosts` as the admin whose session `cookie` carries:
/// `(id, store, token)`.
pub fn enroll(site: &str, cookie: &str, body: &Value) -> Result<(String, String, String), String> {
    let r = ureq::post(&format!("{site}/api/hosts"))
        .set("Cookie", cookie)
        .set("Origin", site)
        .set("Content-Type", "application/json")
        .timeout(std::time::Duration::from_secs(20))
        .send_string(&body.to_string());
    match r {
        Ok(r) => answer(r.status(), &r.into_string().unwrap_or_default()),
        Err(ureq::Error::Status(code, r)) => answer(code, &r.into_string().unwrap_or_default()),
        Err(e) => Err(format!("couldn't reach {site}: {e}")),
    }
}

/// What the enrollment's `status` + `body` mean.
pub fn answer(status: u16, body: &str) -> Result<(String, String, String), String> {
    let v: Value = serde_json::from_str(body).unwrap_or(Value::Null);
    let field = |k: &str| v.get(k).and_then(Value::as_str).map(str::to_string);
    match (status, field("id"), field("store"), field("token")) {
        (200 | 201, Some(id), Some(store), Some(token)) => Ok((id, store, token)),
        (401 | 403, ..) => Err("sign in to the site as an admin in the disky window, then Connect again".into()),
        (409, ..) => Err(format!("that name belongs to a host that can't be re-enrolled ({})", field("error").unwrap_or_default())),
        (s, ..) => Err(format!("the site answered {s}: {}", field("error").unwrap_or_else(|| body.trim().chars().take(200).collect()))),
    }
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

    #[test]
    fn answers() {
        assert_eq!(answer(201, r#"{"id":"m1","store":"m1","token":"t"}"#), Ok(("m1".into(), "m1".into(), "t".into())));
        assert_eq!(answer(200, r#"{"id":"m1","store":"m1","token":"t2"}"#), Ok(("m1".into(), "m1".into(), "t2".into())));
        assert_eq!(answer(403, "{}"), Err("sign in to the site as an admin in the disky window, then Connect again".into()));
        assert_eq!(answer(409, r#"{"error":"primary host"}"#), Err("that name belongs to a host that can't be re-enrolled (primary host)".into()));
        assert_eq!(answer(400, r#"{"error":"bad host"}"#), Err("the site answered 400: bad host".into()));
        assert_eq!(answer(502, "upstream\n"), Err("the site answered 502: upstream".into()));
    }
}
