//! The scheduled agents and the login item.
//!
//! **Agents are plain per-user LaunchAgents**, written to
//! `~/Library/LaunchAgents/<label>.plist` from the bundled templates
//! (`Contents/Library/LaunchAgents/`, with `BundleProgram` resolved to this
//! executable's absolute path) and loaded with `launchctl bootstrap`. Login
//! Items still lists them as "disky" (BTM attributes them by executable path).
//!
//! Not `SMAppService.agent`: for a bundle with no Team ID (self-signed), launchd
//! pins a registered agent to the build that registered it; after a rebuild
//! it SIGKILLs the agent at spawn ("Launch Constraint Violation"), and
//! re-registering didn't reliably clear it (2026-09-30: the drainer
//! crash-looped). Revisit once disky is signed with a Developer ID.
//!
//! The login item stays `SMAppService.mainApp` (LaunchServices opens the app;
//! no launchd launch constraint).

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
    if let Service::Agent(p) = s {
        return agent_status(p);
    }
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
    if let Service::Agent(p) = s {
        return agent_register(p);
    }
    unsafe { service(s).registerAndReturnError() }.map_err(|e| e.localizedDescription().to_string())
}

#[cfg(target_os = "macos")]
pub fn unregister(s: Service) -> Result<(), String> {
    if let Service::Agent(p) = s {
        return agent_unregister(p);
    }
    unsafe { service(s).unregisterAndReturnError() }.map_err(|e| e.localizedDescription().to_string())
}

#[cfg(target_os = "macos")]
pub fn open_login_items_settings() {
    unsafe { SMAppService::openSystemSettingsLoginItems() }
}

pub fn agents_enabled() -> bool {
    AGENT_PLISTS.iter().all(|p| installed_plist(p).exists())
}

/// Enabled intent is the installed plain plist, not a successful launch. A
/// failed agent must remain checked so saving other settings doesn't disable it.
pub fn agents_status() -> String {
    AGENT_PLISTS.iter().map(|p| format!("{}: {}", label(p).rsplit('.').next().unwrap(), status(Service::Agent(p)))).collect::<Vec<_>>().join(" · ")
}

trait AgentMigration {
    fn enabled(&self) -> bool;
    fn legacy(&self) -> bool;
    fn current(&self) -> bool;
    fn running(&self) -> bool;
    fn clear_legacy(&mut self) -> Result<(), String>;
    fn replace_plain(&mut self) -> Result<(), String>;
}

fn migrate(agent: &mut impl AgentMigration) -> Result<(), String> {
    if !agent.enabled() { return Ok(()); }
    let legacy = agent.legacy();
    if (legacy || !agent.current()) && agent.running() {
        return Err("agent upgrade deferred while its job is running; reopen disky after it finishes".into());
    }
    if legacy { agent.clear_legacy()?; }
    if legacy || !agent.current() { agent.replace_plain()?; }
    Ok(())
}

struct InstalledAgent<'a>(&'a str);

impl AgentMigration for InstalledAgent<'_> {
    fn enabled(&self) -> bool { installed_plist(self.0).exists() }
    fn legacy(&self) -> bool {
        let st = unsafe { service(Service::Agent(self.0)).status() };
        st != SMAppServiceStatus::NotRegistered && st != SMAppServiceStatus::NotFound
    }
    fn current(&self) -> bool {
        let out = std::process::Command::new("launchctl").args(["print", &domain_target(self.0)]).output();
        let expected = std::env::current_exe().unwrap();
        out.is_ok_and(|o| o.status.success() && field(&String::from_utf8_lossy(&o.stdout), "program") == Some(expected.to_string_lossy().as_ref()))
    }
    fn running(&self) -> bool { crate::status::job_state(label(self.0)).running }
    fn clear_legacy(&mut self) -> Result<(), String> {
        if self.legacy() {
            unsafe { service(Service::Agent(self.0)).unregisterAndReturnError() }.map_err(|e| e.localizedDescription().to_string())?;
        }
        Ok(())
    }
    fn replace_plain(&mut self) -> Result<(), String> { agent_register(self.0) }
}

/// Run only at GUI startup, preserving disabled agents and the main-app login item.
pub fn migrate_agents() -> Result<(), String> {
    for p in AGENT_PLISTS { migrate(&mut InstalledAgent(p))?; }
    Ok(())
}

fn field<'a>(text: &'a str, name: &str) -> Option<&'a str> {
    text.lines().find_map(|l| l.trim().split_once(" = ").filter(|(key, _)| *key == name).map(|(_, value)| value))
}

fn label(plist: &str) -> &str {
    plist.trim_end_matches(".plist")
}

fn installed_plist(plist: &str) -> std::path::PathBuf {
    crate::status::agents_dir().join(plist)
}

fn domain_target(plist: &str) -> String {
    format!("gui/{}/{}", crate::status::uid(), label(plist))
}

