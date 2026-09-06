#!/usr/bin/env bash
# Phase 3 verification: k3s platform (PID S2 criteria 1-6). Read-only except for one scratch
# namespace named verify-<ts> that is created and deleted. Intent comes from
# k8s/platform/versions.yaml and topology/ipam.yaml; state from kubectl, the Proxmox API,
# the EVE-NG host and the workstation. The node-loss drill (S2.2) runs only with VERIFY_DRILLS=1.
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "missing .env"; exit 1; }
set -a; . ./.env; set +a
: "${PROXMOX_VE_ENDPOINT:?}" "${PROXMOX_VE_API_TOKEN:?}"
KUBECONFIG_PATH=${KUBECONFIG_PATH:-$HOME/.kube/lab-k3s.yaml}
export KUBECONFIG="$KUBECONFIG_PATH"
API_VIP=${API_VIP:-10.100.0.19}
TEST_LB_IP=${TEST_LB_IP:-10.100.0.43}
EVE_OOB=${EVE_OOB:-10.100.0.2}
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new"
ts=$(date -u +%Y%m%dT%H%M%SZ)
ns="verify-$(echo "$ts" | tr -d 'TZ' | tr '[:upper:]' '[:lower:]')"
fail=0; pass=0
ok()   { echo "PASS  $1"; pass=$((pass+1)); }
bad()  { echo "FAIL  $1"; fail=$((fail+1)); }
skip() { echo "SKIP  $1"; }
check(){ local name=$1; shift; if "$@" >/tmp/verify03.$$ 2>&1; then ok "$name"; else bad "$name"; sed 's/^/      /' /tmp/verify03.$$ | head -8; fi; }
pve()  { curl -sk -m 20 -H "Authorization: PVEAPIToken=${PROXMOX_VE_API_TOKEN}" "${PROXMOX_VE_ENDPOINT%/}/api2/json/$1"; }
cleanup(){
  # The default StorageClass retains PVs; a scratch namespace must not leave volumes behind.
  local pvs; pvs=$(kubectl get pv -o jsonpath="{range .items[?(@.spec.claimRef.namespace=='$ns')]}{.metadata.name} {end}" 2>/dev/null)
  kubectl delete ns "$ns" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  [ -n "$pvs" ] && ( sleep 20; kubectl delete pv $pvs --ignore-not-found >/dev/null 2>&1 ) &
  rm -f /tmp/verify03.$$
}
trap cleanup EXIT
echo "# test-03-platform ${ts}"
[ -f "$KUBECONFIG" ] || { bad "kubeconfig ${KUBECONFIG} missing (written by ansible/playbooks/k3s-cluster.yml)"; echo; echo "passed=${pass} failed=$((fail+5))"; exit 1; }

# --- S2.1 three Ready nodes via the VIP; cilium ok; hubble sees flows ------------------------
c1() {
  grep -q "https://${API_VIP}:6443" "$KUBECONFIG" || { echo "kubeconfig does not point at the VIP ${API_VIP}"; return 1; }
  local ready; ready=$(kubectl get nodes --no-headers 2>/dev/null | awk '$2=="Ready"' | wc -l | tr -d ' ')
  [ "$ready" = 3 ] || { echo "ready nodes: $ready"; kubectl get nodes; return 1; }
  local want; want=$(python3 -c "import yaml;print(yaml.safe_load(open('k8s/platform/versions.yaml'))['components']['k3s']['app_version'])")
  kubectl get nodes -o jsonpath='{.items[*].status.nodeInfo.kubeletVersion}' | tr ' ' '\n' | grep -vqx "$want" && { echo "kubelet version != $want"; return 1; }
  cilium status --wait --wait-duration 2m >/dev/null || { cilium status; return 1; }
  # Ask a Cilium agent directly (no hubble CLI needed on the workstation).
  local flows; flows=$(kubectl -n kube-system exec ds/cilium -c cilium-agent -- hubble observe --last 20 -o json 2>/dev/null | wc -l | tr -d ' ')
  [ "${flows:-0}" -gt 0 ] || { echo "hubble observed no flows"; return 1; }
}
check "S2.1 three Ready nodes at k3s $(python3 -c "import yaml;print(yaml.safe_load(open('k8s/platform/versions.yaml'))['components']['k3s']['app_version'])" 2>/dev/null) via ${API_VIP}; cilium status ok; hubble observes flows" c1

