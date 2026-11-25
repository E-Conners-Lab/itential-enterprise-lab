---
name: verify-before-trusting-a-checkout
description: Use before reading code, writing files, or sourcing a .env in the lab repo when more than one checkout exists (~/itential-enterprise-lab and ~/PycharmProjects/itential-enterprise-lab), when code looks stale or missing something the docs mention, when a .env "in the project root" isn't found or has the wrong values, or when git shows the branch behind origin/main.
---

# Verify which checkout you are in

Two checkouts of the same repo existed; one was 179 commits behind origin/main and held uncommitted work. Reading
code or writing files there gave wrong answers. A `.env` saved "in the project root" landed in the other checkout.

## Checks first (read-only)

```bash
.claude/skills/verify-before-trusting-a-checkout/check-checkout.sh
pwd; git rev-parse --show-toplevel
```

It fetches, then prints branch, ahead/behind against `origin/main`, the count of uncommitted entries, and whether
the checkout holds a `.env`. Read the numbers before touching anything.

## Procedure

1. Work only in a checkout that is 0 behind, or bring it up to date safely:
   - Clean tree: `git merge --ff-only origin/main`.
   - Dirty tree: `git stash push -u -m "before-ff $(date +%F)"`, then `git merge --ff-only origin/main`, then tell
     the user the stash exists. Do not `stash pop` without asking if the pop could conflict.
2. If the other checkout is the one with uncommitted work, report it (path, entry count, `git status --short`) and
   let the user decide. Do not move files between checkouts on your own.
3. Before sourcing a `.env`: confirm which directory holds it (`ls -la <dir>/.env`, never `cat` it) and that it is
   the checkout you are working in. If it sits in the other one, say so and ask where it should live.
4. State the checkout path you chose in your next message so the user can correct you.

## Don't

- Don't `git reset --hard`, `git checkout -- .`, `git clean`, or `git pull --rebase` on a dirty tree. Stash and
  `--ff-only`, or stop and ask.
- Don't write a file until you know which checkout `pwd` is in.
- Don't print a `.env`'s contents; check existence and variable names (`cut -d= -f1`) at most.
- Don't assume the checkout you were last in is the current one.

## Verify

Re-run the script: the chosen checkout shows `behind 0`, the intended uncommitted count, and `git stash list`
shows only what you reported.
