// Copyright (c) 2026 Wayne Davies
// SPDX-License-Identifier: MIT (see LICENSE at the repository root)

import { invoke } from "@tauri-apps/api/core";
import type { AppInfo, Progress, View, FileEntry } from "./types";

export interface Cli {
  path: string | null;
  version: string | null;
  models: { id: string; name: string }[];
}
export interface AssistantStatus {
  claude: Cli;
  codex: Cli;
  antigravity: Cli;
  copilot: Cli;
}

export const api = {
  assistantStatus: () => invoke<AssistantStatus>("assistant_status"),
  /// Puts a question to Claude Code; the answer arrives as `ask-chunk` and `ask-done` events.
  ask: (chatId: string, prompt: string, model: string, session: string | null) =>
    invoke<void>("ask", { chatId, prompt, model, session }),
  askCancel: (chatId: string) => invoke<void>("ask_cancel", { chatId }),
  files: (path: string) => invoke<FileEntry[]>("get_files", { path }),
  state: () => invoke<AppInfo>("get_state"),
  progress: () => invoke<Progress>("get_progress"),
  /// The subtree under `path`, with folders smaller than `minFraction` of it folded away.
  tree: (path: string, minFraction = 1 / 4000) => invoke<View>("get_tree", { path, minFraction }),
  startScan: (root?: string) => invoke<void>("start_scan", { root: root ?? null }),
  stopScan: () => invoke<void>("stop_scan"),
  reveal: (path: string) => invoke<void>("reveal", { path }),
  getInfo: (path: string) => invoke<void>("get_info", { path }),
  openPrivacySettings: () => invoke<void>("open_privacy_settings"),
};
