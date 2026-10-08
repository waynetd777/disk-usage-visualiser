// Copyright (c) 2026 Wayne Davies
// SPDX-License-Identifier: MIT (see LICENSE at the repository root)

// The help's text: docs/guide.md, built in, so the help and the guide on GitHub are one copy.
// Each `##` is a section the drawer can open and search.

import guideMd from "../docs/guide.md?raw";

export interface HelpSection {
  title: string;
  /** The `##` heading's anchor, as GitHub makes it, for links within the guide. */
  anchor: string;
  body: string;
}

/** GitHub's anchor for a heading. */
export const slug = (heading: string) =>
  heading
    .trim()
    .toLowerCase()
    .replace(/[^\w\s-]/g, "")
    .replace(/\s/g, "-");

export function parseGuide(md: string): { title: string; intro: string; sections: HelpSection[] } {
  let title = "Help";
  let intro = "";
  const sections: HelpSection[] = [];
  for (const line of md.replace(/\r\n/g, "\n").split("\n")) {
    if (line.includes("](docs/") || line.includes("screenshots/")) continue;
    const h1 = /^# (.+)$/.exec(line);
    const h2 = /^## (.+)$/.exec(line);
    if (h1) title = h1[1].trim();
    else if (h2) sections.push({ title: h2[1].trim(), anchor: slug(h2[1]), body: "" });
    else if (sections.length) sections[sections.length - 1].body += `${line}\n`;
    else intro += `${line}\n`;
  }
  for (const s of sections) s.body = s.body.trim();
  return { title, intro: intro.trim(), sections };
}

export const GUIDE = parseGuide(guideMd);

/** Sections with every word of the query, those with words in their heading first. */
export function searchHelp(q: string): HelpSection[] {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return [];
  return GUIDE.sections
    .map((s) => {
      const head = s.title.toLowerCase();
      const hay = `${head} ${s.body.toLowerCase()}`;
      return words.every((w) => hay.includes(w))
        ? { s, score: words.filter((w) => head.includes(w)).length }
        : null;
    })
    .filter((h): h is { s: HelpSection; score: number } => h !== null)
    .sort((a, b) => b.score - a.score)
    .map((h) => h.s);
}

const esc = (s: string) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

/** Bold, italic, `code` and links. A link keeps its target in data-href, for the drawer to follow. */
function inline(text: string): string {
  const codes: string[] = [];
  let t = text.replace(/`([^`]+)`/g, (_, c: string) => `\u0000${codes.push(c) - 1}\u0000`);
  t = esc(t)
    .replace(
      /\[([^\]]+)\]\(([^)\s]+)\)/g,
      (_, label: string, href: string) => `<a href="#" data-href="${href}">${label}</a>`,
    )
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^\w*])[*_](\S(?:.*?\S)?)[*_](?![\w*])/g, "$1<em>$2</em>");
  // eslint-disable-next-line no-control-regex
  return t.replace(/\u0000(\d+)\u0000/g, (_, i: string) => `<code>${esc(codes[Number(i)])}</code>`);
}

const cells = (row: string) =>
  row
    .trim()
    .replace(/^\||\|$/g, "")
    .split("|")
    .map((c) => c.trim());

/** The guide's Markdown as HTML: paragraphs, `###` headings, lists, tables and quotes. */
export function helpHtml(md: string): string {
  const out: string[] = [];
  const lines = md.split("\n");
  let i = 0;
  const isList = (l: string) => /^\s*([-*]|\d+\.)\s/.test(l);
  while (i < lines.length) {
    const l = lines[i];
    if (!l.trim()) {
      i++;
    } else if (/^### /.test(l)) {
      out.push(`<h4>${inline(l.slice(4))}</h4>`);
      i++;
    } else if (l.startsWith("|")) {
      const rows: string[] = [];
      while (i < lines.length && lines[i].startsWith("|")) rows.push(lines[i++]);
      const body = rows.slice(1).filter((r) => !/^\|[\s|:-]+\|?$/.test(r));
      const head = cells(rows[0])
        .map((c) => `<th>${inline(c)}</th>`)
        .join("");
      const rest = body
        .map(
          (r) =>
            `<tr>${cells(r)
              .map((c) => `<td>${inline(c)}</td>`)
              .join("")}</tr>`,
        )
        .join("");
      out.push(`<table><thead><tr>${head}</tr></thead><tbody>${rest}</tbody></table>`);
    } else if (isList(l)) {
      const ordered = /^\s*\d+\./.test(l);
      const items: string[] = [];
      while (i < lines.length && lines[i].trim() && (isList(lines[i]) || /^\s+\S/.test(lines[i]))) {
        if (isList(lines[i])) items.push(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, ""));
        else items[items.length - 1] += ` ${lines[i].trim()}`;
        i++;
      }
      const tag = ordered ? "ol" : "ul";
      out.push(`<${tag}>${items.map((x) => `<li>${inline(x)}</li>`).join("")}</${tag}>`);
    } else if (l.startsWith(">")) {
      const q: string[] = [];
      while (i < lines.length && lines[i].startsWith(">")) q.push(lines[i++].replace(/^>\s?/, ""));
      out.push(`<blockquote>${inline(q.join(" "))}</blockquote>`);
    } else {
      const p: string[] = [];
      while (
        i < lines.length &&
        lines[i].trim() &&
        !/^(### |\||>)/.test(lines[i]) &&
        !isList(lines[i])
      )
        p.push(lines[i++].trim());
      out.push(`<p>${inline(p.join(" "))}</p>`);
    }
  }
  return out.join("");
}
