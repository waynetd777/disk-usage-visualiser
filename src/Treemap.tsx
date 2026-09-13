import { useEffect, useRef } from "react";
import type { View } from "./types";
import { fmt, fmtN } from "./util";

/// What one block stands for. Folders are `dir`; the files directly inside a folder are one
/// `files` block, and folders too small to draw are one `more` block, so a folder's area is
/// always its whole size.
export type Block =
  | { kind: "dir"; view: View }
  | { kind: "files"; view: View }
  | { kind: "more"; view: View };

export function blockPath(b: Block): string {
  return b.view.path;
}

interface Props {
  tree: View;
  onZoom: (path: string) => void;
  onReveal: (path: string) => void;
  onContext: (b: Block, x: number, y: number) => void;
}

const GAP = 3;
const HEAD = 20;
const PAD = 3;

const CLOUD =
  '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"><path d="M4.6 12.5h7a2.7 2.7 0 0 0 .5-5.35A3.8 3.8 0 0 0 4.9 6.2 3.15 3.15 0 0 0 4.6 12.5z"/></svg>';
export const REVEAL =
  '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12 12 4"/><path d="M6.5 4H12v5.5"/></svg>';

interface Item {
  block: Block;
  size: number;
}

/// Squarified treemap (Bruls, Huizing, van Wijk). Items sorted by size, descending.
function squarify(items: Item[], w: number, h: number): { item: Item; x: number; y: number; w: number; h: number }[] {
  const rects: { item: Item; x: number; y: number; w: number; h: number }[] = [];
  const total = items.reduce((a, b) => a + b.size, 0);
  if (total <= 0 || w <= 0 || h <= 0) return rects;
  const scale = (w * h) / total;
  let rx = 0, ry = 0, rw = w, rh = h;
  let row: { item: Item; a: number }[] = [];
  const worst = (r: { a: number }[], side: number) => {
    let s = 0, mx = 0, mn = Infinity;
    for (const i of r) { s += i.a; mx = Math.max(mx, i.a); mn = Math.min(mn, i.a); }
    return Math.max((side * side * mx) / (s * s), (s * s) / (side * side * mn));
  };
  const layout = (r: { item: Item; a: number }[]) => {
    const s = r.reduce((a, b) => a + b.a, 0);
    if (rw >= rh) {
      const sw = s / rh; let cy = ry;
      for (const i of r) { const ch = i.a / sw; rects.push({ item: i.item, x: rx, y: cy, w: sw, h: ch }); cy += ch; }
      rx += sw; rw -= sw;
    } else {
      const sh = s / rw; let cx = rx;
      for (const i of r) { const cw = i.a / sh; rects.push({ item: i.item, x: cx, y: ry, w: cw, h: sh }); cx += cw; }
      ry += sh; rh -= sh;
    }
  };
  for (const it of items) {
    const a = { item: it, a: it.size * scale };
    const side = Math.min(rw, rh);
    if (side <= 0) break;
    if (row.length && worst([...row, a], side) > worst(row, side)) { layout(row); row = [a]; } else row.push(a);
  }
  if (row.length) layout(row);
  return rects;
}

/// Layout weights come from the scan (`View.weight`): size on disk, with the cloud floor. The
/// loose-files block follows the same rule so a folder of online-only files still shows up.
function looseWeight(v: View): number {
  return v.cloud ? Math.max(v.loose_size, Math.floor(v.loose_apparent / 50)) : v.loose_size;
}

function childrenOf(v: View): Item[] {
  const items: Item[] = v.kids.map((k) => ({ block: { kind: "dir", view: k }, size: k.weight }));
  const lw = looseWeight(v);
  if (lw > 0 || (v.files > 0 && v.kids.length === 0 && v.more === 0)) items.push({ block: { kind: "files", view: v }, size: Math.max(lw, 1) });
  if (v.more > 0 && v.more_weight > 0) items.push({ block: { kind: "more", view: v }, size: v.more_weight });
  items.sort((a, b) => b.size - a.size);
  return items;
}

export function blockTitle(b: Block): string {
  if (b.kind === "files") return b.view.files === 1 ? "1 file" : `${fmtN(b.view.files)} files`;
  if (b.kind === "more") return b.view.more === 1 ? "1 more folder" : `${fmtN(b.view.more)} more folders`;
  return b.view.name;
}

