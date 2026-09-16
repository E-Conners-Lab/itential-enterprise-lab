# 02 — k3s and platform services

Three nodes, an API VIP that does not depend on the CNI, LoadBalancer addresses on the OOB network, storage
that survives losing a node, a certificate authority for the lab, and Postgres — rebuildable, and deliberately not backed up (ADR 0064).

This is the chapter that makes every later chapter's "it just has an address and a certificate" true.
Chapter 07's whole observability stack runs here, and chapter 06 borrows the CA.

---

## Before you start

### What must already be true

- Chapter 01 is green: `verify/test-02-oob.sh` passes 11/11. In particular the OOB gateway is up, because
  the k3s nodes are **single-homed** — their only route out is `10.100.0.1`.
- NetBox holds the three node addresses (`10.100.0.16`, `.17`, `.18`) and the API VIP (`10.100.0.19`). The
  seed play in chapter 01 put them there.
- The Ubuntu template (VM 9000) exists. `tofu/platform` clones it.
- `helm` **3** is on the workstation. Not 4 — see troubleshooting.

### The shape

| | Choice | Why |
|---|---|---|
| Cluster | Three k3s servers, embedded etcd, all schedulable | Three gives etcd a quorum; the lab is not big enough to justify separate workers |
| CNI | Cilium 1.20.1, kube-proxy replacement, Hubble on | Flannel, kube-proxy and ServiceLB are disabled at install time |
| API VIP | kube-vip 1.2.3 as a **static pod**, ARP mode, control-plane only | It must come up before the CNI does, so it cannot depend on a ClusterIP. `svc_enable=false` — MetalLB owns every LoadBalancer |
| LoadBalancer | MetalLB 0.16.1, L2, pool `10.100.0.32-10.100.0.63` | Straight out of the chapter 01 address plan |
| Ingress | The Traefik that k3s **bundles**, configured through a `HelmChartConfig` | `ingress-nginx` was archived; a separately installed Traefik would fight the bundled one |
| Storage | Longhorn 1.12.1, two replicas by default | Two replicas out of three nodes is what makes the node-loss drill pass |
| Certificates | cert-manager 1.21.1 with a self-signed root, `ClusterIssuer` `lab-ca` | `${LAB_DOMAIN}` can never get a public certificate ([ADR 0005](../adr/0005-lab-dns-domain.md)) |
| Object store | Garage 2.4.0, single-node StatefulSet on Longhorn | MinIO was archived in 2026; Garage is small and S3-compatible |
| Database | CloudNativePG 1.30.0 with PostgreSQL 18, **not backed up** — no WAL archiving, no scheduled backups | The lab's databases are rebuilt from the repo; archiving to Garage once filled Garage and then the database volume ([ADR 0064](../adr/0064-lab-databases-are-rebuildable-not-backed-up.md)). The Barman Cloud plugin stays installed, unused |

Decisions and the discarded alternatives: [ADR 0021](../adr/0021-k3s-platform-stack.md) and
[ADR 0031](../adr/0031-garage-object-store-kube-vip-barman-plugin.md).

### The version oracle

`k8s/platform/versions.yaml` is the single place every version and address comes from — the plays read it,
and `tests/test_platform_versions.py` fails CI if it drifts from `docs/image-manifest.md` or
`topology/ipam.yaml`. **Change a version there, not in a play.** If you are substituting your own versions,
that file is the only file to edit.

### Sizing

Three nodes at 4 vCPU / 12 GB / 100 GB each, declared in `tofu/platform/variables.tf` and cross-checked
against `docs/resource-budget.md` by criterion S2.6 on every verify run. Smaller works — Longhorn is the
part that will complain first, and it wants headroom for replicas and snapshots.

---

## The commands, in order

```
make plan-platform          # read-only: three VMs to add
make phase-platform
```

Four steps:

| | Step | What it does |
|---|---|---|
| 1 | `ansible/playbooks/netbox-vms.yml` | Registers the three nodes as virtual machines in NetBox and attaches their reserved addresses. NetBox first, always — it is the inventory source the next step reads |
| 2 | `tofu apply` in `tofu/platform` | Clones the template three times, `vmbr1` only, sizes from the budget |
| 3 | `ansible/playbooks/k3s-cluster.yml` | Runs against `-i inventory/netbox.yml`. Node prep (no swap, kernel modules, iscsid for Longhorn, time from the gateway), the kube-vip static pod, `cluster-init` on the first node and joins on the other two, then writes a workstation kubeconfig pointed at the VIP |
| 4 | `ansible/playbooks/k8s-platform.yml` | Runs on the workstation with `helm` and `kubectl`: Cilium first, then MetalLB, Longhorn, cert-manager and the lab CA, the Traefik configuration, Garage, the Barman plugin, removal of any leftover backup objects, and the CNPG cluster |

The inventory group comes from NetBox, so if step 1 did not run — or a node is not tagged — step 3 has
nothing to configure and succeeds with zero hosts. Read the play recap.

