#!/usr/bin/env bash
# Dev topology verification: the Containerlab host and its four nodes (PID S10.6-S10.12, ADR 0063).
# Intent: clab/versions.yaml (the oracle), docs/image-manifest.md. State from at least two sides: the clab VM
# over SSH (containerlab, Docker, iptables, /proc), NetBox (the VM record; no clab device), the devices
# themselves over SSH from this Mac (verify/devcmd.py with CLAB_AUTOMATION_PASSWORD) and reachability from
# itential-dev and oob-gw. Read-only everywhere: no config is pushed, nothing is written to NetBox.
# Run by `make clab-dev` and `make verify-dev`; never part of `make verify`.
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env"; exit 1; }
set -a; . ./.env; set +a
: "${NETBOX_URL:?}" "${NETBOX_TOKEN:?}" "${CLAB_AUTOMATION_PASSWORD:?CLAB_AUTOMATION_PASSWORD missing in .env (ansible/playbooks/clab-dev.yml generates it)}"
PY=.venv/bin/python
O=clab/versions.yaml
oracle() { ${PY} -c "import sys,yaml;v=yaml.safe_load(open('$O'))
for k in sys.argv[1].split('.'): v=v[k]
print(v)" "$1"; }
CLAB_IP=$(oracle vm.ip)
DEV_IP=${DEV_IP:-10.100.0.65}
OOB_GW=${OOB_GW:-10.100.0.1}
TOPO=/opt/clab/dev/dev.clab.yml
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new"
ts=$(date -u +%Y%m%dT%H%M%SZ)
fail=0; pass=0
ok()  { echo "PASS  $1"; pass=$((pass+1)); }
bad() { echo "FAIL  $1"; fail=$((fail+1)); }
# ONLY="S10.9" runs a subset while iterating (every criterion still runs by default)
check() { local name=$1; shift; if [ -n "${ONLY:-}" ] && ! echo " ${ONLY} " | grep -q " ${name%% *} "; then echo "SKIP  $name"; return; fi; if "$@" >/tmp/verify12a.$$ 2>&1; then ok "$name"; sed 's/^/      /' /tmp/verify12a.$$; else bad "$name"; sed 's/^/      /' /tmp/verify12a.$$ | head -16; fi; }
nb()  { curl -s -m 20 -H "Authorization: Token ${NETBOX_TOKEN}" "$@"; }
# the clab devices have their own password, never the EVE-NG lab's AUTOMATION_PASSWORD
dev() { AUTOMATION_PASSWORD="$CLAB_AUTOMATION_PASSWORD" ${PY} verify/devcmd.py "$@"; }
WORK=$(mktemp -d); trap 'rm -rf "$WORK" /tmp/verify12a.$$' EXIT
mkdir -p verify/results; exec > >(tee "verify/results/${ts}-12a-clab-dev.log") 2>&1
echo "# test-12a-clab-dev ${ts}"
# node rows as "name kind mgmt_ip" for the shell loops
${PY} -c "import yaml;[print(n['name'], n['kind'], n['mgmt_ipv4']) for n in yaml.safe_load(open('$O'))['nodes']]" > "$WORK/nodes.txt"

# --- S10.6 clab VM matches the budget and NetBox; /dev/kvm; containerlab version == oracle == manifest ------------
c6() {
  local facts; facts=$($SSH "ubuntu@${CLAB_IP}" 'nproc; grep MemTotal /proc/meminfo | tr -s " " | cut -d" " -f2; lsblk -bdno SIZE /dev/sda; test -c /dev/kvm && echo kvm; containerlab version | sed -n "s/^ *version: *//p"') || { echo "ssh ubuntu@${CLAB_IP} failed"; return 1; }
  nb "${NETBOX_URL}/api/virtualization/virtual-machines/?name=$(oracle vm.name)" > "$WORK/nb-vm.json"
  : > "$WORK/nb-devices.json"
  while read -r name _ _; do nb "${NETBOX_URL}/api/dcim/devices/?name=${name}" >> "$WORK/nb-devices.json"; echo >> "$WORK/nb-devices.json"; done < "$WORK/nodes.txt"
  FACTS="$facts" WORK="$WORK" ${PY} - <<'PY'
import json, os, re, sys, yaml
o = yaml.safe_load(open("clab/versions.yaml"))
vm = o["vm"]
lines = os.environ["FACTS"].split("\n")
errs = []
cores, mem_kb, disk_b = int(lines[0]), int(lines[1]), int(lines[2])
if cores != vm["cores"]:
    errs.append(f"nproc {cores} != oracle {vm['cores']}")
# the kernel reserves some RAM, so MemTotal is a little under the allocation, never over it
if not (0.9 * vm["memory_mb"] <= mem_kb / 1024 <= vm["memory_mb"]):
    errs.append(f"MemTotal {mem_kb // 1024} MB is not within 90-100% of oracle {vm['memory_mb']} MB")
if disk_b != vm["disk_gb"] * 1024**3:
    errs.append(f"root disk {disk_b} bytes != oracle {vm['disk_gb']} GiB")
if "kvm" not in lines:
    errs.append("/dev/kvm missing: nested KVM is off (tofu/clab cpu type host)")
running = lines[-1].strip()
if running != str(o["containerlab"]):
    errs.append(f"containerlab {running!r} on the VM != oracle {o['containerlab']}")
row = re.search(r"^\| Containerlab \| ([0-9.]+) ", open("docs/image-manifest.md").read(), re.M)
if not row or row.group(1) != str(o["containerlab"]):
    errs.append(f"manifest containerlab row {row and row.group(1)} != oracle {o['containerlab']}")
W = os.environ["WORK"]
r = json.load(open(f"{W}/nb-vm.json")).get("results", [])
if len(r) != 1:
    errs.append(f"NetBox has {len(r)} virtual machines named {vm['name']}")
else:
    have = (r[0]["vcpus"] and int(float(r[0]["vcpus"])), r[0]["memory"], r[0]["disk"], (r[0]["status"] or {}).get("value"))
    if have != (vm["cores"], vm["memory_mb"], vm["disk_gb"], "active"):
        errs.append(f"NetBox VM (vcpus, memory, disk, status) {have} != oracle {(vm['cores'], vm['memory_mb'], vm['disk_gb'], 'active')}")
leaked = [d["name"] for line in open(f"{W}/nb-devices.json") if line.strip() for d in json.loads(line).get("results", [])]
if leaked:
    errs.append(f"clab devices registered in NetBox (they must never be, ADR 0063): {leaked}")
if errs:
    print("\n".join(errs)); sys.exit(1)
print(f"{cores} vCPU, {mem_kb // 1024} MB, {disk_b // 1024**3} GiB, /dev/kvm, containerlab {running} (= manifest); NetBox VM record matches; no clab device in NetBox")
PY
}
check "S10.6 clab VM equals the oracle and NetBox (8/16/60), /dev/kvm present, containerlab version equals oracle and manifest, clab devices absent from NetBox" c6

# --- S10.7 four nodes running with the oracle mgmt IPs; SSH as automation from this Mac and from itential-dev ----
c7() {
  $SSH "ubuntu@${CLAB_IP}" "sudo -n containerlab inspect -t ${TOPO} --format json" > "$WORK/inspect.json" || { echo "containerlab inspect failed on ${CLAB_IP}"; return 1; }
  WORK="$WORK" ${PY} - <<'PY' || return 1
import json, os, sys, yaml
o = yaml.safe_load(open("clab/versions.yaml"))
j = json.load(open(f"{os.environ['WORK']}/inspect.json"))
rows = [r for v in j.values() for r in v] if isinstance(j, dict) else j
have = {r["name"].split("-", 2)[-1]: (r["state"], r["ipv4_address"].split("/")[0]) for r in rows}
want = {n["name"]: ("running", n["mgmt_ipv4"]) for n in o["nodes"]}
if have != want:
    print(f"containerlab {have} != oracle {want}"); sys.exit(1)
print(f"containerlab: {len(have)} running at {sorted(v[1] for v in have.values())}")
PY
  local name kind ip out banner
  while read -r name kind ip; do
    out=$(dev "$ip" "show version" 2>&1) || { echo "${name} ${ip}: SSH as automation from this Mac failed: ${out:0:200}"; return 1; }
    case "$kind" in
      ceos) echo "$out" | grep -q "Arista" || { echo "${name}: show version is not EOS: ${out:0:200}"; return 1; } ;;
      *) echo "$out" | grep -q "^${name} uptime is" || { echo "${name}: show version does not name the router: ${out:0:200}"; return 1; } ;;
    esac
    # itential-dev holds no clab password of its own for the verify, so from there the proof is the SSH server's
    # banner over the routed path (TCP through clab's DOCKER-USER allowlist); the login itself is proved above
    banner=$($SSH "ubuntu@${DEV_IP}" "timeout 8 bash -c 'exec 3<>/dev/tcp/${ip}/22; head -c 7 <&3'" </dev/null 2>/dev/null)
    [ "$banner" = "SSH-2.0" ] || { echo "${name} ${ip}: no SSH banner from itential-dev (${DEV_IP}): '${banner}'"; return 1; }
    echo "${name} ${ip}: login from the Mac, SSH banner from itential-dev"
  done < "$WORK/nodes.txt"
}
check "S10.7 four nodes running with the oracle mgmt IPs, reachable over SSH as automation from this Mac and from itential-dev" c7

