//! disky — the disk-tree macOS app (Tauri v2 host).
//!
//! A menu-bar item (no Dock icon) that reports the scheduled scan and Full
//! Disk Access, kicks a scan, switches between the prod and dev sites, and
//! opens a window on the site plus a local Settings window (scope, schedule,
//! FDA onboarding). The app binary is also the LaunchAgents' TCC identity
//! (`disky job …`, `disky agent -- …`; see `agent.rs`, `jobs.rs`).
//!
//! See `specs/tauri-native-app.md` (Phases 5–7 and "Settings").

mod agent;
mod applink;
mod jobs;
mod services;
mod settings;
mod status;

use std::path::PathBuf;
use std::process::Command;
use std::time::Duration;

use serde::Serialize;
use tauri::image::Image;
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager, Wry, WebviewUrl, WebviewWindowBuilder};

use settings::Settings;

/// System Settings → Privacy & Security → Full Disk Access.
const FDA_PANE: &str = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles";

fn site_url() -> String {
    settings::load().site_url()
}

/// The bundled `dt-walker` (`Contents/Resources/dt-walker`), unless
/// `DISK_TREE_WALKER` is already set. `agent`/`job` hand it to their child.
pub(crate) fn locate_walker() -> Option<PathBuf> {
    if std::env::var_os("DISK_TREE_WALKER").is_some() {
        return None;
    }
    let exe = std::env::current_exe().ok()?;
    let dir = exe.parent()?;
    [dir.join("dt-walker"), dir.join("../Resources/dt-walker")].into_iter().find(|c| c.is_file())
}

/// A headless mode (`agent`, `job`, `probe`, `scan now`, `agents`, `login-item`) when
/// `args` selects one: its exit code. Checked before `run()`, so launchd jobs
/// never touch Tauri/AppKit.
pub fn headless(args: &[std::ffi::OsString]) -> Option<i32> {
    agent::dispatch(args)
}

fn open(target: &str) {
    let _ = Command::new("/usr/bin/open").arg(target).spawn();
}

/// Show the site window, creating it if needed; with `url`, navigate there.
fn show_window(app: &AppHandle, url: Option<tauri::Url>) {
    if let Some(w) = app.get_webview_window("main") {
        if let Some(u) = url {
            let _ = w.navigate(u);
        }
        let _ = w.show();
        let _ = w.set_focus();
        activate(app);
        return;
    }
    let target = match url.or_else(|| site_url().parse().ok()) {
        Some(u) => u,
        None => return jobs::note(&format!("disky: bad site URL {:?}", site_url())),
    };
    let built = WebviewWindowBuilder::new(app, "main", WebviewUrl::External(target))
        .title("disky")
        .user_agent(&applink::user_agent())
        .inner_size(1280.0, 860.0)
        .min_inner_size(480.0, 400.0)
        // WKWebView drops `window.open` / `target=_blank` without a handler:
        // send them to the default browser (the site's "sign in with your
        // browser" link, external links), never a second in-app window.
        .on_new_window(|url, _| {
            if matches!(url.scheme(), "http" | "https") {
                open(url.as_str());
            }
            tauri::webview::NewWindowResponse::Deny
        })
        // No menu bar (an accessory app), so no View › Reload: ⌘R reloads
        // here, e.g. after a site deploy.
        .initialization_script(RELOAD_KEY)
        .build();
    // An accessory app isn't activated by its launch: without this a new
    // window opens behind the frontmost app's and the launch looks like a nop.
    if let Ok(w) = built {
        let _ = w.show();
        let _ = w.set_focus();
        activate(app);
    }
}

/// Bring the app forward with a window open. An accessory (menu-bar only)
/// app can't take the front from a launch or reopen on macOS 14+: tao's
/// `activateIgnoringOtherApps:` and `activate` both leave it behind the
/// frontmost app. So while a window is open disky is a regular app (Dock icon,
/// menu bar, ⌘-Tab), and goes back to accessory when the last one closes.
#[cfg(target_os = "macos")]
fn activate(app: &AppHandle) {
    let _ = app.set_activation_policy(tauri::ActivationPolicy::Regular);
    let _ = app.run_on_main_thread(|| unsafe {
        use objc2::runtime::AnyObject;
        use objc2::{class, msg_send};
        let ns_app: *mut AnyObject = msg_send![class!(NSApplication), sharedApplication];
        let () = msg_send![ns_app, activate];
    });
}

#[cfg(not(target_os = "macos"))]
fn activate(_: &AppHandle) {}

