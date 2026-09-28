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
          "ranged_steered", "injected", "governed", "flood_seen",
          # A Bash flood of a family the governor never condenses -- test,
          # build, or an unclassifiable chain. It entered context whole; it is
          # counted so the passed-through volume can be told apart by family.
          "flood_passed",
          # The Read channel's floods, counted the way `flood_seen` counts
          # Bash's: a whole-file read that came back over the threshold, and a
          # whole-file read `pre_read` refused once and handed ranges for.
          "read_flood", "big_read_denied",
          # The knowledge artefacts save no tokens; their case rests on being
          # consulted and on the privacy rule holding. Neither was counted
          # until 2026-09-22 (D-20260922-03). `consulted` is one `sift search`,
          # `bug find` or `decisions` call with how many rows it returned;
          # `lint_blocked` is a `lint --staged` run that found a hard-fail
          # code, which is the commit the pre-commit hook refuses.
          "consulted", "lint_blocked",
          # A `bug add`, with the session it ran in, so a later `bug find` hit
          # on that bug can say what finding the fix cost (D-20260928-03).
          "bug_added")

CONSULT_COMMANDS = ("search", "bug_find", "decisions")

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


SINCE_RE = r"^(\d+)\.(day|days|week|weeks|month|months|year|years)$"


def valid_since(since: str) -> bool:
    """Whether `--since` is a window this module can read. An unreadable one
    used to fall through to no cutoff at all, so `--since 7d` printed the whole
    history under the heading `since 7d`."""
    import re
    return re.match(SINCE_RE, since.strip()) is not None


