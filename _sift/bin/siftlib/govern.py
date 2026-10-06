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
import os
import re
import shlex
from pathlib import Path
from typing import Dict, NamedTuple, Optional

# Families, and what may be done to each. Only the three whose information is
# structural -- and therefore recoverable from the log -- may be replaced.
REPLACE_FAMILIES = ("grep_flood", "file_print", "git_show")

DEFAULTS = {
    "advise": True,
    "enabled": True,
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
# git's global options that take their value as the next word. Everything
# else before the subcommand is a flag on its own, `--flag=value` included.
_GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
_GIT_SHOW_SUBCOMMAND_RE = re.compile(r"(?:show|diff|log)\b")
# Test and build runners, matched at the head of a command -- after any
# environment assignments and runner prefix (`python3 -m`, `uv run`, `npx`) --
# never anywhere in it. Matched anywhere, `grep -rn pytest src/` and
# `cat webpack.config.js` were test and build output, never condensed; `make`
# was fixed for exactly this earlier, and the rest were missed.
_TEST_RE = re.compile(r"^(?:pytest|py\.test|unittest|vitest|jest|mocha|rspec|phpunit|tox|nox"
                      r"|go\s+test|cargo\s+test|dotnet\s+test|mix\s+test|swift\s+test"
                      r"|(?:npm|yarn|pnpm|bun)\s+(?:run\s+)?test(?::\S*)?"
                      r"|(?:\./)?gradlew?\s+(?:\S+\s+)*test|mvn\s+(?:\S+\s+)*test"
                      r"|\S*tests?/run\.py)\b")
_BUILD_RE = re.compile(r"^(?:tsc|webpack|rollup|esbuild|make|cmake|ninja|mvn|(?:\./)?gradlew?"
                       r"|vite\s+build|next\s+build|cargo\s+build|go\s+build|dotnet\s+build"
                       r"|(?:npm|yarn|pnpm|bun)\s+(?:run\s+)?build|docker\s+(?:compose\s+)?build"
                       r"|swift\s+build|xcodebuild)\b")
# Quoted values too: `W="a dir/with spaces" python3 x.py` is python3, and the
# unquoted form took `dir/with` for the command and kept a path fragment as
# its ledger label.
_ENV_PREFIX_RE = re.compile(r"""^(?:\w+=(?:"[^"]*"|'[^']*'|\S*)\s+)*""")
# What may run in front of the real command without changing what it is.
_RUNNER_PREFIX_RE = re.compile(
    r"^(?:(?:time|env|nice|command|exec|sudo)\s+"
    r"|(?:python3?|py)(?:\.\d+)?\s+-m\s+|(?:python3?|node|bash|sh)(?:\.\d+)?\s+(?=\S)"
    r"|(?:uv|poetry|pipenv|pdm|hatch)\s+run\s+|(?:npx|bunx|pnpx)\s+"
    r"|(?:pnpm|yarn)\s+exec\s+)+")
# Commands that print little and say nothing about the output of what they are
# chained with; a chain of these and one condensable command is condensable.
_QUIET = {"cd", "pushd", "popd", "echo", "printf", "pwd", "export", "set", "true",
          "mkdir", "touch", ":", "source", ".", "clear"}
# Listings whose lines are safe to cut to a head and tail when chained with a
# file print: `ls -a; rg -n foo`.
_INSPECT = {"ls", "find", "tree", "du", "stat", "file", "which", "wc"}
# Stages that only filter what the head of a pipe printed.
_FILTERS = {"grep", "egrep", "fgrep", "rg", "head", "tail", "sort", "uniq", "wc",
            "cut", "awk", "sed", "tr", "jq", "column", "nl", "cat", "less", "more",
            "tee", "fold", "fmt", "rev", "xargs"}


class Condensed(NamedTuple):
    text: str
    original_tokens: int
    entered_tokens: int
    family: str


def estimate_tokens(text: str) -> int:
    """Characters over 3.5, rounded up. Crude, stable, and never self-reported."""
    return -(-len(text) * 2 // 7)


# A heredoc's opening line, `<<'EOF'`, `<<-EOF` or `<< "EOF"`.
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")
# Shell keywords that open a body: what follows them on the segment is the
# command. `for x in ...`, `done`, `fi` and the like print nothing themselves.
_KEYWORD_PREFIX_RE = re.compile(r"^(?:(?:do|then|else|if|elif|while|until|\{|\()\s+)+")
_KEYWORD_ONLY = {"done", "fi", "esac", "}", ")", "do", "then", "else", "for",
                 "select", "case"}
# A value is a run of pieces each told apart by its first character, and
# assignments are separated by mandatory whitespace, so a line can be read as
# assignments only one way. Overlapping alternatives inside a repeat -- `\S*`
# also matching `""`, and an optional separator -- backtracked exponentially
# on `0=0=0=...` and `A="" A="" ... x`, and this runs on every hooked command.
_ASSIGN_VALUE = r"""(?:"[^"]*"|'[^']*'|[^\s"'])*"""
_ASSIGNMENT_RE = re.compile(
    r"^\w+=" + _ASSIGN_VALUE + r"(?:\s+\w+=" + _ASSIGN_VALUE + r")*\s*$")


def _strip_heredocs(command: str) -> str:
    """The command with every heredoc body removed, opening lines kept.

    A body is input, not commands. Split on newlines with the rest, the lines
    of `python3 - <<'EOF'` became "commands" called `import` and `print(`, so
    a chain containing one was unknown whatever else it ran, and its label in
    the ledger was a line of somebody's script.
    """
    lines = command.split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for match in _HEREDOC_RE.finditer(line):
            tag = match.group(2)
            while i < len(lines) and lines[i].strip() != tag:
                i += 1
            i += 1
    return "\n".join(out)


def _split_unquoted(text: str, separators: "tuple[str, ...]") -> "list[str]":
    """Split on any of `separators` outside quotes, longest first.

    The regex split cut inside quotes: `grep -n "a\\|b" f` became a pipe into
    a program called `b"`, which is not a filter, so the commonest grep
    alternation was classified unknown and never condensed.
    """
    parts, buf, quote, i = [], [], "", 0
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(text):
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch == "\\" and i + 1 < len(text):
            buf.append(text[i:i + 2])
            i += 2
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        for sep in separators:
            if text.startswith(sep, i):
                parts.append("".join(buf))
                buf = []
                i += len(sep)
                break
        else:
            buf.append(ch)
            i += 1
    parts.append("".join(buf))
    return parts


def _segments(command: str) -> "list[list[str]]":
    """The commands in a chain, each as its pipeline's stages.

    `;`, `&&`, `||` and newlines start a new command. A pipe does not: in
    `grep x . | head -50` the head of the pipeline is what produces the output
    and the rest only filters it, which is already the behaviour we want.

    Heredoc bodies removed, separators inside quotes ignored, and the shell's
    own keywords taken off: `for f in *.py; do sed -n 1,40p "$f"; done` is a
    chain of `sed`, not of `for`, `do` and `done`. Segments that print nothing
    by construction -- a loop header, a closing keyword, a bare assignment such
    as `S=/some/dir` -- are dropped.
    """
    out = []
    for seg in _split_unquoted(_strip_heredocs(command or ""),
                               ("&&", "||", ";", "\n")):
        seg = _KEYWORD_PREFIX_RE.sub("", seg.strip())
        words = seg.split()
        if not words or words[0] in _KEYWORD_ONLY:
            continue
        if _ASSIGNMENT_RE.match(seg):
            continue
        stages = [st for st in _split_unquoted(seg, ("|",)) if st.strip()]
        if stages:
            out.append(stages)
    return out


def _is_git_show(command: str) -> bool:
    """Whether `command` is `git show`, `git diff` or `git log`, global options and all.

    Tokenized rather than matched: the regex this replaces had overlapping
    alternatives inside a repeat and backtracked exponentially on
    `git --! --! --! ...`, hanging the hook on one crafted command line.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    if not words or words[0] != "git":
        return False
    i = 1
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] in _GIT_VALUE_OPTS else 1
    return i < len(words) and bool(_GIT_SHOW_SUBCOMMAND_RE.match(words[i]))


def _classify_one(segment: str) -> str:
    """The family of a single command, by what it starts with."""
    head = _ENV_PREFIX_RE.sub("", segment.strip(), count=1).lstrip()
    if not head:
        return "unknown"
    if _is_git_show(head):
        return "git_show"
    if _GREP_RE.match(head):
        return "grep_flood"
    if _PRINT_RE.match(head):
        return "file_print"
    return "unknown"


# Interpreters and runners whose own name says nothing about what printed: for
# these the label is the script or subcommand they were given instead.
_RUNNERS = {"python", "python3", "node", "bash", "sh", "zsh", "ruby", "perl",
            "deno", "bun", "npx", "uv", "poetry", "pipenv", "npm", "yarn",
            "pnpm", "cargo", "go", "make"}


def command_label(command: str) -> str:
    """A short, path-free name for what produced a command's output.

    Recorded on a passed-through flood so the ledger can say which commands
    flood, not only that `unknown` ones do. `cd <dir> &&` segments are skipped,
    as they print nothing, and so are banners like `echo ---` when anything
    else ran; an interpreter or runner is named with its script's
    basename or its subcommand (`python3 candidates.py`, `npm run`), and a
    path is never kept whole, since the ledger must not carry one out of the
    repo.
    """
    segments = _segments(command)
    # A banner is no name for a chain: `echo ---; sed -n ...` is a sed.
    loud = [seg for seg in segments if _first_word(seg[0]) not in _QUIET]
    for stages in loud or segments:
        words = _ENV_PREFIX_RE.sub("", stages[0].strip(), count=1).split()
        if not words or words[0] == "cd":
            continue
        head = re.split(r"[\\/]", words[0])[-1]
        if head in _RUNNERS:
            if len(words) > 1 and words[1] in ("-c", "-e", "--eval"):
                # Inline code, not a script: its text is no name for it.
                return "{} {}".format(head, words[1])
            rest = [w for w in words[1:] if not w.startswith("-")]
            if rest[:1] == ["run"] and len(rest) > 1:
                rest = rest[1:]
            if rest[:1] and rest[0].startswith("<<"):
                # A script on stdin: the heredoc's tag is no name for it.
                return "{} <<".format(head)
            if rest:
                return "%s %s" % (head, re.split(r"[\\/]", rest[0])[-1][:40])
        return head[:40]
    return "?"


def _head(segment: str) -> str:
    """A segment with its environment assignments and runner prefix removed."""
    head = _ENV_PREFIX_RE.sub("", segment.strip(), count=1).lstrip()
    return _RUNNER_PREFIX_RE.sub("", head, count=1)


def _first_word(segment: str) -> str:
    words = _ENV_PREFIX_RE.sub("", segment.strip(), count=1).split()
    return words[0].rsplit("/", 1)[-1] if words else ""


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
    segments = _segments(cmd)
    heads = [_head(st) for seg in segments for st in seg]
    if any(_TEST_RE.match(h) for h in heads):
        return "test"
    if any(_BUILD_RE.match(h) for h in heads):
        return "build"
    families = []
    for parts in segments:
        family = _classify_one(parts[0])
        # A pipe into a program, not a filter, is that program's output:
        # `cat f | python3 check.py` prints what the script prints.
        if any(_first_word(p) not in _FILTERS for p in parts[1:]):
            family = "unknown"
        if family == "unknown" and _first_word(parts[0]) in _QUIET:
            continue
        if family == "unknown" and len(segments) > 1 and _first_word(parts[0]) in _INSPECT:
            # Listings, not results: cut with the rest, as before.
            family = "file_print"
        families.append(family)
    if not families:
        return "unknown"
    if len(set(families)) == 1:
        return families[0]
    # Mixed. Only condensable parts, of different kinds: head and tail lines
    # are the one treatment honest about interleaved output. Anything else in
    # the chain -- a test runner the patterns do not know, a script -- may be
    # what printed the lines that matter, so it is left whole.
    if all(f in REPLACE_FAMILIES for f in families):
        return "file_print"
    return "unknown"


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
        else:
            # Newest again, or the prune below could delete the very log the
            # model is about to be pointed at, as the oldest in the cache.
            os.utime(str(path), None)
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
        if in_diff and line.startswith("commit ") and re.match(r"^commit [0-9a-f]{7,}", line):
            # The next commit of a `git log -p`: its header and message are
            # not diff lines of the file before it.
            flush()
            in_diff = False
            current, hunk_lines = "", 0
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