# --- S10.8 versions: cEOS equals oracle and manifest; C8000v 17.13.01a at licence level network-advantage ---------
c8() {
  local name kind ip
  while read -r name kind ip; do
    case "$kind" in
      ceos) dev "$ip" "show version | json" > "$WORK/ver-${name}.json" || { echo "${name}: SSH failed"; return 1; } ;;
      *) dev "$ip" "show version" > "$WORK/ver-${name}.txt" || { echo "${name}: SSH failed"; return 1; } ;;
    esac
  done < "$WORK/nodes.txt"
  WORK="$WORK" ${PY} - <<'PY'
import json, os, re, sys, yaml
W = os.environ["WORK"]
o = yaml.safe_load(open("clab/versions.yaml"))
manifest = open("docs/image-manifest.md").read()
errs = []
ceos, c8k = o["images"]["ceos"]["version"], o["images"]["c8000v"]["version"]
row = re.search(r"^\| `ceos` \|[^|]*\|([^|]*)\|", manifest, re.M)
if not row or ceos not in row.group(1):
    errs.append(f"manifest ceos row {row and row.group(1).strip()!r} does not name {ceos}")
if f"Running: {c8k}" not in manifest:
    errs.append(f"manifest c8000v section does not say 'Running: {c8k}'")
for n in o["nodes"]:
    if n["kind"] == "ceos":
        v = json.load(open(f"{W}/ver-{n['name']}.json")).get("version", "")
        if not (v == ceos or v.startswith(ceos + "-")):
            errs.append(f"{n['name']}: EOS {v!r} != oracle {ceos}")
    else:
        t = open(f"{W}/ver-{n['name']}.txt").read()
        if not re.search(rf"Version {re.escape(c8k)}\b", t):
            errs.append(f"{n['name']}: IOS XE version line is not {c8k}: {re.findall(r'Version \S+', t)[:1]}")
        level = re.search(r"(?m)^\s*License Level:\s*(\S*)\s*$", t)
        if not level or level.group(1) != "network-advantage":
            errs.append(f"{n['name']}: licence level {level and level.group(1)!r}, want network-advantage")
