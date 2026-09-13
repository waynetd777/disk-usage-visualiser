mod cache;
mod scan;
mod volume;

use serde::Serialize;
use std::path::Path;
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager, State};
use tauri_plugin_window_state::{AppHandleExt, StateFlags};

/// Everything except VISIBLE. The window is deliberately created hidden and shown once it
/// carries the theme colour, so restoring a saved "visible" would put it back on screen too
/// early and reintroduce the white flash.
const STATE_FLAGS: StateFlags = StateFlags::all().difference(StateFlags::VISIBLE);

/// Startup timing, printed when DU_TIMING is set. Run the binary inside the bundle to see it:
///   DU_TIMING=1 "/Applications/Disk Usage.app/Contents/MacOS/DiskUsage"
static START: Mutex<Option<Instant>> = Mutex::new(None);

pub fn mark_start() {
    if let Ok(mut g) = START.lock() {
        *g = Some(Instant::now());
    }
}

pub fn stamp(what: &str) {
    if std::env::var_os("DU_TIMING").is_none() {
        return;
    }
    let ms = START.lock().ok().and_then(|g| *g).map(|t| t.elapsed().as_millis()).unwrap_or(0);
    eprintln!("[timing] {ms:>5} ms  {what}");
}

pub struct AppState {
    /// The folder being shown. Persisted, so the app reopens on the same root.
    root: Mutex<String>,
    /// The scan on screen: the cached one at launch, replaced when a fresh scan completes.
    scan: Mutex<Option<Arc<cache::Scan>>>,
    progress: Arc<scan::Progress>,
    scanning: Arc<AtomicBool>,
    /// True while the cached scan is being read from disk at launch.
    loading: Arc<AtomicBool>,
    scan_started: Mutex<Option<Instant>>,
}

#[derive(Serialize)]
struct ScanSummary {
    finished: String,
    duration_ms: u64,
    items: u64,
    size: u64,
    apparent: u64,
    denied: u64,
    cache_bytes: u64,
}

#[derive(Serialize)]
struct AppInfo {
    root: String,
    root_name: String,
    scanning: bool,
    loading: bool,
    scan: Option<ScanSummary>,
    volume: volume::Volume,
    recent: Vec<cache::Recent>,
    /// Whether the app can read a folder that only Full Disk Access opens.
    full_disk_access: bool,
    home: String,
}

#[derive(Serialize)]
struct ProgressInfo {
    scanning: bool,
    items: u64,
    bytes: u64,
    dirs: u64,
    denied: u64,
    current: String,
    elapsed_ms: u64,
    /// Items counted by the previous scan of this root, if there was one — what the bar is
    /// measured against.
    expected_items: Option<u64>,
}

fn home() -> String {
    std::env::var("HOME").unwrap_or_else(|_| "/".into())
}

/// ~/Library/Safari is only readable with Full Disk Access (~/Library/CloudStorage is NOT a
/// valid probe: it is readable without it — lesson from backup-manager).
fn has_full_disk_access() -> bool {
    std::fs::read_dir(Path::new(&home()).join("Library/Safari")).is_ok()
}

#[tauri::command]
fn get_state(state: State<AppState>) -> AppInfo {
    let root = state.root.lock().map(|r| r.clone()).unwrap_or_else(|_| "/".into());
    let scan = state.scan.lock().ok().and_then(|s| s.clone());
    AppInfo {
        root_name: volume::root_name(&root),
        scanning: state.scanning.load(Ordering::SeqCst),
        loading: state.loading.load(Ordering::SeqCst),
        scan: scan.map(|s| ScanSummary {
            finished: s.finished.clone(),
            duration_ms: s.duration_ms,
            items: s.items,
            size: s.size,
            apparent: s.apparent,
            denied: s.denied,
            cache_bytes: cache::scan_file_size(&s.root),
        }),
        volume: volume::info(&root),
        recent: cache::load_recent(),
        full_disk_access: has_full_disk_access(),
        home: home(),
        root,
    }
}

#[tauri::command]
fn get_progress(state: State<AppState>) -> ProgressInfo {
    let p = &state.progress;
    let expected = state.scan.lock().ok().and_then(|s| s.as_ref().map(|s| s.items));
    ProgressInfo {
        scanning: state.scanning.load(Ordering::SeqCst),
        items: p.items.load(Ordering::Relaxed),
        bytes: p.bytes.load(Ordering::Relaxed),
        dirs: p.dirs.load(Ordering::Relaxed),
        denied: p.denied.load(Ordering::Relaxed),
        current: p.current.lock().map(|c| c.clone()).unwrap_or_default(),
        elapsed_ms: state.scan_started.lock().ok().and_then(|s| *s).map(|t| t.elapsed().as_millis() as u64).unwrap_or(0),
        expected_items: expected,
    }
}

