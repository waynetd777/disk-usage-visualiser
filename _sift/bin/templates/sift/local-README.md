# `local/` — private by construction

Gitignored. Nothing in here is committed, pushed, or visible to anyone else. It exists so the sift directory never has to choose between being honest and being safe to publish.

Write here:

- People in an HR, performance or conduct context.
- Money attached to a person or a supplier — salaries, day rates, rate cards, bonuses.
- Anything from a personal vault, notes app or private life.
- Absolute paths on your machine, and the contents of anything outside the repo.
- A half-formed thought you are not ready to put a colleague's name on.

**If an entry is a mix, split it:** the reusable, technical half goes in the sift directory, the specifics stay here.

**When in doubt, put it here.** Moving a note out later costs nothing. Moving it the other way costs a history rewrite and a force-push — and for anything about a person, a disclosure you cannot take back.

`sift doctor` verifies this folder is still gitignored and that nothing in it has become tracked; lint W19 blocks the commit if it ever does.
