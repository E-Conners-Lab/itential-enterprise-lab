# 08 — Production HA and migration

Rebuild the Platform in Itential's HA2 shape across eleven VMs, replay every asset chapters 05–07 built
onto it, move the service name, and retire the single-VM dev stack.

This is the longest chapter and the one with the most to learn from, because almost nothing in it failed
loudly. Eleven separate faults in this phase were **silent**: a Platform that reported a task `complete`
while returning nothing, Sentinels that could never reach quorum so no failover ever ran, an image without
`curl` so every health check lied, an MCP server publishing device passwords. Each one is in
*Troubleshooting* below, named by the symptom you would actually see.

---

## Before you start

### What must already be true

- Chapters 05–07 green on the dev stack. **The replay has nothing to replay otherwise** — this chapter does
  not build assets, it copies the ones you already have.
- The images are staged on the hypervisor from chapter 05 (`images/fetch.sh itential`). The same pins are
  reused; nothing is re-pulled from the registry.
- Roughly 49 GB of RAM free for the eleven new VMs. The dev-stack VM is still running at this point, so the
  plan over-commits by its 24 GB until the retirement at the end of the chapter. That is stated in the
  resource budget rather than hidden.
- `.env` will gain eight database secrets. Every one is **generated on first run and persisted before use**.

### The shape

Itential's HA2 is the production reference architecture: two Platform nodes, a three-member MongoDB replica
set, three Redis with Sentinel, and one Gateway. The guide requires one component per server, so this lab
runs eleven VMs — the nine components plus a load balancer and a tools VM.

| VM | Address | Size | Role |
|---|---|---|---|
| `iap-lb` | `10.100.0.71` | 1 / 1 GB | nginx in front of the Platform nodes |
| `iap-01`, `iap-02` | `.72`, `.73` | 4 / 8 GB | Platform 6.5.2 |
| `mongo-01…03` | `.74`–`.76` | 2 / 4 GB | Replica set `rs0` |
| `redis-01…03` | `.77`–`.79` | 1 / 2 GB | Redis + Sentinel |
| `iag-01` | `.80` | 4 / 6 GB | Gateway 5 cluster with its runner |
| `tools-01` | `.81` | 4 / 8 GB | MCP, Ollama, and the directory |

The guide's own sizes are 16 cores and 64–128 GB per node. These are lab sizes; the shape is what is being
reproduced, not the capacity. `itential/ha2/versions.yaml` is the oracle for all of it — which VM runs what,
the replica-set and Sentinel names, the ports and the accounts — and `tests/test_platform_ha2.py` holds it
to the IP plan and the resource budget.

Decisions: [ADR 0053](../adr/0053-production-platform-ha2.md) for the environment,
[ADR 0055](../adr/0055-replay-onto-production.md) for the migration.

### Active/Standby, deliberately

**Read this before you build, or you will spend a day on it.** Both Platform nodes are built, configured,
attached to the same databases and proved healthy — and then every node but the first is **parked** with
`docker compose stop platform`.

That is not a compromise; it is one of Itential's own architectures, and it follows from a hard limit:
Gateway Manager accepts exactly **one connection per gateway cluster**. Only the node holding that
connection can reach a device. But the Platform spreads job, task and agent execution across every running
node, so work that lands on the other node cannot reach anything. The worker flags
(`ITENTIAL_JOB_WORKER_ENABLED`, `ITENTIAL_TASK_WORKER_ENABLED`) cover jobs and tasks, but the agent
execution engine has no equivalent flag.

Measured both ways:

| | Standby running | Standby parked |
|---|---|---|
| `verify/test-05-itential.sh` | 12/12 | 12/12 |
| `verify/test-06-flowai.sh` (agents) | 9/14 | **14/14** |
| `verify/test-06b-platform.sh` | 5/5 | 5/5 |
| `verify/test-08-platform-ha2.sh` | 9/9 | 9/9 |

Failover is `docker compose start platform` on the standby VM, and `restart: unless-stopped` keeps a parked
node down across a reboot. Genuine active/active needs a gateway cluster per Platform node, which is a later
element.

---

## The commands, in order

### 1. Build

