"""BM25 search over the sift directory (PLAN 4).

Tokenizer, query syntax and scoring are ported from waynes-world's
`scripts/search_wiki.py` -- it is tested and it works. What changed: the
corpus is conventions, decisions, the journal and file descriptions rather
than vault notes, and scoring is flat BM25 over the whole document -- the
per-field weights went with the pages that had the fields.

Query syntax:
    bare tokens    OR-by-default, BM25's native behaviour
    "a b"          require the literal phrase as a substring
    a AND b        both required
    NOT a / -a     exclude
    +a             require
    tag:foo        require frontmatter tag `foo` (or a sub-tag `foo/...`)
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import util
from .config import Config
from .paths import Ctx

K1 = 1.5
B = 0.75
DEFAULT_TOP = 10
_OPERATORS = {"AND", "OR", "NOT"}


def tokenize(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


class ParsedQuery:
    def __init__(self) -> None:
        self.bare: List[str] = []
        self.required: List[str] = []
        self.excluded: List[str] = []
        self.phrases: List[str] = []
        self.tags: List[str] = []
        self.excluded_tags: List[str] = []


def parse_query(q: str) -> ParsedQuery:
    """Two-pass: lex into typed tokens, then resolve AND / NOT against
    neighbours. Ported wholesale; the edge cases here were paid for once."""
    raw: List[dict] = []
    i, n = 0, len(q)
    while i < n:
        c = q[i]
        if c.isspace():
            i += 1
            continue
        if c == '"':
            end = q.find('"', i + 1)
            if end < 0:
                phrase = q[i + 1:].strip()
                if phrase:
                    raw.append({"kind": "phrase", "value": phrase.lower(), "prefix": None})
                break
            phrase = q[i + 1:end].strip()
            if phrase:
                raw.append({"kind": "phrase", "value": phrase.lower(), "prefix": None})
            i = end + 1
            continue
        prefix = None
        start = i
        if c in "+-":
            prefix = c
            start = i + 1
        j = start
        while j < n and not q[j].isspace() and q[j] != '"':
            j += 1
        word = q[start:j]
        if not word:
            i = j
            continue
        upper = word.upper()
        if prefix is None and upper in _OPERATORS:
            raw.append({"kind": "op", "value": upper, "prefix": None})
        else:
            raw.append({"kind": "word", "value": word.lower(), "prefix": prefix})
        i = j

    required_idx: Set[int] = set()
    excluded_idx: Set[int] = set()
    for idx, tok in enumerate(raw):
        if tok["prefix"] == "+":
            required_idx.add(idx)
        elif tok["prefix"] == "-":
            excluded_idx.add(idx)
    for idx, tok in enumerate(raw):
        if tok["kind"] != "op":
            continue
        if tok["value"] == "NOT":
            for k in range(idx + 1, len(raw)):
                if raw[k]["kind"] != "op":
                    excluded_idx.add(k)
                break
        elif tok["value"] == "AND":
            for k in range(idx - 1, -1, -1):
                if raw[k]["kind"] != "op":
                    required_idx.add(k)
                break
            for k in range(idx + 1, len(raw)):
                if raw[k]["kind"] != "op":
                    required_idx.add(k)
                break

    out = ParsedQuery()
    for idx, tok in enumerate(raw):
        if tok["kind"] == "op":
            continue
        if tok["kind"] == "phrase":
            out.phrases.append(tok["value"])
            continue
        value = tok["value"]
        if value.startswith("tag:"):
            tag = value[4:].strip()
            if tag:
                (out.excluded_tags if idx in excluded_idx else out.tags).append(tag)
            continue
        if idx in excluded_idx:
            out.excluded.append(value)
        elif idx in required_idx:
            out.required.append(value)
        else:
            out.bare.append(value)
    return out


def query_terms(pq: ParsedQuery) -> List[str]:
    terms: List[str] = []
    for t in pq.bare + pq.required:
        terms.extend(tokenize(t))
    for phrase in pq.phrases:
        terms.extend(tokenize(phrase))
    return terms


def _tag_matches(doc_tags: Set[str], query_tag: str) -> bool:
    return any(t == query_tag or t.startswith(query_tag + "/") for t in doc_tags)


def passes_filter(doc: dict, pq: ParsedQuery) -> bool:
    tf = doc["tf"]
    for token in pq.required:
        sub = tokenize(token)
        if sub and any(tf.get(s, 0) == 0 for s in sub):
            return False
    for token in pq.excluded:
        sub = tokenize(token)
        if sub and all(tf.get(s, 0) > 0 for s in sub):
            return False
    for phrase in pq.phrases:
        if phrase not in doc.get("raw_lower", ""):
            return False
    if pq.tags or pq.excluded_tags:
        doc_tags = set(doc.get("tags", []))
        for t in pq.tags:
            if not _tag_matches(doc_tags, t):
                return False
        for t in pq.excluded_tags:
            if _tag_matches(doc_tags, t):
                return False
    return True


def build_idf(docs: Sequence[dict]) -> Dict[str, float]:
    n_docs = len(docs)
    df: Dict[str, int] = defaultdict(int)
    for doc in docs:
        for term in doc["tf"]:
            df[term] += 1
    return {term: math.log((n_docs - n + 0.5) / (n + 0.5) + 1) for term, n in df.items()}


def bm25(terms: Sequence[str], doc: dict, idf: Dict[str, float], avgdl: float) -> float:
    dl = doc["length"] or 1
    tf = doc["tf"]
    return sum(
        idf[t] * (tf[t] * (K1 + 1)) / (tf[t] + K1 * (1 - B + B * dl / avgdl))
        for t in terms if t in idf and tf.get(t, 0) > 0
    )


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------

def _headings(body: str) -> str:
    return " ".join(line.lstrip("#").strip()
                    for line in body.splitlines() if line.startswith("#"))


def _documents(ctx: Ctx) -> List[dict]:
    """Every markdown document in the sift directory, plus the journal.

    Not a fixed list of three: whatever a repo commits under the sift directory
    is content someone will want to find, and with the pages gone those
    documents are the sift directory. Decisions are indexed one record at a
    time, because a hit on the whole file tells you nothing about which
    decision matched.
    """
    from . import util
    docs: List[dict] = []

    # `local/` is indexed too. It is gitignored, so this is the owner searching
    # their own machine -- and leaving it out meant a migrated repo could hold
    # every note the previous tool ever took and answer "no results" to all of
    # them. Hits are labelled `local`, because acting on one may mean promoting
    # a technical half and leaving the specifics where they are.
    paths = sorted(ctx.dir.glob("*.md"))
    paths += sorted(ctx.local.glob("*.md")) if ctx.local.is_dir() else []
    for path in paths:
        local = path.parent == ctx.local
        rel_name = "" if local else path.name
        text = util.read_text(path)
        if not text.strip():
            continue
        ident = path.stem
        body, _ = _strip_frontmatter(text)
        docs.append({
            "layer": "local" if local else "doc", "id": ident, "title": _title_of(body, ident),
            "path": ctx.rel(path),
            "tf": Counter(tokenize(ident.replace("-", " ")) * 5
                          + tokenize(_headings(body)) * 2 + tokenize(body)),
            "length": max(1, len(tokenize(body))),
            "raw_lower": body.lower(), "tags": [],
            "snippet": util.first_sentence(body),
        })

    for rec in util.read_jsonl(ctx.journal):
        kind = str(rec.get("kind") or "note")
        text = " ".join(str(rec.get(k) or "") for k in
                        ("error", "root_cause", "fix", "detail", "title"))
        if not text.strip():
            continue
        docs.append({
            "layer": "journal", "id": str(rec.get("id") or kind),
            "title": "{}: {}".format(kind, text[:60]),
            "path": ctx.rel(ctx.journal),
            "tf": Counter(tokenize(kind) * 2 + tokenize(text)),
            "length": max(1, len(tokenize(text))),
            "raw_lower": text.lower(), "tags": [kind], "snippet": text[:160],
        })

    from . import journal as journal_mod
    for rec in journal_mod.decisions(ctx):
        title = "{} — {}".format(rec.get("id", ""), rec.get("title", ""))
        body = " ".join(str(rec.get(f, "")) for f in journal_mod.FIELDS)
        docs.append({
            "layer": "decision", "id": str(rec.get("id") or "decision"),
            "title": title, "path": ctx.rel(ctx.decisions),
            "tf": Counter(tokenize(title) * 4 + tokenize(body)),
            "length": max(1, len(tokenize(body))),
            "raw_lower": (title + " " + body).lower(), "tags": [], "snippet": title,
        })
    return docs


def _strip_frontmatter(text: str) -> "tuple":
    """Frontmatter is not prose; a leftover `covers:` must not score a hit."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip("\n"), text[:end]
    return text, ""