def _cutoff(since: str) -> Optional[str]:
    """`7.days` / `6.months` -> an ISO timestamp to compare strings against."""
    import re
    from datetime import datetime, timedelta, timezone
    m = re.match(SINCE_RE, since.strip())
    if not m:
        return None
    count = int(m.group(1))
    unit = m.group(2).rstrip("s")
    days = {"day": 1, "week": 7, "month": 30, "year": 365}[unit] * count
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def summarise(ctx: Ctx, since: str = "7.days",
              usage_exclude: Optional[set] = None) -> Dict[str, Any]:
    """`usage_exclude`: sessions whose usage another repo's summary counts, so
    `--all` counts a session that worked in two repos once (their savings
    here still count, as savings without a usage log)."""
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
    # The size of the reads that were refused up front, and of the Bash outputs
    # that went over the threshold. Summed so the "tokens avoided" account can
    # credit a refusal, and the "reaching context" account can show what the
    # uncondensed floods weighed -- both were counts only before.
    denied_tokens = 0
    flood_seen_tokens = 0
    flood_passed_tokens = 0
    passed_families: Dict[str, int] = {}
    passed_commands: Dict[str, Dict[str, int]] = {}
    # Ranged reads recorded before each row carried its path and the change in
    # its file's credit. Each of those rows holds `whole - window` on its own,
    # so a file read in three windows was credited near three times over; they
    # are folded per file here, with the session and the file's size standing
    # in for the path the row never had. `_fold_old_ranged` does the sum.
    old_ranged: Dict[tuple, List[tuple]] = {}
    consulted: Dict[str, Dict[str, int]] = {
        c: {"calls": 0, "hits": 0} for c in CONSULT_COMMANDS}
    blocked_codes: Dict[str, int] = {}
    sessions: set = set()
    # bug id -> the session that recorded it, from any time: a bug found this
    # week may have been fixed months ago.
    bug_sessions: Dict[str, str] = {}
    bug_hits: Dict[str, int] = {}
    bug_hits_unnamed = 0
    # What sift saved, per session, so the percentages and dollars can be
    # limited to the sessions whose usage is known -- a Codex session with no
    # log, or a transcript cleaned up, has savings but nothing they are out of.
    saved_by_session: Dict[str, int] = {}
    # (session, path) -> [(ts, at_call, tokens)] for ranged rows, across the
    # whole ledger. A file's rows only sum right as a set -- each is the
    # change in the file's credit -- so a window cutting between them could
    # keep a later -9000 without the +9000 before it. The set is counted in
    # the window of its last row.
    ranged_groups: Dict[tuple, List[tuple]] = {}
    # (session, at_call) of each flood carried at its original size, so its
    # condensation can take the cut back off the cost.
    flood_at: set = set()
    for row in util.read_jsonl(ctx.ledger):
        ts = str(row.get("ts", ""))
        if row.get("event") == "bug_added" and row.get("bug"):
            bug_sessions[str(row["bug"])] = str(row.get("session", ""))
        if row.get("event") == "ranged_steered" and "path" in row:
            key = (str(row.get("session", "")), str(row.get("path", "")))
            ranged_groups.setdefault(key, []).append(
                (ts, row.get("at_call"), int(row.get("tokens", 0) or 0)))
            if not (cutoff and ts < cutoff):
                counts["ranged_steered"] += 1
            continue
        if cutoff and ts < cutoff:
            continue
        sessions.add(str(row.get("session", "")))
        event = str(row.get("event", ""))
        if event in counts:
            counts[event] += 1
        tokens = int(row.get("tokens", 0) or 0)
        # `dup_warned` is not here: in `warn` mode the read goes ahead, so a
        # warned duplicate is tokens that entered, not tokens kept out. It was
        # credited whole to avoided until 2026-09-22.
        sess_id = str(row.get("session", ""))
        if event == "dup_denied":
            avoided += tokens
            saved_by_session[sess_id] = saved_by_session.get(sess_id, 0) + tokens
        if event == "ranged_steered":
            if "whole_tokens" not in row:
                avoided += tokens
                saved_by_session[sess_id] = saved_by_session.get(sess_id, 0) + tokens
            else:
                key = (str(row.get("session", "")), int(row.get("whole_tokens", 0) or 0))
                old_ranged.setdefault(key, []).append(
                    (int(row.get("whole_tokens", 0) or 0),
                     int(row.get("read_tokens", 0) or 0)))
        if event == "injected":
            injected += tokens
            saved_by_session[sess_id] = saved_by_session.get(sess_id, 0) - tokens
        if event == "read_flood":
            read_flood_tokens += tokens
        if event == "big_read_denied":
            denied_tokens += tokens
        if event == "flood_seen":
            flood_seen_tokens += tokens
        if event == "flood_passed":
            flood_passed_tokens += tokens
            family = str(row.get("family", "?"))
            passed_families[family] = passed_families.get(family, 0) + 1
            label = str(row.get("command") or "?")
            entry = passed_commands.setdefault(label, {"count": 0, "tokens": 0})
            entry["count"] += 1
            entry["tokens"] += tokens
        if event == "consulted":
            command = str(row.get("command", ""))
            if command in consulted:
                consulted[command]["calls"] += 1
                if int(row.get("hits", 0) or 0) > 0:
                    consulted[command]["hits"] += 1
                    if command == "bug_find":
                        ids = row.get("ids") or []
                        if not ids:
                            bug_hits_unnamed += 1
                        for bug in ids:
                            bug_hits[str(bug)] = bug_hits.get(str(bug), 0) + 1
        if event == "lint_blocked":
            for code, n in (row.get("codes") or {}).items():
                blocked_codes[str(code)] = blocked_codes.get(str(code), 0) + int(n or 0)
        at = row.get("at_call")
        sess = str(row.get("session", ""))
        if at is not None:
            spans[sess] = max(spans.get(sess, 0), int(at))
            if event in ("flood_seen", "flood_passed", "read_flood", "injected"):
                # Cost: these tokens entered the conversation and stayed.
                carried.append((sess, int(at), tokens, "cost"))
                if event == "injected":
                    carried.append((sess, int(at), tokens, "injected"))
                if event == "flood_seen":
                    flood_at.add((sess, int(at)))
            elif event == "dup_denied" or (event == "ranged_steered" and "path" in row):
                # A read kept out is kept out of every later turn too, the same
                # as a condensed flood; only condensing was carried until
                # 0.22.0, so the reads' re-sends went uncredited. A ranged
                # row's tokens are the change in its file's credit, possibly
                # negative, so the sum over a file's rows is weighted right.
                # Rows from before the per-file fold carry nothing.
                carried.append((sess, int(at), tokens, "saved"))
            elif event == "governed":
                saved = int(row.get("original_tokens", 0) or 0) - \
                    int(row.get("entered_tokens", 0) or 0)
                carried.append((sess, int(at), saved, "saved"))
                # What stayed in context is what entered. Its `flood_seen` row
                # carried the flood at its original size, so the cut is taken
                # back off; without that a condensed flood was carried twice,
                # as cost at its full size and as saving for the part cut.
                # With no `flood_seen` (advice off), the entered size is the
                # cost on its own.
                if (sess, int(at)) in flood_at:
                    carried.append((sess, int(at), -saved, "cost"))
                else:
                    carried.append((sess, int(at),
                                    int(row.get("entered_tokens", 0) or 0), "cost"))
        if event == "governed":
            saved_by_session[sess_id] = saved_by_session.get(sess_id, 0) + max(
                0, int(row.get("original_tokens", 0) or 0)
                - int(row.get("entered_tokens", 0) or 0))
            gov_original += int(row.get("original_tokens", 0) or 0)
            gov_entered += int(row.get("entered_tokens", 0) or 0)
            family = str(row.get("family", "?"))
            gov_families[family] = gov_families.get(family, 0) + 1
    avoided += _fold_old_ranged(old_ranged)
    for (sess, whole), rows in old_ranged.items():
        credit = _fold_old_ranged({(sess, whole): rows})
        saved_by_session[sess] = saved_by_session.get(sess, 0) + credit
    for (sess, _path), rows in ranged_groups.items():
        if cutoff and max(r[0] for r in rows) < cutoff:
            continue
        net = sum(r[2] for r in rows)
        avoided += net
        saved_by_session[sess] = saved_by_session.get(sess, 0) + net
        sessions.add(sess)
        for _ts, at, tokens in rows:
            if at is not None:
                spans[sess] = max(spans.get(sess, 0), int(at))
                carried.append((sess, int(at), tokens, "saved"))
    # What those sessions consumed, from their transcripts: the denominator
    # for sift's saving. `cli` rows are sift's own commands, not a session.
    from . import usage as usage_mod  # local: keep the hook-hot path free of it.
    exclude = usage_exclude or set()
    used = usage_mod.usage((s for s in sessions if s and s != "cli" and s not in exclude),
                           cutoff)
    found, priced = set(used["found"]), set(used["priced"])
    carry_saved_by = _carry_by_session(spans, carried, "saved")
    carry_injected_by = _carry_by_session(spans, carried, "injected")

    def net_carry(group: set) -> int:
        return sum(carry_saved_by.get(s, 0) - carry_injected_by.get(s, 0) for s in group)
    replay = _bug_replay(ctx, bug_hits, bug_sessions, usage_mod)
    replay["hits_unnamed"] = bug_hits_unnamed
    return {
        # Savings limited to the sessions whose usage was found (for the token
        # percentages) and priced (for dollars); see `saved_by_session`.
        "saved_in_matched": sum(saved_by_session.get(s, 0) for s in found),
        "saved_in_priced": sum(saved_by_session.get(s, 0) for s in priced),
        "saved_cached_matched": net_carry(found),
        "saved_cached_priced": net_carry(priced),
        "bug_replay": replay,
        "usage_sessions": used["sessions"],
        "usage_sessions_found": used["sessions_found"],
        "context_in": used["context_in"],
        "context_resent": used["context_resent"],
        "output_tokens": used["output_tokens"],
        "context_in_usd": used["context_in_usd"],
        "context_resent_usd": used["context_resent_usd"],
        "output_usd": used["output_usd"],
        "context_in_priced": used["context_in_priced"],
        "context_resent_priced": used["context_resent_priced"],
        "by_model_usd": used["by_model_usd"],
        "since": since,
        "index_hits": counts["index_hit"],
        "index_misses": counts["index_miss"],
        "dup_warned": counts["dup_warned"],
        "dup_denied": counts["dup_denied"],
        "ranged_steered": counts["ranged_steered"],
        # No longer `_est`: every contributor is a real size now -- a refused
        # duplicate read is the file as the scan measured it, a ranged read is
        # the whole file minus the windows that were actually returned, per
        # file, and nothing once the windows cover it. Still a counterfactual
        # for a ranged read: it assumes the whole file would otherwise have
        # been read, which is what the refusal and the hint steer away from.
        "tokens_avoided": avoided,
        "tokens_injected": injected,
        "governed_calls": counts["governed"],
        "governed_original_tokens": gov_original,
        "governed_entered_tokens": gov_entered,
        "governed_saved_tokens": max(0, gov_original - gov_entered),
        "governed_families": gov_families,
        # Floods seen, whether or not anything was done about them. With
        # `enabled` off this is the whole point: zero here after a week of real
        # work is the evidence that condensation would buy nothing.
        "floods_seen": counts["flood_seen"],
        # Floods of the families the governor never condenses (test, build,
        # unclassifiable). They entered context whole; the tokens and the
        # per-family count are here so the passed-through share of the balance
        # is visible and can be told apart from what condensing could have cut.
        "floods_passed": counts["flood_passed"],
        "flood_passed_tokens": flood_passed_tokens,
        "passed_families": passed_families,
        "passed_commands": passed_commands,
        # The same count for the Read tool, which is where the first real
        # sessions put most of the tokens: whole-file reads that came back over
        # the threshold, what they weighed, and how many `big_read_mode: deny`
        # turned into ranged reads.
        "read_floods": counts["read_flood"],
        "read_flood_tokens": read_flood_tokens,
        "big_reads_denied": counts["big_read_denied"],
        # Sizes to go with the two counts above: what the refused reads would
        # have weighed (an avoided cost), and what every Bash flood weighed
        # (the condensed share of it is `governed_original_tokens`).
        "denied_tokens": denied_tokens,
        "flood_seen_tokens": flood_seen_tokens,
        # Size times the turns that followed, which is what a token in the
        # context actually costs. Kept as two numbers because netting them
        # hides the whole point: `carry_cost` is what entered and stayed,
        # `carry_saved` is what condensing kept out of every later turn.
        # Mostly cache reads at roughly a tenth the price of fresh tokens, so
        # do not turn either into money naively.
        "carry_cost": _carry(spans, carried, "cost"),
        "carry_saved": _carry(spans, carried, "saved"),
        # sift's own injected context, carried: part of carry_cost, and the
        # amount taken off carry_saved for sift's net re-send saving.
        "carry_injected": _carry(spans, carried, "injected"),
        "carry_basis": "turns after each event, per session",
        # Whether the written-down knowledge is read at all: calls to each
        # consulting command, and how many of them returned anything. Not a
        # token figure and never added to one.
        "consulted": consulted,
        # Commits the privacy rule stopped: `lint --staged` runs that found a
        # hard-fail code, by code.
        "lint_blocked": counts["lint_blocked"],
        "lint_blocked_codes": blocked_codes,
    }


