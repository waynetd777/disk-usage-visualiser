"""`sift lint` - the deterministic backstop for the rules that cannot be undone.

Six checks survive the removal of the pages. Three are hard errors the
pre-commit hook blocks on -- W15 secrets, W16 out-of-repo absolute paths, W19
a tracked file under `local/` or `local/` missing from `.gitignore` -- because
a later commit cannot undo any of them: a pushed secret has to be rotated, a
laptop path is a small leak and useless to a colleague, and a committed
`local/` file is a disclosure about a person that lives in history. W20 warns
on HR and compensation vocabulary, W04 catches a backticked path that does not
exist, and W21 catches two clones that picked the same decision id.

Why any of this remains, when the pages it used to check are gone: the rules
it enforces are the one thing the task eval could show a model does not
reconstruct on its own, and the content it checks is written *by* a model,
across long sessions, under the instruction decay `post_tool` re-injection
exists to slow. Re-injection lowers the odds of the rule being forgotten;
this catches it when it is forgotten anyway.

The ten page checks went with the pages, and four more went with them because
they were not worth their noise: `files.jsonl` hygiene (W09, W13, W18) is
machine-written and cheap when wrong, W14's whole-directory token budget is
meaningless for three files, and W17's one-sentence-per-line is a convention
that does not need a checker. They are in `git show v1-pages:src/siftlib/lint.py`
if the judgement was wrong.

The codes that block a commit are the ones the pre-commit hook passes to
`--only` (`templates/githooks/pre-commit`); nothing in config changes them.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

from . import gitutil, ignore, templates, util
from .config import Config
from .paths import Ctx

SEV_ERROR = "error"
SEV_WARNING = "warning"
SEV_INFO = "info"


SECRET_PATTERNS = [
    # A named secret with a value. The name may run on (`AWS_SECRET_ACCESS_KEY`,
    # `api_key_prod`) and may be a quoted JSON key, which is how one sits in a
    # config file: `"api_key": "..."` has a quote between the name and the colon.
    re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd|pwd|access[_-]?key)"
               r"[A-Za-z0-9_]*['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+=.]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # Credentials in any URL's userinfo, not only a database's: `https://u:p@host`.
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/'\"@:]+:[^\s/'\"@]+@"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"),
    # OpenAI and Anthropic keys carry dashes and underscores after `sk-`
    # (`sk-proj-...`, `sk-ant-api03-...`), which `[A-Za-z0-9]` stopped at.
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"xox[bap]-[A-Za-z0-9-]+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\."),
]

# The character before the path may be anything that cannot itself be part of
# a path. Listing the openers instead -- whitespace, quote, paren -- missed the
# one form a model actually writes in markdown: `` `/Users/wayned/notes` ``
# passed `lint --staged` clean, and so did `path=/Users/...` and `at: ~/x`,
# which is a hard-fail privacy check failing open on its normal case.
ABSOLUTE_PATTERNS = [
    re.compile(r"(^|[^A-Za-z0-9_./~-])/(Users|home|Volumes|private|tmp|var)/"),
    re.compile(r"(^|[^A-Za-z0-9_./~-])~/"),
    re.compile(r"C:\\\\Users\\\\"),
    re.compile(r"C:\\Users\\"),
    # The class above excludes `/` so that a repo-relative `docs/Users/` is not
    # a leak, and that let two real forms of a machine path through: a
    # `file:///Users/...` link, where the character before is the URL's own
    # slash, and a WSL mount, `/mnt/c/Users/...`, where it is the drive
    # letter. Both are named here on their own.
    re.compile(r"(?i)file:/+(?:[A-Za-z0-9.-]+/)?(Users|home|Volumes|private|tmp|var)/"),
    re.compile(r"(?i)/mnt/[a-z]/Users/"),
]
# Paths that are the same on every machine, so they are not a leak. Scrubbed
# from the line before W16 looks at it rather than excusing the line: skipping
# the whole line meant `ran it from /Users/someone/work 2>/dev/null` passed a
# hard-fail privacy check because it also mentioned /dev/null.
ABSOLUTE_ALLOW = ("/usr/bin/python3", "/dev/null")


def _without_allowed(line: str) -> str:
    for allowed in ABSOLUTE_ALLOW:
        line = line.replace(allowed, "")
    return line

# W20. Deliberately narrow: this cannot detect "Bob is struggling", and
# pretending otherwise would train people to ignore it. It catches the
# vocabulary that is almost never about code -- an HR process, or money
# attached to a person -- and says where that material belongs instead.
HR_PATTERNS = [
    re.compile(r"(?i)\b(performance review|performance improvement plan|\bPIP\b|"
               r"disciplinary|grievance|probation review|redundanc|severance|"
               r"succession plan|talent review|calibration session)\b"),
    # An amount, not any digit: "W20" sitting near the word "compensation" is
    # how a check like this starts crying wolf, and the first thing it cried
    # wolf at was the page documenting it.
    re.compile(r"(?i)\b(salary|remuneration|compensation|day ?rate|rate ?card|"
               r"cost to company|\bCTC\b|headcount cost|bonus|equity grant)\b"
               r"[^\n]{0,40}?(?:[R$£€]\s?\d|\d{3})"),
    # The amount's tail is bounded: unbounded, it and the 40-character window
    # overlapped and a long run of digits took a minute to search.
    re.compile(r"(?i)(?:[R$£€]\s?\d|\d{3})[\d ,.]{0,30}[^\n]{0,40}?\b(salary|remuneration|"
               r"compensation|day ?rate|rate ?card|cost to company|\bCTC\b|"
               r"per annum|p\.?a\.?)\b"),
]


class Issue(dict):
    def __init__(self, code: str, severity: str, path: str, line: int,
                 message: str, fix: str) -> None:
        super().__init__(code=code, severity=severity, path=path, line=line,
                         message=message, fix=fix)


def summarise(issues: Sequence[dict]) -> Dict[str, int]:
    return {
        "errors": sum(1 for i in issues if i["severity"] == SEV_ERROR),
        "warnings": sum(1 for i in issues if i["severity"] == SEV_WARNING),
        "info": sum(1 for i in issues if i["severity"] == SEV_INFO),
    }


def _in_fence(lines: Sequence[str]) -> List[bool]:
    out: List[bool] = []
    fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            out.append(True)
            fence = not fence
            continue
        out.append(fence)
    return out


def run(ctx: Ctx, cfg: Config,
        only: Optional[Set[str]] = None,
        staged: bool = False) -> List[dict]:
    issues: List[dict] = []

    def want(code: str) -> bool:
        return not (only and code not in only)

    if not ctx.exists:
        return issues

    staged_set: Optional[Set[str]] = None
    if staged:
        staged_set = {p for p in gitutil.staged_files(ctx.root) if ctx.is_sift_path(p)}

    def selected(rel_to_root: str) -> bool:
        return staged_set is None or rel_to_root in staged_set

    tracked = set(gitutil.ls_files(ctx.root))

    # --- W19: is local/ actually local? ------------------------------------
    if want("W19"):
        prefix = ctx.dir_name + "/local/"
        leaked = sorted(p for p in tracked if p.startswith(prefix))
        for path in leaked:
            issues.append(Issue("W19", SEV_ERROR, path, 1,
                                "a file under local/ is tracked by git - local/ is the "
                                "one place private material is allowed, and it only works "
                                "while nothing in it is committed",
                                "git rm --cached '{}' and check the .gitignore stanza "
                                "is intact".format(path)))
        if not leaked:
            # Asked of git, not read off the root .gitignore: a nested
            # `_sift/.gitignore` ignoring `local/` is fine, a negated
            # `!_sift/local/` is not, and a substring test got both wrong.
            if not _local_ignored(ctx, prefix):
                issues.append(Issue("W19", SEV_ERROR, ".gitignore", 1,
                                    "local/ is not gitignored, so nothing stops private "
                                    "material being committed",
                                    "add '{}' to .gitignore".format(prefix)))

    # --- W21: two clones, one decision id ----------------------------------
    # Union merge keeps both records now instead of merging them into one, so
    # nothing is lost -- but two records answering to `D-20260916-01` make every
    # citation of it ambiguous, and only a person can say which keeps the number.
    if want("W21"):
        from . import journal as journal_mod
        seen: Dict[str, int] = {}
        for rec in journal_mod.decisions(ctx):
            did = str(rec.get("id", ""))
            seen[did] = seen.get(did, 0) + 1
        for did, count in sorted(seen.items()):
            if count > 1:
                issues.append(Issue("W21", SEV_WARNING, ctx.rel(ctx.decisions), 1,
                                    "{} records share the id {} - two clones decided on "
                                    "the same day".format(count, did),
                                    "renumber all but one to the next free id for that "
                                    "date and fix any reference to it"))

    # --- every document in the sift directory, as text -------------------------------
    # Not "every page": there are no pages. Anything committed under the sift directory
    # is content a colleague will read, which is the only property
    # these checks care about.
    if staged_set is None:
        candidates = [p for p in sorted(ctx.dir.rglob("*")) if p.is_file()]
    else:
        # Include paths from the index even when the worktree copy was changed
        # or removed after staging. The index is the content the hook guards.
        candidates = [ctx.root / p for p in sorted(staged_set)]
    for path in candidates:
        # Every committed file under the sift directory, not only `.md` and
        # `.jsonl`: a secret in `config.json` or a machine path in
        # `.siftignore` is committed all the same, and both went unscanned.
        # Binaries are told by content, below, not by extension.
        rel_sift = path.relative_to(ctx.dir).as_posix()
        if rel_sift.startswith(("local/", ".cache/", "bin/")):
            continue
        rel = ctx.dir_name + "/" + rel_sift
        if not selected(rel):
            continue
        text = (gitutil.staged_text(ctx.root, rel)
                if staged_set is not None else util.read_text(path))
        if text is None:  # staged deletion: no content will enter the commit
            continue
        if not text or "\x00" in text:
            continue
        lines = text.split("\n")
        # `conventions.md` and the README state the privacy rule, which means
        # spelling out the vocabulary the rule is about. A check that fires on
        # its own documentation is noise -- but W15 and W16 still apply there,
        # because a secret in a generated file is still a secret.
        own_docs = rel_sift in ("conventions.md", "README.md")
        if path.suffix.lower() == ".jsonl":
            scanned = _jsonl_lines(issues, rel, lines, want)
        else:
            scanned = list(enumerate(lines))
        _secret_scan(issues, rel, scanned, want, privacy=not own_docs,
                     generated=_generated_lines(text) if own_docs else frozenset())

        if want("W04") and path.suffix.lower() == ".md":
            fences = _in_fence(lines)
            for i, line in enumerate(lines):
                if fences[i]:
                    continue
                for token in re.findall(r"`([^`]+)`", line):
                    problem = _check_code_ref(ctx, token, tracked, path)
                    if problem:
                        issues.append(Issue("W04", SEV_WARNING, rel, i + 1,
                                            problem, "Fix the reference"))

    issues.sort(key=lambda i: (0 if i["severity"] == SEV_ERROR else
                               1 if i["severity"] == SEV_WARNING else 2,
                               i["code"], i["path"], i["line"]))
    return issues


_CODE_REF_RE = re.compile(r"^(?P<path>[A-Za-z0-9_.@/\-]+)(?::(?P<line>\d+))?$")


def _check_code_ref(ctx: Ctx, token: str, tracked: Set[str],
                    page_path: Path) -> Optional[str]:
    if " " in token or token.endswith("/") or "*" in token:
        return None
    m = _CODE_REF_RE.match(token)
    if not m:
        return None
    rel = m.group("path")
    if "/" not in rel:
        return None  # a bare word or filename is prose, not a code reference
    if rel.startswith(("http", "./", "../", "/", "-")):
        # A leading slash is a slash-command or an absolute path; neither is a
        # repo-relative code reference. W16 owns absolute paths.
        return None
    candidates = [rel]
    if not rel.startswith(ctx.dir_name + "/"):
        candidates.append(ctx.dir_name + "/" + rel)
    hit = next((c for c in candidates if c in tracked), None)
    if hit is None:
        on_disk = next((c for c in candidates if (ctx.root / c).exists()), None)
        if on_disk is None:
            return "code reference not found in the repo: " + token
        hit = on_disk
    line_no = m.group("line")
    if line_no:
        try:
            total = len((ctx.root / hit).read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            return None
        if int(line_no) > total:
            return "line {} is past the end of {} ({} lines)".format(line_no, hit, total)
    return None


def _generated_lines(text: str) -> "frozenset[int]":
    """The 0-based line numbers of our own generated marker block.

    Only `conventions.md` and `README.md` have one, and only W16 is waived
    inside it: the block is byte-identical on every machine by construction, so
    `/Users/...` in it is an example of the rule rather than a leak from
    somebody's laptop. Anything a user writes below the end marker is theirs
    and stays checked -- which is where the marker tells them to write.
    """
    before, block, _after = templates.marker_split(text)
    if block is None:
        return frozenset()
    first = before.count("\n")
    return frozenset(range(first, first + block.count("\n") + 1))


def _local_ignored(ctx: Ctx, prefix: str) -> bool:
    """Whether git would ignore a file under local/. When git cannot answer
    (no repository, git missing), the root and nested .gitignore files are read
    instead, as before -- a guess, but not a false alarm on every run."""
    proc = gitutil.run(ctx.root, ["check-ignore", "-q", prefix + ".sift-probe"])
    if proc.returncode in (0, 1):
        return proc.returncode == 0
    root = util.read_text(ctx.root / ".gitignore")
    nested = util.read_text(ctx.dir / ".gitignore")
    return prefix in root or "local/" in nested.split()


def _jsonl_lines(issues: List[dict], rel: str, lines: Sequence[str],
                 want) -> List["tuple"]:
    """Each JSONL line, plus every line of every string value in it, decoded.

    A record stores a newline as `\\n`, so on the raw line the letter before a
    path on its second line is `n`, which W16 reads as part of a word: a
    `bug add` whose error spanned two lines committed `/Users/...` clean.
    Decoded, the path starts a line of its own. A line that does not parse is
    reported (W22): the reader skips it, so a record in it is lost silently."""
    out: List[tuple] = []
    for i, raw in enumerate(lines):
        out.append((i, raw))
        if not raw.strip() or raw.lstrip().startswith("//"):
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            if want("W22"):
                issues.append(Issue("W22", SEV_WARNING, rel, i + 1,
                                    "line is not valid JSON, so every reader skips "
                                    "it and any record in it is lost",
                                    "fix or split the line; two records glued "
                                    "together need a newline between them"))
            continue
        for value in _strings(record):
            for part in re.split(r"\r\n|[\n\r\u2028\u2029\u0085]", value):
                out.append((i, part))
    return out


def _strings(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in list(value.keys()) + list(value.values()) for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


# Inside our own generated block, the examples of the rule itself -- written
# with an ellipsis, `/Users/...` -- are scrubbed before W16 looks. Waiving the
# whole block let anything typed between the markers through.
_RULE_EXAMPLE = re.compile(r"(?:~|/[A-Za-z]+)/\.\.\.")


def _secret_scan(issues: List[dict], rel: str, lines: Sequence["tuple"],
                 want, privacy: bool = True,
                 generated: "frozenset[int]" = frozenset()) -> None:
    """`privacy=False` for the files we generate ourselves: `conventions.md`
    states the rule, which means spelling out the vocabulary the rule is about.
    A check that fires on its own documentation is noise. W15 and W16 still
    apply there -- a secret in a generated file is still a secret -- except for
    W16 inside the generated block itself, which `generated` carries.

    `lines` is (line index, text) pairs, so one committed line can be scanned
    as several texts (a JSONL record's decoded values) and still be reported
    once, at its own line number."""
    reported: Set[tuple] = set()

    def report(code: str, i: int, issue: dict) -> None:
        if (code, i) not in reported:
            reported.add((code, i))
            issues.append(issue)

    for i, line in lines:
        if privacy and want("W20"):
            for pattern in HR_PATTERNS:
                if pattern.search(line):
                    report("W20", i, Issue(
                        "W20", SEV_WARNING, rel, i + 1,
                        "reads like HR or compensation material, which is never "
                        "what a committed sift directory is for",
                        "move it to local/ - that directory is gitignored and exists "
                        "for exactly this"))
                    break
        if want("W15"):
            for pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    report("W15", i, Issue("W15", SEV_ERROR, rel, i + 1,
                                           "looks like a secret",
                                           "Remove it and rotate the secret"))
                    break
        if want("W16"):
            probe = _without_allowed(line)
            if i in generated:
                probe = _RULE_EXAMPLE.sub("", probe)
            for pattern in ABSOLUTE_PATTERNS:
                if pattern.search(probe):
                    report("W16", i, Issue("W16", SEV_ERROR, rel, i + 1,
                                           "absolute path outside the repo",
                                           "Move it to local/ or make it repo-relative"))
                    break
