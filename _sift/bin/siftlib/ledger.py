"""`.cache/ledger.jsonl` - the proof that the index is earning its keep.

Every hook that avoids a read or injects context writes one line; `sift ledger`
adds them up. Writes are best-effort: a hook must never fail because the cache
directory is read-only.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from . import paths, util
from .paths import Ctx

EVENTS = ("index_hit", "index_miss", "dup_warned", "dup_denied",
          "ranged_steered", "nudge_commit", "nudge_stop", "injected",
          "governed", "governed_rerun", "flood_seen",
          # The Read channel's floods, counted the way `flood_seen` counts
          # Bash's: a whole-file read that came back over the threshold, and a
          # whole-file read `pre_read` refused once and handed ranges for.
          "read_flood", "big_read_denied")

# There used to be a DEFAULT_AVOIDED_TOKENS = 400 here, added to every event
# whose size was unknown. It made `tokens_avoided_est` a count wearing a
# measurement's clothes, and it was quoted for weeks as though it were real.
# An event whose saving cannot be measured now contributes nothing.


def record(ctx: Ctx, session: str, event: str, tokens: int = 0,
           at_call: Optional[int] = None, **extra: Any) -> None:
    """`at_call` is the session's tool-call count when this happened.

    Without it the ledger can only say what a payload cost once. A token that
    enters the context is re-sent on every later turn, so what a flood really
    costs is its size times the turns that follow it -- on one measured run
    that was 46 times the one-off figure. `summarise` needs the position to
    work that out, and nothing else records it.
    """
    if util.readonly():
        return
    row = {"ts": util.now_iso(), "session": session, "event": event,
           "tokens": int(tokens)}
    if at_call is not None:
        row["at_call"] = int(at_call)
    row.update(extra)
    try:
        util.append_line(ctx.ledger, util.jdump(row))
    except OSError:
        pass


def _cutoff(since: str) -> Optional[str]:
    """`7.days` / `6.months` -> an ISO timestamp to compare strings against."""
    import re
    from datetime import datetime, timedelta, timezone
    m = re.match(r"^(\d+)\.(day|days|week|weeks|month|months|year|years)$", since.strip())
    if not m:
        return None
    count = int(m.group(1))
    unit = m.group(2).rstrip("s")
    days = {"day": 1, "week": 7, "month": 30, "year": 365}[unit] * count
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def summarise(ctx: Ctx, since: str = "7.days") -> Dict[str, Any]:
    cutoff = _cutoff(since)
    counts: Dict[str, int] = {e: 0 for e in EVENTS}
    # session -> (last tool call seen, [(at_call, tokens signed)])
    spans: Dict[str, int] = {}
    carried: List[tuple] = []
    avoided = 0
    injected = 0
    # Governance is measured, not estimated: both numbers come from the real
    # output, before and after. Kept apart from `tokens_avoided` for that
    # reason -- mixing a measurement with a guess makes both unquotable.
    gov_original = 0
    gov_entered = 0
    gov_families: Dict[str, int] = {}
    read_flood_tokens = 0
    for row in util.read_jsonl(ctx.ledger):
        ts = str(row.get("ts", ""))
        if cutoff and ts < cutoff:
            continue
        event = str(row.get("event", ""))
        if event in counts:
            counts[event] += 1
        tokens = int(row.get("tokens", 0) or 0)
        if event in ("dup_warned", "dup_denied", "ranged_steered"):
            avoided += tokens
        if event == "injected":
            injected += tokens
        if event == "read_flood":
            read_flood_tokens += tokens
        at = row.get("at_call")
        sess = str(row.get("session", ""))
        if at is not None:
            spans[sess] = max(spans.get(sess, 0), int(at))
            if event in ("flood_seen", "read_flood", "injected"):
                # Cost: these tokens entered the conversation and stayed.
                carried.append((sess, int(at), tokens, "cost"))
            elif event == "governed":
                saved = int(row.get("original_tokens", 0) or 0) - \
                    int(row.get("entered_tokens", 0) or 0)
                carried.append((sess, int(at), saved, "saved"))
        if event == "governed":
            gov_original += int(row.get("original_tokens", 0) or 0)
            gov_entered += int(row.get("entered_tokens", 0) or 0)
            family = str(row.get("family", "?"))
            gov_families[family] = gov_families.get(family, 0) + 1
    return {
        "since": since,
        "index_hits": counts["index_hit"],
        "index_misses": counts["index_miss"],
        "dup_warned": counts["dup_warned"],
        "dup_denied": counts["dup_denied"],
        "ranged_steered": counts["ranged_steered"],
        "nudges": counts["nudge_commit"] + counts["nudge_stop"],
        # No longer `_est`: every contributor is a real size now -- a
        # duplicate read is the file as the scan measured it, a ranged read is
        # the whole file minus the window that was actually returned.
        "tokens_avoided": avoided,
        "tokens_injected": injected,
        "governed_calls": counts["governed"],
        "governed_original_tokens": gov_original,
        "governed_entered_tokens": gov_entered,
        "governed_saved_tokens": max(0, gov_original - gov_entered),
        "governed_families": gov_families,
        # A condensed result the model had to work around costs more than it
        # saved, so the re-run count sits beside the saving, not in a footnote.
        "governed_reruns": counts["governed_rerun"],
        # Floods seen, whether or not anything was done about them. With
        # `enabled` off this is the whole point: zero here after a week of real
        # work is the evidence that condensation would buy nothing.
        "floods_seen": counts["flood_seen"],
        # The same count for the Read tool, which is where the first real
        # sessions put most of the tokens: whole-file reads that came back over
        # the threshold, what they weighed, and how many `big_read_mode: deny`
        # turned into ranged reads.
        "read_floods": counts["read_flood"],
        "read_flood_tokens": read_flood_tokens,
        "big_reads_denied": counts["big_read_denied"],
        # Size times the turns that followed, which is what a token in the
        # context actually costs. Kept as two numbers because netting them
        # hides the whole point: `carry_cost` is what entered and stayed,
        # `carry_saved` is what condensing kept out of every later turn.
        # Mostly cache reads at roughly a tenth the price of fresh tokens, so
        # do not turn either into money naively.
        "carry_cost": _carry(spans, carried, "cost"),
        "carry_saved": _carry(spans, carried, "saved"),
        "carry_basis": "turns after each event, per session",
    }


# Everything `summarise` returns as a plain running count, and therefore
# everything `--all` can add up across repos. `governed_families` is a dict and
# `governed_saved_tokens` is derived from two of these, so both are handled
# apart; `since` and `carry_basis` are labels every repo shares.
_SUMMABLE = (
    "index_hits", "index_misses", "dup_warned", "dup_denied", "ranged_steered",
    "nudges", "tokens_avoided", "tokens_injected", "governed_calls",
    "governed_original_tokens", "governed_entered_tokens", "governed_reruns",
    "floods_seen", "read_floods", "read_flood_tokens", "big_reads_denied",
    "carry_cost", "carry_saved")


def summarise_all(root: Path, since: str = "7.days") -> Dict[str, Any]:
    """The same summary, added up over every sift repo found under `root`.

    Repos are found by the one shared walk in `upgrade.discover`, not a
    registry -- the reasons are there. A repo with nothing to show in the window
    is left out of the per-repo table -- whether it has no ledger at all or a
    ledger with no events since the cutoff, so the two are not treated
    differently -- and its name goes in `idle` for the footer instead. So the
    table is the repos that did something, `idle` is the rest, and
    `repos_found` is the count the walk actually saw. Carry cost/saving stay
    summable here: each repo's figure is already the sum over its own sessions,
    so a machine-wide total is the sum of those.
    """
    from . import upgrade  # local: keep the hook-hot `record` path free of it.
    repos: List[Dict[str, Any]] = []
    idle: List[str] = []
    found = upgrade.discover(root)
    for repo in found:
        try:
            name = repo.relative_to(root).as_posix()
        except ValueError:
            name = str(repo)
        ctx = Ctx(repo, paths.resolve_dir_name(repo))
        data = summarise(ctx, since) if ctx.ledger.exists() else None
        if data is None or _is_idle(data):
            idle.append(name)
            continue
        repos.append({"repo": name, **data})
    return {
        "root": str(root),
        "since": since,
        "repos_found": len(found),
        "repo_count": len(repos),
        "repos": repos,
        "idle": idle,
        "total": _aggregate(repos, since),
    }


def _is_idle(data: Dict[str, Any]) -> bool:
    """True when a repo's ledger recorded nothing in the window -- every
    summable count zero and no governed families -- so it is a footer name
    rather than a table row."""
    if any(int(data.get(key, 0) or 0) for key in _SUMMABLE):
        return False
    return not (data.get("governed_families") or {})


def _aggregate(repos: List[Dict[str, Any]], since: str) -> Dict[str, Any]:
    total: Dict[str, Any] = {"since": since}
    for key in _SUMMABLE:
        total[key] = sum(int(r.get(key, 0) or 0) for r in repos)
    families: Dict[str, int] = {}
    for r in repos:
        for family, count in (r.get("governed_families") or {}).items():
            families[family] = families.get(family, 0) + int(count or 0)
    total["governed_families"] = families
    total["governed_saved_tokens"] = max(
        0, total["governed_original_tokens"] - total["governed_entered_tokens"])
    total["carry_basis"] = "turns after each event, per session"
    return total


def _carry(spans: Dict[str, int], rows: List[tuple], want: str) -> int:
    """Weight each event by the turns that came after it in its own session.

    The session's length is the last tool call the ledger saw, not its true
    end, so this understates an event near the finish and is exact for
    nothing. It is still the right order of magnitude, which the one-off
    figure is not: on one measured run the carried cost of the Bash output was
    46 times what the payloads weighed once.
    """
    total = 0
    for sess, at, tokens, kind in rows:
        if kind != want:
            continue
        total += tokens * max(0, spans.get(sess, at) - at)
    return total
