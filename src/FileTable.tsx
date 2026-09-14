import { useEffect, useMemo, useState } from "react";
import { api } from "./api";
import type { FileEntry } from "./types";
import { fmt, fmtN } from "./util";

const columns = [ ["name", "Name"], ["modified", "Date Modified"], ["size", "Size"], ["disk_size", "On Disk"], ["kind", "Kind"] ] as const;
type SortKey = typeof columns[number][0];
const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

export default function FileTable({ path, onReveal }: { path: string; onReveal: (path: string) => void }) {
  const [files, setFiles] = useState<FileEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: "name", desc: false });
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let alive = true;
    setFiles(null); setError(null);
    api.files(path).then(f => { if (alive) setFiles(f); }).catch(e => { if (alive) setError(String(e)); });
    return () => { alive = false; };
  }, [path, retry]);
  const rows = useMemo(() => (files ?? []).filter(f => f.name.toLocaleLowerCase().includes(query.toLocaleLowerCase())).sort((a, b) => {
    const x = a[sort.key], y = b[sort.key];
    const cmp = typeof x === "string" && typeof y === "string" ? collator.compare(x, y) : Number(x ?? -1) - Number(y ?? -1);
    return (sort.desc ? -cmp : cmp) || collator.compare(a.name, b.name);
  }), [files, query, sort]);
  return <section className="file-list" aria-label="Files in this folder" onClick={e => e.stopPropagation()} onContextMenu={e => e.stopPropagation()} onMouseMove={e => e.stopPropagation()}>
    <div className="file-toolbar">
      <input type="search" aria-label="Search files" placeholder="Search files by name" value={query} onChange={e => setQuery(e.target.value)} onKeyDown={e => { if (e.key === "Escape" && query) { e.stopPropagation(); setQuery(""); } }} />
      <span>{files ? `${fmtN(rows.length)} of ${fmtN(files.length)} files` : "Files"}</span>
    </div>
    <div className="file-scroll">
      <table>
        <thead><tr>{columns.map(([key, label]) => <th key={key} aria-sort={sort.key === key ? (sort.desc ? "descending" : "ascending") : "none"}><button onClick={() => setSort(s => ({ key, desc: s.key === key ? !s.desc : key !== "name" && key !== "kind" }))}>{label}{sort.key === key ? (sort.desc ? " ▾" : " ▴") : ""}</button></th>)}<th><span className="file-action-label">Finder</span></th></tr></thead>
        <tbody>{rows.map(f => <tr key={f.path} onDoubleClick={() => onReveal(f.path)}>
          <td title={f.name}>{f.name}</td><td>{f.modified === null ? "—" : new Date(f.modified * 1000).toLocaleString()}</td><td>{fmt(f.size)}</td><td>{fmt(f.disk_size)}</td><td>{f.kind}</td><td><button className="file-reveal" aria-label={`Reveal ${f.name} in Finder`} onClick={() => onReveal(f.path)}>↗</button></td>
        </tr>)}</tbody>
      </table>
      {error ? <div className="file-message" role="alert">{error} <button onClick={() => setRetry(r => r + 1)}>Retry</button></div> : !files ? <div className="file-message" role="status">Loading files…</div> : rows.length === 0 ? <div className="file-message">{query ? "No matching files." : "No files directly in this folder."}</div> : null}
    </div>
    <div className="file-note">Current files · Double-click a file to reveal it in Finder</div>
  </section>;
}
