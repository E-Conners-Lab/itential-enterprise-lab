# 0055 — The phase 5-7 assets replay onto production from shared task files, targeted by a variable

- **Status:** accepted (PR #24, 2026-09-11) — the replay, the cut-over and the retirement are done and S11.5 passes
- **Date:** 2026-09-10
- **Related:** ADR 0035 (the dev-stack), ADR 0037/0038 (FlowAI), ADR 0039 (the Device Broker), ADR 0040 (Platform coverage), ADR 0053 (the production environment; S11.5 "migration by replay"), ADR 0054 (integrations over adapters), PID S11 (amendment 1.20)

## Context

ADR 0053 decided that the production environment is populated by *replaying* what phases 5-7 built rather
than by restoring VM 205's database, and stated the mechanism as: "the phase 5-7 plays that address the
Platform by name run against production once `itential.lab.internal` points at the load balancer".

That is true of the **verifies** — `verify/test-05`, `test-06`, `test-06b`, `test-06c` all reach
`https://itential.lab.internal`, so they follow the DNS record at cut-over with no change at all. It is not
true of the **plays**. `itential.yml`, `platform.yml` and `flowai.yml` run *on* the Platform host and address
it as `http://127.0.0.1:3000`; their target is the inventory group `itential-host`, which is VM 205. Worse,
two of the three mix building the dev-stack with creating Platform assets:

| Play | Builds the dev-stack | Creates Platform assets |
|---|---|---|
| `itential.yml` | runner image, Compose files, the OpenLDAP bootstrap, the stack `.env`, the Gateway 5 client certificate, `compose up`, the LDAP adapter, the Gateway Manager registration, the adapter `npm install` | the adapter instances, Inventory Manager (`lab`, `lab-hosts`), the Device Broker, the NetBox VLAN groups, the workflow import |
| `platform.yml` | — | device groups, Golden Config trees, the compliance plan, MOP templates, the backup schedule, the form, the LCM model and instances, the Integration Models |
| `flowai.yml` | Ollama and the pinned models on the dev VM | the Model Registry profiles, the agent project, the agents |

Running them unchanged against production would rebuild a dev-stack on `iap-01`. Copying the asset halves
into new `platform-ha2-*` plays would leave two copies of the same 500 lines to drift, which is exactly the
failure ADR 0053 tried to avoid by replaying rather than restoring.

The environments also differ in three measured ways:

1. **Role re-sync.** Loading an adapter registers new roles; without a re-sync the administrator gets 403 on
   the adapter's routes. The dev-stack does it in the database (`docker exec mongodb mongosh`) because its
   administrator is an LDAP account. Production has no LDAP yet and its MongoDB is a TLS replica set on three
   other VMs, so it must do the same thing through the authorization API — which
   `platform-ha2-gateway.yml` already does once, before the adapters load.
2. **The in-lab Ollama.** `llm.profiles`'s `ollama-lab` reads `http://ollama:11434`, a Compose service name
   only resolvable inside the dev-stack's network. On production Ollama runs on `tools-01`, so the profile
   must carry that VM's address.
3. **The administrator.** Dev logs in as `admin@itential` (LDAP); production as the local `admin` of
   `itential/ha2/versions.yaml` `platform.admin_user`.

## Decision

1. **The asset half of each play becomes a task file that both environments include**, under
   `ansible/playbooks/tasks/`:
   - `platform-assets.yml` — the adapter instances, the role re-sync, Inventory Manager and the host
     inventory, the Device Broker, the Gateway 5 probes, the NetBox VLAN groups and the workflow import
     (lifted out of `itential.yml`).
   - `flowai-assets.yml` — the Model Registry profiles, the agent project and the agents (lifted out of
     `flowai.yml`).
   - `roles-to-admin.yml` — every role assigned to `admin_group` and to the local administrator through the
     authorization API, lifted out of `platform-ha2-gateway.yml` so it has one definition.
   - `roles-to-admin-ldap.yml` — the dev-stack's database re-sync, unchanged.
   The task files are the only definition of those assets; the callers differ only in what they pass.
2. **The target is a variable, not a rewrite.** Every asset-creating play takes
   `hosts: "{{ platform_target | default('itential-host') }}"`, so the default is the dev-stack and an
   extra-vars file selects production. The play still runs *on* the Platform node and still addresses it as
   `http://127.0.0.1:3000`: no DNS change and no certificate are needed, which is what lets the replay run
   and be verified **before** the cut-over rather than after it.
3. **`ansible/playbooks/vars/itential-prod.yml` is the production overlay** — `platform_target`,
   `platform_admin_user`, `role_resync_tasks` and `ollama_base_url` — passed as `-e @…`. It is held to
   `itential/ha2/versions.yaml` by `tests/test_platform_ha2.py`, so the oracle stays the single source of the
   addresses and the account name.
4. **`platform-ha2-replay.yml` is the production entry point**: it logs in as the local administrator,
   includes `tasks/platform-assets.yml`, imports `platform.yml` (which is entirely assets and needs only the
   target variable), then includes `tasks/flowai-assets.yml` with the production Ollama. The order is the one
   ADR 0038 requires: workflows, then the Platform applications, then the agents whose tools reference them.
5. **The dev-stack keeps working unchanged.** `make phase-itential` and `make phase-flowai` run the same
   plays with no extra vars and therefore against VM 205, until it is retired. That is what makes the two
   environments comparable during the cut-over (ADR 0054 item 5).
6. **The replay reproduces what exists, not what is next.** No asset changes shape during the migration: the
   adapter-based NetBox and ServiceNow instances are replayed as they are, and the conversion to Integration
   Models (ADR 0054) happens afterwards, as that ADR already fixed.

7. **Production runs the same directory the dev-stack runs, because there is no local account to create.**
   Measured on 6.5.2 while replaying: the image's default user (`ITENTIAL_DEFAULT_USER_*`) cannot hold a group
   membership - the Platform rewrites its account document on every login, so a `memberOf` written through the
   authorization API is gone by the next request, and an `assignedRoles` update clears it outright - and
   Inventory Manager and its peers refuse such a caller: *"User must be a member of at least one of the
   assigned groups with roles: inventory:read and inventory:update"*, even with all 175 roles held directly.
   Nor can a replacement be created: Platform 6 exposes no account-creation route (the Admin Essentials bundle
   only lists and patches accounts), so accounts come from the AAA source or not at all. This is the same wall
   Phase 5 hit on the dev-stack - *"the built-in local admin's memberOf is ignored by the session; observed"* -
   and it has the same answer. Production therefore runs the same OpenLDAP image, the same upstream LDIF and
   the same adapter settings as the dev-stack, on `tools-01` rather than beside the Platform, which ADR 0053
   already listed among the production components. `platform.admin_user` is `admin@itential`, provisioned by
   its first login and given every role and membership of admin_group by a write to the replica set - exactly
   what `itential.yml` does against VM 205's MongoDB. `platform.bootstrap_user` (`admin`) stays for the first
   login only. `ansible/playbooks/platform-ha2-identity.yml` builds it, after the tools VM exists.

8. **The load balancer is active/standby, not active/active, because a Gateway 5.5.2 holds exactly one
   Platform connection.** `GATEWAY_CONNECT_HOSTS` is plural in name only - a comma-separated list is rejected
   with *"too many colons in address"* - and the connection it makes is node-local: only the node holding it
   can reach a device, and the other answers *"No connection found to Gateway with cluster id lab"* or, for
   Configuration Manager's device list, simply never answers. With `ip_hash` on the HTTP upstream, whether the
   lab worked depended on which node a client hashed to. Both upstreams therefore name the first Platform node
   as the only primary and the rest as `backup`, so traffic and the gateway connection sit on the same node
   and fail over together - which is what Itential's own Active/Standby architecture does between data
   centres. Scaling to genuine active/active means one Gateway per Platform node, and that is a later element.

9. **One Platform node runs the workers; the others serve the UI and the API.** Gateway Manager accepts
   exactly one connection per gateway cluster - pointing a second gateway process at the other node is
   refused with *"There is already an active connection for gateway cluster: lab"* - so only the node holding
   it can reach a device. The Platform, though, spreads job and task execution across every node whose
   workers are enabled, and a Configuration Manager compliance run then stalls halfway: reproducibly, 7 of
   12 devices completed and the instance errored, and the same plan completed cleanly as soon as the second
   node was stopped. `ITENTIAL_JOB_WORKER_ENABLED` and `ITENTIAL_TASK_WORKER_ENABLED` are therefore true on
   `platform.nodes[0]` and false on the rest, which is Itential's Active/Standby shape and the same node the
   load balancer and the gateway stream prefer. The cost is stated plainly: losing the worker node stops job
   execution until it returns, while the UI and the API keep serving from the standby (drill S11.6a). Genuine
   active/active needs a gateway cluster per Platform node, and that is a later element.

   **The gateway's own HA flags do not close this.** `GATEWAY_CONNECT_SERVER_HA_ENABLED` and
   `GATEWAY_CONNECT_SERVER_HA_IS_PRIMARY` exist in the 5.5.2 image, and Itential's *Choose a deployment
   architecture* page says what they are for: several gateway **servers**, of which *"only one gateway server
   can be active at a time. The active node maintains the connection to Gateway Manager and handles all
   incoming requests"*. That is redundancy for the gateway, not a second connection for a second Platform
   node. Nor is there a Platform-to-Gateway path to fall back on: the HA2 network table lists Platform to
   Gateway on 8083/8443, but a Gateway 5 server listens only on 50051 (the runner's gRPC) - those ports are
   Gateway 4's REST API. One WebSocket per cluster is all there is, and 6.5.2 does not proxy a gateway call
   from a Platform node that does not hold it.

   **The worker split does not cover the agent engine.** `ITENTIAL_JOB_WORKER_ENABLED` and
   `ITENTIAL_TASK_WORKER_ENABLED` are the only such flags the image has; the agent execution engine
   distributes independently, so a FlowAI session whose tool call lands on the standby ends `FAILED` with
   *"session is terminal (sibling tool may have failed)"*.

   **So the environment is Active/Standby** (owner decision 2026-09-10), which is one of Itential's own
   architectures rather than a lash-up: every Platform node is built, configured and attached to the same
   databases - `platform-ha2-platform.yml` proves each one healthy, node by node - and then every node but
   `platform.nodes[0]` is parked with `docker compose stop platform`. `restart: unless-stopped` keeps a
   parked node down across a reboot. The failover is `docker compose start platform` on that VM, and drill
   S11.6a runs exactly that: start the standby, stop the active node, watch the load balancer keep serving,
   then restore. The whole trade, measured both ways:

   | | standby running | standby parked |
   |---|---|---|
   | `test-05` | 12/12 | 12/12 |
   | `test-06` (agents) | 9/14 | **14/14** |
   | `test-06b` | 5/5 | 5/5 |
   | `test-06c` | 6/6 | 6/6 |
   | `test-08` | 9/9 | 9/9 *(S11.4 reworded)* |

   S11.4 therefore reads "the active Platform node serves through the load balancer and the standby is built
   and parked" - it checks the standby's container exists and is not running, and that the VM is otherwise
   up. S11.7 expects one `iap_exporter` target up, not two. Genuine active/active needs a gateway cluster per
   Platform node, and that stays a later element.

## Consequences

- One definition per asset. A change to a workflow, an inventory or an agent lands in one file and reaches
  both environments the next time each is run.
- `itential.yml` shrinks to the dev-stack and an include; `platform-ha2-gateway.yml` loses its inline role
  block. Both are re-run to prove the extraction is behaviour-preserving before the replay is trusted.
- The replay can be re-run at any time and is idempotent, so the cut-over is a DNS change and not a deadline:
  anything created on VM 205 between the replay and the cut-over is picked up by running the replay again.
- Gateway 5 in production runs on `iag-01` with cluster id `lab`, the same id the dev-stack uses, so the
  inventories and the Device Broker replay with no edit; the two clusters never talk to each other because
  each registers with its own Platform.
- Both environments now authenticate the same way, so `admin@itential` is one account name across the lab
  and the phase 5-7 verifies need no edit at cut-over. The password is the upstream LDIF's own value, which
  `ITENTIAL_ADMIN_PASSWORD` already carries: an accepted lab exception, recorded in `.env.example` since
  Phase 5, that the identity phase removes when OpenLDAP moves to k3s behind Keycloak.
- One more play in the phase 8 sequence (`platform-ha2-identity.yml`) and one more container on `tools-01`.
  The dev-stack's LDAP block leaves `itential.yml` for `tasks/ldap-admin.yml`, parameterised by `ldap_url`
  and the mongosh invocation, so the two environments share the definition rather than copying it.
- Two operational traps are now recorded in the plays that hit them: `nginx.conf` is bind-mounted as a
  single file and Ansible's template module writes atomically, so the running container keeps the old inode
  and `nginx -s reload` re-reads the file it already has - the container is recreated instead; and the
  Platform image carries `wget` but not `curl`, so the compose healthcheck that used `curl` marked both
  nodes unhealthy for as long as they ran.
- The production Gateway 5 needs `GATEWAY_SERVER_DISTRIBUTED_EXECUTION=true`, which the dev-stack's vendored
  Compose file sets and the HA2 template had not. Without it the server runs a `runCode` task on its own
  musl image, where `pip install pyats` fails, and the Platform records the task as *complete* with a null
  result - so `wf-show-command-v1` returned no parse and `wf-branch-vlan-v1`'s NetBox journal entry was
  never written, both silently. That is ADR 0038's glibc-runner finding reappearing as a missing flag.
- Gateway 5's netsdk services live in the etcd store with the absolute path of the pex that runs them, and
  both the server and the runner register them - last writer wins. A recreated server writes its own musl
  path while the execution lands on the glibc runner, and every `send-command` then fails with
  *"netsdk-musl-linux-amd64.pex: No such file or directory"*. The play restarts the runner whenever the
  server changed, so the runner's paths are always the ones in the store.
- The Ollama profile differs per environment, so `verify/test-06` S4c's provider-profile check passes on
  production only once `tools-01`'s Ollama has the pinned models pulled; that pull is part of the replay.
