//! `SMAppService` registration: the bundled LaunchAgents and the login item.
//!
//! Registering an agent tells launchd to load
//! `Contents/Library/LaunchAgents/<plist>` from this bundle; it shows in
//! System Settings → General → Login Items as "disky", and the user can turn
//! it off there. Agents need no approval step (daemons would).

#[cfg(target_os = "macos")]
use objc2_foundation::NSString;
#[cfg(target_os = "macos")]
use objc2_service_management::{SMAppService, SMAppServiceStatus};

pub const AGENT_PLISTS: [&str; 2] = ["com.runsascoded.disky.scan.plist", "com.runsascoded.disky.drain.plist"];

#[derive(Clone, Copy)]
pub enum Service<'a> {
    LoginItem,
    Agent(&'a str),
}

#[cfg(target_os = "macos")]
fn service(s: Service) -> objc2::rc::Retained<SMAppService> {
    unsafe {
        match s {
            Service::LoginItem => SMAppService::mainAppService(),
            Service::Agent(plist) => SMAppService::agentServiceWithPlistName(&NSString::from_str(plist)),
        }
    }
}

#[cfg(target_os = "macos")]
pub fn status(s: Service) -> &'static str {
    let st = unsafe { service(s).status() };
    match st {
        SMAppServiceStatus::NotRegistered => "not registered",
        SMAppServiceStatus::Enabled => "enabled",
        SMAppServiceStatus::RequiresApproval => "requires approval",
        SMAppServiceStatus::NotFound => "not found",
        _ => "unknown",
    }
}

#[cfg(target_os = "macos")]
pub fn register(s: Service) -> Result<(), String> {
    unsafe { service(s).registerAndReturnError() }.map_err(|e| e.localizedDescription().to_string())
}

#[cfg(target_os = "macos")]
pub fn unregister(s: Service) -> Result<(), String> {
    unsafe { service(s).unregisterAndReturnError() }.map_err(|e| e.localizedDescription().to_string())
}

#[cfg(target_os = "macos")]
pub fn open_login_items_settings() {
    unsafe { SMAppService::openSystemSettingsLoginItems() }
}

pub fn agents_enabled() -> bool {
    AGENT_PLISTS.iter().all(|p| status(Service::Agent(p)) == "enabled")
}

/// `disky agents register|unregister|status [NAME]` (NAME: `scan`, `drain`;
/// default both), `disky login-item on|off|status`.
pub fn cli(kind: &str, action: Option<&str>, only: Option<&str>) -> i32 {
    let targets: Vec<(String, Service)> = match kind {
        "agents" => AGENT_PLISTS
            .iter()
            .filter(|p| only.is_none_or(|n| **p == format!("com.runsascoded.disky.{n}.plist")))
            .map(|p| (p.to_string(), Service::Agent(p)))
            .collect(),
        _ => vec![("login item".to_string(), Service::LoginItem)],
    };
    let mut code = 0;
    for (name, s) in targets {
        let res = match action.unwrap_or("status") {
            "status" => Ok(()),
            "register" | "on" => register(s),
            "unregister" | "off" => unregister(s),
            other => {
                eprintln!("disky {kind}: unknown action {other:?} (register|unregister|status, on|off)");
                return 2;
            }
        };
        if let Err(e) = res {
            eprintln!("{name}: {e}");
            code = 1;
        }
        println!("{name}: {}", status(s));
    }
    code
}