if errs:
    print("\n".join(errs)); sys.exit(1)
print(f"cEOS {ceos} on both switches (= manifest); C8000v {c8k} at network-advantage on both routers")
PY
}
check "S10.8 cEOS version equals the oracle and the manifest; C8000v runs 17.13.01a with licence level network-advantage" c8

# --- S10.9 OSPF: every adjacency the oracle implies is FULL, on both ends ---------------------------------------
c9() {
  local name kind ip
  while read -r name kind ip; do
    case "$kind" in
      ceos) dev "$ip" "show ip ospf neighbor | json" > "$WORK/ospf-${name}.json" || { echo "${name}: SSH failed"; return 1; } ;;
      *) dev "$ip" "show ip ospf neighbor" > "$WORK/ospf-${name}.txt" || { echo "${name}: SSH failed"; return 1; } ;;
    esac
  done < "$WORK/nodes.txt"
  WORK="$WORK" ${PY} - <<'PY'
import json, os, re, sys, yaml
W = os.environ["WORK"]
o = yaml.safe_load(open("clab/versions.yaml"))
lo = {n["name"]: n["loopback"] for n in o["nodes"]}
errs, total = [], 0
for n in o["nodes"]:
    # every link runs OSPF area 0 (routed /31s and the switch pair's Vlan99): the peers are the far ends' router-ids
    want = sorted(lo[l["b"]["node"]] for l in o["links"] if l["a"]["node"] == n["name"]) + \
           sorted(lo[l["a"]["node"]] for l in o["links"] if l["b"]["node"] == n["name"])
    if n["kind"] == "ceos":
        d = json.load(open(f"{W}/ospf-{n['name']}.json"))
        have = [e["routerId"] for v in d.get("vrfs", {}).values() for inst in v.get("instList", {}).values()
                for e in inst.get("ospfNeighborEntries", []) if e.get("adjacencyState", "").lower() == "full"]
    else:
        have = [m.group(1) for m in re.finditer(r"(?m)^(\d+\.\d+\.\d+\.\d+)\s+\d+\s+FULL/", open(f"{W}/ospf-{n['name']}.txt").read())]
    if sorted(have) != sorted(want):
        errs.append(f"{n['name']}: FULL neighbours {sorted(have)} != oracle {sorted(want)}")
    total += len(have)