export function blockSize(b: Block): number {
  if (b.kind === "files") return b.view.loose_size;
  if (b.kind === "more") return b.view.more_size;
  return b.view.size;
}

export function blockCloud(b: Block): number {
  if (!b.view.cloud) return 0;
  if (b.kind === "files") return b.view.loose_apparent;
  if (b.kind === "more") return b.view.more_apparent;
  return b.view.apparent;
}

/// Every block on screen, by the id written into its element, so hover and click can find it.
const registry = new Map<string, Block>();

function draw(b: Block, x: number, y: number, w: number, h: number, out: HTMLElement, id: string) {
  if (w < 3 || h < 3) return;
  registry.set(id, b);
  const v = b.view;
  const el = document.createElement("div");
  const zoomable = b.kind === "dir" && (v.kids.length > 0 || v.files > 0 || v.more > 0);
  el.className =
    b.kind === "dir"
      ? `blk d${((v.depth - 1) % 8) + 1}${zoomable ? " zoom" : ""}${v.cloud ? " cloud" : ""}`
      : `blk ${b.kind}`;
  el.style.cssText = `left:${x}px;top:${y}px;width:${w}px;height:${h}px`;
  el.dataset.id = id;
  let labelled = false;
  if (w >= 56 && h >= 30) {
    labelled = true;
    const hd = document.createElement("div");
    hd.className = "hd";
    const nm = document.createElement("span");
    nm.className = "n";
    nm.textContent = blockTitle(b);
    hd.appendChild(nm);
    if (w >= 110) {
      const s = document.createElement("span");
      s.className = "s";
      const size = blockSize(b);
      const cloud = blockCloud(b);
      if (cloud > 0) {
        s.innerHTML =
          w >= 320 ? `${fmt(size)} on disk · ${CLOUD}<span class="cl">${fmt(cloud)}</span> in cloud`
          : w >= 190 ? `${fmt(size)} · ${CLOUD}<span class="cl">${fmt(cloud)}</span>`
          : `${fmt(size)} ${CLOUD}`;
      } else s.textContent = fmt(size);
      hd.appendChild(s);
    }
    if (b.kind === "dir") {
      const rv = document.createElement("button");
      rv.className = "rv";
      rv.title = "Reveal in Finder";
      rv.setAttribute("aria-label", "Reveal in Finder");
      rv.innerHTML = REVEAL;
      rv.dataset.reveal = id;
      hd.appendChild(rv);
    }
    el.appendChild(hd);
  }
  out.appendChild(el);
  if (b.kind !== "dir") return;
  const top = labelled ? HEAD : PAD;
  const iw = w - PAD * 2 - 2, ih = h - top - PAD - 2;
  if (iw < 10 || ih < 10) return;
  const items = childrenOf(v);
  if (!items.length) return;
  let i = 0;
  for (const r of squarify(items, iw, ih)) {
    const gx = r.x > 0 ? GAP / 2 : 0, gy = r.y > 0 ? GAP / 2 : 0;
    draw(r.item.block, PAD + r.x + gx, top + r.y + gy, r.w - gx - GAP / 2, r.h - gy - GAP / 2, el, `${id}.${i++}`);
  }
}

