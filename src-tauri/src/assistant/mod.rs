// Copyright (c) 2026 Wayne Davies
// SPDX-License-Identifier: MIT (see LICENSE at the repository root)

//! Ask: runs an AI coding CLI already installed and signed in on this Mac — Claude Code, Codex,
//! Antigravity or GitHub Copilot — non-interactively, and streams its answer back to the window
//! as events. It has no tools: the message carries the scan, and the app itself deletes nothing.
//! A follow-up resumes the CLI's session. The model id says which CLI answers.

mod antigravity;
mod claude;
mod codex;
mod copilot;

use serde::Serialize;
use std::io::Read;
use std::path::PathBuf;
use std::process::{Child, ChildStdout, Command, Stdio};
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Manager};

const SYSTEM: &str = "You are a disk-space adviser inside Disk Usage, a macOS app that maps where a disk's space goes. \
The user's message carries the scan: the folder on screen and the biggest folders inside it, with sizes on disk, item counts and the size of loose files directly inside each. \
Your job is to find where space can be reclaimed safely. \
Say in plain language what each big folder is for, which ones are safe to clear or shrink and how (an app's own clear-cache or empty-trash command, a Finder step, a Terminal command), roughly how much each would free, and what to leave alone: system folders, cloud placeholders that take no local space, and anything the user would have to recreate by hand. \
Rank by space reclaimed against risk, biggest safe wins first, and say when something is too small to be worth the trouble. \
Be honest when a folder's purpose is unclear from its name and path, and ask before guessing. \
The app deletes nothing and you have no tools and no files to read, so answer from the message alone, run no commands, and never claim to have checked or removed anything; if a Terminal command would help the user, give it exactly, and warn when it is destructive. \
The app's figures are measured facts, not faults. The disk's used figure is larger than the scan total, and that gap is expected: the guide below says why (local snapshots, purgeable space, swap, folders the scan skips or cannot read). Never suggest the app is wrong or broken. Mention the gap or skipped folders only when the user asks or it changes the advice, briefly, and when folders could not be read end with the Full Disk Access step. \
Write tightly: short paragraphs or bullets, headings only when they help, paths as written in the message.";

/// The guide sections that explain the app's own figures, so the model explains them as the
/// guide does rather than guessing.
const GUIDE_SECTIONS: [&str; 5] = [
    "The map",
    "Free space",
    "Cloud folders",
    "Scanning",
    "Full Disk Access",
];

/// The system prompt: SYSTEM, then the relevant sections of docs/guide.md, built once.
fn system() -> &'static str {
    static PROMPT: std::sync::LazyLock<String> = std::sync::LazyLock::new(|| {
        let guide = include_str!("../../../docs/guide.md");
        let picked: Vec<String> = guide
            .split("\n## ")
            .skip(1)
            .filter_map(|sec| {
                let (title, body) = sec.split_once('\n')?;
                GUIDE_SECTIONS
                    .contains(&title.trim())
                    .then(|| format!("## {}\n{}", title.trim(), body.trim()))
            })
            .collect();
        format!(
            "{SYSTEM}\n\nFrom the app's user guide:\n\n{}",
            picked.join("\n\n")
        )
    });
    &PROMPT
}

#[derive(Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct Chunk {
    chat_id: String,
    text: String,
}

#[derive(Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct Done {
    chat_id: String,
    session_id: Option<String>,
    text: String,
    error: Option<String>,
}

/// The CLI processes under way, by chat, so a chat can be stopped and nothing outlives the app.
#[derive(Default)]
pub struct Running {
    children: Mutex<Vec<(String, u32)>>,
}

impl Running {
    fn remove(&self, pid: u32) {
        if let Ok(mut ch) = self.children.lock() {
            ch.retain(|(_, p)| *p != pid);
        }
    }
    pub fn kill_all(&self) {
        if let Ok(ch) = self.children.lock() {
            for (_, pid) in ch.iter() {
                kill_group(*pid);
            }
        }
    }
}

/// The CLIs start helpers of their own, so the whole process group goes.
fn kill_group(pid: u32) {
    unsafe {
        libc::kill(-(pid as libc::pid_t), libc::SIGTERM);
    }
}

/// Where a CLI is installed: the usual places first, then the login shell's PATH.
fn find(name: &str, extra: &[&str]) -> Option<PathBuf> {
    let home = std::env::var("HOME").unwrap_or_default();
    let mut places: Vec<String> = vec![format!("{home}/.local/bin/{name}")];
    places.extend(extra.iter().map(|p| format!("{home}/{p}")));
    places.extend([
        format!("/opt/homebrew/bin/{name}"),
        format!("/usr/local/bin/{name}"),
    ]);
    for p in places {
        let p = PathBuf::from(p);
        if p.is_file() {
            return Some(p);
        }
    }
    let out = Command::new("/bin/zsh")
        .args(["-lc", &format!("command -v {name}")])
        .output()
        .ok()?;
    let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
    (!s.is_empty() && PathBuf::from(&s).is_file()).then(|| PathBuf::from(s))
}