if errs:
    print("\n".join(errs)); sys.exit(1)
print(f"{total // 2} adjacencies FULL on both ends ({len(o['links'])} links in the oracle)")
PY
}
check "S10.9 OSPF area 0: every adjacency on the oracle's links is FULL on both ends" c9

# --- S10.10 BGP: both iBGP pairs and eBGP rtr1-sw1 Established on both ends; the switches' /27s on rtr2 -----------
c10() {
  local name kind ip p
  while read -r name kind ip; do
    case "$kind" in
      ceos) dev "$ip" "show ip bgp summary | json" > "$WORK/bgp-${name}.json" || { echo "${name}: SSH failed"; return 1; } ;;
      *) dev "$ip" "show ip bgp summary" > "$WORK/bgp-${name}.txt" || { echo "${name}: SSH failed"; return 1; } ;;
    esac
  done < "$WORK/nodes.txt"
  local rtr2; rtr2=$(awk '$1=="clab-rtr2"{print $3}' "$WORK/nodes.txt")
  for p in $(${PY} -c "import yaml;[print(v['prefix']) for v in yaml.safe_load(open('$O'))['vlans']]"); do
    dev "$rtr2" "show ip route ${p%/*} $(${PY} -c "import ipaddress,sys;print(ipaddress.ip_network(sys.argv[1]).netmask)" "$p")" > "$WORK/route-${p%/*}.txt" || { echo "clab-rtr2: SSH failed"; return 1; }
  done
  WORK="$WORK" ${PY} - <<'PY'
import json, os, re, sys, yaml
W = os.environ["WORK"]
o = yaml.safe_load(open("clab/versions.yaml"))
errs = []
def established(n):
    if n["kind"] == "ceos":
        peers = json.load(open(f"{W}/bgp-{n['name']}.json")).get("vrfs", {}).get("default", {}).get("peers", {})
        return {ip for ip, p in peers.items() if p.get("peerState") == "Established"}
    # IOS XE summary: an established neighbour's last column is its prefix count; other states print the state name
    rx = re.compile(r"^(\d+\.\d+\.\d+\.\d+)\s+4(?:\s+\d+){6}\s+\S+\s+\d+\s*$")
    return {m.group(1) for line in open(f"{W}/bgp-{n['name']}.txt") for m in [rx.match(line.rstrip())] if m}