```
make plan-platform-ha2      # read-only: eleven VMs to add
make phase-platform-ha2
```

| | Step | What it does |
|---|---|---|
| 1–2 | `ansible/playbooks/netbox-seed.yml`, `ansible/playbooks/netbox-vms.yml` | NetBox first, from the same oracle |
| 3 | `tofu apply` in `tofu/platform-ha2` | Eleven VMs, one resource per component |
| 4 | `ansible/playbooks/oob-gw.yml --tags dns` | The new names resolve |
| 5 | `ansible/playbooks/platform-ha2-hosts.yml` | Docker, the lab CA, and a cert-manager certificate for every host that terminates TLS |
| 6 | `images/fetch.sh itential-load-ha2` | Relays the staged tarballs to the production VMs |
| 7 | `ansible/playbooks/platform-ha2-mongodb.yml` | Replica set `rs0`: keyFile intra-cluster auth, `requireTLS`, and the three SCRAM accounts the Deployer defines |
| 8 | `ansible/playbooks/platform-ha2-redis.yml` | One master, two replicas, three Sentinels, ACL users only |
| 9 | `ansible/playbooks/platform-ha2-platform.yml` | Both Platform nodes on the replica set and Sentinel, adapters per node, the local administrator declared, nginx in front — then the standby parked |
| 10 | `ansible/playbooks/platform-ha2-tools.yml` | MCP, Ollama and OpenLDAP on the tools VM |
| 11 | `ansible/playbooks/platform-ha2-identity.yml` | Provisions `admin@itential` in the directory |
| 12 | `ansible/playbooks/platform-ha2-gateway.yml` | The Gateway 5 cluster on its own VM, registered with the production Gateway Manager |

The order of 10 → 11 → 12 matters: Gateway Manager filters gateways and connections by the caller's group
membership, and only the directory account has one. The tools VM and the directory account therefore come
**before** the gateway.

### 2. Replay

```
make replay-platform-ha2
```

Every asset chapters 05–07 built, recreated on production: workflows, both Golden Config trees, the
compliance plan, the MOP templates, the Lifecycle Manager model and its form, both Integration Models, the
agent projects, and both inventories.

The mechanism is worth understanding, because it is what makes this repeatable. The asset half of
`itential.yml` and `flowai.yml` lives in shared task files that both environments include; every
asset-creating play takes its target from `platform_target` and its administrator from
`platform_admin_user`; and `ansible/playbooks/vars/itential-prod.yml` is the overlay that selects
production. The dev-stack path is unchanged — the same task files, different variables. The replay is
idempotent: re-run it immediately before the cut-over.

**The replay reproduces what exists, not what is next.** Nothing changes shape during a migration.

### 3. Prove it before moving anything

This is [ADR 0055](../adr/0055-replay-onto-production.md) decision 2, and it is the reason the cut-over was
uneventful. Every chapter 05–07 verify takes an address override, so production can be proved while
`itential.${LAB_DOMAIN}` still points at the dev stack:

```
IT_IP=10.100.0.71 IT_MCP_IP=10.100.0.81 verify/test-05-itential.sh
IT_IP=10.100.0.71 verify/test-06b-platform.sh
```

### 4. Cut over

A document change, not a manual edit. `topology/ipam.yaml` moves the service name onto the load balancer as
an alias and renames the dev-stack VM; the resolver and NetBox follow the document.

```
itential.${LAB_DOMAIN}      10.100.0.65  →  10.100.0.71   (the load balancer)
mcp.${LAB_DOMAIN}           .81 and .65  →  10.100.0.81   (tools-01)
itential-dev.${LAB_DOMAIN}  (new)        →  10.100.0.65   (the dev stack)
```

That last row also fixes something nobody had noticed: `mcp.${LAB_DOMAIN}` had been returning **both**
addresses.

### 5. Retire

Owner-approved, and the order matters:

1. **Monitoring and inventory first** — a monitored host that vanishes raises alarms.
2. Then `tofu destroy` in `tofu/itential`, which owns exactly one resource.
3. Then the address and the names.