# Everything `summarise` returns as a plain running count, and therefore
# everything `--all` can add up across repos. `governed_families` is a dict and
# `governed_saved_tokens` is derived from two of these, so both are handled
# apart; `since` and `carry_basis` are labels every repo shares.
_SUMMABLE = (
    "index_hits", "index_misses", "dup_warned", "dup_denied", "ranged_steered",
    "tokens_avoided", "tokens_injected", "governed_calls",
    "governed_original_tokens", "governed_entered_tokens",
    "floods_seen", "floods_passed", "flood_passed_tokens",
    "read_floods", "read_flood_tokens", "big_reads_denied",
    "denied_tokens", "flood_seen_tokens",
    "carry_cost", "carry_saved", "carry_injected", "lint_blocked",
    "usage_sessions", "usage_sessions_found", "context_in", "context_resent",
    "output_tokens", "context_in_priced", "context_resent_priced",
    "saved_in_matched", "saved_in_priced", "saved_cached_matched", "saved_cached_priced")
# Dollars are floats, summed apart from the counts above.
_SUMMABLE_USD = ("context_in_usd", "context_resent_usd", "output_usd")


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
    ctxs = [Ctx(repo, paths.resolve_dir_name(repo)) for repo in found]
    # A session that worked in two repos wrote to both ledgers, and each
    # repo's summary read its whole usage, so the total counted it twice. It
    # belongs to the repo where it did the most; the others count its savings
    # as savings without a usage log.
    owner = _session_owners(ctxs, since)
    for repo, ctx in zip(found, ctxs):
        try:
            name = repo.relative_to(root).as_posix()
        except ValueError:
            name = str(repo)
        others = {s for s, r in owner.items() if r != ctx.root}
        data = summarise(ctx, since, usage_exclude=others) if ctx.ledger.exists() else None
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
    if any(int(v.get("calls", 0) or 0) for v in (data.get("consulted") or {}).values()):
        return False
    return not (data.get("governed_families") or {})


