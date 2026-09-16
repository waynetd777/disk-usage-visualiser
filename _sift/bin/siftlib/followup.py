"""What an install still needs from a person or an agent, and how to say so.

`sift init` used to end with the repo wired up and nobody told that the wiring
is not the point: a migrated repo can pass every check while holding zero file
descriptions and a 137KB import nothing has read. That is not a health problem
a checker catches, it is unfinished setup, and the only reason it was ever
visible is that somebody happened to look.

So it is computed in one place and said in three: at the end of `init`, in
`doctor`, and once per session to the agent, which is the one that can actually
do the work. Each item disappears from all three the moment it is done.

An available upgrade travels the same three channels and is kept separate from
the setup items on purpose. It is not unfinished setup -- the install is fine,
the world moved -- and it is the one notice here that must not be acted on
without being asked, because `sift update` rewrites the runtime and the
generated blocks in a repo whose owner did not ask for that today.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import upgrade as upgrade_mod, util
from .config import Config
from .paths import Ctx


def pending(ctx: Ctx, cfg: Config) -> List[Dict[str, Any]]:
    """[{id, what, how, agent}] — empty when the install is finished."""
    out: List[Dict[str, Any]] = []
    dir_name = ctx.dir_name

    imports = sorted(p.name for p in ctx.local.glob("openwolf-*")) if ctx.local.is_dir() else []
    if imports and not _has_decisions(ctx) and _journal_len(ctx) <= 2:
        out.append({
            "id": "promote-import",
            "what": "{} imported file{} in {}/local/ that nothing has read yet"
                    .format(len(imports), "" if len(imports) == 1 else "s", dir_name),
            "how": "read them and write what is still true into decisions and "
                   "the journal; the specifics stay in local/",
            "agent": "Read {}/local/openwolf-* in ranges — one is usually far too "
                     "large to hold at once — and record as you go: `sift decide` "
                     "for a decision, `sift bug add` for a bug that could recur. "
                     "Anything naming a person, a cost or a path outside the repo "
                     "stays in local/ — see conventions.md."
                     .format(dir_name),
        })

    undescribed_hot, hot_total = _hot_gap(ctx, cfg)
    if undescribed_hot:
        out.append({
            "id": "describe",
            "what": "{} of the {} busiest files have no description".format(
                len(undescribed_hot), hot_total),
            "how": "`sift describe --set <path> \"…\"` — the busiest files first; "
                   "the rest accrue as you work",
            "agent": "The pre-read hook has nothing to offer for the files this "
                     "repo changes most. Describe these, reading each one first "
                     "if you do not already know it: {}{}"
                     .format(", ".join(undescribed_hot[:12]),
                             "" if len(undescribed_hot) <= 12
                             else " (and {} more — `sift scan` lists the rest)"
                                  .format(len(undescribed_hot) - 12)),
        })

    return out


def _has_decisions(ctx: Ctx) -> bool:
    return bool(util.read_text(ctx.decisions).strip())


def _journal_len(ctx: Ctx) -> int:
    return len([l for l in util.read_text(ctx.journal).splitlines() if l.strip()])


def _hot_gap(ctx: Ctx, cfg: Config) -> "tuple[List[str], int]":
    """(busiest files with no description, how many were considered).

    Not every file, and not merely one: a 466-file repo reported "nothing left
    to set up" at 49 descriptions, because the check only asked whether any
    existed. Coverage of everything is the wrong target too — conventions say
    descriptions accrue as files are worked on, and most files are never
    opened. What the pre-read hook can actually use is a description of the
    files this repo keeps changing, so that is what is asked for.
    """
    from . import scan as scan_mod

    cache = util.read_json(ctx.cache / "scan.json", default=None)
    if not isinstance(cache, dict):
        return ([], 0)
    files = cache.get("files") or {}
    churn = cache.get("churn") or {}
    if not files:
        return ([], 0)
    top_n = int(cfg.get("setup", "describe_top", default=25) or 25)
    ranked = sorted((p for p in files if not files[p].get("binary")),
                    key=lambda p: (-int(churn.get(p, 0) or 0), p))[:top_n]
    described = scan_mod.load_descriptions(ctx)
    return ([p for p in ranked if p not in described], len(ranked))


def lines(items: List[Dict[str, Any]], dir_name: str) -> List[str]:
    """For a person, at the end of a command."""
    if not items:
        return []
    out = ["", "Not done yet — the install is wired up, but:"]
    for item in items:
        out.append("  - {}".format(item["what"]))
        out.append("      {}".format(item["how"]))
    out.append("")
    out.append('Say "finish the sift setup" to the agent in this repo, or leave it: '
               "`sift doctor` lists these until they are done.")
    return out


def user_message(items: List[Dict[str, Any]]) -> str:
    """One line for the person, who otherwise sees nothing at all.

    The agent gets told what to do at session start, but a person who does not
    know the instruction exists cannot ask for it, and an agent waits to be
    asked. So the person is told too, in one line, including the words that
    start the work.
    """
    if not items:
        return ""
    return "sift: this install is not finished — {}. Say \"finish the sift setup\" " \
           "when you want it done.".format("; ".join(i["what"] for i in items))


def agent_text(items: List[Dict[str, Any]]) -> str:
    """For the agent, once per session. It is the one that can do the work."""
    if not items:
        return ""
    body = ["This sift install is not finished. Unless the person asks for "
            "something else first:"]
    for i, item in enumerate(items, 1):
        body.append("{}. {}".format(i, item["agent"]))
    return "\n".join(body)


# ---------------------------------------------------------------------------
# An upgrade is available
# ---------------------------------------------------------------------------

def upgrade_item(ctx: Ctx, cfg: Config,
                 state: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """{id, what, how, agent, state} when this repo's runtime is behind the
    clone on this machine, else None.

    Never the clone's own path: this text reaches the agent, and an absolute
    path outside the repo is the one thing conventions.md says must not travel
    (W16). `sift version` prints the path, where a person asked for it.
    """
    if not cfg.get("setup", "check_upgrade", default=True):
        return None
    try:
        state = state if state is not None else upgrade_mod.status(ctx)
    except Exception:  # noqa: BLE001 - a notice never breaks the thing it annotates
        return None
    if state.get("state") not in upgrade_mod.STALE:
        return None

    if state["state"] == upgrade_mod.BEHIND:
        what = "sift {} is installed here; the clone on this machine has {}".format(
            state["installed"], state["clone_version"])
    else:
        what = ("the sift runtime here is not the one the clone would install "
                "(both say {})".format(state["installed"]))
    return {
        "id": "upgrade",
        "state": state["state"],
        "what": what,
        "how": "`sift update` in this repo — it rewrites {}/bin and the generated "
               "blocks, so give it its own commit".format(ctx.dir_name),
        "agent": "{}. Do not run it unprompted: say so, and run `sift update` from "
                 "the repo root only if the person asks for it. It replaces "
                 "{}/bin and rewrites the generated marker blocks, so it belongs "
                 "in a commit of its own.".format(what[0].upper() + what[1:], ctx.dir_name),
    }


def upgrade_lines(item: Optional[Dict[str, Any]]) -> List[str]:
    """For a person, at the end of a command."""
    if not item:
        return []
    return ["", "An upgrade is available: {}.".format(item["what"]), "  " + item["how"]]


def upgrade_user_message(item: Optional[Dict[str, Any]]) -> str:
    if not item:
        return ""
    return "sift: {} — run `sift update` when it suits you.".format(item["what"])


def upgrade_agent_text(item: Optional[Dict[str, Any]]) -> str:
    if not item:
        return ""
    return item["agent"]
