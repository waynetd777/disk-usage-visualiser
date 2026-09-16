<!-- sift:begin (generated — edits inside this block are overwritten on upgrade; add local rules below the end marker) -->
# Conventions

This folder holds what cannot be re-derived from the code about `disk-usage-visualiser`, and nothing else. No per-subsystem pages, and none are to be written: a model works out what the code does by reading it, and prose that restates the code costs tokens to carry and rots the moment the code moves.

## Before you read or change code

- `python3 _sift/bin/sift.py map <path>` — one line per file, with the symbol ranges to read instead of the whole file.
- `python3 _sift/bin/sift.py search "<terms>"` — over this folder. For code, use grep.
- `python3 _sift/bin/sift.py bug find "<error text>"` — check whether it is already known.

## After you change code

- Fixed a bug: `sift bug add --error … --root-cause … --fix … --files …`.
- Made a decision worth remembering: `sift decide "<title>"`, then fill in the block it appends.
- A file's one-line description is now wrong: `sift describe --set <path> "…"`.
- Stage `_sift/` in the same commit as the code. There is nothing else to maintain.

## What never goes in

This folder is committed and your colleagues read it, so confidential material never goes in it. Never write here:

- Secrets of any kind — keys, tokens, connection strings, passwords.
- Absolute paths outside the repo (`/Users/...`, `~/Downloads/...`). Repo-relative or nothing.
- People in an HR, performance or conduct context.
- Money attached to a person or a supplier: salaries, day rates, rate cards, bonuses.
- Anything from a personal vault, notes app or private life.

`local/` is gitignored and exists for exactly this.

**If an entry is a mix, split it.** The reusable, technical half goes here; the specifics go in `local/`. "The import job is slow because the vendor throttles us above 50 requests a second" belongs here; the same note naming the account manager and what we pay them belongs in `local/`.

**When in doubt, use `local/`.** A useful note being private costs one person's access. The reverse costs a history rewrite and a force-push that breaks everyone's clone — and, for anything about a person, a disclosure you cannot take back.

Lint is a backstop, not the decision: W15 blocks secrets, W16 out-of-repo paths, W19 a `local/` file that has become tracked, and W20 warns on HR and compensation vocabulary. No pattern can tell whether a sentence about a colleague should be published; you can.

## What is in here

`decisions.jsonl` (dated records of why, superseded rather than deleted), `journal.jsonl` (bugs and sessions), `files.jsonl` (one line per source file). All three are written only through the commands above, never by hand — `sift decisions` renders the decisions as markdown to read. `bin/` is the committed runtime, so a fresh clone works with nothing installed; its comments rot like any other prose, so trust the docs over them. `.cache/` and `local/` are gitignored.

One sentence per line: git merges line-wise, so unrelated edits to a paragraph do not conflict. The three append-only files are JSONL on `merge=union`, which is safe because one record is one line. `conventions.md` is the only file edited in place, so it is the only one that conflicts — and a conflict in it is a real disagreement, so resolve it like source.
<!-- sift:end -->
