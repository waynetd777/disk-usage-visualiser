// Copyright (c) 2026 Wayne Davies
// SPDX-License-Identifier: MIT (see LICENSE at the repository root)

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import type { FileEntry } from "./types";
import { fmt, fmtN } from "./util";

const columns = [
  ["name", "Name"],
  ["modified", "Date Modified"],
  ["size", "Size"],
  ["disk_size", "On Disk"],
  ["kind", "Kind"],
] as const;
type SortKey = (typeof columns)[number][0];
const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });
// Rows are added to the table in pages as the list is scrolled: a folder with thousands of files
// would otherwise freeze the app while every row is laid out at once.
const PAGE = 120;

export default function FileTable({
  path,
  onReveal,
}: {
  path: string;
  onReveal: (path: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: "name", desc: false });
  const [retry, setRetry] = useState(0);
  // The listing is kept with the request it answers, so a new path or a retry shows as loading
  // until its own answer arrives.
  const request = `${path}\0${String(retry)}`;
  const [result, setResult] = useState<{ request: string; files?: FileEntry[]; error?: string }>();
  const current = result?.request === request ? result : undefined;
  const files = current?.files ?? null;
  const error = current?.error ?? null;
  useEffect(() => {
    let alive = true;
    api
      .files(path)
      .then((f) => {
        if (alive) setResult({ request, files: f });
      })
      .catch((e: unknown) => {
        if (alive) setResult({ request, error: String(e) });
      });
    return () => {
      alive = false;
    };
  }, [path, request]);
  const rows = useMemo(
    () =>
      (files ?? [])
        .filter((f) => f.name.toLocaleLowerCase().includes(query.toLocaleLowerCase()))
        .sort((a, b) => {
          const x = a[sort.key],
            y = b[sort.key];
          const cmp =
            typeof x === "string" && typeof y === "string"
              ? collator.compare(x, y)
              : Number(x ?? -1) - Number(y ?? -1);
          return (sort.desc ? -cmp : cmp) || collator.compare(a.name, b.name);
        }),
    [files, query, sort],
  );
  // How many rows are in the table so far, kept with the rows it counts, so a new listing, filter
  // or sort starts over from one page.
  const [page, setPage] = useState<{ rows: FileEntry[]; shown: number }>();
  const shown = page?.rows === rows ? page.shown : PAGE;
  const scroller = useRef<HTMLDivElement>(null);
  const extend = () => {
    const el = scroller.current;
    if (el && shown < rows.length && el.scrollTop + el.clientHeight >= el.scrollHeight - 400) {
      setPage({ rows, shown: Math.min(rows.length, shown + PAGE) });
    }
  };
  // A tall window can show a whole page with no scrollbar, so keep adding until it scrolls.
  useLayoutEffect(extend);
  const visible = rows.slice(0, shown);
  return (
    <section
      className="file-list"
      aria-label="Files in this folder"
      onClick={(e) => e.stopPropagation()}
      onContextMenu={(e) => e.stopPropagation()}
      onMouseMove={(e) => e.stopPropagation()}
    >
      <div className="file-toolbar">
        <input
          type="search"
          aria-label="Search files"
          placeholder="Search files by name"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape" && query) {
              e.stopPropagation();
              setQuery("");
            }
          }}
        />
        <span>{files ? `${fmtN(rows.length)} of ${fmtN(files.length)} files` : "Files"}</span>
      </div>
      <div className="file-scroll" ref={scroller} onScroll={extend}>
        <table>
          <thead>
            <tr>
              {columns.map(([key, label]) => (
                <th
                  key={key}
                  aria-sort={sort.key === key ? (sort.desc ? "descending" : "ascending") : "none"}
                >
                  <button
                    onClick={() =>
                      setSort((s) => ({
                        key,
                        desc: s.key === key ? !s.desc : key !== "name" && key !== "kind",
                      }))
                    }
                  >
                    {label}
                    {sort.key === key ? (sort.desc ? " ▾" : " ▴") : ""}
                  </button>
                </th>
              ))}
              <th>
                <span className="file-action-label">Finder</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {visible.map((f) => (
              <tr key={f.path} onDoubleClick={() => onReveal(f.path)}>
                <td title={f.name}>{f.name}</td>
                <td>{f.modified === null ? "—" : new Date(f.modified * 1000).toLocaleString()}</td>
                <td>{fmt(f.size)}</td>
                <td>{fmt(f.disk_size)}</td>
                <td>{f.kind}</td>
                <td>
                  <button
                    className="file-reveal"
                    aria-label={`Reveal ${f.name} in Finder`}
                    onClick={() => onReveal(f.path)}
                  >
                    ↗
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {error ? (
          <div className="file-message" role="alert">
            {error} <button onClick={() => setRetry((r) => r + 1)}>Retry</button>
          </div>
        ) : !files ? (
          <div className="file-message" role="status">
            Loading files…
          </div>
        ) : rows.length === 0 ? (
          <div className="file-message">
            {query ? "No matching files." : "No files directly in this folder."}
          </div>
        ) : shown < rows.length ? (
          <div className="file-message" role="status">
            {fmtN(shown)} of {fmtN(rows.length)} shown · scroll for more
          </div>
        ) : null}
      </div>
      <div className="file-note">Current files · Double-click a file to reveal it in Finder</div>
    </section>
  );
}
