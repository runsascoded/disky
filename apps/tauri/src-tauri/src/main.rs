// Prevents an extra console window on Windows in release. No-op on macOS.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    let args: Vec<std::ffi::OsString> = std::env::args_os().collect();
    if let Some(code) = disk_tree_app_lib::headless(&args) {
        std::process::exit(code);
    }
    disk_tree_app_lib::run()
}