def _session_owners(ctxs: List[Ctx], since: str) -> Dict[str, Path]:
    """session id -> the repo whose ledger has the most rows for it in the
    window."""
    cutoff = _cutoff(since)
    rows: Dict[str, Dict[Path, int]] = {}
    for ctx in ctxs:
        if not ctx.ledger.exists():
            continue
        for row in util.read_jsonl(ctx.ledger):
            if cutoff and str(row.get("ts", "")) < cutoff:
                continue
            sess = str(row.get("session", ""))
            per = rows.setdefault(sess, {})
            per[ctx.root] = per.get(ctx.root, 0) + 1
    return {s: max(per.items(), key=lambda kv: kv[1])[0] for s, per in rows.items()}


def _aggregate(repos: List[Dict[str, Any]], since: str) -> Dict[str, Any]:
    total: Dict[str, Any] = {"since": since}
    for key in _SUMMABLE:
        total[key] = sum(int(r.get(key, 0) or 0) for r in repos)
    families: Dict[str, int] = {}
    passed: Dict[str, int] = {}
    commands: Dict[str, Dict[str, int]] = {}
    blocked: Dict[str, int] = {}
    consulted: Dict[str, Dict[str, int]] = {
        c: {"calls": 0, "hits": 0} for c in CONSULT_COMMANDS}
    for r in repos:
        for family, count in (r.get("governed_families") or {}).items():
            families[family] = families.get(family, 0) + int(count or 0)
        for family, count in (r.get("passed_families") or {}).items():
            passed[family] = passed.get(family, 0) + int(count or 0)
        for label, c in (r.get("passed_commands") or {}).items():
            entry = commands.setdefault(label, {"count": 0, "tokens": 0})
            entry["count"] += int(c.get("count", 0) or 0)
            entry["tokens"] += int(c.get("tokens", 0) or 0)
        for code, count in (r.get("lint_blocked_codes") or {}).items():
            blocked[code] = blocked.get(code, 0) + int(count or 0)
        for command, c in (r.get("consulted") or {}).items():
            if command in consulted:
                consulted[command]["calls"] += int(c.get("calls", 0) or 0)
                consulted[command]["hits"] += int(c.get("hits", 0) or 0)
    for key in _SUMMABLE_USD:
        total[key] = sum(float(r.get(key, 0) or 0) for r in repos)
    by_model: Dict[str, float] = {}
    for r in repos:
        for model, usd in (r.get("by_model_usd") or {}).items():
            by_model[model] = by_model.get(model, 0.0) + float(usd or 0)
    total["by_model_usd"] = by_model
    replay: Dict[str, Any] = {"hits": 0, "bugs": 0, "priced": 0, "predate": 0,
                              "tokens": 0, "usd": 0.0, "usd_known": False,
                              "hits_unnamed": 0}
    for r in repos:
        rr = r.get("bug_replay") or {}
        for key in ("hits", "bugs", "priced", "predate", "tokens", "hits_unnamed"):
            replay[key] += int(rr.get(key, 0) or 0)
        replay["usd"] += float(rr.get("usd", 0) or 0)
        replay["usd_known"] = replay["usd_known"] or bool(rr.get("usd_known"))
    total["bug_replay"] = replay
    total["governed_families"] = families
    total["passed_families"] = passed
    total["passed_commands"] = commands
    total["lint_blocked_codes"] = blocked
    total["consulted"] = consulted
    total["governed_saved_tokens"] = max(
        0, total["governed_original_tokens"] - total["governed_entered_tokens"])
    total["carry_basis"] = "turns after each event, per session"
    return total