/// The bundled template with `BundleProgram` resolved to this executable.
fn render_agent(plist: &str) -> Result<plist::Dictionary, String> {
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let tpl = exe
        .parent()
        .ok_or("no exe dir")?
        .join(format!("../Library/LaunchAgents/{plist}"));
    let mut d = plist::Value::from_file(&tpl)
        .map_err(|e| format!("{}: {e}", tpl.display()))?
        .into_dictionary()
        .ok_or("template isn't a dict")?;
    d.remove("BundleProgram");
    d.remove("AssociatedBundleIdentifiers");
    let args = d.get_mut("ProgramArguments").and_then(|v| v.as_array_mut()).ok_or("no ProgramArguments")?;
    args[0] = plist::Value::String(exe.to_string_lossy().into_owned());
    Ok(d)
}

fn launchctl(args: &[&str]) -> bool {
    std::process::Command::new("launchctl").args(args).output().is_ok_and(|o| o.status.success())
}

fn agent_status(plist: &str) -> &'static str {
    if !installed_plist(plist).exists() {
        "not registered"
    } else {
        let out = std::process::Command::new("launchctl").args(["print", &domain_target(plist)]).output();
        match out {
            Ok(o) if o.status.success() => loaded_status(&String::from_utf8_lossy(&o.stdout), &std::env::current_exe().unwrap().to_string_lossy()),
            _ => "installed, not loaded",
        }
    }
}

fn loaded_status(text: &str, expected: &str) -> &'static str {
    if field(text, "program") != Some(expected) { return "upgrade needed"; }
    if field(text, "state") != Some("running") && field(text, "last exit code").is_some_and(|v| v != "0") { return "last launch failed"; }
    "enabled"
}

fn agent_register(plist: &str) -> Result<(), String> {
    let d = render_agent(plist)?;
    InstalledAgent(plist).clear_legacy()?;
    let path = installed_plist(plist);
    std::fs::create_dir_all(path.parent().unwrap()).map_err(|e| e.to_string())?;
    plist::Value::Dictionary(d).to_file_xml(&path).map_err(|e| e.to_string())?;
    agent_bootout(plist)?;
    let p = path.to_string_lossy();
    if !launchctl(&["bootstrap", &format!("gui/{}", crate::status::uid()), &p]) {
        return Err(format!("launchctl bootstrap {p} failed"));
    }
    Ok(())
}

/// bootout, then wait until launchd has dropped the label (a KeepAlive job
/// takes a moment; bootstrapping before that fails with exit 5).
fn agent_bootout(plist: &str) -> Result<(), String> {
    let target = domain_target(plist);
    launchctl(&["bootout", &target]);
    for _ in 0..240 {
        if !launchctl(&["print", &target]) {
            return Ok(());
        }
        std::thread::sleep(std::time::Duration::from_millis(250));
    }
    Err(format!("launchctl bootout {target} timed out"))
}

fn agent_unregister(plist: &str) -> Result<(), String> {
    InstalledAgent(plist).clear_legacy()?;
    agent_bootout(plist)?;
    match std::fs::remove_file(installed_plist(plist)) {
        Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e.to_string()),
        _ => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Default)]
    struct Fake { enabled: bool, legacy: bool, current: bool, running: bool, calls: Vec<&'static str> }
    impl AgentMigration for Fake {
        fn enabled(&self) -> bool { self.enabled }
        fn legacy(&self) -> bool { self.legacy }
        fn current(&self) -> bool { self.current }
        fn running(&self) -> bool { self.running }
        fn clear_legacy(&mut self) -> Result<(), String> { self.calls.push("unregister legacy"); self.legacy = false; Ok(()) }
        fn replace_plain(&mut self) -> Result<(), String> { self.calls.push("replace plain"); self.current = true; Ok(()) }
    }

    #[test]
    fn migration_preserves_intent_and_is_idempotent() {
        for (enabled, legacy, current, expected) in [
            (true, true, false, vec!["unregister legacy", "replace plain"]),
            (true, true, true, vec!["unregister legacy", "replace plain"]),
            (true, false, true, vec![]),
            (true, false, false, vec!["replace plain"]),
            (false, true, false, vec![]),
            (false, false, false, vec![]),
        ] {
            let mut agent = Fake { enabled, legacy, current, ..Default::default() };
            migrate(&mut agent).unwrap();
            assert_eq!(agent.calls, expected);
            agent.calls.clear();
            migrate(&mut agent).unwrap();
            assert_eq!(agent.calls, Vec::<&str>::new());
        }
    }

    #[test]
    fn upgrade_does_not_interrupt_an_active_job() {
        let mut agent = Fake { enabled: true, legacy: true, running: true, ..Default::default() };
        assert_eq!(migrate(&mut agent), Err("agent upgrade deferred while its job is running; reopen disky after it finishes".into()));
        assert_eq!(agent.calls, Vec::<&str>::new());
        assert_eq!(agent.legacy, true);
    }

    #[test]
    fn loaded_health_does_not_hide_old_programs_or_failed_launches() {
        let exe = "/Applications/disky.app/Contents/MacOS/disky";
        assert_eq!(loaded_status("program = Contents/MacOS/disky\nlast exit code = 78\n", exe), "upgrade needed");
        assert_eq!(loaded_status(&format!("program = {exe}\nstate = not running\nlast exit code = 78\n"), exe), "last launch failed");
        assert_eq!(loaded_status(&format!("program = {exe}\nstate = running\nlast exit code = 78\n"), exe), "enabled");
        assert_eq!(loaded_status(&format!("program = {exe}\nstate = not running\nlast exit code = 0\n"), exe), "enabled");
    }
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
