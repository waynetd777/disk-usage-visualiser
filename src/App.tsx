// Copyright (c) 2026 Wayne Davies
// SPDX-License-Identifier: MIT (see LICENSE at the repository root)

import { useCallback, useEffect, useRef, useState } from "react";
import { open as pickFolder } from "@tauri-apps/plugin-dialog";
import { writeText } from "@tauri-apps/plugin-clipboard-manager";
import { api } from "./api";
import Ask from "./Ask";
import HelpDrawer from "./Help";
import Tooltips from "./Tooltips";
import { describeView } from "./context";
import { useAssistant } from "./assistant";
import { hideSplash } from "./main";
import Treemap, { REVEAL, type Block } from "./Treemap";
import type { AppInfo, Progress, View } from "./types";
import { abbrev, fmt, fmtDuration, fmtN, fmtWhen, parentPath } from "./util";

const RELOAD_ICON = (
  <svg
    viewBox="0 0 16 16"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.8"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9" />
    <path d="M13.6 1.8v3.4h-3.4" />
  </svg>
);
const ASK_ICON = (
  <svg
    viewBox="0 0 16 16"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.8"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M2.5 3.5A1.5 1.5 0 0 1 4 2h8a1.5 1.5 0 0 1 1.5 1.5v6A1.5 1.5 0 0 1 12 11H7l-3.2 2.7V11H4a1.5 1.5 0 0 1-1.5-1.5z" />
  </svg>
);
const HELP_ICON = (
  <svg
    viewBox="0 0 16 16"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.8"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <circle cx="8" cy="8" r="6.2" />
    <path d="M6.2 6.3a1.9 1.9 0 1 1 2.7 1.7c-.6.3-.9.7-.9 1.3" />
    <path d="M8 11.4h.01" />
  </svg>
);
const STOP_ICON = (
  <svg viewBox="0 0 16 16" fill="currentColor">
    <rect x="3" y="3" width="10" height="10" rx="2" />
  </svg>
);
const RevealIcon = () => <span className="svgwrap" dangerouslySetInnerHTML={{ __html: REVEAL }} />;

/// Folders smaller than this share of the folder on screen are folded into a "more" block.
const MIN_FRACTION = 1 / 4000;

/// Whether the map draws the volume's free space, remembered between launches. On unless turned off.
const FREE_KEY = "showFree";
function loadShowFree(): boolean {
  try {
    return localStorage.getItem(FREE_KEY) !== "0";
  } catch {
    return true;
  }
}
function saveShowFree(on: boolean) {
  try {
    localStorage.setItem(FREE_KEY, on ? "1" : "0");
  } catch {
    // Not remembered; the toggle still works for this launch.
  }
}

