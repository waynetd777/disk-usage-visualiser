// Copyright (c) 2026 Wayne Davies
// SPDX-License-Identifier: MIT (see LICENSE at the repository root)

// Which AI CLIs are installed and the models they offer. Ask needs Claude Code, Codex,
// Antigravity or GitHub Copilot on this Mac; with none, the Ask button is disabled and says why.
// Checked once at start. The app has no settings screen, so the model is chosen in the Ask panel
// and remembered between launches.

import { useSyncExternalStore } from "react";
import { api, type AssistantStatus } from "./api";

export type Provider = "claude" | "codex" | "antigravity" | "copilot";
export interface ModelOpt {
  id: string;
  name: string;
  provider: Provider;
}

export const DEFAULT_MODEL = "claude:opus";

export const providerOf = (id: string): Provider =>
  id.startsWith("agy:")
    ? "antigravity"
    : id.startsWith("copilot:")
      ? "copilot"
      : id.startsWith("claude")
        ? "claude"
        : "codex";

/** The model menu's headings: by the tool that runs them (Antigravity offers Claude and GPT models too). */
const GROUP_NAME: Record<Provider, string> = {
  claude: "Claude Code",
  codex: "ChatGPT (Codex)",
  antigravity: "Antigravity (Google)",
  copilot: "GitHub Copilot",
};

/** The models by the tool that runs them, in a fixed order, leaving out tools with none. */
export const modelGroups = (models: ModelOpt[]) =>
  (["claude", "codex", "antigravity", "copilot"] as const)
    .map((p) => ({
      provider: p,
      name: GROUP_NAME[p],
      models: models.filter((m) => m.provider === p),
    }))
    .filter((g) => g.models.length);

/** The model to use: the preferred one if it is still offered, else the first that is. */
export function pickModel(preferred: string, models: ModelOpt[]): string {
  return models.some((m) => m.id === preferred) || models.length === 0 ? preferred : models[0].id;
}

const MODEL_KEY = "askModel";
export function loadModel(): string {
  try {
    return localStorage.getItem(MODEL_KEY) ?? DEFAULT_MODEL;
  } catch {
    return DEFAULT_MODEL;
  }
}
export function saveModel(id: string) {
  try {
    localStorage.setItem(MODEL_KEY, id);
  } catch {
    // Not remembered; the choice still holds for this launch.
  }
}

export interface Assistant {
  /** null until the first check returns. */
  status: AssistantStatus | null;
  models: ModelOpt[];
  /** False only once the check has found no model to offer, so nothing flickers at start. */
  available: boolean;
  /** Why Ask is disabled, for its tooltip; null while it is available. */
  reason: string | null;
}

let state: Assistant = { status: null, models: [], available: true, reason: null };
const listeners = new Set<() => void>();

export function refreshAssistant() {
  api
    .assistantStatus()
    .then((status) => {
      const tag = (p: Provider) => (m: { id: string; name: string }) => ({ ...m, provider: p });
      const models = [
        ...status.claude.models.map(tag("claude")),
        ...status.codex.models.map(tag("codex")),
        ...status.antigravity.models.map(tag("antigravity")),
        ...status.copilot.models.map(tag("copilot")),
      ];
      const installed = (["claude", "codex", "antigravity", "copilot"] as const).filter(
        (p) => status[p].path,
      );
      const reason =
        models.length > 0
          ? null
          : installed.length === 0
            ? "Ask needs an AI CLI on this Mac. Install Claude Code, Codex, Antigravity or GitHub Copilot, sign in, then relaunch."
            : `${installed.map((p) => GROUP_NAME[p]).join(" and ")} installed but offering no models: run it once in Terminal to sign in, then relaunch.`;
      state = { status, models, available: models.length > 0, reason };
      listeners.forEach((l) => l());
    })
    .catch(() => {});
}
refreshAssistant();

export function useAssistant(): Assistant {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => state,
  );
}