const RELOAD_KEY: &str = "addEventListener('keydown', e => {
  if (e.metaKey && !e.ctrlKey && !e.altKey && e.key.toLowerCase() === 'r') { e.preventDefault(); location.reload() }
}, true)";

/// Whether macOS launched the app as a login item: the launch's `oapp` Apple
/// event says so (`keyAEPropData` = `keyAELaunchedAsLogInItem`). Only
/// meaningful while that event is current, i.e. during launch (`setup`).
#[cfg(target_os = "macos")]
fn launched_as_login_item() -> bool {
    use objc2_foundation::NSAppleEventManager;
    let code = |s: &[u8; 4]| u32::from_be_bytes(*s);
    let Some(ev) = NSAppleEventManager::sharedAppleEventManager().currentAppleEvent() else { return false };
    ev.eventID() == code(b"oapp")
        && ev.paramDescriptorForKeyword(code(b"prdt")).is_some_and(|d| d.enumCodeValue() == code(b"lgit"))
}

#[cfg(not(target_os = "macos"))]
fn launched_as_login_item() -> bool {
    false
}

/// The local Settings window (`settings/index.html`, the only window with IPC).
fn show_settings(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("settings") {
        let _ = w.show();
        let _ = w.set_focus();
        activate(app);
        return;
    }
    let built = WebviewWindowBuilder::new(app, "settings", WebviewUrl::App("index.html".into()))
        .title("disky settings")
        .inner_size(520.0, 720.0)
        .resizable(true)
        .build();
    if let Ok(w) = built {
        let _ = w.set_focus();
        activate(app);
    }
}

/// A `disky://` URL from LaunchServices (the browser's "Open in disky"). A
/// link from the other known site (prod ↔ dev) switches the app to that site.
fn open_deep_link(app: &AppHandle, deep: &tauri::Url) {
    let current = settings::load().site;
    let mut sites = vec![];
    match tauri::Url::parse(&site_url()) {
        Ok(u) => sites.push((current.as_str(), u)),
        Err(e) => return jobs::note(&format!("disky: bad site URL: {e}")),
    }
    // An explicit `DISKY_URL` pins the site; otherwise the known ones qualify.
    if std::env::var_os("DISKY_URL").is_none() {
        for (name, url) in [("prod", settings::PROD_URL), ("dev", settings::DEV_URL)] {
            if name != current {
                sites.push((name, url.parse().expect("known site URL")));
            }
        }
    }
    match applink::resolve(deep, &sites) {
        Ok((name, link)) => {
            if name != current {
                let mut s = settings::load();
                s.site = name.to_string();
                if let Err(e) = settings::save(&s) {
                    jobs::note(&format!("disky: can't save settings: {e}"));
                }
                jobs::note(&format!("disky: switched site {current} → {name} for a sign-in link"));
                if let Some(t) = app.try_state::<Tray>() {
                    t.refresh();
                }
            }
            show_window(app, Some(link))
        }
        // Never log the URL itself: a valid-looking one carries a token.
        Err(e) => jobs::note(&format!("disky: refused a disky:// link: {e}")),
    }
}

fn fda_line(granted: bool) -> &'static str {
    if granted { "Full Disk Access ✓" } else { "Grant Full Disk Access…" }
}

fn login_enabled() -> bool {
    services::status(services::Service::LoginItem) == "enabled"
}

fn set_agents(on: bool) -> Result<(), String> {
    for p in services::AGENT_PLISTS {
        let s = services::Service::Agent(p);
        if on { services::register(s)? } else { services::unregister(s)? }
    }
    Ok(())
}

fn set_login(on: bool) -> Result<(), String> {
    let s = services::Service::LoginItem;
    if on { services::register(s) } else { services::unregister(s) }
}

// --- Settings window commands ---------------------------------------------

#[derive(Serialize)]
struct UiState {
    settings: Settings,
    site_url: String,
    fda: bool,
    agents: bool,
    login: bool,
    scan: String,
}

fn ui_state() -> UiState {
    let s = settings::load();
    UiState {
        site_url: s.site_url(),
        settings: s,
        fda: agent::has_full_disk_access(),
        agents: services::agents_enabled(),
        login: login_enabled(),
        scan: status::scan_line(),
    }
}

#[tauri::command]
fn get_state() -> UiState {
    ui_state()
}