export default function App() {
  const [info, setInfo] = useState<AppInfo | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [viewPath, setViewPath] = useState<string | null>(null);
  const [tree, setTree] = useState<View | null>(null);
  const [treeError, setTreeError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [menu, setMenu] = useState<{ block: Block; x: number; y: number } | null>(null);
  const [showFree, setShowFree] = useState(loadShowFree);
  const [askOpen, setAskOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  // A question Help hands to Ask, typed into its box; `n` makes a repeat of the same text count.
  const [askSeed, setAskSeed] = useState<{ text: string; n: number } | null>(null);
  // Ask and Help share the right edge, so opening one closes the other.
  const openAsk = useCallback((on: boolean) => {
    setAskOpen(on);
    if (on) setHelpOpen(false);
  }, []);
  const openHelp = useCallback((on: boolean) => {
    setHelpOpen(on);
    if (on) setAskOpen(false);
  }, []);
  const assistant = useAssistant();
  const toggleFree = useCallback(() => {
    setShowFree((on) => {
      saveShowFree(!on);
      return !on;
    });
  }, []);
  const toastTimer = useRef<number | undefined>(undefined);
  const wasScanning = useRef(false);
  // Where you have been, like Finder's back and forward. Zooming pushes; a new root starts over.
  const [hist, setHist] = useState<{ stack: string[]; idx: number }>({ stack: [], idx: -1 });

  const say = useCallback((m: string) => {
    setToast(m);
    window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 2400);
  }, []);

  const refreshInfo = useCallback(
    () =>
      api
        .state()
        .then(setInfo)
        .catch(() => {}),
    [],
  );

  // Startup: wait for the cached scan to load, then draw it. Poll quickly; the read is ~100s of ms.
  useEffect(() => {
    let alive = true;
    const tick = () => {
      api
        .state()
        .then((s) => {
          if (!alive) return;
          setInfo(s);
          if (s.loading) {
            window.setTimeout(tick, 150);
            return;
          }
          setViewPath((p) => p ?? s.root);
          setHist((h) => (h.stack.length ? h : { stack: [s.root], idx: 0 }));
          hideSplash();
        })
        .catch(() => {
          if (alive) window.setTimeout(tick, 400);
        });
    };
    tick();
    return () => {
      alive = false;
    };
  }, []);

  const [treeTick, setTreeTick] = useState(0);
  // The parts of `info` the effects below follow; `info` itself changes on every poll.
  const ready = info !== null && !info.loading;
  const infoRoot = info?.root;
  const scanFinished = info?.scan?.finished;
  // While a scan runs, follow its progress; when it ends, reload the state and the tree.
  useEffect(() => {
    if (!ready) return;
    let alive = true;
    let timer: number | undefined;
    const tick = () => {
      api
        .progress()
        .then((p) => {
          if (!alive) return;
          setProgress(p);
          if (p.scanning) {
            wasScanning.current = true;
            timer = window.setTimeout(tick, 250);
          } else {
            if (wasScanning.current) {
              wasScanning.current = false;
              void refreshInfo();
              setTree(null); // forces a reload for the same path below
              setTreeTick((t) => t + 1);
            }
            timer = window.setTimeout(tick, 1500);
          }
        })
        .catch(() => {
          if (alive) timer = window.setTimeout(tick, 1000);
        });
    };
    tick();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [ready, infoRoot, refreshInfo]);

  // The tree for the folder on screen. Reloaded when the path changes or a scan finishes.
  useEffect(() => {
    if (!viewPath || !ready || infoRoot === undefined) return;
    let alive = true;
    api
      .tree(viewPath, MIN_FRACTION)
      .then((t) => {
        if (alive) {
          setTree(t);
          setTreeError(null);
        }
      })
      .catch((e) => {
        if (!alive) return;
        // The path is not in this scan (e.g. the root changed): fall back to the root.
        if (viewPath !== infoRoot) {
          setViewPath(infoRoot);
          setHist({ stack: [infoRoot], idx: 0 });
        } else {
          setTree(null);
          setTreeError(String(e));
        }
      });
    return () => {
      alive = false;
    };
  }, [viewPath, infoRoot, ready, scanFinished, treeTick]);

  const scanning = progress?.scanning ?? info?.scanning ?? false;

  const reveal = useCallback(
    (path: string) => {
      api
        .reveal(path)
        .then(() => say(`Revealed in Finder: ${path}`))
        .catch((e) => say(String(e)));
    },
    [say],
  );

  const go = useCallback((path: string) => {
    setMenu(null);
    setViewPath(path);
    setHist((h) => {
      if (h.stack[h.idx] === path) return h;
      const stack = [...h.stack.slice(0, h.idx + 1), path];
      return { stack, idx: stack.length - 1 };
    });
  }, []);
  const zoomTo = go;

  const canBack = hist.idx > 0;
  const canForward = hist.idx >= 0 && hist.idx < hist.stack.length - 1;
  const back = useCallback(() => {
    if (hist.idx <= 0) return;
    setMenu(null);
    setViewPath(hist.stack[hist.idx - 1]);
    setHist({ ...hist, idx: hist.idx - 1 });
  }, [hist]);
  const forward = useCallback(() => {
    if (hist.idx >= hist.stack.length - 1) return;
    setMenu(null);
    setViewPath(hist.stack[hist.idx + 1]);
    setHist({ ...hist, idx: hist.idx + 1 });
  }, [hist]);

  const up = useCallback(() => {
    if (!viewPath || !info || viewPath === info.root) return;
    go(parentPath(viewPath) ?? info.root);
  }, [viewPath, info, go]);

  const startScan = useCallback(
    (root?: string) => {
      api
        .startScan(root)
        .then(() => {
          if (root) {
            setViewPath(root);
            setHist({ stack: [root], idx: 0 });
          }
          void refreshInfo();
        })
        .catch((e) => say(String(e)));
    },
    [refreshInfo, say],
  );

  const stopScan = useCallback(() => {
    api
      .stopScan()
      .then(() =>
        say(`Scan stopped. Showing the ${info?.scan ? fmtWhen(info.scan.finished) : "last"} scan.`),
      )
      .catch((e) => say(String(e)));
  }, [say, info]);

  const chooseFolder = useCallback(async () => {
    const picked = await pickFolder({
      directory: true,
      multiple: false,
      defaultPath: info?.root ?? "/",
      title: "Choose a folder to scan",
    });
    if (typeof picked === "string" && picked) {
      if (scanning) await api.stopScan().catch(() => {});
      // Give the running scan a moment to notice the cancel before the new one starts.
      window.setTimeout(() => startScan(picked), scanning ? 300 : 0);
    }
  }, [info, scanning, startScan]);

  // Shortcuts: ⌘R rescan, ⌘O choose a folder, ⌘↑ / Esc up a level, ⌘⇧R reveal the folder on
  // screen, ⌘K Ask.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (menu) {
          setMenu(null);
          return;
        }
        if (askOpen) {
          setAskOpen(false);
          return;
        }
        if (helpOpen) {
          setHelpOpen(false);
          return;
        }
        up();
        return;
      }
      // ? outside a text box opens and closes the help, with or without ⌘.
      if (e.key === "?" && !e.ctrlKey && !e.altKey) {
        if ((e.target as HTMLElement)?.closest?.("input, textarea, select")) return;
        e.preventDefault();
        openHelp(!helpOpen);
        return;
      }
      if (!e.metaKey) return;
      if (e.key === "k") {
        e.preventDefault();
        if (assistant.available) openAsk(!askOpen);
      } else if (e.key === "r" && !e.shiftKey) {
        e.preventDefault();
        if (!scanning) startScan();
      } else if ((e.key === "R" || e.key === "r") && e.shiftKey) {
        e.preventDefault();
        if (viewPath) reveal(viewPath);
      } else if (e.key === "o") {
        e.preventDefault();
        void chooseFolder();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        up();
      } else if (e.key === "[") {
        e.preventDefault();
        back();
      } else if (e.key === "]") {
        e.preventDefault();
        forward();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [
    menu,
    askOpen,
    helpOpen,
    openAsk,
    openHelp,
    assistant.available,
    up,
    back,
    forward,
    scanning,
    startScan,
    viewPath,
    reveal,
    chooseFolder,
  ]);

  useEffect(() => {
    if (!menu) return;
    const close = (e: MouseEvent) => {
      if (!(e.target as HTMLElement).closest?.(".menu")) setMenu(null);
    };
    window.addEventListener("mousedown", close);
    return () => window.removeEventListener("mousedown", close);
  }, [menu]);

  const home = info?.home ?? "";
  const root = info?.root ?? "/";
  const crumbs: { name: string; path: string }[] = [];
  if (info && viewPath) {
    crumbs.push({ name: info.root_name, path: root });
    if (viewPath !== root) {
      const rel = root === "/" ? viewPath.slice(1) : viewPath.slice(root.length + 1);
      let p = root;
      for (const part of rel.split("/").filter(Boolean)) {
        p = p === "/" ? `/${part}` : `${p}/${part}`;
        crumbs.push({ name: part, path: p });
      }
    }
  }

  const pct =
    progress && progress.expected_items
      ? Math.min(99, Math.round((100 * progress.items) / progress.expected_items))
      : null;
  const usedPct = info && info.volume.total ? (100 * info.volume.used) / info.volume.total : 0;
  // Whole-disk scans only: what statfs counts as used but no folder walk can reach.
  const notScanned =
    info && root === "/" && info.scan ? Math.max(0, info.volume.used - info.scan.size) : 0;
  // Free space only means something beside a whole volume, so it is drawn at the top of a scan of
  // `/` or of a disk under /Volumes, never inside a folder.
  const volumeRoot = root === "/" || /^\/Volumes\/[^/]+$/.test(root);
  const freeHere = volumeRoot && viewPath === root && info ? info.volume.free : 0;
  const showFdaBanner = info && !info.full_disk_access && (info.scan?.denied ?? 0) > 0;
  // What Ask puts to Claude: the folder on screen, as it is when the question is sent.
  const askContext = useCallback(
    () => (tree && info ? { path: tree.path, text: describeView(tree, info) } : null),
    [tree, info],
  );

  return (
    <div className="app">
      <aside className="sidebar" data-tauri-drag-region="deep">
        <div className="brand">Disk Usage</div>
        <div className="sec first">Scanning</div>
        <button
          className="rootpick"
          onClick={() => void chooseFolder()}
          title="Choose the top-level folder to scan (⌘O)"
        >
          <span className="disk" />
          <span className="nm">
            {info ? (root === "/" ? info.root_name : abbrev(root, home)) : "…"}
          </span>
          <span className="chev">▾</span>
        </button>
        {info && info.volume.total > 0 && (
          <div className="used" title={`${fmt(info.volume.free)} free`}>
            {fmt(info.volume.used)} used of {fmt(info.volume.total)}
            <div className="bar">
              <i style={{ width: `${usedPct.toFixed(1)}%` }} />
            </div>
            {notScanned > 0 && (
              <div
                className="unscanned"
                title="Space the disk reports as used that the folder walk cannot reach: local Time Machine snapshots, purgeable caches, swap, the recovery volume, and folders macOS denied access to."
              >
                incl. {fmt(notScanned)} not scanned
              </div>
            )}
          </div>
        )}
        <button className="ghost" onClick={() => void chooseFolder()}>
          Choose folder…
        </button>

        {info && info.recent.length > 0 && (
          <>
            <div className="sec">Recent folders</div>
            {info.recent.map((r) => (
              <button
                key={r.path}
                className={`recent${r.path === root ? " cur" : ""}`}
                onClick={() => {
                  if (r.path !== root) startScan(r.path);
                }}
                title={`${r.path}\nScanned ${fmtWhen(r.when)}`}
              >
                <span className="nm">{r.path === "/" ? r.name : abbrev(r.path, home)}</span>
                <span className="sz">{fmt(r.size)}</span>
              </button>
            ))}
          </>
        )}

        <div className="sec">Colour = folder level</div>
        <div className="legend">
          {[1, 2, 3, 4, 5, 6, 7, 8].map((i) => (
            <div key={i} className={`d${i}`}>
              <i />
              <span>Level {i}</span>
            </div>
          ))}
          <div className="f">
            <i />
            <span>Loose files</span>
          </div>
          {showFree && volumeRoot && (
            <div className="fr">
              <i />
              <span>Free space</span>
            </div>
          )}
        </div>

        <div className="spacer" />
        <div className="foot">
          {info?.scan ? (
            <>
              <b>Last scan {fmtWhen(info.scan.finished)}</b>
              <br />
              {fmtN(info.scan.items)} items in {fmtDuration(info.scan.duration_ms)}
              <br />
              {info.scan.denied > 0 ? (
                <span
                  className="warnt"
                  title="Mostly system folders owned by root (e.g. /private/var/folders, /System/Library/Templates). Full Disk Access opens your own protected folders, not those."
                >
                  {fmtN(info.scan.denied)} folder{info.scan.denied === 1 ? "" : "s"} not readable
                </span>
              ) : (
                <span>Cached for next launch</span>
              )}
            </>
          ) : scanning ? (
            <>
              <b>First scan running</b>
              <br />
              The map appears when it finishes.
            </>
          ) : (
            <>
              <b>No scan yet</b>
              <br />
              Press Reload to scan.
            </>
          )}
        </div>
      </aside>

      <main className="main">
        <div className="topbar" data-tauri-drag-region="deep">
          <div className="navbtns">
            <button
              className="navb"
              disabled={!canBack}
              onClick={back}
              title="Back (⌘[)"
              aria-label="Back"
            >
              ‹
            </button>
            <button
              className="navb"
              disabled={!canForward}
              onClick={forward}
              title="Forward (⌘])"
              aria-label="Forward"
            >
              ›
            </button>
          </div>
          <div className="crumbs">
            {crumbs.map((c, i) => (
              <span key={c.path} className="crumb">
                {i > 0 && <span className="sep">›</span>}
                {i === crumbs.length - 1 ? (
                  <span className="cur">{c.name}</span>
                ) : (
                  <button onClick={() => zoomTo(c.path)}>{c.name}</button>
                )}
              </span>
            ))}
            {tree && (
              <>
                <span className="tot">
                  {fmt(tree.size)}
                  {tree.cloud ? ` on disk · ${fmt(tree.apparent)} in cloud` : ""} ·{" "}
                  {fmtN(tree.items)} items
                </span>
                <button
                  className="rvc"
                  title={`Reveal ${tree.name} in Finder (⌘⇧R)`}
                  onClick={() => reveal(tree.path)}
                >
                  <RevealIcon />
                </button>
              </>
            )}
          </div>
          <div className="grow" />
          <button
            className={`btn toggle${showFree ? " on" : ""}`}
            onClick={toggleFree}
            aria-pressed={showFree}
            title={
              volumeRoot
                ? `${showFree ? "Hide" : "Show"} the disk's free space in the map`
                : "Free space is shown at the top of a whole-disk scan"
            }
          >
            <span className="sw" />
            <span>Free space</span>
          </button>
          {scanning ? (
            <span className="pill info">
              Scanning{info?.scan ? ` · showing ${fmtWhen(info.scan.finished)} scan` : ""}
            </span>
          ) : info?.scan ? (
            <span className="pill good">Up to date · {fmtWhen(info.scan.finished)}</span>
          ) : null}
          {scanning ? (
            <button
              className="btn stop"
              onClick={stopScan}
              title="Stop the scan and keep the last results"
            >
              {STOP_ICON}
              <span>Stop</span>
            </button>
          ) : (
            <button className="btn" onClick={() => startScan()} title="Re-scan the whole tree (⌘R)">
              {RELOAD_ICON}
              <span>Reload</span>
            </button>
          )}
          {/* A disabled button gets no pointer events, so the reason sits on a wrapper. */}
          <span className="tipwrap" title={assistant.reason ?? undefined}>
            <button
              className={`btn${askOpen ? " on" : ""}`}
              disabled={!assistant.available}
              onClick={() => openAsk(!askOpen)}
              aria-pressed={askOpen}
              title="Ask an AI where space can be reclaimed (⌘K)"
            >
              {ASK_ICON}
              <span>Ask</span>
            </button>
          </span>
          <button
            className={`btn${helpOpen ? " on" : ""}`}
            onClick={() => openHelp(!helpOpen)}
            aria-pressed={helpOpen}
            aria-label="Help"
            title="Help (?)"
          >
            {HELP_ICON}
          </button>
        </div>

        {scanning && progress && (
          <div className="scan">
            <div className="what">
              <span className="spinner" />
              <b>
                {info?.scan ? "Rescanning" : "Scanning"} {info?.root_name}
              </b>
              <span className="path">{progress.current}</span>
            </div>
            <div className="nums">
              {fmtN(progress.items)}
              {progress.expected_items
                ? ` of ~${fmt(progress.expected_items).replace(/ .*/, "")}${progress.expected_items >= 1e6 ? "M" : progress.expected_items >= 1e3 ? "K" : ""} items`
                : " items"}
              {pct !== null ? ` · ${pct}%` : ""} · {fmtDuration(progress.elapsed_ms)}
            </div>
            <div className={`bar${pct === null ? " indet" : ""}`}>
              <i style={pct === null ? {} : { width: `${pct}%` }} />
            </div>
          </div>
        )}

        <div className="content">
          {showFdaBanner && (
            <div className="banner warn">
              <div className="grow">
                <b>{fmtN(info.scan!.denied)} folders could not be read.</b> Give Disk Usage Full
                Disk Access to include them, then Reload.
              </div>
              <button className="btn small" onClick={() => void api.openPrivacySettings()}>
                Open System Settings
              </button>
            </div>
          )}
          {tree ? (
            <Treemap
              tree={tree}
              free={showFree && freeHere > 0 ? freeHere : null}
              onZoom={zoomTo}
              onReveal={reveal}
              onContext={(block, x, y) => setMenu({ block, x, y })}
            />
          ) : (
            <div className="empty">
              {treeError
                ? treeError
                : scanning
                  ? "Scanning… the map appears when the first scan finishes."
                  : info && !info.loading
                    ? "No scan yet. Press Reload."
                    : "Loading…"}
            </div>
          )}
        </div>
      </main>

      {menu && (
        <ContextMenu
          {...menu}
          onClose={() => setMenu(null)}
          onReveal={reveal}
          onZoom={zoomTo}
          say={say}
        />
      )}
      {toast && <div className="toast">{toast}</div>}
      <Ask open={askOpen} onClose={() => setAskOpen(false)} context={askContext} seed={askSeed} />
      <HelpDrawer
        open={helpOpen}
        onClose={() => setHelpOpen(false)}
        onAsk={(text) => {
          if (!assistant.available) return;
          setAskSeed({ text, n: Date.now() });
          openAsk(true);
        }}
      />
      <Tooltips />
    </div>
  );
}