Then, once, on each workstation (manual step 6c):

```
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain docs/lab-root-ca.crt
```

`k8s-platform.yml` exports the root certificate to `docs/lab-root-ca.crt` — public material only, and it is
committed on purpose so a reader can see what they are trusting.

---

## What "done" looks like

Around 20–30 minutes on the tested hardware, most of it Helm charts pulling images over the NAT and
Longhorn's DaemonSet settling on three nodes. The steps that take real time are Cilium (nodes are
`NotReady` until it is up, which is expected and not a fault), and Longhorn.

```
kubectl --kubeconfig ~/.kube/lab-k3s.yaml get nodes
```

Three nodes `Ready` at `v1.36.4+k3s1`, reached through `10.100.0.19` — not through any node's own address.
`cilium status` is `ok` and `hubble observe` returns flows. Traefik holds `10.100.0.32`. A `LoadBalancer`
Service gets an address from the pool and answers **from your workstation and from EVE-NG** — both sides of
the OOB network. `kubectl -n cnpg-system get cluster` shows the platform database healthy, and
`kubectl get scheduledbackups.postgresql.cnpg.io,objectstores.barmancloud.cnpg.io -A` returns nothing.

The kubeconfig lands at `~/.kube/lab-k3s.yaml` with the context named `lab-k3s`. It is never committed.

---

## Verification

`verify/test-03-platform.sh` is this chapter's script; `make verify` runs it along with every other phase's.

```
verify/test-03-platform.sh
```

| Criterion | What a PASS means |
|---|---|
| S2.1 | Three `Ready` nodes at the pinned k3s version, reached via the VIP; `cilium status` ok; Hubble observes flows |
| S2.3 | A `LoadBalancer` Service gets its expected pool address from MetalLB and answers **from the workstation and from EVE-NG** |
| S2.4 | `ClusterIssuer` `lab-ca` issues a certificate for a `${LAB_DOMAIN}` name that chains to `docs/lab-root-ca.crt` |
| S2.5 | The CNPG cluster is healthy and `pg_isready` answers, and nothing backs a lab database up: no `ScheduledBackup`, `ObjectStore`, plugin reference, Barman sidecar or WAL backlog in any namespace |
| S2.6 | The nodes' vCPU, RAM and disk **on the hypervisor** equal `docs/resource-budget.md` |
| S2.2 | The node-loss drill. Skipped by default because it is disruptive |

The drill is the one worth running once, deliberately:

```
VERIFY_DRILLS=1 verify/test-03-platform.sh
```

It creates a Longhorn PVC and a pod that writes a marker into it, finds which node the pod landed on,
**stops that VM through the hypervisor API**, and waits for the pod to come back somewhere else with the
marker intact — within five minutes. Then it starts the node again and waits for it to be `Ready`. A pass
here is the difference between "Longhorn is installed" and "Longhorn works".

---

## Troubleshooting

**Nodes are `NotReady` and stay that way.** Expected between step 3 and step 4 — there is no CNI yet. If
they are still `NotReady` after `k8s-platform.yml`, look at the Cilium pods first; everything else in that
play runs after Cilium and will queue behind it.

**`helm` fails with an unexpected error from `kubernetes.core`.** The Ansible `kubernetes.core` collection
drives the `helm` binary, and it does not work with Helm 4. The repo pins Helm 3 and `make bootstrap`
refuses to continue without it. Set `HELM3_BIN` in `.env` if yours is not at the Homebrew path.

**The API VIP does not answer, or answers only from one node.** kube-vip runs as a static pod, authenticates
with the node's *local* k3s kubeconfig, and needs to resolve the name `kubernetes` before the cluster is
serving DNS. The node prep writes that name into the node's hosts file pointing at its own API server. If
you rebuild a node by hand, that entry has to come back, or kube-vip starts and does nothing — no error,
just no VIP.

**A MetalLB address answers from inside the cluster but not from EVE-NG.** This is the chapter 01
return-path amendment showing up. A dual-homed host — EVE-NG and NetBox are the two by design — sends
traffic for `10.100.0.0/24` down its return-path table, and that table needs the **on-link route for the
OOB `/24`** or the packet goes to the gateway and comes back from a direction the host does not expect.
The gateway also permits OOB-to-OOB forwarding so that a misrouted host degrades into a hairpin instead of
a silent drop. If you have added a dual-homed host of your own, it needs the same two things.

**Longhorn volumes will not schedule, or every replica lands on one node.** Over-provisioning. Garage was
originally asked for 50 GiB, which filled two nodes to 100 % and forced every later replica onto the third
— which then looked like a Longhorn scheduling bug. Garage is now 20 GiB and Longhorn's over-provisioning
is 200 %. If you resize a volume, check `Longhorn → Node` for the per-node scheduled figure before blaming
the scheduler.

