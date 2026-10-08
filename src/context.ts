// Copyright (c) 2026 Wayne Davies
// SPDX-License-Identifier: MIT (see LICENSE at the repository root)

// What Ask puts to Claude: the folder on screen and its biggest folders, as text.

import type { AppInfo, View } from "./types";
import { fmt, fmtN } from "./util";

/** What the chat is looking at: the folder on screen and the scan behind it, as text. */
export interface Context {
  path: string;
  text: string;
}

/// The folder on screen and its biggest folders, nested, for the first message of a chat. Folders
/// under half a percent of the shown folder are left out, with a line for what was folded.
export function describeView(v: View, info: AppInfo): string {
  const lines: string[] = [];
  const vol = info.volume;
  lines.push(
    `Scan root: ${info.root} — on the volume "${vol.name}", ${fmt(vol.total)} in all, ${fmt(vol.used)} used, ${fmt(vol.free)} free.`,
  );
  if (info.scan && info.scan.denied > 0) {
    lines.push(
      `${fmtN(info.scan.denied)} folders were skipped because the app could not read them; the sizes below cover what it could.`,
    );
  }
  const cloud = v.cloud ? ` (${fmt(v.apparent)} counting files kept only in the cloud)` : "";
  lines.push(
    `Folder on screen: ${v.path} — ${fmt(v.size)} on disk${cloud}, ${fmtN(v.items)} items, ` +
      `${fmt(v.loose_size)} in ${fmtN(v.files)} files directly inside it.`,
  );
  lines.push("");
  lines.push(
    "Biggest folders inside it, largest first; an indented line is inside the one above it:",
  );
  const floor = v.size * 0.005;
  const perLevel = [14, 8, 5];
  let budget = 90;
  const walk = (node: View, depth: number) => {
    if (depth >= perLevel.length || budget <= 0) return;
    const kids = [...node.kids].sort((a, b) => b.size - a.size);
    const shown = kids.filter((k) => k.size >= floor).slice(0, perLevel[depth]);
    const pad = "  ".repeat(depth);
    for (const k of shown) {
      if (budget-- <= 0) return;
      const bits = [`${fmt(k.size)} on disk`, `${fmtN(k.items)} items`];
      if (k.files > 0) bits.push(`${fmt(k.loose_size)} in ${fmtN(k.files)} files directly inside`);
      if (k.cloud)
        bits.push(`cloud folder: ${fmt(k.apparent)} in the cloud, ${fmt(k.size)} kept locally`);
      if (k.denied > 0) bits.push(`${fmtN(k.denied)} unreadable folders`);
      lines.push(`${pad}- ${depth === 0 ? k.path : k.name + "/"} — ${bits.join(", ")}`);
      walk(k, depth + 1);
    }
    const folded = kids.length - shown.length + node.more;
    const foldedSize = kids.slice(shown.length).reduce((s, k) => s + k.size, 0) + node.more_size;
    if (folded > 0 && foldedSize > 0 && budget-- > 0) {
      lines.push(`${pad}- …${fmtN(folded)} smaller folders, ${fmt(foldedSize)} together`);
    }
  };
  walk(v, 0);
  return lines.join("\n");
}