Two plays gained pruning they never had, which is what let this be a document change rather than a cleanup
by hand: `ansible/playbooks/netbox-vms.yml` now deletes a virtual machine in the cluster it no longer
lists, and `ansible/playbooks/observability.yml` deletes a Zabbix host it stamped with its own path that
the documents no longer name. That filter fails safe — Zabbix's built-ins and anything added by hand are
never candidates.

---

## What "done" looks like

The build is the better part of a day with the replay and the verification runs. The VM creation is
minutes; MongoDB and Redis are quick; the two Platform nodes' first boots and the adapter installs are the
long poles, and the replay is tens of minutes.

- `https://itential.${LAB_DOMAIN}` answers **on the load balancer**, and a session is valid on either node.
- `rs.status()` shows one PRIMARY and two SECONDARY, over TLS with SCRAM, and unauthenticated clients are
  refused.
- Redis has one master, two replicas, and three Sentinels that agree on the master, with ACL users only.
- The active node serves; the standby's container exists, is **not running**, and its VM is otherwise up.
- Configuration Manager sees the same twelve devices NetBox holds, through the Device Broker on the gateway
  VM.
- The official Itential dashboard's MongoDB and Redis rows show the production replica sets — the one panel
  family that could never have data while the database was a standalone container.
- The dev-stack VM is gone from the hypervisor, NetBox, Zabbix, DNS and the IP plan, and its address is
  released and silent.

---

## Verification

`verify/test-08-platform-ha2.sh` is this chapter's script.

```
verify/test-08-platform-ha2.sh
```

| Criterion | What a PASS means |
|---|---|
| S11.1 | The eleven VMs run at the sizes of the oracle and the budget, and NetBox holds each one |
| S11.2 | MongoDB is a three-member replica set — one PRIMARY, two SECONDARY — with authentication and TLS |
| S11.3 | Redis has one master, two replicas and three Sentinels that agree, with ACL users only |
| S11.4 | The active Platform node serves through the load balancer **and the standby is built and parked** |
| S11.5 | Everything chapters 05–07 built exists on production, and a **live device call** succeeds |
| S11.7 | The official dashboard's MongoDB and Redis rows show the production replica sets |
| S11.8 | The dev-stack VM is retired and every trace of it removed |
| S11.6a/b/c | The three failover drills, behind `VERIFY_DRILLS=1` |

The final run was **7 passed, 0 failed, 0 deferred** — the first run in the whole series with nothing
deferred.

The drills:

```
VERIFY_DRILLS=1 verify/test-08-platform-ha2.sh
```

- **S11.6a** — start the standby, stop the active node, watch the load balancer keep serving, restore, park
  again.
- **S11.6b** — stop the MongoDB primary; a new one is elected within 30 s and the Platform keeps serving.
- **S11.6c** — stop the Redis master; Sentinel promotes a replica and the Platform keeps serving.

Every drill **ends by waiting for the environment to come back** — the directory account has to log in
again, and the active Platform is restarted once if it has not recovered within 80 s. A drill that leaves
the environment worse than it found it is not a passing drill.

---

## Troubleshooting

These are the eleven findings of this phase, in the order they cost time. Almost every one of them
succeeded loudly and failed silently.

**Both Platform nodes read unhealthy and the load balancer has no upstream — because the image has no
`curl`.** The Compose healthcheck called `curl`, which the Platform image does not carry. The healthcheck
therefore failed permanently, both nodes were marked unhealthy, and nothing said why. The vendored upstream
dev-stack Compose file tries `wget` first for exactly this reason. If you write a healthcheck for a
container, check the binary is in the image before you check the endpoint.

Two related faults sat underneath it: the Platform Compose published only 3000 and 3443, so the load
balancer's layer-4 upstream had nothing to reach at all; and `/health/server` **needs a session**, and
refuses a caller with no group membership — so the health task has to try the directory account before the
bootstrap user. `/login` is the liveness endpoint.

**`received unexpected message from server: tasks/list`.** The nginx stream upstream to Gateway Manager was
round-robin, so successive reconnect attempts landed on different Platform nodes and the gateway rejected
the reply. Hashing on the source address fixed the symptom but pinned the connection to an arbitrary node;
the correct configuration names the first Platform node as the only **primary** with the others as
**backup**, so the connection lands on the node the replay runs on and a failed primary still hands over.