fn version(bin: &PathBuf) -> Option<String> {
    let out = Command::new(bin).arg("--version").output().ok()?;
    String::from_utf8_lossy(&out.stdout)
        .lines()
        .map(str::trim)
        .find(|l| !l.is_empty())
        .map(|l| l.trim_end_matches('.').to_string())
}

#[derive(Serialize)]
pub struct Model {
    id: String,
    name: String,
}

#[derive(Serialize)]
pub struct Cli {
    path: Option<String>,
    version: Option<String>,
    models: Vec<Model>,
}

#[derive(Serialize)]
pub struct Status {
    claude: Cli,
    codex: Cli,
    antigravity: Cli,
    copilot: Cli,
}

/// Which CLIs are installed, and the models each offers. With no models anywhere, the window
/// disables Ask and says why.
#[tauri::command]
pub async fn assistant_status() -> Result<Status, String> {
    tauri::async_runtime::spawn_blocking(|| {
        let cli = |bin: Option<PathBuf>, models: Vec<Model>| Cli {
            version: bin.as_ref().and_then(version),
            models: if bin.is_some() { models } else { Vec::new() },
            path: bin.map(|p| p.to_string_lossy().to_string()),
        };
        Status {
            claude: cli(claude::find(), claude::models()),
            codex: cli(codex::find(), codex::models()),
            antigravity: cli(antigravity::find(), antigravity::models()),
            copilot: cli(copilot::find(), copilot::models()),
        }
    })
    .await
    .map_err(|e| e.to_string())
}

fn spawn(
    cmd: &mut Command,
    running: &Running,
    chat_id: &str,
    name: &str,
) -> Result<(Child, ChildStdout, std::thread::JoinHandle<String>), String> {
    use std::os::unix::process::CommandExt;
    cmd.stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0);
    let mut child = cmd
        .spawn()
        .map_err(|e| format!("Couldn't start {name}: {e}"))?;
    let stdout = child.stdout.take().ok_or("no stdout")?;
    let stderr = child.stderr.take();
    let err = std::thread::spawn(move || {
        let mut s = String::new();
        if let Some(mut e) = stderr {
            let _ = e.read_to_string(&mut s);
        }
        s
    });
    if let Ok(mut ch) = running.children.lock() {
        ch.push((chat_id.to_string(), child.id()));
    }
    Ok((child, stdout, err))
}

fn finish(mut child: Child, running: &Running) -> bool {
    let ok = child.wait().is_ok_and(|s| s.success());
    running.remove(child.id());
    ok
}

/// The error for a CLI that answered nothing: its stderr if it failed with any, else a plain line.
fn silent(name: &str, ok: bool, stderr: std::thread::JoinHandle<String>) -> String {
    let msg = stderr.join().unwrap_or_default();
    if msg.trim().is_empty() || ok {
        format!("{name} stopped without answering")
    } else {
        msg.trim().to_string()
    }
}

/// Puts a question to the CLI the model id names; the answer arrives as `ask-chunk` events while
/// it is written and `ask-done` closes it, with the session to resume for a follow-up.
#[tauri::command]
pub async fn ask(
    app: AppHandle,
    running: tauri::State<'_, Arc<Running>>,
    chat_id: String,
    prompt: String,
    model: String,
    session: Option<String>,
) -> Result<(), String> {
    let cli = if model.starts_with(antigravity::PREFIX) {
        "agy"
    } else if model.starts_with(copilot::PREFIX) {
        "copilot"
    } else if model.starts_with("claude") {
        "claude"
    } else {
        "codex"
    };
    // Each CLI gets an empty folder of its own to run in, so there is nothing to read.
    let cwd = app
        .path()
        .app_data_dir()
        .map_err(|e| e.to_string())?
        .join(cli);
    std::fs::create_dir_all(&cwd).map_err(|e| e.to_string())?;
    let running = Arc::clone(&running);
    match cli {
        "agy" => antigravity::ask(app, running, cwd, chat_id, prompt, model, session),
        "copilot" => copilot::ask(app, running, cwd, chat_id, prompt, model, session),
        "claude" => claude::ask(app, running, cwd, chat_id, prompt, model, session),
        _ => codex::ask(app, running, cwd, chat_id, prompt, model, session),
    }
}

/// Stops a chat's answer; the window hears `ask-done` as the process ends.
#[tauri::command]
pub fn ask_cancel(running: tauri::State<'_, Arc<Running>>, chat_id: String) {
    if let Ok(ch) = running.children.lock() {
        for (_, pid) in ch.iter().filter(|(id, _)| id == &chat_id) {
            kill_group(*pid);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A renamed guide heading would silently drop a section from the prompt.
    #[test]
    fn prompt_carries_every_guide_section_it_names() {
        let p = system();
        for title in GUIDE_SECTIONS {
            assert!(
                p.contains(&format!("## {title}\n")),
                "missing section {title}"
            );
        }
        assert!(p.starts_with(SYSTEM));
        assert!(!p.contains("## Ask"));
    }
}