#[tauri::command]
fn save_settings(app: AppHandle, settings: Settings, agents: bool, login: bool) -> Result<UiState, String> {
    for t in &settings.schedule {
        settings::parse_hm(t).ok_or_else(|| format!("bad time {t:?} (want HH:MM)"))?;
    }
    if !["machine", "home"].contains(&settings.scope.as_str()) {
        return Err(format!("bad scope {:?}", settings.scope));
    }
    let old = settings::load();
    settings::save(&settings)?;
    if agents != services::agents_enabled() {
        set_agents(agents)?;
    }
    if login != login_enabled() {
        set_login(login)?;
    }
    if settings.site != old.site {
        site_changed(&app);
    }
    Ok(ui_state())
}

#[tauri::command]
fn open_fda_settings() {
    open(FDA_PANE);
}

#[tauri::command]
fn scan_now() -> Result<(), String> {
    status::scan_now()
}

// --- tray -------------------------------------------------------------------

struct Tray {
    scan: MenuItem<Wry>,
    fda: MenuItem<Wry>,
    prod: CheckMenuItem<Wry>,
    dev: CheckMenuItem<Wry>,
    local: CheckMenuItem<Wry>,
    scheduled: CheckMenuItem<Wry>,
    login: CheckMenuItem<Wry>,
}

impl Tray {
    fn refresh(&self) {
        let _ = self.scan.set_text(status::scan_line());
        let granted = agent::has_full_disk_access();
        let _ = self.fda.set_text(fda_line(granted));
        let _ = self.fda.set_enabled(!granted);
        let site = settings::load().site;
        let _ = self.prod.set_checked(site == "prod");
        let _ = self.dev.set_checked(site == "dev");
        let _ = self.local.set_checked(site == "local");
        let _ = self.scheduled.set_checked(services::agents_enabled());
        let _ = self.login.set_checked(login_enabled());
    }
}

/// The site setting changed: re-point an open site window, re-check the menu.
fn site_changed(app: &AppHandle) {
    if let (Some(w), Ok(u)) = (app.get_webview_window("main"), site_url().parse()) {
        let _ = w.navigate(u);
    }
    if let Some(t) = app.try_state::<Tray>() {
        t.refresh();
    }
}

fn set_site(app: &AppHandle, site: &str) {
    let mut s = settings::load();
    s.site = site.to_string();
    match settings::save(&s) {
        Ok(()) => site_changed(app),
        Err(e) => jobs::note(&format!("disky: can't save settings: {e}")),
    }
}

