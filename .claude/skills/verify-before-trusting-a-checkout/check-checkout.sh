#!/usr/bin/env bash
# Read-only. Usage: check-checkout.sh [dir ...]   (default: the known lab checkouts)
dirs=("$@"); [ ${#dirs[@]} -eq 0 ] && dirs=(~/itential-enterprise-lab ~/PycharmProjects/itential-enterprise-lab)
for d in "${dirs[@]}"; do
  [ -d "$d/.git" ] || { echo "== $d: not a checkout"; continue; }
  echo "== $d"
  git -C "$d" fetch --quiet origin || echo "  fetch FAILED"
  echo "  branch: $(git -C "$d" rev-parse --abbrev-ref HEAD)"
  read -r ahead behind < <(git -C "$d" rev-list --left-right --count HEAD...origin/main)
  echo "  ahead $ahead / behind $behind vs origin/main"
  echo "  uncommitted entries: $(git -C "$d" status --porcelain | wc -l | tr -d ' ')"
  echo "  .env here: $([ -f "$d/.env" ] && echo yes || echo no)"
done
