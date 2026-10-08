// Copyright (c) 2026 Wayne Davies
// SPDX-License-Identifier: MIT (see LICENSE at the repository root)

//! Antigravity CLI (`agy -p --output-format stream-json`), Google's successor to Gemini CLI.
//! It has no flag to limit its tools, and some it would use unasked (web search); but a custom
//! agent's `tools` list is enforced, so each run writes one into its working folder
//! (`.agents/agents/disk-usage-ask.md`) with no tools at all. Model ids carry an "agy:" prefix,
//! since it also offers Claude models.

use super::{finish, silent, spawn, system, Chunk, Done, Model, Running};
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Arc;
use tauri::{AppHandle, Emitter};

pub const PREFIX: &str = "agy:";
const AGENT: &str = "disk-usage-ask";

pub fn find() -> Option<PathBuf> {
    super::find("agy", &[])
}

/// The models the signed-in account offers (`agy models`: id, tab, name). Empty when it isn't
/// signed in, so the app offers none.
pub fn models() -> Vec<Model> {
    let Some(bin) = find() else {
        return Vec::new();
    };
    let Ok(out) = Command::new(bin).arg("models").output() else {
        return Vec::new();
    };
    String::from_utf8_lossy(&out.stdout)
        .lines()
        .filter_map(|l| l.split_once('\t'))
        .map(|(id, name)| Model {
            id: format!("{PREFIX}{}", id.trim()),
            name: name.trim().to_string(),
        })
        .collect()
}

/// The agent the run uses: no tools, and the instructions as its system prompt.
fn write_agent(cwd: &Path) -> Result<(), String> {
    let dir = cwd.join(".agents").join("agents");
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let md = format!(
        "---\nname: {AGENT}\ndescription: Disk Usage's Ask, no tools\nmainAgent: true\ninheritMcp: false\ntools: []\n---\n# Instructions\n{}\n",
        system()
    );
    std::fs::write(dir.join(format!("{AGENT}.md")), md).map_err(|e| e.to_string())
}

pub fn ask(
    app: AppHandle,
    running: Arc<Running>,
    cwd: PathBuf,
    chat_id: String,
    prompt: String,
    model: String,
    session: Option<String>,
) -> Result<(), String> {
    let bin = find().ok_or(
        "Antigravity CLI isn't installed, or couldn't be found. Install it and sign in, then try again.",
    )?;
    write_agent(&cwd)?;
    let mut cmd = Command::new(bin);
    cmd.current_dir(&cwd).arg("-p").arg(&prompt).args([
        "--agent",
        AGENT,
        "--model",
        model.strip_prefix(PREFIX).unwrap_or(&model),
        "--sandbox",
        "--disable-slash-commands",
        "--output-format",
        "stream-json",
    ]);
    if let Some(s) = &session {
        cmd.args(["--conversation", s]);
    }
    let (child, stdout, stderr) = spawn(&mut cmd, &running, &chat_id, "Antigravity")?;

    std::thread::spawn(move || {
        let mut text = String::new();
        let mut session_id = None;
        let mut error = None;
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            let Ok(v) = serde_json::from_str::<serde_json::Value>(&line) else {
                continue;
            };
            match v["event"].as_str() {
                Some("step_update") => {
                    let s = &v["step_update"];
                    if let Some(c) = s["conversation_id"].as_str() {
                        session_id = Some(c.to_string());
                    }
                    if s["step_type"] == "agent_response" {
                        if let Some(t) = s["text_delta"].as_str().filter(|t| !t.is_empty()) {
                            text.push_str(t);
                            let _ = app.emit(
                                "ask-chunk",
                                Chunk {
                                    chat_id: chat_id.clone(),
                                    text: t.to_string(),
                                },
                            );
                        }
                    }
                }
                Some("result") => {
                    let r = &v["result"];
                    if let Some(c) = r["conversation_id"].as_str() {
                        session_id = Some(c.to_string());
                    }
                    if text.is_empty() {
                        if let Some(t) = r["response"].as_str() {
                            text = t.to_string();
                        }
                    }
                    // A refused action ends the turn with no answer.
                    let denied: Vec<&str> = r["denied_actions"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .filter_map(|d| d["display_name"].as_str())
                        .collect();
                    if text.trim().is_empty() && !denied.is_empty() {
                        error = Some(format!(
                            "Antigravity stopped without answering: it tried something Ask doesn't allow ({}). Try asking again, or another model.",
                            denied.join(", ")
                        ));
                    } else if r["status"].as_str().is_some_and(|s| s != "SUCCESS")
                        && text.is_empty()
                    {
                        error = Some(format!(
                            "Antigravity returned an error ({})",
                            r["status"].as_str().unwrap_or("")
                        ));
                    }
                }
                _ => {}
            }
        }
        let ok = finish(child, &running);
        if error.is_none() && text.trim().is_empty() {
            error = Some(silent("Antigravity", ok, stderr));
        }
        let _ = app.emit(
            "ask-done",
            Done {
                chat_id,
                session_id,
                text,
                error,
            },
        );
    });
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn agent_lists_no_tools() {
        let dir = std::env::temp_dir().join(format!("du-agy-{}", std::process::id()));
        write_agent(&dir).unwrap();
        let md = std::fs::read_to_string(dir.join(".agents/agents/disk-usage-ask.md")).unwrap();
        assert!(md.contains("tools: []\n---"));
        assert!(!md.contains("search_web") && !md.contains("run_command"));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