**`There is already an active connection for gateway cluster: lab`.** This is the finding the whole
architecture turns on. Gateway Manager accepts exactly one connection per gateway cluster; pointing a
second gateway process at the other Platform node is refused with that message. Only the node holding the
connection can reach a device.

The symptom you actually see is worse than the message, because it appears somewhere else: a Configuration
Manager compliance run **stalls halfway**. Reproducibly, 7 of 12 devices completed and the instance errored
— and the same plan completed cleanly as soon as the second node was stopped. Job and task execution had
been spread across both nodes, and the half that landed on the node without the gateway connection could
not reach anything.

The gateway's own HA flags do not close this. `GATEWAY_CONNECT_SERVER_HA_ENABLED` and
`GATEWAY_CONNECT_SERVER_HA_IS_PRIMARY` exist in the 5.5.2 image, but Itential's *Choose a deployment
architecture* page says what they are for: several gateway **servers** of which only one is active and
maintains the connection. That is redundancy for the gateway, not a second connection for a second Platform
node. Nor is there a fallback path — the HA2 network table lists Platform-to-Gateway on 8083/8443, but a
Gateway 5 server listens only on 50051; those ports are Gateway 4's REST API. One WebSocket per cluster is
all there is.

**`netsdk-musl-linux-amd64.pex: No such file or directory` on every send-command.** Gateway 5's netsdk
services live in the etcd store with the **absolute path of the pex that runs them**, and both the server
and the runner register them — last writer wins. Recreating the server (which enabling distributed
execution does) wrote its own musl path, while execution lands on the glibc runner. So every send-command
looked for a file that exists only in the other image.

The first fix — restart the runner when the server changed — was a race, not a fix: the server rewrites the
store with its own path *every time it reconnects to Gateway Manager*, which a Platform restart causes. The
gateway play is now how the store is put right after anything that reconnected the gateway, and it
registers the runner's services **last**, unconditionally. The server also announces itself by its Compose
service name rather than `0.0.0.0`, because with distributed execution the runner dials the server back on
the address it announces.

Only a **live** device call catches this. Every other read is served from the cache, which is why S11.5
now issues one.

**A task reports `complete` and returns nothing.** `GATEWAY_SERVER_DISTRIBUTED_EXECUTION` was missing from
the HA2 gateway Compose template. Without it, the server executes `runCode` on its own musl image, `pip`
cannot install pyATS, and the Platform records the task as **complete with a null result**. The structured
show-command workflow returned no parse, and the VLAN workflow's NetBox journal entry was never written —
with no error anywhere. This is the glibc-runner finding of chapter 06 reappearing as a missing flag. It
was found by running the chapter 06 verification against production, and one assertion caught it: the
journal-entry check.

**`sentinel-user` — Sentinels must authenticate to each other, or no failover ever runs.**
`sentinel auth-user` and `sentinel auth-pass` authenticate a Sentinel to the **monitored master** only.
Peer Sentinels connect to each other directly, and with `user default off` those connections were refused
with `NOAUTH`. Every Sentinel marked the other two `+sdown` seconds after discovering them on the master's
hello channel, so the quorum of 2 was never reachable: a stopped master went `+sdown` but never `+odown`,
and the drill sat for 108 seconds with nothing promoted.

`sentinel sentinel-user` and `sentinel sentinel-pass` are the missing directives. `ckquorum` then reports
`OK 3 usable Sentinels` and the drill promotes a replica in 8 seconds. Note also that the play must
**restart** Sentinel when it has rewritten the base configuration: the files live in a mounted directory,
so Compose sees no change and the container keeps running with what it read at startup.

**`describe_inventory` returns device passwords to any MCP client.** The interim control excludes
`describe_inventory` and Configuration Manager's `get_devices` by tag, because their replies carry the
Inventory Manager node attributes with `itential_password` in cleartext. The dev stack's Compose override
set it; **the HA2 tools template did not** — so from the moment the cut-over pointed `mcp.${LAB_DOMAIN}` at
the tools VM, the production MCP server exposed both. Caught by chapter 05's S4.7, which now passes against
production with no address override. When you rebuild a component in a new place, the exclusions and
exceptions do not come with it.

