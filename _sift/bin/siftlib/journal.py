"""`log`, `bug` and `decide` — the only writers to journal.jsonl and decisions.jsonl.

One writer means one format, which is what makes `merge=union` safe and
`bug find` reliable. Both files are append-only; a decision is superseded by a
later one rather than edited or deleted (Ruling 5).

Decisions were markdown until D-20260915-10. Union merge is only safe when
every line is unique, and a decision block was mostly identical boilerplate --
two people deciding on the same day silently shredded each other's records. One
record per line fixes that, and `sift decisions` renders the markdown back.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

from . import gitutil, util
from .config import Config
from .paths import Ctx

KINDS = ("bug", "gotcha", "session", "ingest", "decision-ref", "migrate", "note")


def make_id(ts: str, who: str, detail: str) -> str:
    stamp = ts.replace("-", "").replace(":", "").replace("Z", "")
    stamp = stamp.replace("T", "-")
    return "j-{}-{}".format(stamp, util.short_id(ts, who, detail))


def append(ctx: Ctx, kind: str, detail: str,
           files: Sequence[str] = (),
           tags: Sequence[str] = (), extra: Optional[Dict[str, Any]] = None) -> dict:
    ts = util.now_iso()
    who = gitutil.user_email(ctx.root)
    entry: Dict[str, Any] = {
        "id": make_id(ts, who, detail),
        "ts": ts,
        "who": who,
        "commit": gitutil.head(ctx.root),
        "kind": kind,
        "detail": detail,
    }
    if files:
        entry["files"] = list(files)
    if tags:
        entry["tags"] = list(tags)
    if extra:
        entry.update(extra)
    util.append_line(ctx.journal, util.jdump(entry))
    return entry


def entries(ctx: Ctx) -> List[dict]:
    return util.read_jsonl(ctx.journal)


def recent(ctx: Ctx, count: int = 3) -> List[dict]:
    rows = sorted(entries(ctx), key=lambda r: str(r.get("ts", "")))
    return rows[-count:][::-1]


def add_bug(ctx: Ctx, error: str, root_cause: str, fix: str,
            files: Sequence[str] = (), tags: Sequence[str] = (),) -> dict:
    detail = error.strip() or "bug"
    return append(ctx, "bug", detail, files=files, tags=tags,
                  extra={"error": error, "root_cause": root_cause, "fix": fix})


def _quarantined(ctx: Ctx) -> List[dict]:
    """Bugs an import held back from the journal, kept searchable.

    A migration quarantines an entry when it carries a secret or a path outside
    the repo, and writes it to `local/` rather than dropping it. Leaving it out
    of `bug find` meant the one question the journal exists to answer — has this
    been debugged before? — was answered "no" by a repo holding the answer.
    They are marked `local`, so nothing copies one into a commit by reflex.
    """
    out: List[dict] = []
    if not ctx.local.is_dir():
        return out
    for path in sorted(ctx.local.glob("*quarantine*.jsonl")):
        for rec in util.read_jsonl(path):
            if isinstance(rec, dict):
                out.append(dict(rec, local=True, kind=rec.get("kind") or "bug"))
    return out


def find_bugs(ctx: Ctx, query: str, top: int = 5) -> List[dict]:
    """Token-overlap ranking over bug entries. Deliberately not BM25: the
    corpus is tiny and the query is usually a pasted error string."""
    from .search import tokenize
    terms = set(tokenize(query))
    if not terms:
        return []
    scored: List["tuple"] = []
    for entry in entries(ctx) + _quarantined(ctx):
        if entry.get("kind") != "bug":
            continue
        blob = " ".join(str(entry.get(k, "")) for k in
                        ("detail", "error", "root_cause", "fix"))
        blob += " " + " ".join(entry.get("files", []) or [])
        doc = set(tokenize(blob))
        if not doc:
            continue
        overlap = len(terms & doc)
        if not overlap:
            continue
        scored.append((overlap / float(len(terms)), entry))
    scored.sort(key=lambda x: (-x[0], str(x[1].get("ts", ""))))
    return [dict(entry, score=round(score, 3)) for score, entry in scored[:top]]


# ---------------------------------------------------------------------------
# decisions.jsonl
# ---------------------------------------------------------------------------

FIELDS = ("context", "decision", "consequences")

_DECISION_ID = re.compile(r"^D-(\d{8})-(\d{2})$")


def decisions(ctx: Ctx) -> List[dict]:
    """Every decision, oldest first. Order is the file's, not the id's: a union
    merge can interleave two clones' appends, and the id carries the date."""
    return [r for r in util.read_jsonl(ctx.decisions) if isinstance(r, dict) and r.get("id")]