/// The subtree under `path`, pruned to what can be drawn. `min_fraction` is the smallest
/// folder worth a block, as a share of the folder being viewed (the UI passes ~1/4000).
#[tauri::command]
fn get_tree(state: State<AppState>, path: String, min_fraction: f64) -> Result<scan::View, String> {
    let scan = state.scan.lock().ok().and_then(|s| s.clone()).ok_or("no scan yet")?;
    let root = scan.root.clone();
    let rel = if path == root {
        String::new()
    } else if root == "/" {
        path.trim_start_matches('/').to_string()
    } else {
        path.strip_prefix(&format!("{root}/")).ok_or_else(|| format!("{path} is not under {root}"))?.to_string()
    };
    let parts: Vec<&str> = rel.split('/').filter(|s| !s.is_empty()).collect();
    let node = scan.tree.find(&parts).ok_or_else(|| format!("{path} is not in the scan"))?;
    let depth = parts.len() as u32;
    let min_size = ((node.size as f64) * min_fraction.clamp(0.0, 1.0)) as u64;
    Ok(scan::view(node, &path, depth, min_size.max(1), depth + 14))
}

fn start_scan_thread(app: &AppHandle, root: String) -> Result<(), String> {
    let state: State<AppState> = app.state();
    if !Path::new(&root).is_dir() {
        return Err(format!("{root} is not a folder"));
    }
    if state.scanning.swap(true, Ordering::SeqCst) {
        return Err("a scan is already running".into());
    }
    // Switching root: show that root's cached scan (if any) straight away, and remember it.
    let switched = state.root.lock().map(|r| *r != root).unwrap_or(true);
    if switched {
        if let Ok(mut r) = state.root.lock() {
            *r = root.clone();
        }
        cache::save_root(&root);
        let cached = cache::load_scan(&root).map(Arc::new);
        if let Ok(mut s) = state.scan.lock() {
            *s = cached;
        }
    }
    state.progress.reset();
    if let Ok(mut s) = state.scan_started.lock() {
        *s = Some(Instant::now());
    }
    let progress = state.progress.clone();
    let scanning = state.scanning.clone();
    let handle = app.clone();
    std::thread::spawn(move || {
        let t0 = Instant::now();
        let name = volume::root_name(&root);
        let result = scan::walk(Path::new(&root), name.clone(), false, &progress);
        if let Some(tree) = result {
            let s = cache::Scan {
                root: root.clone(),
                root_name: name.clone(),
                finished: chrono::Local::now().to_rfc3339(),
                duration_ms: t0.elapsed().as_millis() as u64,
                items: tree.items,
                size: tree.size,
                apparent: tree.apparent,
                denied: tree.denied,
                tree,
            };
            let st: State<AppState> = handle.state();
            // Only publish if the root is still the one this scan was for.
            let still = st.root.lock().map(|r| *r == root).unwrap_or(false);
            if still {
                if let Ok(mut cur) = st.scan.lock() {
                    *cur = Some(Arc::new(s.clone()));
                }
            }
            cache::push_recent(cache::Recent { path: root.clone(), name, size: s.size, items: s.items, when: s.finished.clone() });
            scanning.store(false, Ordering::SeqCst);
            if let Err(e) = cache::save_scan(&s) {
                eprintln!("could not cache the scan: {e}");
            }
        } else {
            scanning.store(false, Ordering::SeqCst);
        }
    });
    Ok(())
}

#[tauri::command]
fn start_scan(app: AppHandle, root: Option<String>) -> Result<(), String> {
    let state: State<AppState> = app.state();
    let root = match root {
        Some(r) if !r.is_empty() => r,
        _ => state.root.lock().map(|r| r.clone()).unwrap_or_else(|_| "/".into()),
    };
    start_scan_thread(&app, root)
}

/// Ends the scan in flight. The scan on screen stays what it was.
#[tauri::command]
fn stop_scan(state: State<AppState>) {
    state.progress.cancel.store(true, Ordering::SeqCst);
}

#[tauri::command]
fn reveal(path: String) -> Result<(), String> {
    if !Path::new(&path).exists() {
        return Err(format!("{path} does not exist"));
    }
    Command::new("/usr/bin/open").arg("-R").arg(&path).status().map_err(|e| e.to_string())?;
    Ok(())
}

