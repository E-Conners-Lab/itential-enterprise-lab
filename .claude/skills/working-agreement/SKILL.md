---
name: working-agreement
description: Use at the start of any work on the Itential lab, a shared Itential instance, Proxmox, or ServiceNow, and whenever you are about to write, patch, import, delete, or run something that changes state, when a hypothesis you stated turns out wrong, or when the same approach has failed more than once.
---

# Working agreement

## Read first, write second

1. Investigate read-only: GETs, listings, `git status`, `--dry-run`. Say what you found.
2. Before any write to a shared instance (Platform, Itential Cloud, Proxmox, ServiceNow, Vault, NetBox), STOP
   and ask. The request must say:
   - exactly what will change (object, field, before -> after),
   - the rollback (the saved backup, the inverse call),
   - what you will check afterwards.
3. Back up first when the write replaces data (a saved copy of the list, the export, the config).
4. Approval covers that change only. A new write, a new instance, or a wider scope needs asking again.

Local work in the checkout (edits, new files) is fine without asking, after `verify-before-trusting-a-checkout`.
Commits, pushes, and PRs only when the user says so.

## Correct the record

When a hypothesis proves wrong, say so plainly in the next message ("I was wrong: I said X; testing showed Y"),
fix any file, note, or memory that recorded the wrong claim, and say what changed. Don't quietly pivot, and don't
keep a wrong claim in a doc because it is awkward to retract.

## Stop after repeated failure

After 2-3 failed attempts at the same approach, stop. Report: what you tried, the exact errors, what you believe
the cause is (and how sure), and one or two options. Don't try variants of the same thing, don't escalate to
riskier tools (bypassing a filter, another account, `-o StrictHostKeyChecking=no`) to get past a block. A denied
or filtered action is a boundary, not a puzzle.

## Secrets

Never put secrets, tokens, or passwords in files, commands you echo, or replies. Check they exist; don't print them.
Never ask the user for credentials: have them log in themselves.

## Verify

Final reports separate what you verified (with the evidence) from what you inferred, and say what you did not do.
