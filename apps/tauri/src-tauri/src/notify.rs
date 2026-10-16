//! Failure alerts (spec `scan-failure-notifications.md`): a macOS notification
//! when this app may post them. macOS refuses that to an app without a
//! Developer ID ("Notifications are not allowed for this application",
//! `UNErrorDomain` 1: the self-signed and ad-hoc builds), so otherwise a banner
//! through AppleScript (`display notification`, posted as Script Editor: a click
//! opens Script Editor, not the log) plus the `notify` fallback command in
//! `disky.json` (`disk-tree notify -f`: Pushover / Discord), with the title and
//! message appended:
//!
//! ```json
//! {"notify": {"command": ["/opt/homebrew/bin/direnv", "exec", "/…/wt/m3", "/…/wt/m3/.venv/bin/disk-tree", "notify", "-f"]}}
//! ```
//!
//! The fallback's credentials stay in that command's environment (direnv), not
//! in app config. With neither, an alert is only a line in `disky.log`.
//!
//! Pushover set up in the app (Settings → Alerts, or `disky pushover set`: the
//! user key and app token, in the Keychain) pushes every alert to the phone,
//! native notification or not, and replaces the `notify` command (which then only
//! runs if the push fails).
//!
//! The jobs post as the app (they're its bundle's executable), so one
//! authorization covers them; the app asks for it ([`ask`]) when a scan is
//! started from its menu or Settings, and at launch while scheduled scans are on.
//! Clicking an alert opens what it names (a log, a settings pane): the app's
//! delegate ([`install_delegate`]) reads it from the notification's `userInfo`.

use std::collections::BTreeMap;
use std::process::Command;
use std::ptr::NonNull;
use std::sync::mpsc;
use std::time::Duration;

use serde_json::Value;

use crate::jobs::{config_path, note, Step};

/// One alert. `key` names it for throttling (and replaces an earlier
/// notification with the same key); `open` is what a click opens.
pub struct Alert {
    pub key: String,
    pub title: String,
    pub body: String,
    pub open: Option<String>,
    /// Seconds within which the same key isn't sent again (0: always send).
    pub every: i64,
}

/// How alerts would go out now.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Route {
    /// macOS notifications, as disky.
    Native,
    /// macOS notifications, and Pushover.
    NativePush,
    /// Not asked yet (the next scan from the menu asks).
    Unasked,
    /// A Script Editor banner, and the push fallback.
    BannerPush,
    /// A Script Editor banner only (no fallback configured).
    Banner,
}

pub fn route() -> Route {
    match status() {
        Some(Auth::Allowed) if pushover_keys().is_some() => Route::NativePush,
        Some(Auth::Allowed) => Route::Native,
        Some(Auth::Unasked) => Route::Unasked,
        _ if pushover_keys().is_some() || fallback().is_some() => Route::BannerPush,
        _ => Route::Banner,
    }
}

/// The menu's line for [`route`].
pub fn menu_line(r: Route) -> &'static str {
    match r {
        Route::Native => "Failure alerts: notifications ✓",
        Route::NativePush => "Failure alerts: notifications + push ✓",
        Route::Unasked => "Failure alerts: allow notifications…",
        Route::BannerPush => "Failure alerts: banner + push",
        Route::Banner => "Failure alerts: banner only (no push)",
    }
}

/// Send `a` (unless the same key went out within `a.every`): natively when
/// allowed and the post succeeds, else through the fallback.
pub fn alert(a: &Alert) {
    let now = crate::settings::now();
    let mut sent = load_sent();
    if a.every > 0 && sent.get(&a.key).is_some_and(|t| now - t < a.every) {
        return;
    }
    let native = match status() {
        Some(Auth::Allowed) => post(a).map_err(|e| note(&format!("disky: notification failed ({e}); falling back"))).ok(),
        _ => None,
    };
    let local = match native {
        Some(()) => Some("notification".to_string()),
        None => banner(a),
    };
    // The phone: Pushover when set up in the app; the `notify` command when it isn't (or the push failed)
    // and no native notification went out.
    let push = pushover_keys().and_then(|k| send_pushover(&k, a)).or_else(|| native.is_none().then(|| send_fallback(a)).flatten());
    let parts: Vec<String> = [local, push].into_iter().flatten().collect();
    let how = (!parts.is_empty()).then(|| parts.join(" + "));
    note(&format!("disky alert [{}] {}: {} → {}", a.key, a.title, a.body, how.as_deref().unwrap_or("logged only")));
    if how.is_some() {
        sent.insert(a.key.clone(), now);
        // Keys a month old can't throttle anything.
        sent.retain(|_, t| now - *t < 30 * 86_400);
        save_sent(&sent);
    }
}

pub const PUSHOVER_USER: &str = "disky-pushover-user";
pub const PUSHOVER_TOKEN: &str = "disky-pushover-token";