by = {n["name"]: n for n in o["nodes"]}
est = {name: established(n) for name, n in by.items()}
for s in o["routing"]["bgp"]:
    if s["b_ip"] not in est[s["a"]]:
        errs.append(f"{s['a']} -> {s['b']} ({s['type']}, {s['b_ip']}) not Established on {s['a']}")
    if s["a_ip"] not in est[s["b"]]:
        errs.append(f"{s['b']} -> {s['a']} ({s['type']}, {s['a_ip']}) not Established on {s['b']}")
for v in o["vlans"]:
    net, plen = v["prefix"].split("/")
    t = open(f"{W}/route-{net}.txt").read()
    if f"Routing entry for {v['prefix']}" not in t or '"bgp' not in t:
        errs.append(f"clab-rtr2 has no BGP route for {v['prefix']}: {t.strip()[:160]!r}")
if errs:
    print("\n".join(errs)); sys.exit(1)
print(f"{len(o['routing']['bgp'])} sessions Established on both ends; clab-rtr2 carries {[v['prefix'] for v in o['vlans']]} from BGP")
PY
}
check "S10.10 BGP: iBGP rtr1-rtr2 and sw1-sw2 over loopbacks and eBGP rtr1-sw1 Established on both ends; the switch VLAN /27s are on rtr2" c10

# --- S10.11 VLANs 10 and 20 active on both switches and carried on the switch-to-switch trunk -------------------
c11() {
  local name kind ip
  while read -r name kind ip; do
    [ "$kind" = ceos ] || continue
    dev "$ip" "show vlan | json" > "$WORK/vlan-${name}.json" || { echo "${name}: SSH failed"; return 1; }
    dev "$ip" "show interfaces switchport | json" > "$WORK/sp-${name}.json" || { echo "${name}: SSH failed"; return 1; }
  done < "$WORK/nodes.txt"
  WORK="$WORK" ${PY} - <<'PY'
import json, os, sys, yaml
W = os.environ["WORK"]
o = yaml.safe_load(open("clab/versions.yaml"))
def expand(allowed):
    out = set()
    for part in str(allowed).split(","):
        if "-" in part:
            a, b = part.split("-"); out |= set(range(int(a), int(b) + 1))
        elif part.strip().isdigit():
            out.add(int(part))
    return out
errs = []
trunk = [l for l in o["links"] if "trunk" in l]
for n in [n for n in o["nodes"] if n["kind"] == "ceos"]:
    vlans = json.load(open(f"{W}/vlan-{n['name']}.json")).get("vlans", {})
    for v in o["vlans"]:
        got = vlans.get(str(v["id"]), {})
        if got.get("status") != "active" or got.get("name") != v["name"]:
            errs.append(f"{n['name']}: VLAN {v['id']} {got.get('name')!r} {got.get('status')!r}, want {v['name']!r} active")
    ports = json.load(open(f"{W}/sp-{n['name']}.json")).get("switchports", {})
    for l in trunk:
        side = l["a"] if l["a"]["node"] == n["name"] else l["b"] if l["b"]["node"] == n["name"] else None
        if not side:
            continue
        info = ports.get(side["ifname"], {}).get("switchportInfo", {})
        missing = set(l["trunk"]) - expand(info.get("trunkAllowedVlans", ""))
        if info.get("mode") != "trunk" or missing:
            errs.append(f"{n['name']} {side['ifname']}: mode {info.get('mode')!r}, allowed {info.get('trunkAllowedVlans')!r}, missing {sorted(missing)}")
if errs:
    print("\n".join(errs)); sys.exit(1)
print(f"VLANs {[v['id'] for v in o['vlans']]} active on both switches; trunk carries {trunk[0]['trunk']}")
PY
}
check "S10.11 VLANs 10 and 20 active on both switches and allowed on the switch-to-switch trunk" c11