export default function Treemap({ tree, onZoom, onReveal, onContext }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const tipRef = useRef<HTMLDivElement>(null);
  const cbs = useRef({ onZoom, onReveal, onContext });
  cbs.current = { onZoom, onReveal, onContext };

  useEffect(() => {
    const map = ref.current;
    if (!map) return;
    const render = () => {
      registry.clear();
      map.innerHTML = "";
      const W = map.clientWidth, H = map.clientHeight;
      if (W < 20 || H < 20) return;
      const items = childrenOf(tree);
      if (!items.length) {
        const e = document.createElement("div");
        e.className = "empty";
        e.textContent = tree.items === 0 ? "This folder is empty." : "Nothing big enough to draw here.";
        map.appendChild(e);
        return;
      }
      let i = 0;
      for (const r of squarify(items, W, H)) {
        const gx = r.x > 0 ? GAP / 2 : 0, gy = r.y > 0 ? GAP / 2 : 0;
        const gr = r.x + r.w < W - 0.5 ? GAP / 2 : 0, gb = r.y + r.h < H - 0.5 ? GAP / 2 : 0;
        draw(r.item.block, r.x + gx, r.y + gy, r.w - gx - gr, r.h - gy - gb, map, `${i++}`);
      }
    };
    render();
    const ro = new ResizeObserver(render);
    ro.observe(map);
    return () => ro.disconnect();
  }, [tree]);

  const blockAt = (t: EventTarget | null): { el: HTMLElement; b: Block } | null => {
    const el = (t as HTMLElement | null)?.closest?.(".blk") as HTMLElement | null;
    if (!el) return null;
    const b = registry.get(el.dataset.id ?? "");
    return b ? { el, b } : null;
  };

  const onClick = (e: React.MouseEvent) => {
    const rv = (e.target as HTMLElement).closest?.(".rv") as HTMLElement | null;
    if (rv) {
      e.stopPropagation();
      const b = registry.get(rv.dataset.reveal ?? "");
      if (b) cbs.current.onReveal(b.view.path);
      return;
    }
    const hit = blockAt(e.target);
    if (!hit) return;
    if (e.metaKey) { cbs.current.onReveal(hit.b.view.path); return; }
    if (hit.b.kind === "dir" && hit.el.classList.contains("zoom")) cbs.current.onZoom(hit.b.view.path);
  };

  const onContextMenu = (e: React.MouseEvent) => {
    const hit = blockAt(e.target);
    if (!hit) return;
    e.preventDefault();
    if (tipRef.current) tipRef.current.hidden = true;
    cbs.current.onContext(hit.b, e.clientX, e.clientY);
  };

  const onMove = (e: React.MouseEvent) => {
    const tip = tipRef.current;
    if (!tip) return;
    const hit = blockAt(e.target);
    if (!hit) { tip.hidden = true; return; }
    const b = hit.b, v = b.view;
    const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;");
    let html: string;
    if (b.kind === "files") {
      html = `<b>${blockTitle(b)} directly in ${esc(v.name)}</b><div class="p">${esc(v.path)}</div><div class="r"><span>On disk</span><b>${fmt(v.loose_size)}</b>` +
        (v.cloud ? `<span>In cloud</span><b>${fmt(v.loose_apparent)}</b>` : "") +
        `<span>Share of ${esc(v.name)}</span><b>${v.size ? Math.round((100 * v.loose_size) / v.size) : 0}%</b></div>` +
        `<div class="hint">⌘-click or right-click to reveal ${esc(v.name)} in Finder</div>`;
    } else if (b.kind === "more") {
      html = `<b>${blockTitle(b)} in ${esc(v.name)}</b><div class="p">${esc(v.path)}</div><div class="r"><span>On disk</span><b>${fmt(v.more_size)}</b>` +
        `<span>Share of ${esc(v.name)}</span><b>${v.size ? Math.round((100 * v.more_size) / v.size) : 0}%</b></div>` +
        `<div class="hint">Each is too small to draw at this zoom. Zoom into ${esc(v.name)} to see them.</div>`;
    } else {
      const zoomable = hit.el.classList.contains("zoom");
      html = `<b>${esc(v.name)}</b><div class="p">${esc(v.path)}</div><div class="r"><span>On disk</span><b>${fmt(v.size)}</b>` +
        (v.cloud ? `<span>In cloud</span><b>${fmt(v.apparent)}</b>` : "") +
        `<span>Items</span><b>${fmtN(v.items)}</b>` +
        `<span>Level</span><b>${v.depth}</b>` +
        (v.denied ? `<span>Unreadable</span><b>${fmtN(v.denied)} folder${v.denied === 1 ? "" : "s"}</b>` : "") +
        `</div>` +
        (v.cloud && v.weight > v.size ? `<div class="hint">Drawn by cloud size: little or nothing of this is on disk.</div>` : "") +
        `<div class="hint">${zoomable ? "Click to zoom in · " : ""}⌘-click or right-click to reveal in Finder</div>`;
    }
    tip.innerHTML = html;
    tip.hidden = false;
    const r = tip.getBoundingClientRect();
    let x = e.clientX + 14, y = e.clientY + 16;
    if (x + r.width > window.innerWidth - 8) x = e.clientX - r.width - 10;
    if (y + r.height > window.innerHeight - 8) y = e.clientY - r.height - 10;
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  };

  return (
    <>
      <div className="map" ref={ref} onClick={onClick} onContextMenu={onContextMenu} onMouseMove={onMove} onMouseLeave={() => { if (tipRef.current) tipRef.current.hidden = true; }} />
      <div className="tip" ref={tipRef} hidden />
    </>
  );
}
