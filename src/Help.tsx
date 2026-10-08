// Copyright (c) 2026 Wayne Davies
// SPDX-License-Identifier: MIT (see LICENSE at the repository root)

// The help drawer: `?` or the title bar's ? button opens it on the right, showing the user guide
// (docs/guide.md) section by section. Typing searches it; a link in the guide opens its section.
// It stays open while you use the map beside it; Esc or × closes it.

import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { GUIDE, type HelpSection, helpHtml, searchHelp } from "./guides";

const CHEVRON = (
  <svg
    className="help-chev"
    viewBox="0 0 16 16"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.8"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M6 3.5 10.5 8 6 12.5" />
  </svg>
);

function HelpText({ md, onLink }: { md: string; onLink: (anchor: string) => void }) {
  const html = useMemo(() => helpHtml(md), [md]);
  return (
    <div
      className="help-text"
      onClick={(e) => {
        const a = (e.target as HTMLElement).closest<HTMLElement>("a[data-href]");
        if (!a) return;
        e.preventDefault();
        const m = /^#([\w-]+)$/.exec(a.dataset.href ?? "");
        if (m) onLink(m[1]);
      }}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}

/** `scroll` brings it to the top of the drawer: the section a link opened. */
function Section({
  s,
  open,
  scroll,
  onLink,
}: {
  s: HelpSection;
  open?: boolean;
  scroll?: boolean;
  onLink: (anchor: string) => void;
}) {
  const ref = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    if (scroll) ref.current?.scrollIntoView({ block: "start" });
  }, [scroll]);
  return (
    <details ref={ref} className="help-sec" open={open}>
      <summary>
        {CHEVRON}
        {s.title}
      </summary>
      <HelpText md={s.body} onLink={onLink} />
    </details>
  );
}

export default function HelpDrawer({
  open,
  onClose,
  onAsk,
}: {
  open: boolean;
  onClose: () => void;
  /** Opens Ask with a question typed in, for what the guide doesn't answer. */
  onAsk: (question: string) => void;
}) {
  const [q, setQ] = useState("");
  const [at, setAt] = useState<string | null>(null);
  const box = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) box.current?.focus();
  }, [open]);

  // Closing clears the search, so it reopens on the guide.
  const close = () => {
    setQ("");
    onClose();
  };

  const goTo = (anchor: string) => {
    setQ("");
    setAt(anchor);
  };
  const query = q.trim();

  let body: ReactNode;
  if (query) {
    const hits = searchHelp(query).slice(0, 30);
    body = hits.length ? (
      hits.map((s) => <Section key={s.anchor} s={s} open onLink={goTo} />)
    ) : (
      <p className="help-none">Nothing in the help matches “{query}”. Ask can look further.</p>
    );
  } else {
    body = (
      <>
        {GUIDE.intro && <HelpText md={GUIDE.intro} onLink={goTo} />}
        <div>
          {GUIDE.sections.map((s, i) => (
            <Section
              key={s.anchor}
              s={s}
              open={at ? s.anchor === at : i === 0}
              scroll={s.anchor === at}
              onLink={goTo}
            />
          ))}
        </div>
      </>
    );
  }

  return (
    <aside
      className="help"
      hidden={!open}
      role="complementary"
      aria-label="Help"
      onClick={(e) => e.stopPropagation()}
      onContextMenu={(e) => e.stopPropagation()}
      onMouseMove={(e) => e.stopPropagation()}
    >
      <div className="help-head">
        <b>{GUIDE.title}</b>
        <span className="help-sub">help</span>
        <div className="grow" />
        <button
          className="btn small"
          onClick={onClose}
          aria-label="Close help"
          title="Close help (Esc)"
        >
          ✕
        </button>
      </div>
      <div className="help-search">
        <input
          ref={box}
          type="search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search the help"
          aria-label="Search the help"
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.stopPropagation();
              if (q) setQ("");
              else close();
            }
          }}
        />
      </div>
      <div className="help-body">{body}</div>
      <div className="help-foot">
        <button
          type="button"
          className="btn small"
          title="Ask the AI how something in Disk Usage works"
          onClick={() => onAsk(query ? `How do I ${query} in Disk Usage?` : "How do I ")}
        >
          Ask about Disk Usage
        </button>
      </div>
    </aside>
  );
}
