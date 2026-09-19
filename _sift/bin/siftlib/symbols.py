"""Top-level symbol extraction (BUILD-SPEC 17.2).

Line-anchored regexes only. This is an index, not a parser: a symbol runs from
its declaration line to the line before the next one, which is wrong for nested
declarations and right for the thing it is used for - telling an agent which
`offset`/`limit` to read.

`LANG_PATTERNS` is data. Adding a language is a table entry, not a code change.
"""
from __future__ import annotations

import re
from typing import Dict, List, Sequence, Tuple

# (compiled pattern, kind). Name is group 1.
_TS = [
    (r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)", "fn"),
    (r"^(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)", "class"),
    (r"^(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>", "fn"),
    (r"^(?:export\s+)?(?:interface|type)\s+([A-Za-z_$][\w$]*)", "type"),
]
_PY = [
    (r"^(?:async\s+)?def\s+(\w+)", "fn"),
    (r"^class\s+(\w+)", "class"),
]
_GO = [
    (r"^func\s+(?:\([^)]+\)\s+)?(\w+)", "fn"),
    (r"^type\s+(\w+)\s+struct\b", "class"),
    (r"^type\s+(\w+)\s+interface\b", "type"),
]
_RS = [
    (r"^(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)", "fn"),
    (r"^(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum)\s+(\w+)", "class"),
    (r"^impl(?:<[^>]*>)?\s+(?:[\w:<>]+\s+for\s+)?(\w+)", "section"),
]
_JVM = [
    (r"^\s{0,4}(?:public|private|protected)?\s*(?:static\s+)?(?:final\s+)?(?:abstract\s+)?(?:class|interface|enum|object|data class)\s+(\w+)", "class"),
    (r"^\s{4}(?:public|private|protected)?\s*(?:static\s+)?(?:suspend\s+)?(?:fun\s+|[\w<>\[\], ]+\s+)(\w+)\s*\(", "fn"),
]
_MD = [(r"^##?\s+(.+)$", "section")]

LANG_PATTERNS: Dict[str, List[Tuple[str, str]]] = {}
for _ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
    LANG_PATTERNS[_ext] = _TS
LANG_PATTERNS[".py"] = _PY
LANG_PATTERNS[".go"] = _GO
LANG_PATTERNS[".rs"] = _RS
for _ext in (".java", ".kt"):
    LANG_PATTERNS[_ext] = _JVM
LANG_PATTERNS[".md"] = _MD

_COMPILED: Dict[str, List[Tuple["re.Pattern[str]", str]]] = {}


def _patterns(ext: str) -> List[Tuple["re.Pattern[str]", str]]:
    ext = ext.lower()
    if ext not in _COMPILED:
        _COMPILED[ext] = [(re.compile(p), k) for p, k in LANG_PATTERNS.get(ext, [])]
    return _COMPILED[ext]


def supported(ext: str) -> bool:
    return ext.lower() in LANG_PATTERNS


def extract(text: str, ext: str, max_count: int = 30,
            token_divisor: float = 3.5) -> List[dict]:
    """[{name, kind, start, end, tokens}] with 1-based inclusive line numbers."""
    patterns = _patterns(ext)
    if not patterns:
        return []
    lines = text.splitlines()
    hits: List[dict] = []
    for idx, line in enumerate(lines):
        for pattern, kind in patterns:
            m = pattern.match(line)
            if not m:
                continue
            name = m.group(1).strip()
            if not name:
                break
            hits.append({"name": name, "kind": kind, "start": idx + 1})
            break
        # One past the limit on purpose: the extra hit is never returned, but
        # its start line is what closes the last one that is. Breaking at
        # `max_count` left the final symbol ending at EOF, and `pre_read`
        # offered that range to the agent -- hundreds of lines to read five.
        if len(hits) > max_count:
            break
    import math
    for i, hit in enumerate(hits):
        hit["end"] = (hits[i + 1]["start"] - 1) if i + 1 < len(hits) else len(lines)
        if hit["end"] < hit["start"]:
            hit["end"] = hit["start"]
        chars = sum(len(l) + 1 for l in lines[hit["start"] - 1:hit["end"]])
        hit["tokens"] = int(math.ceil(chars / token_divisor))
    return hits[:max_count]


def format_hint(symbols: Sequence[dict], limit: int = 6) -> str:
    """`handleLogin fn L12-88, SessionStore class L90-210` for hook output."""
    bits = []
    for s in symbols[:limit]:
        bits.append("{} {} L{}-{}".format(s.get("name", "?"), s.get("kind", "?"),
                                          s.get("start", 0), s.get("end", 0)))
    if len(symbols) > limit:
        bits.append("+{} more".format(len(symbols) - limit))
    return ", ".join(bits)
