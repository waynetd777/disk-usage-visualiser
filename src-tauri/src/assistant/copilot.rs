// Copyright (c) 2026 Wayne Davies
// SPDX-License-Identifier: MIT (see LICENSE at the repository root)

//! GitHub Copilot CLI (`copilot -p --output-format json --stream on`). Only its view tool exists
//! (`--available-tools=view`; an empty list would mean every tool, web_fetch included), in an
//! empty folder, so there is nothing to read. Its built-in GitHub MCP server and the user's
//! instruction files are left out. It lists no models, so the app offers "auto" and Copilot
//! routes to whatever the account allows. It takes no system prompt, so the instructions open the
//! first message.

use super::{finish, silent, spawn, system, Chunk, Done, Model, Running};
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::Command;
use std::sync::Arc;
use tauri::{AppHandle, Emitter};

pub const PREFIX: &str = "copilot:";

pub fn find() -> Option<PathBuf> {
    super::find("copilot", &[])
}

pub fn models() -> Vec<Model> {
    vec![Model {
        id: format!("{PREFIX}auto"),
        name: "Copilot (Auto)".into(),
    }]
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
        "GitHub Copilot CLI isn't installed, or couldn't be found. Install it and sign in, then try again.",
    )?;
    // A follow-up resumes the session, which has the instructions already.
    let prompt = if session.is_some() {
        prompt
    } else {
        format!("<instructions>\n{}\n</instructions>\n\n{prompt}", system())
    };
    let mut cmd = Command::new(bin);
    cmd.current_dir(&cwd)
        .arg("-p")
        .arg(&prompt)
        .args([
            "--model",
            model.strip_prefix(PREFIX).unwrap_or(&model),
            "--output-format",
            "json",
            "--stream",
            "on",
        ])
        .args([
            "--no-custom-instructions",
            "--disable-builtin-mcps",
            "--no-auto-update",
            "--disallow-temp-dir",
            "--available-tools=view",
        ]);
    if let Some(s) = &session {
        cmd.arg(format!("--resume={s}"));
    }
    let (child, stdout, stderr) = spawn(&mut cmd, &running, &chat_id, "GitHub Copilot")?;

    std::thread::spawn(move || {
        let mut text = String::new();
        let mut session_id = None;
        let mut error = None;
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            let Ok(v) = serde_json::from_str::<serde_json::Value>(&line) else {
                continue;
            };
            let d = &v["data"];
            match v["type"].as_str() {
                Some("assistant.message_delta") => {
                    if let Some(t) = d["deltaContent"].as_str().filter(|t| !t.is_empty()) {
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
                Some("session.error") => {
                    error = Some(
                        d["message"]
                            .as_str()
                            .unwrap_or("Copilot returned an error")
                            .to_string(),
                    )
                }
                Some("result") => {
                    if let Some(s) = v["sessionId"].as_str() {
                        session_id = Some(s.to_string());
                    }
                }
                _ => {}
            }
        }
        if !text.is_empty() {
            error = None;
        }
        let ok = finish(child, &running);
        if error.is_none() && text.trim().is_empty() {
            error = Some(silent("Copilot", ok, stderr));
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