# --- S2.3 LoadBalancer Service gets the expected pool IP and answers from the Mac and from EVE-NG ----
c3() {
  kubectl create ns "$ns" >/dev/null || return 1
  kubectl -n "$ns" create deployment verify-web --image=docker.io/library/nginx:1.29.1-alpine --port=80 >/dev/null || return 1
  kubectl -n "$ns" expose deployment verify-web --type=LoadBalancer --port=80 >/dev/null || return 1
  kubectl -n "$ns" annotate svc verify-web "metallb.io/loadBalancerIPs=${TEST_LB_IP}" --overwrite >/dev/null || return 1
  kubectl -n "$ns" rollout status deploy/verify-web --timeout=120s >/dev/null || return 1
  local i lb=""; for i in $(seq 1 12); do lb=$(kubectl -n "$ns" get svc verify-web -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null); [ -n "$lb" ] && break; sleep 5; done
  [ "$lb" = "$TEST_LB_IP" ] || { echo "LB ip: '${lb}' expected ${TEST_LB_IP}"; return 1; }
  # MetalLB's first L2 announcement and neighbour refresh take a few seconds after the IP is assigned.
  local ok=0; for i in $(seq 1 8); do curl -s -m 5 -o /dev/null -w "%{http_code}" "http://${TEST_LB_IP}/" | grep -qx 200 && { ok=1; break; }; sleep 5; done
  [ $ok = 1 ] || { echo "workstation cannot reach ${TEST_LB_IP} after 40s"; return 1; }
  ok=0; for i in $(seq 1 6); do $SSH "root@${EVE_OOB}" "curl -s -m 5 -o /dev/null -w '%{http_code}' http://${TEST_LB_IP}/" | grep -qx 200 && { ok=1; break; }; sleep 5; done
  [ $ok = 1 ] || { echo "EVE-NG host cannot reach ${TEST_LB_IP} after 30s"; return 1; }
}
check "S2.3 LoadBalancer Service gets ${TEST_LB_IP} from MetalLB and answers from the Mac and from EVE-NG" c3

# --- S2.4 cert-manager issues from the lab CA; root CA exported --------------------------------
c4() {
  [ -s docs/lab-root-ca.crt ] || { echo "docs/lab-root-ca.crt missing"; return 1; }
  kubectl get clusterissuer lab-ca -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' | grep -qx True || { echo "ClusterIssuer lab-ca not Ready"; return 1; }
  kubectl -n "$ns" apply -f - >/dev/null <<YAML || return 1
apiVersion: cert-manager.io/v1
kind: Certificate
metadata: {name: verify-test, namespace: ${ns}}
spec:
  secretName: verify-test-tls
  dnsNames: [test.lab.internal]
  issuerRef: {name: lab-ca, kind: ClusterIssuer}
YAML
  kubectl -n "$ns" wait certificate/verify-test --for=condition=Ready --timeout=90s >/dev/null || return 1
  kubectl -n "$ns" get secret verify-test-tls -o jsonpath='{.data.tls\.crt}' | base64 -d > /tmp/verify03-leaf.$$.pem
  openssl verify -CAfile docs/lab-root-ca.crt -untrusted /tmp/verify03-leaf.$$.pem /tmp/verify03-leaf.$$.pem 2>&1 | grep -q ": OK" || { openssl verify -CAfile docs/lab-root-ca.crt -untrusted /tmp/verify03-leaf.$$.pem /tmp/verify03-leaf.$$.pem; rm -f /tmp/verify03-leaf.$$.pem; return 1; }
  rm -f /tmp/verify03-leaf.$$.pem
}
check "S2.4 ClusterIssuer lab-ca issues test.lab.internal chained to docs/lab-root-ca.crt" c4

