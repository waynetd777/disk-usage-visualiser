"""IO helpers, token estimation, frontmatter, output envelope.

Everything here is deliberately dependency-free and side-effect-light so that
hooks (which run with `python3 -I -S`) can import it in a few milliseconds.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Extensions whose content is denser than prose; see BUILD-SPEC 17.1.
CODE_EXTENSIONS = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".go", ".rs", ".java",
    ".kt", ".cs", ".rb", ".php", ".swift", ".c", ".h", ".cpp", ".hpp", ".sh",
    ".sql", ".tf", ".yaml", ".yml", ".json", ".toml",
}

FRONTMATTER_KEY_ORDER = ["id", "title", "kind", "covers", "commit", "verified", "links", "tags"]
# Only `covers` is written as a block list; the rest stay on one line so a
# union merge of an index or a one-line edit never straddles keys.
BLOCK_LIST_KEYS = {"covers"}

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def now_iso() -> str:
    """ISO 8601 UTC, seconds precision, `Z` suffix. Ruling 2: UTC everywhere."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_utc() -> str:
    """`YYYY-MM-DD` in UTC. `datetime.UTC` is 3.11+, so `timezone.utc` it is."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def parse_iso(ts: str) -> Optional[datetime]:
    try:
        return datetime.strptime(ts.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Filesystem
# ---------------------------------------------------------------------------

def readonly() -> bool:
    return os.environ.get("SIFT_READONLY", "") not in ("", "0")


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def atomic_write(path: Path, text: str) -> bool:
    """Write `text` to `path` via a same-directory temp file + os.replace.

    Returns False without writing when SIFT_READONLY is set. Callers that must
    know whether the bytes landed check the return value; most do not care.
    """
    if readonly():
        sys.stderr.write("warning: readonly\n")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".sift-tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def append_line(path: Path, line: str) -> bool:
    """Append one line to a JSONL/text file. Append is atomic enough for our
    sizes on every filesystem we target, and it is what makes merge=union work."""
    if readonly():
        sys.stderr.write("warning: readonly\n")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line.rstrip("\n") + "\n")
    return True


def jdump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def read_jsonl(path: Path) -> List[dict]:
    out: List[dict] = []
    if not path.exists():
        return out
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue  # a half-written or conflict-marked line is skipped, not fatal
        if isinstance(obj, dict):
            out.append(obj)
    return out


def write_jsonl(path: Path, rows: Iterable[dict]) -> bool:
    return atomic_write(path, "".join(jdump(r) + "\n" for r in rows))


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(read_text(path))
    except ValueError:
        return default


def write_json(path: Path, obj: Any) -> bool:
    return atomic_write(path, json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=None) + "\n")


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def estimate_tokens(text: str, ext: str = "") -> int:
    divisor = 3.5 if ext.lower() in CODE_EXTENSIONS else 4.0
    return int(math.ceil(len(text) / divisor))


def estimate_tokens_for_chars(chars: int, ext: str = "") -> int:
    divisor = 3.5 if ext.lower() in CODE_EXTENSIONS else 4.0
    return int(math.ceil(chars / divisor))


# ---------------------------------------------------------------------------
# Frontmatter - a YAML subset: scalars, flow lists, block lists. No PyYAML.
# ---------------------------------------------------------------------------

def _scalar(raw: str) -> Any:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def _flow_list(raw: str) -> List[str]:
    inner = raw.strip()[1:-1].strip()
    if not inner:
        return []
    return [_scalar(p) for p in inner.split(",") if p.strip()]


def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Return (frontmatter dict, body). Missing/malformed frontmatter -> ({}, text)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    block = m.group(1)
    body = text[m.end():]
    data: Dict[str, Any] = {}
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line[:1].isspace() or line.lstrip().startswith("- "):
            i += 1  # orphan continuation; the key that owned it consumed what it wanted
            continue
        key, sep, rest = line.partition(":")
        if not sep:
            i += 1
            continue
        key = key.strip()
        rest = rest.strip()
        if rest.startswith("[") and rest.endswith("]"):
            data[key] = _flow_list(rest)
            i += 1
            continue
        if rest == "":
            items: List[str] = []
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt.strip().startswith("- ") and nxt[:1].isspace() or nxt.startswith("- "):
                    items.append(_scalar(nxt.strip()[2:]))
                    j += 1
                elif not nxt.strip():
                    j += 1
                else:
                    break
            if items:
                data[key] = items
                i = j
                continue
            data[key] = ""
            i += 1
            continue
        data[key] = _scalar(rest)
        i += 1
    return data, body


def _needs_quotes(value: str) -> bool:
    if value == "":
        return True
    if value[0] in "[]{}&*#?|-<>=!%@`'\"" or value[-1] in " \t":
        return True
    return ":" in value and ": " in value


def _emit_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    s = str(value)
    if _needs_quotes(s):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def dump_frontmatter(data: Dict[str, Any]) -> str:
    """Serialise in the fixed key order (BUILD-SPEC 7.1); unknown keys keep
    their relative order after the known ones."""
    keys = [k for k in FRONTMATTER_KEY_ORDER if k in data]
    keys += [k for k in data if k not in FRONTMATTER_KEY_ORDER]
    out = ["---"]
    for key in keys:
        value = data[key]
        if isinstance(value, (list, tuple)):
            if key in BLOCK_LIST_KEYS:
                out.append(f"{key}:")
                for item in value:
                    out.append(f"  - {_emit_scalar(item)}")
                if not value:
                    out[-1] = f"{key}: []"
            else:
                out.append(f"{key}: [" + ", ".join(_emit_scalar(v) for v in value) + "]")
        else:
            out.append(f"{key}: {_emit_scalar(value)}")
    out.append("---")
    return "\n".join(out) + "\n"


def render_page(frontmatter: Dict[str, Any], body: str) -> str:
    body = body if body.startswith("\n") else "\n" + body
    return dump_frontmatter(frontmatter) + body


# ---------------------------------------------------------------------------
# Markdown sections
# ---------------------------------------------------------------------------

def split_sections(body: str) -> "List[Tuple[str, str]]":
    """Split a page body into [(h2 title, section text)] in document order."""
    out: List[Tuple[str, str]] = []
    current: Optional[str] = None
    buf: List[str] = []
    in_fence = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
        if not in_fence and line.startswith("## "):
            if current is not None:
                out.append((current, "\n".join(buf).strip("\n")))
            current = line[3:].strip()
            buf = []
            continue
        if current is not None:
            buf.append(line)
    if current is not None:
        out.append((current, "\n".join(buf).strip("\n")))
    return out


def first_sentence(text: str) -> str:
    """First sentence of a block of prose, skipping list markers and blanks."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("```"):
            continue
        line = re.sub(r"^[-*+]\s+", "", line)
        m = re.search(r"(.+?[.!?])(\s|$)", line)
        return (m.group(1) if m else line).strip()
    return ""


# ---------------------------------------------------------------------------
# Output envelope
# ---------------------------------------------------------------------------

# Colour is used only for signal -- an error, or the next command to run --
# and never for data, matching what the AWS CLI does. It is emitted only to a
# TTY, so anything piped or captured (every test included) stays plain; it
# honours NO_COLOR, obeys an explicit --color, and is always off under --json.
_ANSI = {"red": "1;31", "green": "32", "action": "1;36"}
# A "plain number" cell: digits, optional thousands commas, decimals, sign. A
# column of these is right-aligned like a spreadsheet; "L1-20" or "3 files" is
# text and stays left, which is how the reader tells figures from labels.
_NUMERIC_CELL = re.compile(r"^-?\d[\d,]*(\.\d+)?$")
# A cell may carry colour codes (e.g. a green/red net figure). Width and the
# numeric test go by the visible text, so a coloured cell still lines up.
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _visible(value: Any) -> str:
    return _ANSI_RE.sub("", str(value))


def _color_on(stream: Any, mode: str, json_mode: bool) -> bool:
    if json_mode:
        return False
    if mode == "always":
        return True
    if mode == "never":
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    try:
        return bool(stream.isatty())
    except Exception:
        return False


class Out:
    """Collects warnings and prints either human text or the JSON envelope."""

    def __init__(self, cmd: str, version: str, json_mode: bool, quiet: bool = False,
                 color: str = "auto") -> None:
        self.cmd = cmd
        self.version = version
        self.json_mode = json_mode
        self.quiet = quiet
        self.warnings: List[str] = []
        self._lines: List[str] = []
        # Decided once per stream: stdout carries the next-step hints, stderr
        # carries the errors, and they are not always both a terminal.
        self._out_color = _color_on(sys.stdout, color, json_mode)
        self._err_color = _color_on(sys.stderr, color, json_mode)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def line(self, text: str = "") -> None:
        self._lines.append(text)

    def paint(self, text: str, kind: str, err: bool = False) -> str:
        """Wrap `text` in a semantic colour when its stream allows it.

        `kind` names a meaning (`red`, `green`, `action`), not a raw code, so
        the palette lives in one place. Returns `text` untouched whenever
        colour is off -- every non-TTY, NO_COLOR, or --json run -- so callers
        can paint unconditionally and the plain path is the same string.
        """
        on = self._err_color if err else self._out_color
        code = _ANSI.get(kind)
        if not on or not code:
            return text
        return "\033[{}m{}\033[0m".format(code, text)

    def action(self, text: str, err: bool = False) -> str:
        """A command the reader should run next. Highlighted on stdout by
        default, or on stderr (`err=True`) when it is the fix for an error."""
        return self.paint(text, "action", err=err)

    def table(self, rows: List[List[str]], headers: Optional[List[str]] = None) -> None:
        if not rows:
            return
        cols = len(rows[0])
        widths = [0] * cols
        body = ([headers] if headers else []) + rows
        for row in body:
            for i in range(cols):
                widths[i] = max(widths[i], len(_visible(row[i])))
        numeric = [self._numeric_col(rows, i) for i in range(cols)]

        def justify(value: Any, i: int) -> str:
            # Pad by the visible width so a cell carrying colour codes lines up
            # with the plain cells above and below it.
            s = str(value)
            pad = max(0, widths[i] - len(_visible(s)))
            return (" " * pad + s) if numeric[i] else (s + " " * pad)

        if headers:
            self.line("  ".join(justify(headers[i], i) for i in range(cols)).rstrip())
            self.line("  ".join("-" * widths[i] for i in range(cols)))
        for row in rows:
            self.line("  ".join(justify(row[i], i) for i in range(cols)).rstrip())

    @staticmethod
    def _numeric_col(rows: List[List[str]], i: int) -> bool:
        seen = False
        for row in rows:
            s = _visible(row[i]).strip()
            if not s:
                continue
            seen = True
            if not _NUMERIC_CELL.match(s):
                return False
        return seen

    def flush(self) -> None:
        """Write what has been collected so far, now, and keep collecting.

        Anything that asks the user a question calls this first. `emit` runs at
        the end of the command, so without it the question reaches the screen
        ahead of the output it is about - which is how you get asked to approve
        a diff you have not been shown.
        """
        if self.json_mode or self.quiet:
            return
        for line in self._lines:
            sys.stdout.write(line + "\n")
        self._lines = []
        sys.stdout.flush()

    def emit(self, data: Any = None) -> None:
        if self.json_mode:
            payload = {"ok": True, "cmd": self.cmd, "version": self.version,
                       "data": data, "warnings": self.warnings}
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
            return
        if self.quiet:
            return
        for line in self._lines:
            sys.stdout.write(line + "\n")
        for w in self.warnings:
            sys.stderr.write("warning: " + w + "\n")

    def fail(self, code: str, message: str) -> None:
        if self.json_mode:
            payload = {"ok": False, "cmd": self.cmd, "version": self.version,
                       "error": {"code": code, "message": message}}
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        else:
            sys.stderr.write(self.paint("error:", "red", err=True) + " " + message + "\n")


def num(n: int) -> str:
    """Thousands-grouped integer -- one place decides how a count reads."""
    return "{:,}".format(int(n))


def count(n: int, singular: str, plural: Optional[str] = None) -> str:
    """`1 file`, `3 files`: grouping and pluralisation, decided in one place."""
    word = singular if n == 1 else (plural if plural is not None else singular + "s")
    return "{} {}".format(num(n), word)


def truncate_note(total: int, shown: int) -> str:
    return f"({num(total - shown)} more, use --top)" if total > shown else ""


def short_id(*parts: str) -> str:
    import hashlib
    return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()[:4]
