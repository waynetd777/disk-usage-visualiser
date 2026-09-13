/// Decimal units, as macOS reports them. Three significant figures at most.
export function fmt(b: number): string {
  if (b >= 1e12) return (b / 1e12).toFixed(2).replace(/\.?0+$/, "") + " TB";
  if (b >= 1e9) return (b / 1e9 >= 100 ? Math.round(b / 1e9) : (b / 1e9).toFixed(1)) + " GB";
  if (b >= 1e6) return Math.round(b / 1e6) + " MB";
  if (b >= 1e3) return Math.round(b / 1e3) + " KB";
  return b + " B";
}

export function fmtN(n: number): string {
  return n.toLocaleString("en-GB");
}

/// "/Users/alex/Projects" -> "~/Projects" when the path is under the home folder.
export function abbrev(path: string, home: string): string {
  if (path === home) return "~";
  if (home && path.startsWith(home + "/")) return "~" + path.slice(home.length);
  return path;
}

export function fmtDuration(ms: number): string {
  if (ms < 1000) return `${ms} ms`;
  const s = Math.round(ms / 1000);
  if (s < 90) return `${s} s`;
  const m = Math.floor(s / 60);
  return `${m} min ${s - m * 60} s`;
}

/// "today 09:12", "yesterday 18:40", or "Mon 3 Sep 09:12".
export function fmtWhen(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const hm = d.toTimeString().slice(0, 5);
  const now = new Date();
  const day = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(now) - day(d)) / 86400000);
  if (diff === 0) return `today ${hm}`;
  if (diff === 1) return `yesterday ${hm}`;
  return `${d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })} ${hm}`;
}

export function parentPath(path: string): string | null {
  if (path === "/" || !path.includes("/")) return null;
  const i = path.lastIndexOf("/");
  return i === 0 ? "/" : path.slice(0, i);
}
