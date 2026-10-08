// Copyright (c) 2026 Wayne Davies
// SPDX-License-Identifier: MIT (see LICENSE at the repository root)

// Tooltips everywhere in the app, in place of macOS's plain ones. Any element with a title gets
// one: the title moves to data-tip on first hover so the native tooltip never shows as well.
// Shown after a short pause, below the element and centred on it; above when there is no room
// below, beside it when there is room neither way; always kept inside the window. Gone on
// leaving, pressing, scrolling or the window losing focus. Keyboard focus shows it too.

import { useEffect, useLayoutEffect, useRef, useState } from "react";

const GAP = 6;
const EDGE = 8;
const DELAY = 450;

/** The element's text for a tooltip: its title (moved to data-tip), or an icon-only control's label. */
function tipText(t: HTMLElement): string | null {
  if (t.title) {
    t.dataset.tip = t.title;
    t.removeAttribute("title");
  }
  if (t.dataset.tip) return t.dataset.tip;
  if (!t.textContent?.trim()) return t.getAttribute("aria-label");
  return null;
}

export default function Tooltips() {
  const [tip, setTip] = useState<{ text: string; anchor: DOMRect } | null>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let timer: number | undefined;
    let el: HTMLElement | null = null;
    const hide = () => {
      window.clearTimeout(timer);
      el = null;
      setTip(null);
      setPos(null);
    };
    const show = (target: HTMLElement | null, delay: number) => {
      const t = (target?.closest?.("[title], [data-tip]") ??
        target?.closest?.("button[aria-label], [role=button][aria-label]")) as HTMLElement | null;
      if (t === el) return;
      hide();
      if (!t) return;
      const text = tipText(t);
      if (!text) return;
      el = t;
      timer = window.setTimeout(() => {
        if (el !== t || !t.isConnected) return;
        setTip({ text, anchor: t.getBoundingClientRect() });
      }, delay);
    };
    const over = (e: MouseEvent) => show(e.target as HTMLElement, DELAY);
    const focus = (e: FocusEvent) => {
      // Only keyboard focus: a click focuses too, and a pressed button needs no tooltip.
      const t = e.target as HTMLElement;
      if (t.matches?.(":focus-visible")) show(t, DELAY);
    };
    const out = (e: MouseEvent) => {
      // Leaving the element for somewhere with no tooltip; a move within it keeps the tooltip.
      if (el && !el.contains(e.relatedTarget as Node)) hide();
    };
    document.addEventListener("mouseover", over);
    document.addEventListener("mouseout", out);
    document.addEventListener("focusin", focus);
    document.addEventListener("focusout", hide);
    document.addEventListener("mousedown", hide, true);
    document.addEventListener("keydown", hide, true);
    document.addEventListener("scroll", hide, true);
    window.addEventListener("blur", hide);
    window.addEventListener("resize", hide);
    return () => {
      hide();
      document.removeEventListener("mouseover", over);
      document.removeEventListener("mouseout", out);
      document.removeEventListener("focusin", focus);
      document.removeEventListener("focusout", hide);
      document.removeEventListener("mousedown", hide, true);
      document.removeEventListener("keydown", hide, true);
      document.removeEventListener("scroll", hide, true);
      window.removeEventListener("blur", hide);
      window.removeEventListener("resize", hide);
    };
  }, []);

  // Place the tooltip once its size is known: below, else above, else beside; then clamp.
  useLayoutEffect(() => {
    const b = box.current;
    if (!tip || !b) return;
    const { width: w, height: h } = b.getBoundingClientRect();
    const a = tip.anchor;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let left: number;
    let top: number;
    if (a.bottom + GAP + h + EDGE <= vh) {
      top = a.bottom + GAP;
      left = a.left + a.width / 2 - w / 2;
    } else if (a.top - GAP - h >= EDGE) {
      top = a.top - GAP - h;
      left = a.left + a.width / 2 - w / 2;
    } else if (a.right + GAP + w + EDGE <= vw) {
      left = a.right + GAP;
      top = a.top + a.height / 2 - h / 2;
    } else {
      left = a.left - GAP - w;
      top = a.top + a.height / 2 - h / 2;
    }
    left = Math.max(EDGE, Math.min(left, vw - w - EDGE));
    top = Math.max(EDGE, Math.min(top, vh - h - EDGE));
    setPos({ left, top });
  }, [tip]);

  if (!tip) return null;
  return (
    <div
      ref={box}
      className="tip"
      role="tooltip"
      style={pos ? { left: pos.left, top: pos.top } : { left: 0, top: 0, visibility: "hidden" }}
    >
      {tip.text}
    </div>
  );
}