/// Pushover's user key and app token, from the Keychain.
fn pushover_keys() -> Option<(String, String)> {
    Some((crate::keychain::get(PUSHOVER_USER)?, crate::keychain::get(PUSHOVER_TOKEN)?))
}

pub fn pushover_configured() -> bool {
    pushover_keys().is_some()
}

pub fn set_pushover(user: &str, token: &str) -> Result<(), String> {
    crate::keychain::set(PUSHOVER_USER, "disky Pushover user key", user.trim())?;
    crate::keychain::set(PUSHOVER_TOKEN, "disky Pushover app token", token.trim())
}

pub fn clear_pushover() {
    crate::keychain::delete(PUSHOVER_USER);
    crate::keychain::delete(PUSHOVER_TOKEN);
}

/// POST the alert to Pushover's messages API; `Some("push")` on success.
fn send_pushover((user, token): &(String, String), a: &Alert) -> Option<String> {
    let r = ureq::post("https://api.pushover.net/1/messages.json")
        .timeout(Duration::from_secs(15))
        .send_form(&[("token", token.as_str()), ("user", user.as_str()), ("title", a.title.as_str()), ("message", a.body.as_str())]);
    match r {
        Ok(_) => Some("push".into()),
        // The API's error body names the problem (`user key is invalid`); it never echoes the keys.
        Err(ureq::Error::Status(code, resp)) => {
            note(&format!("disky: pushover {code}: {}", resp.into_string().unwrap_or_default().trim()));
            None
        }
        Err(e) => {
            note(&format!("disky: pushover: {e}"));
            None
        }
    }
}

/// `disky pushover set` (reads `USER_KEY APP_TOKEN` from stdin) | `clear` | `status`.
pub fn pushover_cli(args: &[String]) -> i32 {
    match args.iter().map(String::as_str).collect::<Vec<_>>().as_slice() {
        ["set"] => {
            let mut s = String::new();
            if std::io::Read::read_to_string(&mut std::io::stdin(), &mut s).is_err() {
                eprintln!("disky pushover set: can't read stdin");
                return 1;
            }
            let parts: Vec<&str> = s.split_whitespace().collect();
            let [user, token] = parts.as_slice() else {
                eprintln!("disky pushover set: expected `USER_KEY APP_TOKEN` on stdin");
                return 2;
            };
            match set_pushover(user, token) {
                Ok(()) => {
                    println!("Pushover keys stored in the Keychain; alerts now push");
                    0
                }
                Err(e) => {
                    eprintln!("disky pushover set: {e}");
                    1
                }
            }
        }
        ["clear"] => {
            clear_pushover();
            println!("Pushover keys removed");
            0
        }
        ["status"] => {
            println!("pushover: {}", if pushover_configured() { "keys in Keychain" } else { "not set up" });
            println!("route: {}", menu_line(route()));
            0
        }
        _ => {
            eprintln!("usage: echo \"USER_KEY APP_TOKEN\" | disky pushover set | disky pushover clear | disky pushover status");
            2
        }
    }
}

fn fallback() -> Option<Step> {
    let v: Value = serde_json::from_slice(&std::fs::read(config_path()).ok()?).ok()?;
    serde_json::from_value::<Step>(v.get("notify")?.clone()).ok().filter(|s| !s.command.is_empty())
}

fn send_fallback(a: &Alert) -> Option<String> {
    let step = fallback()?;
    let (program, args) = step.command.split_first()?;
    let out = Command::new(program).args(args).arg("-t").arg(&a.title).arg(&a.body).envs(&step.env).output();
    match out {
        Ok(o) if o.status.success() => Some(format!("fallback ({program})")),
        Ok(o) => {
            let err = String::from_utf8_lossy(&o.stderr);
            note(&format!("disky: alert fallback exited {}: {}", o.status.code().unwrap_or(-1), err.trim().lines().last().unwrap_or("")));
            None
        }
        Err(e) => {
            note(&format!("disky: alert fallback {program}: {e}"));
            None
        }
    }
}

/// A banner through AppleScript (any process may post one; it shows as
/// Script Editor's).
fn banner(a: &Alert) -> Option<String> {
    let script = format!("display notification {} with title {}", applescript_str(&a.body), applescript_str(&a.title));
    match Command::new("/usr/bin/osascript").arg("-e").arg(&script).output() {
        Ok(o) if o.status.success() => Some("banner".into()),
        Ok(o) => {
            note(&format!("disky: alert banner: {}", String::from_utf8_lossy(&o.stderr).trim()));
            None
        }
        Err(e) => {
            note(&format!("disky: alert banner: {e}"));
            None
        }
    }
}

/// `s` as an AppleScript string literal.
fn applescript_str(s: &str) -> String {
    format!("\"{}\"", s.replace('\\', "\\\\").replace('"', "\\\""))
}