/// Serve this Mac's local scans (`jobs::local_scans_dir`) and the bundled
/// SPA (`Contents/Resources/web`, built for the `laptop` store with no
/// sign-in) on `settings::LOCAL_ADDR`, for the `local` site. Loopback only.
fn start_local_server(app: &AppHandle) {
    let Ok(web) = app.path().resource_dir().map(|d| d.join("web")) else { return };
    if !web.join("index.html").exists() {
        return jobs::note(&format!("disky: no local site build at {}", web.display()));
    }
    let cfg = dt_index::http::Config { scans: jobs::local_scans_dir(), web, store: "laptop".into(), root_label: "this Mac".into() };
    std::thread::spawn(move || {
        if let Err(e) = dt_index::http::serve(cfg, settings::LOCAL_ADDR, 4) {
            jobs::note(&format!("disky: local server on {}: {e}", settings::LOCAL_ADDR));
        }
    });
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![get_state, save_settings, open_fda_settings, scan_now])
        .setup(|app| {
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);
            start_local_server(app.handle());

            let site = settings::load().site;
            let tray = Tray {
                scan: MenuItem::with_id(app, "scan_status", status::scan_line(), false, None::<&str>)?,
                fda: MenuItem::with_id(app, "fda", fda_line(true), false, None::<&str>)?,
                prod: CheckMenuItem::with_id(app, "site_prod", "disk.rbw.sh", true, site == "prod", None::<&str>)?,
                dev: CheckMenuItem::with_id(app, "site_dev", "dev.disk.rbw.sh", true, site == "dev", None::<&str>)?,
                local: CheckMenuItem::with_id(app, "site_local", "This Mac (local)", true, site == "local", None::<&str>)?,
                scheduled: CheckMenuItem::with_id(app, "scheduled", "Scheduled scans", true, false, None::<&str>)?,
                login: CheckMenuItem::with_id(app, "login", "Open at login", true, false, None::<&str>)?,
            };
            tray.refresh();
            let site_menu = Submenu::with_items(app, "Site", true, &[&tray.local, &tray.prod, &tray.dev])?;
            let menu = Menu::with_items(
                app,
                &[
                    &tray.scan,
                    &tray.fda,
                    &PredefinedMenuItem::separator(app)?,
                    &MenuItem::with_id(app, "scan_now", "Scan now", true, None::<&str>)?,
                    &MenuItem::with_id(app, "window", "Open disky", true, None::<&str>)?,
                    &MenuItem::with_id(app, "browser", "Open in browser", true, None::<&str>)?,
                    &site_menu,
                    &PredefinedMenuItem::separator(app)?,
                    &tray.scheduled,
                    &tray.login,
                    &MenuItem::with_id(app, "settings", "Settings…", true, None::<&str>)?,
                    &MenuItem::with_id(app, "logs", "Show logs", true, None::<&str>)?,
                    &PredefinedMenuItem::separator(app)?,
                    &MenuItem::with_id(app, "quit", "Quit disky", true, None::<&str>)?,
                ],
            )?;

            TrayIconBuilder::with_id("disky")
                // Monochrome template: macOS tints it for light/dark menu bars.
                .icon(Image::from_bytes(include_bytes!("../icons/tray-template@2x.png"))?)
                .icon_as_template(true)
                .tooltip("disky")
                .menu(&menu)
                .show_menu_on_left_click(true)
                .on_menu_event(|app, event| {
                    match event.id().as_ref() {
                        "scan_now" => {
                            if let Err(e) = status::scan_now() {
                                jobs::note(&format!("disky: scan now: {e}"));
                            }
                        }
                        "window" => show_window(app, None),
                        "browser" => open(&site_url()),
                        "site_prod" => set_site(app, "prod"),
                        "site_dev" => set_site(app, "dev"),
                        "site_local" => set_site(app, "local"),
                        "settings" => show_settings(app),
                        "logs" => open(&jobs::logs_dir().to_string_lossy()),
                        "fda" => open(FDA_PANE),
                        "scheduled" => {
                            if let Err(e) = set_agents(!services::agents_enabled()) {
                                jobs::note(&format!("disky: scheduled scans: {e}"));
                            }
                        }
                        "login" => {
                            if let Err(e) = set_login(!login_enabled()) {
                                jobs::note(&format!("disky: login item: {e}"));
                                services::open_login_items_settings();
                            }
                        }
                        "quit" => app.exit(0),
                        _ => {}
                    }
                    if let Some(t) = app.try_state::<Tray>() {
                        t.refresh();
                    }
                })
                .build(app)?;
            app.manage(tray);

            // First run, or no Full Disk Access yet: open Settings (onboarding).
            // Otherwise a launch the person asked for (Spotlight, Finder, `open`)
            // opens the window; a login-item launch stays in the menu bar.
            if !settings::exists() || !agent::has_full_disk_access() {
                if !settings::exists() {
                    let _ = settings::save(&Settings::default());
                }
                show_settings(app.handle());
            } else if !launched_as_login_item() {
                show_window(app.handle(), None);
            }

            // Keep the menu fresh (launchd, the run state and settings are the
            // record): every 3 s while a scan runs (its progress line), else 20 s.
            let handle = app.handle().clone();
            std::thread::spawn(move || loop {
                let busy = settings::load_progress().is_some() || settings::load_state().force;
                std::thread::sleep(Duration::from_secs(if busy { 3 } else { 20 }));
                if let Some(t) = handle.try_state::<Tray>() {
                    t.refresh();
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error building disky")
        .run(|app, event| match event {
            // Closing a window leaves the menu-bar item running.
            tauri::RunEvent::ExitRequested { api, code, .. } if code.is_none() => api.prevent_exit(),
            // Opened again while running (Spotlight, Finder, the Dock): show the window.
            #[cfg(target_os = "macos")]
            tauri::RunEvent::Reopen { has_visible_windows: false, .. } => show_window(app, None),
            // A window built in `setup` predates the end of launch, when an
            // activation is still overridden: activate again once launched.
            tauri::RunEvent::Ready if app.get_webview_window("main").is_some() => activate(app),
            #[cfg(target_os = "macos")]
            tauri::RunEvent::WindowEvent { label, event: tauri::WindowEvent::Destroyed, .. } => {
                if app.webview_windows().keys().all(|l| *l == label) {
                    let _ = app.set_activation_policy(tauri::ActivationPolicy::Accessory);
                }
            }
            #[cfg(target_os = "macos")]
            tauri::RunEvent::Opened { urls } => {
                for u in &urls {
                    open_deep_link(app, u);
                }
            }
            _ => {}
        });
}
