"""What the sessions behind a ledger actually consumed, from their transcripts.

The ledger only sees tool output, so on its own it can say what sift kept out
but not what that was out of. Claude Code records the API's own token usage on
every assistant message in the session transcript, so the denominator is
there to be read rather than estimated.

Three things make the sum honest. A message is written to the transcript more
than once while it streams, each copy carrying the same usage, so messages are
counted once by id. A subagent's requests are in their own files under the
session's directory, and sift's hooks credit its tool calls to the parent
session, so those files are counted with it. And only messages inside the
ledger's window are counted, so a session that began before the cutoff is not
charged in full.

Codex keeps its own session logs (`~/.codex/sessions/YYYY/MM/DD/rollout-*-<id>.jsonl`),
whose `token_count` events carry each request's usage. Its `input_tokens`
include the cached ones, so new context is the difference; an event is counted
only when the running total moved, since Codex repeats the last one. Codex's
models are not in the price table, so they are counted in tokens only.

A transcript can be cleaned up by the harness, so the result carries how many
of the sessions were found; the caller says so rather than presenting a
partial total as the whole.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from . import hookcheck, prices

FIELDS = ("context_in", "context_resent", "output_tokens")
# Dollars, and the tokens that were priced -- a model missing from the price
# table is counted in tokens but not here, so an average price per token is
# cost over priced tokens, never cost over all of them.
COSTS = ("context_in_usd", "context_resent_usd", "output_usd")
PRICED = ("context_in_priced", "context_resent_priced")


CODEX_SESSIONS = Path("~/.codex/sessions").expanduser()
_CODEX_ID = re.compile(r"-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")


def _index(base: Path, codex: Path) -> Dict[str, List[Path]]:
    """session id -> its transcripts (Claude Code's, its subagents', Codex's),
    one walk of each tree."""
    out: Dict[str, List[Path]] = {}
    if base.is_dir():
        for path in base.glob("*/*.jsonl"):
            out.setdefault(path.stem, []).append(path)
        for path in base.glob("*/*/subagents/*.jsonl"):
            out.setdefault(path.parent.parent.name, []).append(path)
    if codex.is_dir():
        for path in codex.glob("*/*/*/rollout-*.jsonl"):
            m = _CODEX_ID.search(path.stem)
            if m:
                out.setdefault(m.group(1), []).append(path)
    return out


_CACHE: Dict[str, Dict[str, List[Path]]] = {}


def _files_for(session: str) -> List[Path]:
    # Read at call time, not import, so a test that points it elsewhere after
    # sift is imported is still obeyed.
    env = os.environ.get("SIFT_TRANSCRIPTS")
    base = Path(env).expanduser() if env else hookcheck.TRANSCRIPTS
    env = os.environ.get("SIFT_CODEX_SESSIONS")
    codex = Path(env).expanduser() if env else CODEX_SESSIONS
    key = "{}|{}".format(base, codex)
    if key not in _CACHE:
        _CACHE[key] = _index(base, codex)
    return _CACHE[key].get(session, [])


def usage(sessions: Iterable[str], cutoff: Optional[str] = None) -> Dict[str, int]:
    """{sessions, sessions_found, context_in, context_resent, output_tokens}.

    context_in is new tokens written to context (uncached input plus cache
    writes): what sift's saving is a share of. context_resent is cache reads,
    the conversation re-sent on each turn."""
    total: Dict = {"sessions": 0, "sessions_found": 0}
    total.update({f: 0 for f in FIELDS + PRICED})
    total.update({f: 0.0 for f in COSTS})
    total["by_model_usd"] = {}
    seen: set = set()
    for session in sorted(set(sessions)):
        total["sessions"] += 1
        files = _files_for(session)
        if not files:
            continue
        total["sessions_found"] += 1
        for path in files:
            if path.name.startswith("rollout-"):
                _read_codex(path, cutoff, total)
            else:
                _read_claude(path, cutoff, total, seen)
    return total


def _add(total: Dict, model: Optional[str], u: dict) -> None:
    fresh = int(u.get("input_tokens") or 0)
    written = int(u.get("cache_creation_input_tokens") or 0)
    read = int(u.get("cache_read_input_tokens") or 0)
    out = int(u.get("output_tokens") or 0)
    total["context_in"] += fresh + written
    total["context_resent"] += read
    total["output_tokens"] += out
    rates = prices.price(model)
    if rates is None:
        return
    rate_in, rate_out, rate_read = rates
    # The write split by cache lifetime, when the transcript carries it; a
    # 1-hour write costs 2x input where a 5-minute one costs 1.25x.
    split = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    w1h = int(split.get("ephemeral_1h_input_tokens") or 0)
    w5m = written - w1h if split else written
    cost_in = (fresh * rate_in + w5m * rate_in * prices.WRITE_5M
               + w1h * rate_in * prices.WRITE_1H) / 1e6
    cost_read = read * rate_read / 1e6
    total["context_in_usd"] += cost_in
    total["context_resent_usd"] += cost_read
    total["output_usd"] += out * rate_out / 1e6
    total["context_in_priced"] += fresh + written
    total["context_resent_priced"] += read
    by = total["by_model_usd"]
    by[model] = by.get(model, 0.0) + cost_in + cost_read


def _lines(path: Path) -> Iterable[dict]:
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            if '"usage"' not in line and '"token_count"' not in line \
                    and '"turn_context"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


def _read_claude(path: Path, cutoff: Optional[str], total: Dict, seen: set) -> None:
    for row in _lines(path):
        message = row.get("message")
        if not isinstance(message, dict):
            continue
        u = message.get("usage")
        mid = message.get("id")
        if not isinstance(u, dict) or not mid or mid in seen:
            continue
        if cutoff and str(row.get("timestamp", "")) < cutoff:
            continue
        seen.add(mid)
        _add(total, message.get("model"), u)


def _read_codex(path: Path, cutoff: Optional[str], total: Dict) -> None:
    model = None
    last_total = None
    for row in _lines(path):
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        if row.get("type") == "turn_context":
            model = payload.get("model") or model
            continue
        if payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        running = (info.get("total_token_usage") or {}).get("total_tokens")
        if running == last_total:
            continue
        last_total = running
        if cutoff and str(row.get("timestamp", "")) < cutoff:
            continue
        u = info.get("last_token_usage") or {}
        cached = int(u.get("cached_input_tokens") or 0)
        _add(total, model, {
            "input_tokens": max(0, int(u.get("input_tokens") or 0) - cached),
            "cache_read_input_tokens": cached,
            "output_tokens": int(u.get("output_tokens") or 0)})
