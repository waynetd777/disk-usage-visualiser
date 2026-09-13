// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

/// The app menu labels an unbundled process by its executable name ("DiskUsage" in
/// `tauri dev`). Setting the process name before AppKit starts fixes the menu title; the Dock
/// label of a dev binary cannot be fixed this way (it names the executable), and the bundled
/// .app takes its name from Info.plist anyway.
#[cfg(target_os = "macos")]
fn set_process_name() {
    use objc2_foundation::{NSProcessInfo, NSString};
    let info = NSProcessInfo::processInfo();
    info.setProcessName(&NSString::from_str("Disk Usage"));
}

fn main() {
    disk_usage_visualiser_lib::mark_start();
    #[cfg(target_os = "macos")]
    set_process_name();
    disk_usage_visualiser_lib::run()
}
