"""Bash output governance: condense a flood before it enters the conversation.

The task eval behind this tool found the sift directory's prose changed no outcome and
cost 1.2 to 9.3 times. What it never touched is the other direction: anything
that lands in context is re-sent with every later request, so a single grep
flood is not paid once, it is paid for the rest of the session. That is the
mechanism with numbers behind it, and this module is that mechanism.

The rules here are OpenWolf's (`src/hooks/bash-output-governor.ts` in
cytostack/openwolf), arrived at with production measurement behind them and
followed as a design rather than copied as code -- theirs is AGPL TypeScript,
this is not. What was taken: the token threshold, the family split, which
families may be replaced at all, the 70% abandonment rule, and two refusals
that look like details and are not.

- **Test and build output is never replaced.** A truncated stack trace is
  worse than an expensive one, so those families get a suggestion instead.
- **A pointer must name a file that exists.** A model trusts the pointer and
  stops looking, so when the log could not be written the note says the text
  is gone rather than naming a file that is not there.

Everything in here is pure except `preserve`, so the rules can be tested
without a repo, a hook payload or a session.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Dict, NamedTuple, Optional

# Families, and what may be done to each. Only the three whose information is
# structural -- and therefore recoverable from the log -- may be replaced.
REPLACE_FAMILIES = ("grep_flood", "file_print", "git_show")
SUGGEST_FAMILIES = ("test", "build", "unknown")

DEFAULTS = {
    "advise": True,
    "enabled": False,
    "threshold_tokens": 2000,
    "max_log_bytes": 4 * 1024 * 1024,
    "cache_budget_bytes": 64 * 1024 * 1024,
    "head_lines": 80,
    "tail_lines": 30,
    "grep_per_file": 3,
    "abandon_ratio": 0.7,
}

_GREP_RE = re.compile(r"^(grep|egrep|fgrep|rg|ag|ack)\b")
_PRINT_RE = re.compile(r"^(cat|head|tail|sed|nl|bat)\b")
_GIT_SHOW_RE = re.compile(r"^git\s+(?:-[A-Za-z]\s+\S+\s+|-\S+\s+"
                          r"|--\S+(?:[= ]\S+)?\s+)*(show|diff|log)\b")
_TEST_RE = re.compile(r"\b(pytest|unittest|vitest|jest|mocha|go\s+test|cargo\s+test"
                      r"|npm\s+(?:run\s+)?test|yarn\s+test|tests?/run\.py)\b")
_BUILD_RE = re.compile(r"\b(tsc|webpack|vite\s+build|rollup|cargo\s+build"
                       r"|npm\s+run\s+build|yarn\s+build|docker\s+build|mvn|gradle)\b")
# `make` is an ordinary English word as well as a command, so it only counts at
# the head of one. As a bare `\bmake\b` in the group above, `grep -rn make src/`
# classified as a build: the grep flood was never condensed, and the agent was
# told build output can be very large.
_MAKE_RE = re.compile(r"(?:^|[;&|\n]\s*)(?:\w+=\S*\s+)*make\b")
_ENV_PREFIX_RE = re.compile(r"^(?:\w+=\S*\s+)*")


class Condensed(NamedTuple):
    text: str
    original_tokens: int
    entered_tokens: int
    family: str


def estimate_tokens(text: str) -> int:
    """Characters over 3.5, rounded up. Crude, stable, and never self-reported."""
    return -(-len(text) * 2 // 7)


# `;`, `&&`, `||` and newlines start a new command. A pipe does not: in
# `grep x . | head -50` the head of the pipeline is what produces the output
# and the rest only filters it, which is already the behaviour we want.
_SEGMENT_RE = re.compile(r"(?:;|&&|\|\||\n)")


def _classify_one(segment: str) -> str:
    """The family of a single command, by what it starts with."""
    head = _ENV_PREFIX_RE.sub("", segment.strip(), count=1).lstrip()
    if not head:
        return "unknown"
    if _GIT_SHOW_RE.match(head):
        return "git_show"
    if _GREP_RE.match(head):
        return "grep_flood"
    if _PRINT_RE.match(head):
        return "file_print"
    return "unknown"


def classify(command: str) -> str:
    """Which output family a command belongs to.

    Test and build are checked first and anywhere in the command, because
    `npm test 2>&1 | grep -i fail` is a test run whose output must survive
    intact, not a grep whose output may be cut to pieces.

    Chains are classified by their parts, not by the first word. Half of one
    real session's commands were `cd <path> && sed -n ...` or
    `find . ... | sort`, whose first word is in no family at all, so they were
    read as `unknown` and left alone however much they printed -- the governor
    could not see them, whatever the threshold. A chain whose parts all agree
    keeps that family; a mixed chain falls back to `file_print`, whose head
    and tail lines are the only treatment that is honest about output
    interleaved from several commands. Grep's per-file match counts would be a
    lie about `ls -a; rg -n ...`, where half the lines are not matches.
    """
    cmd = command.strip()
    if not cmd:
        return "unknown"
    if _TEST_RE.search(cmd):
        return "test"
    if _BUILD_RE.search(cmd) or _MAKE_RE.search(cmd):
        return "build"
    segments = [seg for seg in _SEGMENT_RE.split(cmd) if seg.strip()]
    if len(segments) <= 1:
        return _classify_one(cmd)
    families = [_classify_one(seg) for seg in segments]
    replaceable = {f for f in families if f in REPLACE_FAMILIES}
    if not replaceable:
        return "unknown"
    if len(replaceable) == 1 and len(set(families)) == 1:
        return families[0]
    return "file_print"


def prune(cache_dir: Path, budget_bytes: int, keep: Optional[Path] = None) -> int:
    """Drop the oldest logs until the cache fits its budget. Returns bytes freed.

    `keep` is never deleted. Without it this is OpenWolf's issue #82 waiting to
    happen: the prune runs right after the write, an output near the budget
    evicts itself, and the model is handed a pointer to a file that no longer
    exists -- which it trusts, and stops looking.
    """
    try:
        logs = sorted((p for p in cache_dir.glob("*.log") if p.is_file()),
                      key=lambda p: p.stat().st_mtime)
    except OSError:
        return 0
    total = 0
    sizes = []
    for path in logs:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        sizes.append((path, size))
        total += size
    freed = 0
    for path, size in sizes:
        if total <= budget_bytes:
            break
        if keep is not None and path == keep:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        freed += size
    return freed


def preserve(cache_dir: Path, stdout: str, max_bytes: int,
             budget_bytes: Optional[int] = None) -> Optional[str]:
    """Write the full output and return its path, or None if it was not kept.

    None is a meaningful answer and the caller must say so rather than pointing
    at nothing: an oversized output is exactly the one a model most wants to go
    back to, and a pointer it cannot follow is worse than being told plainly
    that the text is gone.

    The existence check at the end is not belt and braces. The prune runs
    between the write and the return, so the only honest way to report a file
    as preserved is to look for it after everything that could remove it.
    """
    raw = stdout.encode("utf-8", "replace")
    if len(raw) > max_bytes:
        return None
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha1(raw).hexdigest()[:16] + ".log"
        path = cache_dir / name
        if not path.exists():
            path.write_bytes(raw)
        if budget_bytes:
            prune(cache_dir, int(budget_bytes), keep=path)
        return str(path) if path.is_file() else None
    except OSError:
        return None


def _elide(shown: int, total: int, unit: str = "lines") -> str:
    return "... {} more {} condensed (not shown)".format(total - shown, unit)


def _condense_grep(stdout: str, per_file: int, head: int, tail: int) -> str:
    """First matches per file, a count for the rest, then a total.

    A flood of matches is nearly always answering "where does this live", and
    the first hit in each file answers it. The per-file counts keep the shape
    of the answer, which a plain head would throw away.
    """
    per: "Dict[str, list]" = {}
    order = []
    plain = []
    for line in stdout.split("\n"):
        if not line:
            continue
        m = re.match(r"^([^:]+):", line)
        if not m:
            plain.append(line)
            continue
        path = m.group(1)
        if path not in per:
            per[path] = []
            order.append(path)
        per[path].append(line)
    if not order:
        return _condense_generic(stdout, head, tail)
    out = []
    total = 0
    for path in order:
        hits = per[path]
        total += len(hits)
        out.extend(hits[:per_file])
        if len(hits) > per_file:
            out.append("  {}: {} more matches".format(path, len(hits) - per_file))
    out.append("-- {} matches in {} files".format(total, len(order)))
    if plain:
        out.append("-- plus {} lines that were not path:match".format(len(plain)))
    return "\n".join(out)


def _condense_git_show(stdout: str, head: int, tail: int) -> str:
    """Commit header and per-file diff-line counts; the hunks go."""
    out = []
    current = ""
    hunk_lines = 0
    in_diff = False

    def flush() -> None:
        if current and hunk_lines:
            out.append("  {}: {} diff lines (see full log)".format(current, hunk_lines))

    for line in stdout.split("\n"):
        if line.startswith("diff --git"):
            flush()
            in_diff = True
            current = line[len("diff --git "):]
            hunk_lines = 0
            continue
        if not in_diff:
            out.append(line)
            continue
        hunk_lines += 1
    flush()
    if not in_diff:
        return _condense_generic(stdout, head, tail)
    return "\n".join(out)


def _condense_generic(stdout: str, head: int, tail: int) -> str:
    lines = stdout.split("\n")
    if len(lines) <= head + tail + 10:
        return stdout
    return "\n".join(lines[:head] + [_elide(head + tail, len(lines))] + lines[-tail:])


def condense(command: str, stdout: str, cfg: Dict[str, object],
             log_path: Optional[str]) -> Optional[Condensed]:
    """The condensed replacement for `stdout`, or None to leave it alone.

    None on every route that is not clearly a win: an output under the
    threshold, a family that may only be suggested at, and -- the rule worth
    keeping -- a condensation that did not save at least 30%. Rewriting a
    result the model then has to work around costs more than the tokens it
    saved, which is why the re-run rate sits next to the savings in the ledger.
    """
    family = classify(command)
    if family not in REPLACE_FAMILIES:
        return None
    original = estimate_tokens(stdout)
    threshold = int(cfg.get("threshold_tokens") or DEFAULTS["threshold_tokens"])
    if original < threshold:
        return None

    head = int(cfg.get("head_lines") or DEFAULTS["head_lines"])
    tail = int(cfg.get("tail_lines") or DEFAULTS["tail_lines"])
    if family == "grep_flood":
        body = _condense_grep(stdout, int(cfg.get("grep_per_file") or 3), head, tail)
    elif family == "git_show":
        body = _condense_git_show(stdout, head, tail)
    else:
        body = _condense_generic(stdout, head, tail)

    if log_path is None:
        pointer = ("\n[sift: this output was ~{:,} tokens and was condensed. It was too "
                   "large to cache, so the full text was NOT kept -- re-run the command "
                   "if you need it verbatim.]".format(original))
    else:
        pointer = ("\n[sift: this output was ~{:,} tokens and was condensed. Full output "
                   "preserved verbatim at {}]".format(original, log_path))
    text = body + pointer
    entered = estimate_tokens(text)
    ratio = float(cfg.get("abandon_ratio") or DEFAULTS["abandon_ratio"])
    if entered > original * ratio:
        return None
    return Condensed(text, original, entered, family)


def suggestion(command: str) -> Optional[str]:
    """What to say about a family that must not be replaced.

    Said once per family per session by the caller. A note on every test run
    would cost more context than the run it is complaining about.
    """
    family = classify(command)
    if family not in ("test", "build"):
        return None
    cmd = command.strip()
    return ("sift: {} output can be very large, and every line of it stays in context "
            "for the rest of the session. Consider capping it yourself:\n"
            "  {} > /tmp/last-run.log 2>&1; s=$?; tail -n 150 /tmp/last-run.log; exit $s\n"
            "The full log stays on disk. This note appears once per session per family."
            .format(family, cmd))