def _bug_replay(ctx: Ctx, hits: Dict[str, int], sessions: Dict[str, str],
                usage_mod: Any) -> Dict[str, Any]:
    """What the bugs `bug find` returned this period cost to fix the first
    time: each bug's session, from its start to the `bug add`, counted once
    per bug however often it was found again. An upper bound -- the session
    may have done other work first, and a second look might have been quicker
    without the record. A bug with no session is `predate`: recorded before
    `bug add` noted one and outside every transcript this repo still has, or
    imported from another tool."""
    from . import journal as journal_mod  # local: not on the hook path.
    out: Dict[str, Any] = {"hits": sum(hits.values()), "bugs": len(hits),
                           "priced": 0, "predate": 0, "tokens": 0, "usd": 0.0,
                           "usd_known": False}
    if not hits:
        return out
    ts_of = {str(e.get("id")): str(e.get("ts", "")) for e in
             journal_mod.entries(ctx) + journal_mod._quarantined(ctx)
             if e.get("kind") == "bug"}
    for bug in hits:
        ts = ts_of.get(bug, "")
        session = sessions.get(bug)
        if not session or session == "cli":
            session = usage_mod.session_at(ctx.root, ts) if ts else None
        used = usage_mod.usage([session], None, until=ts) if session and ts else None
        if not used or not used["sessions_found"]:
            out["predate"] += 1
            continue
        tokens = used["context_in"] + used["context_resent"] + used["output_tokens"]
        if not tokens:
            out["predate"] += 1
            continue
        out["priced"] += 1
        out["tokens"] += tokens
        if used["context_in_priced"]:
            out["usd_known"] = True
            out["usd"] += (used["context_in_usd"] + used["context_resent_usd"]
                           + used["output_usd"])
    return out


def _fold_old_ranged(groups: Dict[tuple, List[tuple]]) -> int:
    """The credit for ranged rows that predate the per-file accounting: per
    file (session and file size standing in for the path), the whole file less
    every window returned, floored at zero. Two files of the same size read in
    one session fold together and understate; that is the conservative side."""
    total = 0
    for rows in groups.values():
        whole = max(w for w, _ in rows)
        total += max(0, whole - sum(r for _, r in rows))
    return total


def _carry_by_session(spans: Dict[str, int], rows: List[tuple], want: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for sess, at, tokens, kind in rows:
        if kind == want:
            out[sess] = out.get(sess, 0) + tokens * max(0, spans.get(sess, at) - at)
    return out


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
