//! disky — the disk-tree macOS app (Tauri v2 host).
//!
//! A menu-bar item (no Dock icon) that reports the scheduled scan and Full
//! Disk Access, kicks a scan, and opens a window on the web UI (disk.rbw.sh by
//! default; `DISKY_URL` overrides). The app binary is also the LaunchAgents'
//! TCC identity: `disky agent -- CMD…` (see `agent.rs`), so the scans it
//! schedules read what the app was granted.
//!
//! See `specs/tauri-native-app.md` (Phases 5–7).

mod agent;
mod jobs;
mod services;
mod status;

use std::path::PathBuf;
use std::process::Command;
use std::time::Duration;

use tauri::image::Image;
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindowBuilder};

const DEFAULT_URL: &str = "https://disk.rbw.sh";
/// System Settings → Privacy & Security → Full Disk Access.
const FDA_PANE: &str = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles";

fn site_url() -> String {
    std::env::var("DISKY_URL").unwrap_or_else(|_| DEFAULT_URL.to_string())
}

/// The bundled `dt-walker` (`Contents/Resources/dt-walker`), unless
/// `DISK_TREE_WALKER` is already set. `agent` hands it to its child.
pub(crate) fn locate_walker() -> Option<PathBuf> {
    if std::env::var_os("DISK_TREE_WALKER").is_some() {
        return None;
    }
    let exe = std::env::current_exe().ok()?;
    let dir = exe.parent()?;
    [dir.join("dt-walker"), dir.join("../Resources/dt-walker")]
        .into_iter()
        .find(|c| c.is_file())
}

/// A headless mode (`agent`, `probe`) when `args` selects one: its exit code.
/// Checked before `run()`, so launchd jobs never touch Tauri/AppKit.
pub fn headless(args: &[std::ffi::OsString]) -> Option<i32> {
    agent::dispatch(args)
}

fn open(target: &str) {
    let _ = Command::new("/usr/bin/open").arg(target).spawn();
}

fn show_window(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.set_focus();
        return;
    }
    let url = site_url();
    let Ok(parsed) = url.parse() else {
        eprintln!("disky: bad DISKY_URL {url:?}");
        return;
    };
    let _ = WebviewWindowBuilder::new(app, "main", WebviewUrl::External(parsed))
        .title("disky")
        .inner_size(1280.0, 860.0)
        .min_inner_size(480.0, 400.0)
        .build();
}

fn fda_line(granted: bool) -> &'static str {
    if granted {
        "Full Disk Access ✓"
    } else {
        "Grant Full Disk Access…"
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);

            let scan = MenuItem::with_id(app, "scan_status", status::scan_line(), false, None::<&str>)?;
            let granted = agent::has_full_disk_access();
            let fda = MenuItem::with_id(app, "fda", fda_line(granted), !granted, None::<&str>)?;
            let scheduled = CheckMenuItem::with_id(app, "scheduled", "Scheduled scans", true, services::agents_enabled(), None::<&str>)?;
            let login = CheckMenuItem::with_id(
                app, "login", "Open at login", true,
                services::status(services::Service::LoginItem) == "enabled", None::<&str>,
            )?;
            let menu = Menu::with_items(
                app,
                &[
                    &scan,
                    &fda,
                    &PredefinedMenuItem::separator(app)?,
                    &MenuItem::with_id(app, "scan_now", "Scan now", true, None::<&str>)?,
                    &MenuItem::with_id(app, "window", "Open disky", true, None::<&str>)?,
                    &MenuItem::with_id(app, "browser", "Open in browser", true, None::<&str>)?,
                    &MenuItem::with_id(app, "logs", "Show logs", true, None::<&str>)?,
                    &PredefinedMenuItem::separator(app)?,
                    &scheduled,
                    &login,
                    &PredefinedMenuItem::separator(app)?,
                    &MenuItem::with_id(app, "quit", "Quit disky", true, None::<&str>)?,
                ],
            )?;

            let (scheduled_c, login_c) = (scheduled.clone(), login.clone());
            TrayIconBuilder::with_id("disky")
                // Monochrome template: macOS tints it for light/dark menu bars.
                .icon(Image::from_bytes(include_bytes!("../icons/tray-template@2x.png"))?)
                .icon_as_template(true)
                .tooltip("disky")
                .menu(&menu)
                .show_menu_on_left_click(true)
                .on_menu_event(move |app, event| match event.id().as_ref() {
                    "scan_now" => {
                        let label = if status::job_state(status::SCAN_LABEL).loaded {
                            status::SCAN_LABEL
                        } else {
                            status::LEGACY_SCAN_LABEL
                        };
                        let _ = status::kickstart(label);
                    }
                    "scheduled" => {
                        let on = !services::agents_enabled();
                        for p in services::AGENT_PLISTS {
                            let s = services::Service::Agent(p);
                            let r = if on { services::register(s) } else { services::unregister(s) };
                            if let Err(e) = r {
                                eprintln!("disky: {p}: {e}");
                            }
                        }
                        let _ = scheduled_c.set_checked(services::agents_enabled());
                    }
                    "login" => {
                        let s = services::Service::LoginItem;
                        let on = services::status(s) != "enabled";
                        let r = if on { services::register(s) } else { services::unregister(s) };
                        if let Err(e) = r {
                            eprintln!("disky: login item: {e}");
                            services::open_login_items_settings();
                        }
                        let _ = login_c.set_checked(services::status(s) == "enabled");
                    }
                    "window" => show_window(app),
                    "browser" => open(&site_url()),
                    "logs" => {
                        let home = std::env::var("HOME").unwrap_or_default();
                        open(&format!("{home}/Library/Logs/disk-tree"));
                    }
                    "fda" => open(FDA_PANE),
                    "quit" => app.exit(0),
                    _ => {}
                })
                .build(app)?;

            // Keep the status lines fresh (launchd + the log are the record).
            std::thread::spawn(move || loop {
                std::thread::sleep(Duration::from_secs(20));
                let _ = scan.set_text(status::scan_line());
                let granted = agent::has_full_disk_access();
                let _ = fda.set_text(fda_line(granted));
                let _ = fda.set_enabled(!granted);
                let _ = scheduled.set_checked(services::agents_enabled());
                let _ = login.set_checked(services::status(services::Service::LoginItem) == "enabled");
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error building disky")
        .run(|_app, event| {
            // Closing the window leaves the menu-bar item running.
            if let tauri::RunEvent::ExitRequested { api, code, .. } = event {
                if code.is_none() {
                    api.prevent_exit();
                }
            }
        });
}