fn sent_path() -> std::path::PathBuf {
    config_path().with_file_name("disky-alerts.json")
}

fn load_sent() -> BTreeMap<String, i64> {
    std::fs::read(sent_path()).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default()
}

fn save_sent(m: &BTreeMap<String, i64>) {
    let _ = std::fs::write(sent_path(), serde_json::to_vec_pretty(m).unwrap());
}

// --- UserNotifications ----------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Auth {
    Allowed,
    Denied,
    Unasked,
}

/// What the person allowed; `None` when it can't be told (no answer in 5 s,
/// or not running from the app bundle).
#[cfg(target_os = "macos")]
pub fn status() -> Option<Auth> {
    use objc2_user_notifications::{UNAuthorizationStatus, UNNotificationSettings, UNUserNotificationCenter};
    in_bundle()?;
    let (tx, rx) = mpsc::channel();
    let block = block2::RcBlock::new(move |s: NonNull<UNNotificationSettings>| {
        let st = unsafe { s.as_ref() }.authorizationStatus();
        let _ = tx.send(st);
    });
    UNUserNotificationCenter::currentNotificationCenter().getNotificationSettingsWithCompletionHandler(&block);
    let st = rx.recv_timeout(Duration::from_secs(5)).ok()?;
    Some(match st {
        UNAuthorizationStatus::Authorized | UNAuthorizationStatus::Provisional => Auth::Allowed,
        UNAuthorizationStatus::NotDetermined => Auth::Unasked,
        _ => Auth::Denied,
    })
}

/// `UNUserNotificationCenter` raises outside an app bundle (`cargo test`, a
/// bare binary).
#[cfg(target_os = "macos")]
fn in_bundle() -> Option<()> {
    let exe = std::env::current_exe().ok()?;
    exe.parent()?.parent()?.join("Info.plist").exists().then_some(())
}

/// Ask for permission once (a no-op once answered).
#[cfg(target_os = "macos")]
pub fn ask() {
    if status() == Some(Auth::Unasked) {
        std::thread::spawn(|| note(&format!("disky: notifications {}", request().unwrap_or_else(|e| format!("not allowed: {e}")))));
    }
}

/// Request permission and wait for the answer: `allowed` / `declined`, or
/// the system's error.
#[cfg(target_os = "macos")]
pub fn request() -> Result<String, String> {
    use objc2_foundation::NSError;
    use objc2_user_notifications::{UNAuthorizationOptions, UNUserNotificationCenter};
    in_bundle().ok_or("not running from the app bundle")?;
    let (tx, rx) = mpsc::channel();
    let block = block2::RcBlock::new(move |ok: objc2::runtime::Bool, e: *mut NSError| {
        let _ = tx.send(match unsafe { e.as_ref() } {
            Some(e) => Err(format!("{} ({} {})", e.localizedDescription(), e.domain(), e.code())),
            None => Ok(if ok.as_bool() { "allowed" } else { "declined" }.to_string()),
        });
    });
    UNUserNotificationCenter::currentNotificationCenter()
        .requestAuthorizationWithOptions_completionHandler(UNAuthorizationOptions::Alert | UNAuthorizationOptions::Sound, &block);
    // The person may take a while to answer the prompt.
    rx.recv_timeout(Duration::from_secs(120)).map_err(|_| "no answer in 120 s".to_string())?
}

#[cfg(target_os = "macos")]
fn post(a: &Alert) -> Result<(), String> {
    use objc2_foundation::{NSDictionary, NSError, NSString};
    use objc2_user_notifications::{UNMutableNotificationContent, UNNotificationRequest, UNNotificationSound, UNUserNotificationCenter};
    let c = UNMutableNotificationContent::new();
    c.setTitle(&NSString::from_str(&a.title));
    c.setBody(&NSString::from_str(&a.body));
    c.setSound(Some(&UNNotificationSound::defaultSound()));
    if let Some(o) = &a.open {
        let info = NSDictionary::from_slices(&[&*NSString::from_str("open")], &[&*NSString::from_str(o)]);
        // `userInfo` is untyped (`NSDictionary<AnyObject, AnyObject>`).
        unsafe { c.setUserInfo(&*(&*info as *const NSDictionary<NSString, NSString>).cast::<NSDictionary>()) };
    }
    let req = UNNotificationRequest::requestWithIdentifier_content_trigger(&NSString::from_str(&a.key), &c, None);
    let (tx, rx) = mpsc::channel();
    let block = block2::RcBlock::new(move |e: *mut NSError| {
        let _ = tx.send(unsafe { e.as_ref() }.map(|e| e.localizedDescription().to_string()));
    });
    UNUserNotificationCenter::currentNotificationCenter().addNotificationRequest_withCompletionHandler(&req, Some(&block));
    match rx.recv_timeout(Duration::from_secs(5)) {
        Ok(None) => Ok(()),
        Ok(Some(e)) => Err(e),
        Err(_) => Err("no answer in 5 s".into()),
    }
}

