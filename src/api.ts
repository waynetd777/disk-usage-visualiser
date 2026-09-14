import { invoke } from "@tauri-apps/api/core";
import type { AppInfo, Progress, View, FileEntry } from "./types";

export const api = {
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
