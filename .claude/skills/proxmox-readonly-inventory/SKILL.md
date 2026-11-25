---
name: proxmox-readonly-inventory
description: Use to list or audit the lab's Proxmox VMs without changing anything, to compare VM vCPU/RAM/disk to docs/resource-budget.md, or when ssh to the Proxmox host prints "Host key verification failed" (especially under BatchMode), "REMOTE HOST IDENTIFICATION HAS CHANGED", or you are tempted to use StrictHostKeyChecking=no.
---

# Proxmox read-only inventory

Node is `homelab`. Everything here is GET-only. Prefer the API; use ssh only for what the API cannot show.

## Checks first

- Is the credential present? `[ -n "$PVE_TOKEN" ] && echo set` (a `user@realm!tokenid=secret` string; never echo it).
- Host and token variable names: check `.env.example` or `docs/discovery.md` (host `homelab`); do not invent them.

## Preferred: API with a PVEAPIToken header

```bash
H="Authorization: PVEAPIToken=$PVE_TOKEN"
curl -sk -H "$H" "https://$PVE_HOST:8006/api2/json/cluster/resources?type=vm" -o "$SCRATCH/res.json"
curl -sk -H "$H" "https://$PVE_HOST:8006/api2/json/nodes/homelab/qemu/<id>/config"   # one VM's config
```

Only `-X GET` (the default). No POST/PUT/DELETE, no `qm`/`pct` that change state. `-k` is for the lab's
self-signed certificate on a trusted LAN; say so when you use it.

## Compare to the budget with a script, not by eye

```bash
python3 .claude/skills/proxmox-readonly-inventory/budget_compare.py "$SCRATCH/res.json" docs/resource-budget.md
```

Prints `ok`, `DIFF` (live vs budget vCPU/RAM GB/disk GB), `NOT in budget`, or `MISSING in Proxmox`; exit 1 on any
difference. Budget rows with no backtick name (e.g. a bare VM id) are skipped, so eyeball those. Report
differences; amending the budget is a separate, user-approved change (the doc says to amend it in the same PR as a
sizing change).

## ssh: "Host key verification failed"

Under BatchMode this usually means an UNKNOWN host (no known_hosts entry), not a mismatch.

1. Read-only look: `ssh-keygen -F <host>` (empty = unknown). A real mismatch says "REMOTE HOST IDENTIFICATION HAS
   CHANGED" and needs a different conversation: stop and tell the user.
2. Get the fingerprint without trusting it: `ssh-keyscan -t ed25519 <host> | ssh-keygen -lf -`
3. Show the user the fingerprint; ask them to compare with the host console (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`)
   or a known-good record. Add it only on their say-so: `ssh-keyscan -t ed25519 <host> >> ~/.ssh/known_hosts`.
4. If a stale entry exists and the user confirms the host was rebuilt: `ssh-keygen -R <host>`, then repeat 2-3.

## Don't

- Don't use `StrictHostKeyChecking=no` or `UserKnownHostsFile=/dev/null` to make the error go away.
- Don't trust a key because the scan succeeded.
- Don't print the token, or put it in a URL or a file in the repo.
- Don't change VM state, config, or snapshots from this skill.

## Verify

`budget_compare.py` exits 0, or every difference is listed for the user. Any ssh command now works without
`-o StrictHostKeyChecking` overrides.