#[cfg(target_os = "macos")]
mod delegate {
    use objc2::rc::Retained;
    use objc2::runtime::{NSObject, NSObjectProtocol, ProtocolObject};
    use objc2::{define_class, msg_send, AnyThread};
    use objc2_foundation::NSString;
    use objc2_user_notifications::{UNNotification, UNNotificationPresentationOptions, UNNotificationResponse, UNUserNotificationCenter, UNUserNotificationCenterDelegate};

    define_class!(
        #[unsafe(super(NSObject))]
        #[name = "DiskyAlertDelegate"]
        pub struct Delegate;

        unsafe impl NSObjectProtocol for Delegate {}

        unsafe impl UNUserNotificationCenterDelegate for Delegate {
            #[unsafe(method(userNotificationCenter:didReceiveNotificationResponse:withCompletionHandler:))]
            fn did_receive(&self, _c: &UNUserNotificationCenter, r: &UNNotificationResponse, done: &block2::DynBlock<dyn Fn()>) {
                let info = r.notification().request().content().userInfo();
                let target = info.objectForKey(&NSString::from_str("open"))
                    .and_then(|v| v.downcast::<NSString>().ok())
                    .map(|s| s.to_string());
                if let Some(t) = target {
                    let _ = std::process::Command::new("/usr/bin/open").arg(t).status();
                }
                done.call(());
            }

            // Shown even while disky is the frontmost app.
            #[unsafe(method(userNotificationCenter:willPresentNotification:withCompletionHandler:))]
            fn will_present(&self, _c: &UNUserNotificationCenter, _n: &UNNotification, done: &block2::DynBlock<dyn Fn(UNNotificationPresentationOptions)>) {
                done.call((UNNotificationPresentationOptions::Banner | UNNotificationPresentationOptions::List | UNNotificationPresentationOptions::Sound,));
            }
        }
    );

    /// Route alert clicks to this process (the app; set before launch ends so a
    /// click that launched it is delivered).
    pub fn install() {
        if super::in_bundle().is_none() {
            return;
        }
        let d: Retained<Delegate> = unsafe { msg_send![Delegate::alloc(), init] };
        UNUserNotificationCenter::currentNotificationCenter().setDelegate(Some(ProtocolObject::from_ref(&*d)));
        // The center holds its delegate weakly: keep it for the process's life.
        std::mem::forget(d);
    }
}

#[cfg(target_os = "macos")]
pub use delegate::install as install_delegate;

#[cfg(not(target_os = "macos"))]
pub fn status() -> Option<Auth> {
    None
}

#[cfg(not(target_os = "macos"))]
pub fn ask() {}

#[cfg(not(target_os = "macos"))]
pub fn request() -> Result<String, String> {
    Err("no notifications on this platform".into())
}

#[cfg(not(target_os = "macos"))]
fn post(_: &Alert) -> Result<(), String> {
    Err("no notifications on this platform".into())
}

#[cfg(not(target_os = "macos"))]
pub fn install_delegate() {}

/// The last non-empty line of a log (an alert's detail).
pub fn last_line(p: &std::path::Path) -> Option<String> {
    use std::io::{Read, Seek, SeekFrom};
    let mut f = std::fs::File::open(p).ok()?;
    let len = f.metadata().ok()?.len();
    f.seek(SeekFrom::Start(len.saturating_sub(4096))).ok()?;
    let mut s = String::new();
    f.read_to_string(&mut s).ok()?;
    s.lines().rev().map(str::trim).find(|l| !l.is_empty()).map(String::from)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn last_line_skips_trailing_blanks() {
        let p = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(format!("../../../tmp/test-last-line-{}", std::process::id()));
        std::fs::write(&p, "a\n[2026-10-14T15:00:00Z] dt-capture: upload failed\n\n").unwrap();
        assert_eq!(last_line(&p).as_deref(), Some("[2026-10-14T15:00:00Z] dt-capture: upload failed"));
        std::fs::remove_file(&p).unwrap();
        assert_eq!(last_line(&p), None);
    }

    #[test]
    fn applescript_literals() {
        assert_eq!(applescript_str(r#"exit 1: "r2" \ denied"#), r#""exit 1: \"r2\" \\ denied""#);
    }

    #[test]
    fn menu_lines() {
        assert_eq!(
            [Route::Native, Route::NativePush, Route::Unasked, Route::BannerPush, Route::Banner].map(menu_line),
            [
                "Failure alerts: notifications ✓",
                "Failure alerts: notifications + push ✓",
                "Failure alerts: allow notifications…",
                "Failure alerts: banner + push",
                "Failure alerts: banner only (no push)",
            ]
        );
    }
}