# --- S2.5 CloudNativePG cluster ready; scheduled backup lands in the object store -------------
c5() {
  local cl; cl=$(kubectl -n cnpg-system get clusters.postgresql.cnpg.io -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  [ -n "$cl" ] || { echo "no CNPG Cluster in cnpg-system"; return 1; }
  kubectl -n cnpg-system get clusters.postgresql.cnpg.io "$cl" -o jsonpath='{.status.phase}' | grep -q "healthy" || { kubectl -n cnpg-system get clusters.postgresql.cnpg.io "$cl"; return 1; }
  kubectl -n cnpg-system get clusters.postgresql.cnpg.io "$cl" -o jsonpath='{.status.conditions[?(@.type=="ContinuousArchiving")].status}' | grep -qx True || { echo "WAL archiving not working"; return 1; }
  kubectl -n cnpg-system exec "${cl}-1" -c postgres -- pg_isready -q || { echo "pg_isready failed"; return 1; }
  # full resource name: 'backup' alone is ambiguous once Longhorn's backups CRD exists
  kubectl -n cnpg-system get backups.postgresql.cnpg.io -o jsonpath='{range .items[*]}{.metadata.name} {.status.phase}{"\n"}{end}' | grep -q " completed" || { echo "no completed Backup object"; kubectl -n cnpg-system get backups.postgresql.cnpg.io; return 1; }
}
check "S2.5 CNPG cluster healthy, pg_isready, at least one completed Backup in the object store" c5

# --- S2.6 allocation matches docs/resource-budget.md ---------------------------------------------
c6() {
  local node; node=$(pve nodes | python3 -c "import sys,json;print(json.load(sys.stdin)['data'][0]['node'])") || return 1
  local vms; vms=$(pve "nodes/${node}/qemu")
  python3 - "$vms" <<'PY' || return 1
import json, re, sys
vms = json.loads(sys.argv[1])["data"]
budget = open("docs/resource-budget.md").read()
errs = []
for name in ("k3s-01", "k3s-02", "k3s-03"):
    vm = next((v for v in vms if v["name"] == name), None)
    if not vm: errs.append(f"{name} missing on host"); continue
    m = re.search(r"\| `%s` \| \d+ \| [^|]+ \| (\d+) \| (\d+) \| (\d+) \|" % name, budget)
    if not m: errs.append(f"{name} not in budget"); continue
    vcpu, ram, disk = map(int, m.groups())
    if vm["cpus"] != vcpu or round(vm["maxmem"] / 2**30) != ram or round(vm["maxdisk"] / 2**30) != disk:
        errs.append(f"{name}: host {vm['cpus']}/{round(vm['maxmem']/2**30)}/{round(vm['maxdisk']/2**30)} vs budget {vcpu}/{ram}/{disk}")
if errs: print("\n".join(errs)); sys.exit(1)
print("k3s nodes match the budget")
PY
}
check "S2.6 k3s node vCPU/RAM/disk on the host equal docs/resource-budget.md" c6

# --- S2.2 node-loss drill (disruptive; VERIFY_DRILLS=1 only) -----------------------------------
if [ "${VERIFY_DRILLS:-0}" = 1 ]; then
  c2() {
    kubectl -n "$ns" apply -f - >/dev/null <<YAML || return 1
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: verify-pvc, namespace: ${ns}}
spec: {accessModes: [ReadWriteOnce], storageClassName: longhorn, resources: {requests: {storage: 1Gi}}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: verify-pvc-app, namespace: ${ns}}
spec:
  replicas: 1
  selector: {matchLabels: {app: verify-pvc-app}}
  template:
    metadata: {labels: {app: verify-pvc-app}}
    spec:
      containers: [{name: w, image: docker.io/library/alpine:3.24.1, command: [sh, -c, "echo ${ts} > /data/marker; sleep 3600"], volumeMounts: [{name: d, mountPath: /data}]}]
      volumes: [{name: d, persistentVolumeClaim: {claimName: verify-pvc}}]
YAML
    kubectl -n "$ns" rollout status deploy/verify-pvc-app --timeout=180s >/dev/null || return 1
    local node; node=$(kubectl -n "$ns" get pod -l app=verify-pvc-app -o jsonpath='{.items[0].spec.nodeName}')
    local vmid; vmid=$(pve "nodes/homelab/qemu" | python3 -c "import sys,json;print(next(v['vmid'] for v in json.load(sys.stdin)['data'] if v['name']=='$node'))")
    curl -sk -m 20 -H "Authorization: PVEAPIToken=${PROXMOX_VE_API_TOKEN}" -X POST "${PROXMOX_VE_ENDPOINT%/}/api2/json/nodes/homelab/qemu/${vmid}/status/stop" >/dev/null
    local i rc=1; for i in $(seq 1 60); do sleep 5; if kubectl -n "$ns" exec deploy/verify-pvc-app -- cat /data/marker 2>/dev/null | grep -qx "$ts"; then rc=0; break; fi; done
    curl -sk -m 20 -H "Authorization: PVEAPIToken=${PROXMOX_VE_API_TOKEN}" -X POST "${PROXMOX_VE_ENDPOINT%/}/api2/json/nodes/homelab/qemu/${vmid}/status/start" >/dev/null
    for i in $(seq 1 36); do sleep 5; kubectl get node "$node" --no-headers 2>/dev/null | awk '$2=="Ready"' | grep -q . && break; done
    [ $rc -eq 0 ] || echo "pod with Longhorn PVC did not come back with data within 5 min after stopping $node"
    return $rc
  }
  check "S2.2 drill: stop one node; pod with a Longhorn PVC returns with its data within 5 min" c2
else
  skip "S2.2 node-loss drill (set VERIFY_DRILLS=1 to run; disruptive)"
fi

echo; echo "passed=${pass} failed=${fail}"
[ "$fail" -eq 0 ]