**A pod with a Longhorn PVC never comes back after a node dies.** Two settings make the drill pass and both
are easy to lose: Longhorn's node-down pod deletion policy, and k3s's 30-second unreachable tolerations. By
default Kubernetes waits five minutes before evicting, and the volume stays attached to a node that is
gone. If your drill times out at exactly five minutes, that is what you are looking at.

**A CNPG database volume fills up and postgres crash-loops with "Not enough disk space".** Look for WAL that
cannot be archived before looking at the data. PostgreSQL keeps every WAL segment until its archiver succeeds,
so when this lab archived to Garage, a full Garage (fourteen days of base backups from two clusters in
20 GiB) made every archive attempt fail with "No space left on device", the WAL piled up on the database's own
volume, and Zabbix was down for fifteen hours with nothing alerting. That is why the lab's CNPG databases are
not backed up ([ADR 0064](../adr/0064-lab-databases-are-rebuildable-not-backed-up.md)). If you add archiving
back for a database that needs it, size the object store against the retention first and alert on its free
space. And removing a document from a manifest does not remove the live object: the plays delete the
`ScheduledBackup` and `ObjectStore` and JSON-patch `/spec/plugins` out of the `Cluster`, because the merge
patch that `state: present` sends leaves any key it omits in place.

**Garage's secrets change on every run.** They should not — the play generates them once and keeps them
only in the cluster. Nothing reads them since the CNPG backups went (ADR 0064), but anything that uses Garage
later will need the same secret to survive, so do not delete the `garage` namespace to "reset" it.

**Every lab HTTPS page says "Not Secure".** The lab CA is imported but not *trusted*. macOS treats those as
two separate things and a root with no trust setting signs nothing a browser accepts. This is manual step
6c and it went unnoticed here for five phases, because every internal check passes `--cacert` explicitly
and so never exercises the system trust store. To prove the fix, run `curl https://<a lab name>` **without**
`--cacert` and expect a 200.

**A pod fails with `exec format error` on one node and runs fine on the others.** Not an architecture
mismatch, if the nodes are identical: containerd's *unpacked snapshot* for that layer is corrupt. It
survives everything you would reach for first, because none of these touch snapshots — `ctr images rm`
and a re-pull reuse them (watch the pull move 30 KB instead of megabytes, the tell that nothing was
re-fetched), `ctr content prune` leaves them, a `k3s` restart leaves them, and even an explicit
`--platform linux/amd64` pull *and* run still fails. The blobs themselves verify against their digests,
which is what rules out content corruption.

The fix is to delete that node's containerd state and let it rebuild:

```
systemctl stop k3s && rm -rf /var/lib/rancher/k3s/agent/containerd && systemctl start k3s
```

Longhorn data is in `/var/lib/longhorn`, untouched by this. **Then reboot the node** — and that step is
not optional. Stopping k3s does **not** kill its containers: their `containerd-shim` processes keep
running and holding ports, so the replacement pods come back with `errno=98` (address in use) and
`failed to start TCP listener`. This lab found 39 shims still alive five days after they started. The
sequence is stop → delete → **reboot**, and the volumes rebuild themselves afterwards.

**A CNPG check fails right after a node restart and passes later with no intervention.** S2.5 is
timing-sensitive while the operator settles; it failed twice here and passed on the third run. Re-run
before investigating.

**S2.6 fails after you resized a node.** The size on the hypervisor and `docs/resource-budget.md` disagree.
That check exists because a VM quietly grown to fix a problem is how a lab runs out of RAM three phases
later. Change `tofu/platform/variables.tf` and the budget together, in one commit.

**`tofu` wants to destroy and recreate a node.** Check whether the template changed. A clone's disk cannot
be smaller than its template's, and a new template serial is a different template. Look at the plan output
before approving anything in this directory — these three VMs hold the cluster's etcd.

---

## Tested versions

| Component | Version |
|---|---|
| k3s | `v1.36.4+k3s1` (flannel, kube-proxy, ServiceLB disabled; bundled Traefik kept) |
| Cilium | 1.20.1, kube-proxy replacement, Hubble enabled |
| kube-vip | `v1.2.3`, static pod, ARP, `svc_enable=false` |
| MetalLB | 0.16.1 (chart), `v0.16.1` |
| Longhorn | 1.12.1, default 2 replicas |
| cert-manager | `v1.21.1` |
| Traefik | `v3.7.8`, as bundled by k3s |
| CloudNativePG | operator 1.30.0, PostgreSQL 18.6 operand |
| Barman Cloud plugin | `v0.15.0` (installed, unused since ADR 0064) |
| Garage | `v2.4.0`, 20 GiB data on Longhorn (empty since ADR 0064) |
| Helm | 3 (**not** 4) |
| Node size | 4 vCPU / 12 GB / 100 GB × 3 |

Recorded fallback: k3s `v1.35.8+k3s1`.

---

**Previous:** [01 — Out-of-band network and addressing](01-oob-network-and-addressing.md) · **Next:** [03 — NetBox as the source of truth](03-netbox-source-of-truth.md)
