// Copyright (c) 2026 Wayne Davies
// SPDX-License-Identifier: MIT (see LICENSE at the repository root)

// Ask: a chat panel over the map that puts the scan to Claude Code and asks where space can be
// reclaimed. The first message carries the folder on screen and its biggest folders; a follow-up
// resumes the CLI's session, and carries the scan again only if the folder on screen has changed.

import { useEffect, useMemo, useRef, useState } from "react";
import { listen } from "@tauri-apps/api/event";
import { writeHtml } from "@tauri-apps/plugin-clipboard-manager";
import { api } from "./api";
import { loadModel, modelGroups, pickModel, saveModel, useAssistant } from "./assistant";
import type { Context } from "./context";
import { mdToHtml } from "./md";

const SUGGESTIONS = [
  "What are the opportunities to reclaim space safely?",
  "What are the biggest folders here for?",
  "Which of these are caches I can clear, and how?",
  "What should I leave alone?",
];

interface Message {
  role: "user" | "assistant";
  text: string;
  error?: boolean;
}

const uid = () =>
  typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;

export default function AskPanel({
  open,
  onClose,
  context,
  seed,
}: {
  open: boolean;
  onClose: () => void;
  /** What is on screen now; null until the map has something to show. */
  context: () => Context | null;
  /** A question handed in from elsewhere (Help), typed into the box to edit or send. */
  seed?: { text: string; n: number } | null;
}) {
  const [preferred, setModel] = useState(loadModel);
  const assistant = useAssistant();
  // The remembered model if it is still offered, else the first that is.
  const model = pickModel(preferred, assistant.models);
  const [copied, setCopied] = useState<number | null>(null);
  const [chatId, setChatId] = useState(uid);
  const [messages, setMessages] = useState<Message[]>([]);
  const [session, setSession] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [q, setQ] = useState("");
  const lastPath = useRef<string | null>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const chatRef = useRef(chatId);
  chatRef.current = chatId;

  // One pair of listeners for the panel's life; a chunk for another chat (one stopped and
  // replaced) is ignored.
  useEffect(() => {
    const chunk = listen<{ chatId: string; text: string }>("ask-chunk", (e) => {
      if (e.payload.chatId !== chatRef.current) return;
      setMessages((m) => {
        const last = m[m.length - 1];
        if (!last || last.role !== "assistant") return m;
        return [...m.slice(0, -1), { ...last, text: last.text + e.payload.text }];
      });
    });
    const done = listen<{
      chatId: string;
      sessionId: string | null;
      text: string;
      error: string | null;
    }>("ask-done", (e) => {
      if (e.payload.chatId !== chatRef.current) return;
      const d = e.payload;
      setMessages((m) => {
        const last = m[m.length - 1];
        if (!last || last.role !== "assistant") return m;
        const fin: Message = d.error
          ? { role: "assistant", text: last.text || d.error, error: true }
          : { ...last, text: last.text || d.text };
        return [...m.slice(0, -1), fin];
      });
      if (d.sessionId) setSession(d.sessionId);
      setBusy(false);
    });
    return () => {
      void chunk.then((un) => un());
      void done.then((un) => un());
    };
  }, []);

  useEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  useEffect(() => {
    if (open) box.current?.focus();
  }, [open]);

  // A new seed is typed into the box as it arrives (derived state, set during render), and the
  // box takes focus with the caret at the end, ready to finish the question.
  const [seen, setSeen] = useState(seed);
  if (seed !== seen) {
    setSeen(seed);
    if (seed) setQ(seed.text);
  }
  useEffect(() => {
    if (!seed) return;
    const el = box.current;
    if (el) {
      el.focus();
      window.setTimeout(() => el.setSelectionRange(el.value.length, el.value.length), 0);
    }
  }, [seed]);

  const send = async (question: string) => {
    const text = question.trim();
    if (!text || busy) return;
    const ctx = context();
    let prompt = text;
    if (ctx && (!session || ctx.path !== lastPath.current)) {
      prompt = `${ctx.text}\n\nQuestion: ${text}`;
      lastPath.current = ctx.path;
    } else if (!ctx && !session) {
      prompt = `There is no scan on screen yet.\n\nQuestion: ${text}`;
    }
    setQ("");
    setBusy(true);
    setMessages((m) => [...m, { role: "user", text }, { role: "assistant", text: "" }]);
    try {
      await api.ask(chatId, prompt, model, session);
    } catch (e) {
      setMessages((m) => [...m.slice(0, -1), { role: "assistant", text: String(e), error: true }]);
      setBusy(false);
    }
  };

  const stop = () => {
    void api.askCancel(chatId);
  };

  const fresh = () => {
    if (busy) void api.askCancel(chatId);
    setChatId(uid());
    setMessages([]);
    setSession(null);
    setBusy(false);
    lastPath.current = null;
    box.current?.focus();
  };

  // Both flavours at once: Markdown for a plain text field, HTML for a rich one.
  const copy = async (i: number, text: string) => {
    try {
      await writeHtml(mdToHtml(text), text);
      setCopied(i);
      window.setTimeout(() => setCopied((c) => (c === i ? null : c)), 1500);
    } catch (e) {
      console.error(e);
    }
  };

  const chooseModel = (id: string) => {
    saveModel(id);
    setModel(id);
  };

  const where = useMemo(() => context()?.path ?? null, [context]);

  return (
    <aside
      className="ask"
      hidden={!open}
      aria-label="Ask about reclaiming space"
      onClick={(e) => e.stopPropagation()}
      onContextMenu={(e) => e.stopPropagation()}
      onMouseMove={(e) => e.stopPropagation()}
    >
      <div className="ask-head">
        <b>Ask</b>
        <select
          className="btn small"
          aria-label="Model"
          title="Which Claude model answers"
          value={model}
          onChange={(e) => chooseModel(e.target.value)}
        >
          {modelGroups(assistant.models).map((g) => (
            <optgroup key={g.provider} label={g.name}>
              {g.models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
        <div className="grow" />
        {messages.length > 0 && (
          <button
            className="btn small"
            onClick={fresh}
            title="Start a new chat about what is on screen"
          >
            New chat
          </button>
        )}
        <button className="btn small" onClick={onClose} aria-label="Close" title="Close (Esc)">
          ✕
        </button>
      </div>
      <div className="ask-scroll" ref={scroller}>
        {messages.length === 0 ? (
          <div className="ask-intro">
            <p>
              Claude looks at the scan of {where ? <code>{where}</code> : "the folder on screen"}{" "}
              and says what the biggest folders are for, what is safe to clear and what to leave
              alone. Nothing is deleted; it runs through the Claude Code already signed in on this
              Mac.
            </p>
            <div className="ask-chips">
              {SUGGESTIONS.map((s) => (
                <button key={s} type="button" className="chip" onClick={() => void send(s)}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        ) : (
          messages.map((m, i) =>
            m.role === "user" ? (
              <div key={i} className="ask-q">
                {m.text}
              </div>
            ) : m.text ? (
              <div key={i} className="ask-turn">
                <div
                  className={`ask-a${m.error ? " error" : ""}`}
                  dangerouslySetInnerHTML={{ __html: mdToHtml(m.text) }}
                />
                {!m.error && (
                  <button
                    type="button"
                    className="btn small ask-copy"
                    title="Copy this answer as text and as formatted text"
                    onClick={() => void copy(i, m.text)}
                  >
                    {copied === i ? "Copied" : "Copy"}
                  </button>
                )}
              </div>
            ) : (
              <div key={i} className="ask-a working" role="status">
                Thinking
                <span className="dots" aria-hidden="true">
                  <i>.</i>
                  <i>.</i>
                  <i>.</i>
                </span>
              </div>
            ),
          )
        )}
      </div>
      <form
        className="ask-input"
        onSubmit={(e) => {
          e.preventDefault();
          void send(q);
        }}
      >
        <textarea
          ref={box}
          rows={2}
          value={q}
          placeholder={messages.length ? "Ask a follow-up…" : "Ask about the space here…"}
          aria-label="Your question"
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send(q);
            } else if (e.key === "Escape") {
              e.stopPropagation();
              onClose();
            }
          }}
        />
        {busy ? (
          <button type="button" className="btn stop" onClick={stop} title="Stop the answer">
            Stop
          </button>
        ) : (
          <button type="submit" className="btn" disabled={!q.trim()} title="Send (Enter)">
            Send
          </button>
        )}
      </form>
    </aside>
  );
}