def _title_of(body: str, fallback: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback.replace("-", " ").title()


def build_corpus(ctx: Ctx, layer: str = "docs") -> List[dict]:
    """The corpus: the sift directory's documents, and optionally the file descriptions.

    `layer` kept the "pages" name until they were removed. The
    documents are unconditional now, because they are the content.
    """
    from . import scan as scan_mod
    docs: List[dict] = []
    docs.extend(_documents(ctx))
    if layer in ("files", "all"):
        for path, rec in sorted(scan_mod.load_descriptions(ctx).items()):
            desc = str(rec.get("desc", ""))
            tokens = tokenize(path.replace("/", " ").replace("-", " ").replace("_", " ")) * 4 \
                + tokenize(desc)
            docs.append({
                "layer": "file", "id": path, "title": path, "path": path,
                "tf": Counter(tokens), "length": len(tokens),
                "raw_lower": (path + " " + desc).lower(), "tags": [],
                "snippet": desc,
            })
    return docs


def search(ctx: Ctx, cfg: Config, query: str, top: int = DEFAULT_TOP,
           layer: str = "docs") -> List[dict]:
    docs = build_corpus(ctx, layer)
    if not docs:
        return []
    avgdl = sum(d["length"] for d in docs) / len(docs) or 1.0
    idf = build_idf(docs)
    pq = parse_query(query)
    terms = query_terms(pq)
    # A phrase / required / tag match must surface even when BM25 collapses to
    # zero because every scoring term is a stopword in this corpus.
    keep_zero = bool(pq.phrases or pq.required or pq.tags)
    scored: List[Tuple[float, dict]] = []
    for doc in docs:
        if not passes_filter(doc, pq):
            continue
        score = bm25(terms, doc, idf, avgdl) if terms else 0.0
        if score > 0 or keep_zero:
            scored.append((score, doc))
    scored.sort(key=lambda x: (-x[0], x[1]["id"]))
    out: List[dict] = []
    for score, doc in scored[:top]:
        out.append({
            "id": doc["id"], "title": doc["title"], "layer": doc["layer"],
            "score": round(score, 4), "snippet": doc["snippet"][:200],
            "path": doc["path"],
        })
    return out
