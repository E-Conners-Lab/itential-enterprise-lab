# 0053 — A production Itential environment in the HA2 shape, at lab sizes, from the Itential container images; the dev-stack retires after migration

- **Status:** accepted (PR #24, 2026-09-11) — the eleven-VM HA2 environment is built and S11.1-S11.8 pass
- **Date:** 2026-09-10
- **Related:** ADR 0020/0035 (the dev-stack on one VM), ADR 0038 (Gateway 5 cluster with etcd and the glibc runner), ADR 0049 (company key budget), ADR 0050 (phase order), ADR 0051/0052 (observability, the official dashboard), PID S11 (amendment 1.18)

## Context

Itential's deployment guide (read 2026-09-10) names four shapes. Two are "development environments only":
*all-in-one* (one server, Gateway impossible because Platform and Gateway need different Pythons) and
*minimal* (four single-instance servers). Two are production shapes: **HA2** (2 Platform servers, a 3-member
MongoDB replica set, 3 Redis servers with Sentinel, 1 Gateway server; "the recommended architecture for testing
environments and simple production environments") and **Active/Standby** (17 VMs across three data centres).
Every production statement repeats: each component on its own server, authentication on every component, TLS
between servers recommended, Platform and MongoDB "very sensitive to network latency", same data centre.

The sizes in the guide are enterprise sizes (Platform 16 cores / 64 GB, MongoDB 16 / 128 GB / 1 TB, Redis 8 /
32 GB, Gateway 16 / 32 GB): a faithful HA2 wants ~500 GB of RAM. The host has 314 GB, 197 GB allocated to running
VMs, 280 GB ceiling (docs/resource-budget.md): 83 GB free, of which the firewall track still reserves 40 GB.

Install paths in the guide: (1) the **Itential Deployer** (`itential.deployer` 4.2.0, Ansible, RPM installs on
RHEL/Rocky 8/9; MongoDB replica set, Redis + Sentinel with TLS and ACL users, Platform, Gateway 4.2-4.4; the
Platform and Gateway packages come from Itential Nexus/JFrog, "contact your Itential Sales representative"); (2)
**containers** from Itential's ECR (the credentials the owner already has through the company SSO; the guide's HA
example is "two Platform containers sharing external MongoDB and Redis"; "always pin to a specific maintenance
release tag"); (3) Kubernetes, validated on EKS and AKS only. The Deployer's optional nginx role documents the
load balancer: "listens on port 3443 and forwards requests to port 3000 on the servers ... using sticky sessions".
System requirements for Platform 6: MongoDB 6.0/7.0/8.0, Redis 7.0-7.4 (Valkey 7.2/8.x), Node 20, Python 3.11,
compatible Gateway 5.x; the OS list (RHEL/Rocky 8/9) applies to the RPM install.

Today the lab runs everything on VM 205 as the itential-dev-stack (ADR 0035): Platform 6.5.2, standalone
MongoDB 7.0.40 without auth, Redis 7.4.11 without auth, OpenLDAP, Gateway 5.5.2 + etcd + runner, MCP, Ollama.
Phases 5-7 created every workflow, Golden Config tree, MOP template, LCM model, form, Integration Model, agent,
inventory and host on it **from documents in this repo through plays that address the Platform by name**
(`itential.lab.internal`), which is what makes a rebuild elsewhere a replay.

## Decision

1. **Shape: HA2, one data centre, at lab sizes, every component on its own VM** (the guide's production
   rule), eleven Proxmox VMs from the Ubuntu 24.04 template in the service block of the IP plan
   (the nine Itential component servers, the load balancer in front of them, and the tools VM):

   | VM | Role | vCPU / RAM / disk | Address |
   |---|---|---|---|
   | `iap-lb` | nginx load balancer, TLS with the lab CA, sticky sessions to both Platform nodes (Deployer nginx guide) | 1 / 1 GB / 16 GB | 10.100.0.71, alias **`itential`** after cut-over |
   | `iap-01`, `iap-02` | Platform 6.5.2 container (ECR, maintenance tag pinned), adapters in a bind-mounted custom-services directory, one shared encryption key | 4 / 8 GB / 60 GB each | .72, .73 |
   | `mongo-01..03` | MongoDB 7.0.40 replica set `rs0` (container), keyfile + SCRAM users (`admin`, `itential`, `monitor`), TLS with lab-CA certificates | 2 / 4 GB / 40 GB each | .74-.76 |
   | `redis-01..03` | Redis 7.4.11 replication + one Sentinel each (containers), ACL users as the Deployer defines them (`itential`, `repluser`, `sentineluser`, `monitor`), TLS with lab-CA certificates | 1 / 2 GB / 16 GB each | .77-.79 |
   | `iag-01` | Gateway 5 cluster (gateway5 + etcd + runner) exactly as ADR 0038 runs it on VM 205 | 4 / 6 GB / 60 GB | .80 |
   | `tools-01` | MCP server and the in-lab Ollama (not Itential components; they leave the Platform VMs) | 4 / 8 GB / 40 GB | .81, aliases `mcp`, `ollama` |

   49 GB of RAM: within the 83 GB free; VM 205's 24 GB come back at retirement. Ubuntu rather than Rocky: the
   RHEL/Rocky requirement belongs to the RPM install; the container path is OS-neutral and the lab's Docker host
   automation (`itential-host.yml`) exists for Ubuntu. Rejected: the minimal shape (development-only per the
   guide), Rocky 9 (a second OS to automate for no Itential requirement), k3s for the Platform (validated on EKS
   and AKS only, and MongoDB/Redis would still need VMs).
2. **Install path: the Itential container images from ECR** for the Platform and Gateway (owner decision; no
   Itential Nexus account), the official MongoDB and Redis images for the databases, one Compose file per VM role
   rendered from `itential/versions.yaml` and repo documents. Every credential is generated once and persisted to
   `.env` before use (lab-build-lessons), then moved into Vault in the config/secrets phase. Rejected: the
   Deployer (no package credentials; its Gateway role stops at 4.4; it would also be a second automation stack).
3. **Authentication and TLS everywhere**, as the guide demands for production: MongoDB keyfile + SCRAM + TLS,
   Redis/Sentinel ACLs + TLS, the Platform to both over TLS (`ITENTIAL_MONGO_TLS_ENABLED`, Redis Sentinel
   settings), the load balancer with the lab-CA certificate for `itential.lab.internal`, Gateway 5 to the
   Platform over TLS as today. OpenLDAP (the AAA user Gateway Manager needs, memory 2026-09-07) moves to k3s as a
   Deployment now and is federated by Keycloak in the identity phase.
4. **Migration is a replay, then a cut-over, then retirement.** The Phase 5-7 plays that address the Platform by
   name run against production once `itential.lab.internal` points at the load balancer; `itential.yml` (the
   dev-stack) keeps working for VM 205 until it is deleted. NetBox, Zabbix, Prometheus and the observability
   documents follow the new VMs (the Platform exporter and the official dashboard's exporters move to the
   production VMs: node/process exporters on both Platform VMs, redis/mongodb exporters on their VMs, so the
   dashboard's replica-set panels light up). Retirement of VM 205 is a separate owner-approved step.
5. **This is Phase 8** (`phase-8/platform-ha2`, PID S11); config/secrets, identity, DDI, Containerlab and the
   firewall track move to 9-13. Vault (phase 9) then takes the database and Sentinel credentials.
6. **Acceptance (S11)**: eleven VMs match the budget; MongoDB `rs.status()` shows one PRIMARY and two SECONDARY over
   TLS with auth; Redis one master, two replicas, three Sentinels agreeing on the master; both Platform nodes
   `/health/server` healthy behind the load balancer; every phase 5-7 verify passes against production; drills
   (`VERIFY_DRILLS=1`): stopping one Platform node keeps the UI and a running job, stopping the MongoDB primary
   elects a new one within 30 s and the Platform keeps serving, stopping the Redis master fails over through
   Sentinel; the official dashboard's Redis and MongoDB rows show the replica sets.

## Retirement (done 2026-09-10)

The cut-over moved `itential.lab.internal` to `iap-lb` and `mcp.lab.internal` to `tools-01`, and VM 205 was
deleted with the owner's approval once S11.5 proved every asset it held exists on production. Order mattered:
NetBox and Zabbix first (a monitored host that vanishes alarms), then `tofu destroy` in `tofu/itential` - one
resource, `proxmox_virtual_environment_vm.itential` - then the address and the DNS name. Two plays gained the
pruning they never had: `netbox-vms.yml` deletes a virtual machine it no longer lists, and `observability.yml`
deletes a Zabbix host it stamped but the documents no longer name. Both fail safe, and both mean a future
retirement is a document change rather than a cleanup by hand.

## Consequences

- Eleven more VMs and 49 GB of RAM (`docs/resource-budget.md` section 2 amended); the firewall track's 40 GB stay
  reserved; VM 205's 24 GB return at retirement.
- Two Platform nodes need identical adapter installs: the play installs the NetBox and ServiceNow adapters into
  each node's custom-services directory from the pinned tags (the Kubernetes guide's persistent-volume method).
- The verifies gain the HA drills; `test-05/06/06b/06c` run unchanged against production (one full Anthropic run
  for `test-06`, ADR 0049).
- The lab exporter and the official dashboard keep working unchanged; the replica-set panels become meaningful.
- Not done here: Active/Standby (three data centres), Gateway 4, Itential's RPM path, MongoDB/Redis on RPMs.