**Inventory Manager refuses the administrator, and a group membership disappears between requests.** The
Platform rewrites the default user's account document on **every login**, so a `memberOf` written through
the authorization API is gone by the next request — and an `assignedRoles` PATCH clears `memberOf`
outright. Inventory Manager refuses a caller who holds a role directly but is not a member of a group that
holds it, so the replay could not create the inventories.

Platform 6 exposes no account-creation route, so there is no local account to create. The answer is the
same one chapter 05 reached: the directory. Production runs the same OpenLDAP the dev stack does, on the
tools VM, and `admin@itential` is provisioned through it. The image's default user stays as break-glass
only.

**A gateway cluster create returns 409 on a fresh environment.** The bootstrap user's gateway list read
comes back **empty even when the gateway exists**, because Gateway Manager filters by group membership. The
create tolerates 409 for that reason. It is also why the tools VM and the directory account run before the
gateway play.

**Prometheus loses the dashboard panels after the cut-over.** `apply` prunes keys *inside* a document but
never a document that has stopped being rendered — so the dev stack's superseded ScrapeConfig had to be
removed explicitly. The production exporters are per-node: one ScrapeConfig per Platform node, because each
presents its own certificate; node and process exporters on both Platform nodes; the Redis exporter on all
three Redis servers; and the MongoDB exporter on all three replica-set members, which is what finally gives
the dashboard's replica-set panels data.

**A drill passes and leaves the environment broken.** The Redis failover left all four adapters `DEAD` on
the active Platform node, so the directory account could not log in until the Platform was restarted — and
the *next* check was blamed for it. Each drill now ends by waiting for the directory account to log in
again. If you write a drill, make it prove recovery, not just failure.

**Smaller things measured on the way.** The Platform's Sentinel list is **JSON**
(`ITENTIAL_REDIS_SENTINELS`). The MCP image binds loopback unless `ITENTIAL_MCP_SERVER_HOST` is set. The
OpenTofu provider's guest-agent wait is capped at two minutes here, because the Ubuntu template carries no
agent until the host play installs it. And a `netbox-seed.yml` filter had been invalid since chapter 07, so
the released-reservation cleanup had silently done nothing for a phase.

---

## Tested versions

| Component | Version |
|---|---|
| Itential Platform | 6.5.2 (the same ECR pins chapter 05 staged) |
| Itential Gateway 5 | 5.5.2-amd64, distributed execution, glibc runner |
| MongoDB | 7.0.40, replica set `rs0`, keyFile + SCRAM + `requireTLS`, wiredTiger cache 1 GB |
| Redis | 7.4.11, one master + two replicas, three Sentinels, quorum 2, ACL users only |
| nginx | 1.30.4-alpine — HTTP upstream `ip_hash`, gateway stream primary + backup |
| OpenLDAP | `osixia/openldap` 1.4.0, on the tools VM |
| MCP server | `ghcr.io/itential/itential-mcp` v0.14.0, on the tools VM |
| Exporters | node `v1.12.1`, process `0.8.7`, plus Redis and MongoDB exporters per server |
| Environment | Eleven VMs, 49 GB, Active/Standby |

`itential/ha2/versions.yaml` owns the topology; images are **not** repeated there — they come from
`itential/versions.yaml`, so production and the dev stack cannot drift on a version.

---

## Where this leaves the lab

Production is the Platform. The dev-stack VM is gone, its address is released, and every asset that was
developed on it now lives on an environment built entirely from documents in this repo — which is the only
claim in this series that a restore-from-backup migration could not have made.

Two things are decided and not yet built: [ADR 0054](../adr/0054-integrations-over-adapters.md) converts
NetBox and ServiceNow from npm adapters to Integration Models built from OpenAPI, and genuine
active/active needs a gateway cluster per Platform node. Both are elements after this series, not gaps in
it.

---

**Previous:** [07 — Observability](07-observability.md) · **Back to:** [the series index](README.md)
