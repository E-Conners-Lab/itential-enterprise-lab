#!/usr/bin/env python3
"""Compare live VM sizing to docs/resource-budget.md section 2. Read-only.

Usage: budget_compare.py resources.json [docs/resource-budget.md]
resources.json = output of GET /cluster/resources?type=vm (the "data" list or the whole body).
Exit 1 if any VM differs, is missing from the budget, or is missing from Proxmox.
"""
import json, re, sys

res = json.load(open(sys.argv[1]))
res = res.get("data", res) if isinstance(res, dict) else res
live = {r["name"]: r for r in res if r.get("type") == "qemu" and not r.get("template")}

budget, in_sec = {}, False
for line in open(sys.argv[2] if len(sys.argv) > 2 else "docs/resource-budget.md"):
    if line.startswith("## "):
        in_sec = line.startswith("## 2.")
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    m = re.search(r"`([^`]+)`", cells[0]) if in_sec and len(cells) >= 6 else None
    if m and cells[3].isdigit():
        budget[m.group(1)] = (int(cells[3]), float(cells[4]), float(cells[5]))

bad = 0
for name in sorted(set(budget) | set(live)):
    b, l = budget.get(name), live.get(name)
    if not l:
        print(f"MISSING in Proxmox : {name} (budget {b})"); continue
    got = (int(l["maxcpu"]), round(l["maxmem"] / 2**30, 1), round(l["maxdisk"] / 2**30))
    if not b:
        print(f"NOT in budget      : {name} live={got}"); bad += 1
    elif (got[0], got[1], got[2]) != (b[0], b[1], b[2]):
        print(f"DIFF {name}: live vCPU/RAM/disk={got} budget={b}"); bad += 1
    else:
        print(f"ok   {name}")
sys.exit(1 if bad else 0)
