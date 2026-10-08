// Copyright (c) 2026 Wayne Davies
// SPDX-License-Identifier: MIT (see LICENSE at the repository root)

//! Claude Code in print mode, streaming JSON: text arrives as deltas while it is written. Run
//! with no tools at all and no MCP servers.

use super::{finish, silent, spawn, system, Chunk, Done, Model, Running};
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::Command;
use std::sync::Arc;
use tauri::{AppHandle, Emitter};

/// The app's ids for Claude Code's aliases: "claude:opus" runs `--model opus`.
pub const PREFIX: &str = "claude:";

pub fn find() -> Option<PathBuf> {
    super::find("claude", &[".claude/local/claude"])
}

/// Claude Code's aliases, which it resolves to the newest model of each family, so a new Opus
/// needs no app update.
pub fn models() -> Vec<Model> {
    [
        ("opus", "Claude Opus"),
        ("sonnet", "Claude Sonnet"),
        ("haiku", "Claude Haiku"),
    ]
    .into_iter()
    .map(|(id, name)| Model {
        id: format!("{PREFIX}{id}"),
        name: name.into(),
    })
    .collect()
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
        "Claude Code isn't installed, or couldn't be found. Install it and sign in, then try again.",
    )?;
    let mut cmd = Command::new(bin);
    cmd.current_dir(&cwd).arg("-p").arg(&prompt).args([
        "--model",
        model.strip_prefix(PREFIX).unwrap_or(&model),
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--strict-mcp-config",
        "--tools",
        "",
        "--append-system-prompt",
        system(),
    ]);
    if let Some(s) = &session {
        cmd.args(["--resume", s]);
    }
    let (child, stdout, stderr) = spawn(&mut cmd, &running, &chat_id, "Claude Code")?;
    std::thread::spawn(move || {
        let mut text = String::new();
        let mut session_id = None;
        let mut error = None;
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            let Ok(v) = serde_json::from_str::<serde_json::Value>(&line) else {
                continue;
            };
            match v.get("type").and_then(|t| t.as_str()) {
                Some("stream_event") => {
                    let ev = &v["event"];
                    if ev["type"] == "content_block_delta" && ev["delta"]["type"] == "text_delta" {
                        if let Some(t) = ev["delta"]["text"].as_str() {
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
                Some("system") => {
                    if let Some(s) = v["session_id"].as_str() {
                        session_id = Some(s.to_string());
                    }
                }
                Some("result") => {
                    if let Some(s) = v["session_id"].as_str() {
                        session_id = Some(s.to_string());
                    }
                    if v["is_error"].as_bool() == Some(true) {
                        error = Some(
                            v["result"]
                                .as_str()
                                .unwrap_or("Claude returned an error")
                                .to_string(),
                        );
                    } else if text.is_empty() {
                        if let Some(r) = v["result"].as_str() {
                            text = r.to_string();
                        }
                    }
                }
                _ => {}
            }
        }
        let ok = finish(child, &running);
        if error.is_none() && !ok && text.is_empty() {
            error = Some(silent("Claude", ok, stderr));
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
