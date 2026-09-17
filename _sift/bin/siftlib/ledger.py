"""`.cache/ledger.jsonl` — the proof that the index is earning its keep.

Every hook that avoids a read or injects context writes one line; `sift ledger`
adds them up. Writes are best-effort: a hook must never fail because the cache
directory is read-only.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import util
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