/// The right-click menu on a block.
function ContextMenu({
  block,
  x,
  y,
  onClose,
  onReveal,
  onZoom,
  say,
}: {
  block: Block;
  x: number;
  y: number;
  onClose: () => void;
  onReveal: (path: string) => void;
  onZoom: (path: string) => void;
  say: (m: string) => void;
}) {
  const v = block.view;
  const zoomable = block.kind !== "more" && (v.kids.length > 0 || v.files > 0 || v.more > 0);
  const style = {
    left: Math.min(x, window.innerWidth - 230),
    top: Math.min(y, window.innerHeight - 170),
  };
  return (
    <div className="menu" style={style}>
      <div className="mh">{v.path}</div>
      <button
        onClick={() => {
          onClose();
          onReveal(v.path);
        }}
      >
        Reveal in Finder<span className="k">⌘-click</span>
      </button>
      <button
        disabled={!zoomable}
        onClick={() => {
          onZoom(v.path);
        }}
      >
        Zoom in<span className="k">click</span>
      </button>
      <button
        onClick={() => {
          onClose();
          writeText(v.path)
            .then(() => say(`Copied ${v.path}`))
            .catch((e: unknown) => say(String(e)));
        }}
      >
        Copy path
      </button>
      <hr />
      <button
        onClick={() => {
          onClose();
          api.getInfo(v.path).catch((e: unknown) => say(String(e)));
        }}
      >
        Get Info…
      </button>
    </div>
  );
}