/// Finder's Get Info window for a path, via AppleScript (there is no URL scheme for it).
#[tauri::command]
fn get_info(path: String) -> Result<(), String> {
    if !Path::new(&path).exists() {
        return Err(format!("{path} does not exist"));
    }
    let escaped = path.replace('\\', "\\\\").replace('"', "\\\"");
    let script = format!(
        "tell application \"Finder\"\nactivate\nopen information window of (POSIX file \"{escaped}\" as alias)\nend tell"
    );
    Command::new("/usr/bin/osascript").arg("-e").arg(script).status().map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
fn open_privacy_settings() -> Result<(), String> {
    Command::new("/usr/bin/open")
        .arg("x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles")
        .status()
        .map_err(|e| e.to_string())?;
    Ok(())
}

fn show_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

fn fmt_bytes(b: u64) -> String {
    let b = b as f64;
    if b >= 1e12 {
        format!("{:.2} TB", b / 1e12)
    } else if b >= 1e9 {
        if b / 1e9 >= 100.0 { format!("{:.0} GB", b / 1e9) } else { format!("{:.1} GB", b / 1e9) }
    } else if b >= 1e6 {
        format!("{:.0} MB", b / 1e6)
    } else {
        format!("{:.0} KB", b / 1e3)
    }
}

fn fmt_count(n: u64) -> String {
    let s = n.to_string();
    let mut out = String::new();
    for (i, c) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    out
}

/// The tray menu's first line: what the volume looks like right now.
fn volume_line(root: &str) -> String {
    let v = volume::info(root);
    if v.total == 0 {
        return format!("{}", volume::root_name(root));
    }
    format!("{}: {} used of {}", v.name, fmt_bytes(v.used), fmt_bytes(v.total))
}

pub fn run() {
    let root = cache::load_root();
    tauri::Builder::default()
        .plugin(tauri_plugin_window_state::Builder::default().with_state_flags(STATE_FLAGS).build())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_clipboard_manager::init())
        .manage(AppState {
            root: Mutex::new(root.clone()),
            scan: Mutex::new(None),
            progress: Default::default(),
            scanning: Default::default(),
            loading: Arc::new(AtomicBool::new(true)),
            scan_started: Mutex::new(None),
        })
        .invoke_handler(tauri::generate_handler![
            get_state,
            get_progress,
            get_tree,
            start_scan,
            stop_scan,
            reveal,
            get_info,
            open_privacy_settings
        ])
        .setup(move |app| {
            stamp("setup() start");
            let handle = app.handle().clone();

            // Read the cached scan off the startup path, then start the rescan behind it. The
            // UI polls get_state until `loading` clears, draws the cache, and follows progress.
            {
                let h = handle.clone();
                let root = root.clone();
                std::thread::spawn(move || {
                    let st: State<AppState> = h.state();
                    let cached = cache::load_scan(&root).map(Arc::new);
                    if let Ok(mut s) = st.scan.lock() {
                        *s = cached;
                    }
                    st.loading.store(false, Ordering::SeqCst);
                    stamp("cache loaded");
                    let _ = start_scan_thread(&h, root);
                });
            }

            // Tray: the volume line is information (disabled item, the macOS idiom), then the
            // commands. A progress line is inserted under it only while a scan runs.
            let vol = MenuItem::with_id(app, "vol", volume_line(&root), false, None::<&str>)?;
            let prog = MenuItem::with_id(app, "prog", "Scanning…", false, None::<&str>)?;
            let open = MenuItem::with_id(app, "open", "Open Disk Usage", true, None::<&str>)?;
            let rescan = MenuItem::with_id(app, "rescan", "Rescan", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&vol, &PredefinedMenuItem::separator(app)?, &open, &rescan, &PredefinedMenuItem::separator(app)?, &quit])?;
            // Template image: black + alpha, tinted by macOS for light and dark menu bars.
            let tray_icon = tauri::image::Image::from_bytes(include_bytes!("../icons/tray@2x.png")).expect("tray icon is a valid png");
            let _tray = TrayIconBuilder::with_id("main")
                .icon(tray_icon)
                .icon_as_template(true)
                .menu(&menu)
                .show_menu_on_left_click(true)
                .tooltip("Disk Usage")
                .on_menu_event(move |app, ev| match ev.id.as_ref() {
                    "open" => show_main(app),
                    "rescan" => {
                        show_main(app);
                        let _ = start_scan(app.clone(), None);
                    }
                    "quit" => {
                        let _ = app.save_window_state(STATE_FLAGS);
                        app.exit(0)
                    }
                    _ => {}
                })
                .build(app)?;
            {
                let h = handle.clone();
                let menu = menu.clone();
                std::thread::spawn(move || {
                    let mut prog_in_menu = false;
                    let mut ticks = 0u32;
                    loop {
                        let st: State<AppState> = h.state();
                        let scanning = st.scanning.load(Ordering::SeqCst);
                        if scanning {
                            if !prog_in_menu {
                                let _ = menu.insert(&prog, 1);
                                prog_in_menu = true;
                            }
                            let items = st.progress.items.load(Ordering::Relaxed);
                            let expected = st.scan.lock().ok().and_then(|s| s.as_ref().map(|s| s.items));
                            let line = match expected {
                                Some(e) if e > 0 => format!("Scanning — {}% ({} items)", ((items as f64 / e as f64) * 100.0).min(99.0) as u64, fmt_count(items)),
                                _ => format!("Scanning — {} items", fmt_count(items)),
                            };
                            let _ = prog.set_text(line);
                        } else if prog_in_menu {
                            let _ = menu.remove(&prog);
                            prog_in_menu = false;
                        }
                        // statfs is cheap; every 5 s keeps the used/free line honest.
                        if ticks % 5 == 0 {
                            let root = st.root.lock().map(|r| r.clone()).unwrap_or_else(|_| "/".into());
                            let _ = vol.set_text(volume_line(&root));
                        }
                        ticks = ticks.wrapping_add(1);
                        std::thread::sleep(std::time::Duration::from_secs(1));
                    }
                });
            }

            if let Some(w) = app.get_webview_window("main") {
                // Paint the native window in the theme's background colour before it is shown,
                // so no frame of the default white can appear behind the webview. Keep these two
                // colours in step with --bg in src/styles.css and index.html.
                let dark = matches!(w.theme(), Ok(tauri::Theme::Dark));
                let bg = if dark { tauri::window::Color(30, 30, 32, 255) } else { tauri::window::Color(245, 245, 247, 255) };
                let _ = w.set_background_color(Some(bg));

                // The WKWebView is a separate surface and Tauri's set_background_color is a no-op
                // for it on macOS. WebKit paints the empty document white on its first composite,
                // before index.html's inline <style> is parsed, so turn the webview's own
                // background off (drawsBackground = NO via KVC) and let the window colour show
                // until the page paints. Show from INSIDE the with_webview closure: it is queued
                // onto the event loop, and a show() here would race it on a cold start.
                #[cfg(target_os = "macos")]
                {
                    let (r, g, b) = if dark { (30.0, 30.0, 32.0) } else { (245.0, 245.0, 247.0) };
                    let w_show = w.clone();
                    let _ = w.with_webview(move |wv| {
                        unsafe {
                            use objc2::runtime::AnyObject;
                            let webview: *mut AnyObject = wv.inner() as *mut AnyObject;
                            let color = objc2_app_kit::NSColor::colorWithSRGBRed_green_blue_alpha(r / 255.0, g / 255.0, b / 255.0, 1.0);
                            let _: () = objc2::msg_send![webview, setUnderPageBackgroundColor: &*color];
                            let no = objc2_foundation::NSNumber::new_bool(false);
                            let key = objc2_foundation::NSString::from_str("drawsBackground");
                            let _: () = objc2::msg_send![webview, setValue: &*no, forKey: &*key];
                            let _: () = objc2::msg_send![webview, setWantsLayer: true];
                            let layer: *mut AnyObject = objc2::msg_send![webview, layer];
                            if !layer.is_null() {
                                let cg: *mut AnyObject = objc2::msg_send![&*color, CGColor];
                                let _: () = objc2::msg_send![layer, setBackgroundColor: cg];
                            }
                        }
                        stamp("webview background set");
                        let _ = w_show.show();
                        let _ = w_show.set_focus();
                        stamp("window shown");
                    });
                    // If that message is never delivered, the window would stay hidden for good.
                    let w_fallback = w.clone();
                    std::thread::spawn(move || {
                        std::thread::sleep(std::time::Duration::from_millis(1200));
                        if matches!(w_fallback.is_visible(), Ok(false)) {
                            stamp("fallback show");
                            let _ = w_fallback.show();
                            let _ = w_fallback.set_focus();
                        }
                    });
                }
                #[cfg(not(target_os = "macos"))]
                {
                    let _ = w.show();
                    let _ = w.set_focus();
                }

                // Closing the window hides it; the app stays in the menu bar.
                let w2 = w.clone();
                w.on_window_event(move |ev| {
                    if let tauri::WindowEvent::CloseRequested { api, .. } = ev {
                        api.prevent_close();
                        let _ = w2.app_handle().save_window_state(STATE_FLAGS);
                        let _ = w2.hide();
                    }
                });
            }
            stamp("setup() done");
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, ev| {
            #[cfg(target_os = "macos")]
            if let tauri::RunEvent::Reopen { .. } = ev {
                show_main(app);
            }
            #[cfg(not(target_os = "macos"))]
            let _ = (app, ev);
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formats() {
        assert_eq!(fmt_bytes(612_000_000_000), "612 GB");
        assert_eq!(fmt_bytes(31_200_000_000), "31.2 GB");
        assert_eq!(fmt_bytes(1_280_000_000_000), "1.28 TB");
        assert_eq!(fmt_count(1284930), "1,284,930");
        assert_eq!(fmt_count(12), "12");
    }
}