# --- S10.12 routes on oob-gw and itential-dev via clab; the DOCKER-USER allowlist holds; clab RAM under its line ---
c12() {
  local prefix; prefix=$(oracle mgmt.prefix)
  local host route
  for host in "$OOB_GW" "$DEV_IP"; do
    route=$($SSH "ubuntu@${host}" "ip -j route show ${prefix}" </dev/null) || { echo "ssh ubuntu@${host} failed"; return 1; }
    echo "$route" | ${PY} -c "import json,sys;r=json.load(sys.stdin);assert len(r)==1 and r[0].get('gateway')==sys.argv[1], r" "$CLAB_IP" || { echo "${host}: route to ${prefix} is not via ${CLAB_IP}: ${route}"; return 1; }
  done
  $SSH "ubuntu@${OOB_GW}" "test -s /etc/netplan/61-lab-routes.yaml && grep -q '${prefix}' /etc/netplan/61-lab-routes.yaml" </dev/null || { echo "oob-gw: route not persisted in /etc/netplan/61-lab-routes.yaml"; return 1; }
  # the allowlist, both sides: the rules on clab equal the oracle, and a source outside it (oob-gw's own OOB
  # address, which routes to the prefix) is really dropped
  $SSH "ubuntu@${CLAB_IP}" 'sudo -n iptables -w -S CLAB-DEV-MGMT; echo ===; sudo -n iptables -w -S DOCKER-USER; echo ===; systemctl is-enabled clab-dev-access; echo ===; free -m | awk "/^Mem:/{print \$2, \$3}"' > "$WORK/access.txt" </dev/null || { echo "ssh ubuntu@${CLAB_IP} failed"; return 1; }
  local probe; probe=$(awk '$1=="clab-sw1"{print $3}' "$WORK/nodes.txt")
  if $SSH "ubuntu@${OOB_GW}" "timeout 6 bash -c 'exec 3<>/dev/tcp/${probe}/22'" </dev/null 2>/dev/null; then
    echo "oob-gw (${OOB_GW}, not in access_allow) opened TCP 22 on ${probe}: the allowlist does not hold"; return 1
  fi
  WORK="$WORK" ${PY} - <<'PY'
import os, re, sys, yaml
o = yaml.safe_load(open("clab/versions.yaml"))
chain, docker_user, unit, mem = open(f"{os.environ['WORK']}/access.txt").read().split("===\n")
errs = []
allowed = [m.group(1) for m in re.finditer(r"(?m)^-A CLAB-DEV-MGMT -s (\S+) -j ACCEPT$", chain)]
if allowed != o["access_allow"]:
    errs.append(f"CLAB-DEV-MGMT accepts {allowed}, oracle access_allow {o['access_allow']}")
if not re.search(r"(?m)^-A CLAB-DEV-MGMT -j DROP$", chain):
    errs.append("CLAB-DEV-MGMT has no final DROP")
first = [l for l in docker_user.splitlines() if l.startswith("-A DOCKER-USER")][:1]
if first != [f"-A DOCKER-USER -o {o['mgmt']['bridge']} -j CLAB-DEV-MGMT"]:
    errs.append(f"first DOCKER-USER rule is {first}, want the jump for {o['mgmt']['bridge']}")
if unit.strip() != "enabled":
    errs.append(f"clab-dev-access.service is {unit.strip()!r}, the allowlist would not survive a reboot")
total, used = (int(x) for x in mem.split())
if used > 0.9 * o["vm"]["memory_mb"]:
    errs.append(f"clab uses {used} MB of its {o['vm']['memory_mb']} MB budget line (> 90%)")
if errs:
    print("\n".join(errs)); sys.exit(1)
print(f"allowlist {allowed} + DROP, jump first in DOCKER-USER, unit enabled; RAM {used}/{total} MB")
PY
  echo "oob-gw and itential-dev route ${prefix} via ${CLAB_IP} (oob-gw persisted); oob-gw itself is refused at the allowlist"
}
check "S10.12 oob-gw and itential-dev route the clab mgmt prefix via clab; the DOCKER-USER allowlist equals the oracle and drops other sources; clab RAM under its budget line" c12

echo
echo "passed=${pass} failed=${fail}"
[ "$fail" -eq 0 ]