def next_decision_id(ctx: Ctx, day: Optional[str] = None) -> str:
    """The next free `D-<day>-NN`.

    Sequential and readable because decisions get cited in prose and commit
    messages. Two clones deciding on the same day still pick the same number --
    union merge keeps both records now rather than merging them into one, so
    nothing is lost, and lint W21 reports the clash so it can be renumbered.
    """
    day = day or util.today_utc().replace("-", "")
    used = set()
    for rec in decisions(ctx):
        m = _DECISION_ID.match(str(rec.get("id", "")))
        if m and m.group(1) == day:
            used.add(int(m.group(2)))
    n = 1
    while n in used:
        n += 1
    return "D-{}-{:02d}".format(day, n)


def add_decision(ctx: Ctx, title: str, context: str = "", decision: str = "",
                 consequences: str = "", files: Sequence[str] = (),
                 supersedes: str = "", status: str = "accepted") -> dict:
    """Write one complete decision.

    Complete at the call, like `bug add` and unlike the stub this used to
    append: a record finished by a later hand-edit cannot be one line, and one
    line per record is what makes the file safe to merge.
    """
    did = next_decision_id(ctx)
    ts = util.now_iso()
    rec: Dict[str, Any] = {
        "id": did,
        "ts": ts,
        "who": gitutil.user_email(ctx.root),
        "commit": gitutil.head(ctx.root),
        "title": title.strip(),
        "status": status,
        "context": context.strip(),
        "decision": decision.strip(),
        "consequences": consequences.strip(),
    }
    if files:
        rec["files"] = list(files)
    if supersedes:
        rec["supersedes"] = supersedes
    util.append_line(ctx.decisions, util.jdump(rec))
    heading = "{} — {}".format(did, rec["title"])
    append(ctx, "decision-ref", heading)
    return dict(rec, anchor=_anchor(heading), heading=heading)


def render_decision(rec: dict) -> str:
    """One record as the markdown it used to be stored as."""
    out = ["## {} — {}".format(rec.get("id", ""), rec.get("title", "")), ""]
    out.append("- **Date:** " + str(rec.get("ts", ""))[:10])
    out.append("- **By:** " + str(rec.get("who", "")))
    if rec.get("files"):
        out.append("- **Files:** " + ", ".join(rec["files"]))
    if rec.get("supersedes"):
        out.append("- **Supersedes:** " + str(rec["supersedes"]))
    out.append("- **Status:** " + str(rec.get("status", "accepted")))
    for field in FIELDS:
        body = str(rec.get(field, "")).strip()
        if body:
            out += ["", "**{}.** {}".format(field.capitalize(), body)]
    return "\n".join(out)


def render_decisions(recs: Sequence[dict]) -> str:
    head = ("# Decisions\n\nDated records of *why*, newest last, never deleted — "
            "superseded instead.\nRendered from `decisions.jsonl` by `sift decisions "
            "--markdown`; edit that, not this.\n")
    return head + "".join("\n" + render_decision(r) + "\n" for r in recs)


def _anchor(heading: str) -> str:
    """The GitHub anchor for a decision heading, for links into a rendered view."""
    slug = heading.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    return re.sub(r"[\s_]+", "-", slug).strip("-")
