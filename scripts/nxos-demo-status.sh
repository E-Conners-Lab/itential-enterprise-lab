#!/usr/bin/env bash
# Flip a mock Nexus device's NetBox status, for the source-of-truth demo:
# NetBox decides which devices exist, and the Cisco NX-OS project's
# "Create & Update Inventory from NetBox" workflow makes the Platform agree.
#
# The workflow selects platform=cisco-nxos AND status=active, so flipping one
# device to offline and re-running it drops that node from the inventory.
#
#   scripts/nxos-demo-status.sh offline      # then re-run the workflow -> 1 node
#   scripts/nxos-demo-status.sh active       # then re-run the workflow -> 2 nodes
#   scripts/nxos-demo-status.sh show         # current state, changes nothing
#
# Only ever touches devices tagged nxos-mock.
set -euo pipefail

DEVICE="${DEVICE:-dc1-nxos02}"
WANT="${1:-show}"

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a; . "$root/.env"; set +a
URL="${NETBOX_URL%/}"

api() { curl -sf -H "Authorization: Token $1" "${@:2}"; }

show() {
  api "$NETBOX_DEV_RO_TOKEN" "$URL/api/dcim/devices/?platform=cisco-nxos&limit=50" \
  | python3 -c '
import json, sys
d = json.load(sys.stdin)
res = d["results"]
act = [r for r in res if r["status"]["value"] == "active"]
print("  NetBox   : %d cisco-nxos device(s)" % d["count"])
for r in sorted(res, key=lambda x: x["name"]):
    st = r["status"]["value"]
    print("    %s %-14s %s" % ("->" if st == "active" else "  ", r["name"], st))
print("  Selected : %d match platform=cisco-nxos + status=active" % len(act))
print("  Expect   : inventory nxos = %d node(s) once the workflow runs" % len(act))'
}

case "$WANT" in
  show) show ;;
  active|offline)
    id=$(api "$NETBOX_DEV_RO_TOKEN" "$URL/api/dcim/devices/?name=$DEVICE" \
         | python3 -c 'import json,sys; r=json.load(sys.stdin)["results"]; print(r[0]["id"] if r else "")')
    [ -n "$id" ] || { echo "device $DEVICE not found in NetBox" >&2; exit 1; }

    tags=$(api "$NETBOX_DEV_RO_TOKEN" "$URL/api/dcim/devices/$id/" \
           | python3 -c 'import json,sys; print(",".join(t["slug"] for t in json.load(sys.stdin)["tags"]))')
    case ",$tags," in *,nxos-mock,*) ;; *)
      echo "refusing: $DEVICE is not tagged nxos-mock (tags: ${tags:-none})" >&2; exit 1 ;;
    esac

    curl -sf -X PATCH -H "Authorization: Token $NETBOX_TOKEN" \
      -H "Content-Type: application/json" \
      -d "{\"status\":\"$WANT\"}" "$URL/api/dcim/devices/$id/" >/dev/null
    echo "set $DEVICE -> $WANT"
    echo
    show
    ;;
  *) echo "usage: $0 [show|active|offline]" >&2; exit 2 ;;
esac
