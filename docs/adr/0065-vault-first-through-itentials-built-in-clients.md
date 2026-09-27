# 0065 — Vault first: device and API credentials through Itential's built-in Vault clients

- **Status:** accepted (owner decisions, 2026-09-23); amended 2026-09-24: Phase 9b deferred (PID 1.35)
- **Date:** 2026-09-23
- **Amends:** ADR 0026 (single Raft replica, the AppRole shapes, a read-only Platform, an off-host snapshot),
  ADR 0050 (Phase 9 is split: Vault goes first as 9a), PID S8 and section 3 (amendment 1.34)
- **Related:** ADR 0063 (dev isolation), ADR 0059 (a failed tool call must end the job), ADR 0064 (Alertmanager
  routes to `lab-null`; a later phase sizes its own backup), ADR 0057 (monitoring follows the estate), ADR 0053
  (production HA2)

## Context

PID requirement 8 puts every secret in Vault, and S8 already has the acceptance criteria. The owner's manager
wants Vault in the lab, so it goes before Oxidized and Gitea, which share its phase.

Where the credentials the Platform and Gateway use live today. Production was read GET-only on 2026-09-23;
values were classified, never printed:

| Credential | Where it is written | How production stores it |
|---|---|---|
| Device `automation` password | `ansible/playbooks/tasks/platform-assets.yml` (the `lab` and `lab-hosts` inventory nodes; the dev clab nodes use `CLAB_AUTOMATION_PASSWORD`) as the `itential_password` node attribute | **Plaintext** on all 15 nodes (12 `lab`, 3 `lab-hosts`). The file calls it an accepted SEC exception "until Vault (Phase 9)". It is also why `svc-copilot` has no Inventory Manager role on production (ADR 0063): an inventory read returns the passwords. The `nxos` inventory's two NetBox mocks (ADR 0065 does not touch them) carry a second, different plaintext value |
| NetBox token (the adapter the InventoryBroker consumes, ADR 0039) | `platform-assets.yml`, adapter `NetBox` properties | `$ENC`: encrypted with `ITENTIAL_ENCRYPTION_KEY`, which is itself in `.env` |
| NetBox token and ServiceNow password (Integration Model instances `netbox-api`, `servicenow-api`, ADR 0045) | `ansible/playbooks/tasks/integrations.yml` | `$ENC` (the ServiceNow username is plaintext, as expected). Neither instance sets `proxyOverride`, so both execute directly from the Platform, not through a Gateway |
| NetBox token for runCode tasks (ADR 0048) | `itential/ha2/gateway.compose.yml.j2`, runner environment | Plaintext in the runner's environment on iag-01 |
| LDAP adapter bind password | the LDAP adapter (ADR 0053 identity) | `$ENC`. Not in 9a's scope; it moves with the other service secrets in 9b |

So the passwords that can be read back in the clear are the device passwords. The service tokens are encrypted
with a key that sits in the same `.env`, which is exactly what S8 criterion 3 asks to replace.

What Itential ships, from its documentation and repositories:

- **Gateway 5.5.0 and later has HashiCorp Vault KV v2 as a built-in secret-provider type**:
  `iagctl create secret-provider --type vault`, with token auth (a token file) or AppRole (`--role-id` plus
  `--secret-id-file`, re-read when the file changes). CyberArk CCP is the other built-in; `--type plugin` covers
  everything else. Aliases resolve as `$GATEWAYSECRET_(alias)` in Inventory Manager node attributes, in
  Integration Model instances that execute through a Gateway, in decorators and inline code, and in the native
  send-command/send-config services. The Gateway documentation recommends AppRole over a token that can expire.
  Production and dev run Gateway 5.5.2.
- **itential/assets has no Vault secret-provider plugin because none is needed.** Its AWS, Delinea and Azure
  plugins exist for providers that are not built in, and the Delinea README says so. The upstream PR the
  2026-09-23 roadmap proposed rested on the opposite belief and is withdrawn.
- **The Platform has its own Vault client**: `ITENTIAL_VAULT_URL`, `ITENTIAL_VAULT_AUTH_METHOD` (`token` or
  `approle`), `ITENTIAL_VAULT_TOKEN` (a file path), `ITENTIAL_VAULT_ROLE_ID`, `ITENTIAL_VAULT_SECRET_ID` (a value,
  not a file), `ITENTIAL_VAULT_SECRETS_ENDPOINT`, `ITENTIAL_VAULT_READ_ONLY`. Adapter properties refer to Vault
  as `$SECRET_<path> $KEY_<key>`; when Vault is configured and not read-only, the Platform can also encrypt
  adapter properties into Vault by itself. The documentation shows both authentication methods, recommends
  neither, and leaves "valid policies and TTL/usage limits" to the Vault side.
- **Itential's `itential-dev-stack`** (`scripts/configure-openbao.sh`) wires the Platform to OpenBao with the
  root token in a file and leaves the Gateway unwired: a laptop demo, not a pattern to copy.

The disk: the host's only SSD was re-measured on 2026-09-23. It is healthy (SMART OK, 4 % endurance, zero grown
defects, zero uncorrected errors, 110-187 GB written a day). Wear is decades away, but it is still one disk with
nothing off-host, and Vault's data is the first thing in the lab that cannot be rebuilt from this repo.

## Decision

1. **Phase 9 is split.** Phase **9a** (`phase-9a/vault`, exit tests `verify/test-09a-vault.sh` for production and
   `verify/test-09a-vault-dev.sh` for the dev tier, run by `make verify-dev`) delivers S8
   criteria 2 and 3, plus a rotation drill on the dev tier. Criteria 4 (`.env` reduced) and 5 (cert-manager from
   the Vault PKI) move to a Phase 9b. Oxidized and Gitea (criterion 1) stay in Phase 9.
2. **Only Itential's built-in clients.** The Platform uses its `ITENTIAL_VAULT_*` client and the Gateway uses its
   `vault` secret provider. No custom plugin. `adapter-hashicorp_vault` is not installed in 9a: it lets workflows
   manage Vault, and nothing here needs that.
3. **Placement stays as ADR 0026 decided**: Vault 2.0.4, chart `hashicorp/vault` 0.34.1, on k3s behind VIP
   10.100.0.41, TLS from the lab CA through cert-manager, Raft storage on Longhorn, KV v2 mounted at `lab`.
   Vault's own write volume is negligible next to k3s's (ADR 0064 context, the 2026-09-23 measurement).
   **One Raft replica**, not three: three copies on one physical disk add no durability, and each one has to be
   unsealed.
4. **Paths** (KV v2, mount `lab`): `devices/automation` (`username`, `password`), `services/netbox` (`token`),
   `services/servicenow` (`username`, `password`). The dev tier uses the same paths in its own Vault (decision 9).
5. **The Platform authenticates with AppRole `itential-platform`**, the only shape Itential documents for it.
   The role ID and secret ID go into the Platform hosts' compose environment file (mode 600, written by the play,
   never committed). The Vault side supplies what Itential leaves to it:
   - `secret_id_bound_cidrs` and `token_bound_cidrs` limited to iap-01 and iap-02, so a copied secret ID is
     useless from anywhere else;
   - `secret_id_ttl` 0 and `secret_id_num_uses` 0, because the Platform logs in again with the same secret ID on
     every restart, and an expiring or single-use ID would leave it without Vault after the next reboot;
   - a read-only policy on `lab/data/services/*`, with `ITENTIAL_VAULT_READ_ONLY=true`. The Platform never writes
     to Vault: Ansible seeds it, and the adapter properties carry explicit `$SECRET_` references. Automatic
     property encryption, which needs write access, is not used.
6. **The Gateway authenticates with AppRole `itential-gateway`**: the role ID on the command line, the secret ID
   in a file on iag-01 bound to that host, and a read-only policy on `lab/data/devices/*` and
   `lab/data/services/netbox`. Inventory node attributes become `$GATEWAYSECRET_(<alias>)`. Two things are
   measured on dev before production (decision 9): which container has to hold the secret-ID file under
   distributed execution (server or runner, ADR 0038), and whether the Gateway trusts a Vault certificate signed
   by the lab CA. The integration instances follow whichever path the dev probe proves: the Platform's `$SECRET_`,
   or execution through a Gateway (`cluster_no_proxy`) with `$GATEWAYSECRET_`.
7. **Unsealing is manual.** The owner holds the unseal material; nothing in the repo, in `.env` or in the cluster
   stores it. `make vault-unseal` asks for the key without echoing it, and `make vault-status` reports sealed or
   unsealed. A `VaultSealed` Prometheus rule is added (ADR 0057). **It notifies nobody**, because Alertmanager's
   only receiver is `lab-null` (ADR 0051, 0064): a sealed Vault shows in Grafana, in the Alertmanager UI and in the
   verify, and nowhere else until S7 gets a notification channel.
8. **Vault is backed up off-host**: `make vault-snapshot` takes a Raft snapshot and copies it to the workstation,
   outside the repo, and every play that changes Vault runs it at the end. The snapshot is encrypted by Vault's
   barrier and is useless without the unseal material, which is kept apart from it.
9. **Dev first, and isolated** (ADR 0063): every unknown is measured on `itential-dev` with the `clab` devices,
   against a separate `hashicorp/vault:2.0.4` container on the dev VM that holds only dev secrets
   (`CLAB_AUTOMATION_PASSWORD`, the view-only NetBox token). The dev tier never reaches production's Vault. The
   rotation drill runs there: change the password on one clab device and in the dev Vault, and the next job
   succeeds with nothing changed on the Platform.
10. **Seeding is one-way from `.env`** until Phase 9b makes Vault the source: an Ansible play writes the values
    above into Vault. `.env` still holds them, so S8 criterion 4 is not met by 9a.

## Alternatives rejected

- **A custom HashiCorp Vault plugin for itential/assets.** The Gateway already has one built in.
- **Token auth for the Platform**, as Itential's dev-stack does. A token expires or needs renewing, and the
  Gateway documentation itself steers away from that. AppRole with a non-expiring, address-bound secret ID fails
  only if someone changes the role.
- **Letting the Platform encrypt its own properties into Vault.** Less Ansible, but the Platform would need write
  access to Vault, and the lab's secrets would be written by two different parties.
- **Auto-unseal from a Kubernetes Secret.** Demos would survive reboots, but anyone with cluster admin could unseal
  Vault, which defeats the seal. Transit or cloud-KMS auto-unseal needs a second Vault or a cloud account.
- **Vault in Docker on tools-01.** ADR 0026's placement stands: the VIP is planned, cert-manager and Kubernetes
  auth are in the cluster, and Vault's writes do not change the disk picture.
- **Three Raft replicas.** See decision 3.

## Consequences

- **Vault becomes a dependency of every device job.** Sealed or unreachable, it stops Gateway resolving the
  device password, so no device can be reached. Every k3s node restart that moves the Vault pod needs the owner to
  unseal. The dev probe measures what a job shows in that state, and every device-sending task must still reach
  the workflow's end (ADR 0059), or an agent twin hangs. E14 is the eval.
- Rotation becomes a Vault write: the new value is used on the next job, and nothing on the Platform changes. E15
  proves the value is not cached.
- With no plaintext device password in the inventory, the reason `svc-copilot` has no Inventory Manager role on
  production is gone. Revisiting that is a separate decision.
- The Platform's secret ID is the one credential outside Vault. Its protection is the file mode on two hosts and
  the address binding in Vault.
- `.env` still holds every secret until Phase 9b.
- The PID risk table gains a row for a sealed or lost Vault. The single-SSD row now covers Vault's data through
  decision 8.

## Measured on the dev tier (2026-09-23)

The probe answered decision 6's open questions and found five things the production build must do:

| Question | Answer, measured on dev (Platform 6.5.2, Gateway 5.5.2, Vault 2.0.4) |
|---|---|
| Who resolves `$GATEWAYSECRET_`? | The Gateway **server**; the runner never contacts Vault. The server then hands the plaintext to the runner over the runner gRPC, which is not TLS in this lab (a known exposure on one host's Docker network) |
| Where does the secret-ID file live? | In the server's `/etc/gateway` volume |
| How is the provider configured? | Through the Platform: `POST /gateway_manager/v1/gateways/<cluster>/configuration/import` with the `iagctl db import` YAML. `iagctl create secret-provider` refuses server mode and client mode needs the Gateway's own admin login. The import accepts unknown fields silently, even in `check` mode, so every import is proven by `.../configuration/export` |
| Does the Gateway trust the lab CA? | No, and the vault provider has no CA flag: the server gets the CA through `SSL_CERT_DIR=/etc/ssl/certs:/etc/ssl/lab` |
| Is a provider change picked up live? | No: the server caches providers until it restarts, and every server start re-registers the netsdk services with its musl pex path, so the runner must restart after it (as `platform-ha2-gateway.yml` already does) |
| Device paths with an alias | Gateway `send-command` and Configuration Manager through the InventoryBroker (`isAlive`, configuration read) both work |
| The Platform's client | Reads `ITENTIAL_VAULT_*` from the stack environment; needs `NODE_EXTRA_CA_CERTS` for the lab CA; logs in with AppRole at start and again at every adapter start. `ITENTIAL_VAULT_READ_ONLY=true` works |
| Adapter property | `$SECRET_services/netbox $KEY_token` works |
| Integration instance | Only a reference that is the **entire** value resolves: `Token $SECRET_...` is encrypted verbatim and NetBox answers 403. Vault therefore holds the whole header as `services/netbox` key `header`, and both integration instances keep executing directly from the Platform |

To fix before production: `tasks/platform-assets.yml` compares inventory nodes by name and driver options only,
so a password-to-alias change would be skipped; and `wf-netbox-devices-v1` has no error edge after its NetBox call,
so a failed read (a sealed Vault, for one) dead-ends the job, the ADR 0059 hazard for the agent twins.

## E14 on the dev tier: what a sealed Vault did (2026-09-23)

`verify/test-09a-vault-dev.sh` seals the dev Vault, runs a device job and a NetBox job, and unseals it. The first run
found that "fails closed" was not true yet:

- **A silent failure on every Gateway device task.** With Vault sealed, the Gateway answers a JSON-RPC error
  (`KV read ... returned 503: Vault is sealed`) and the Platform's `sendCommand`/`sendConfig` task still finishes
  `success`. `wf-show-version-v1` ended `complete` with the error object stored as its "show version". The error
  edges ADR 0059 added only ever caught a *thrown* task (a 404 for an unknown node), never this.
- **The only write path dead-ended.** `wf-config-push-v1`'s "config applied?" evaluation had no failure edge by
  deliberate design, so a rejected push, or a sealed Vault, left a retryable errored job that hangs a calling agent.
  Owner decision 2026-09-23: it now ends cleanly with `changed = false` and the reason. The approval-reject path is a
  separate decision and is unchanged.
- **A Platform 6.5.2 bug in its Vault error path**: with Vault sealed, an integration call fails with
  `ReferenceError: log is not defined at Encryption.decryptProperties`. The job still ended cleanly through the error
  edge `wf-netbox-devices-v1` gained today. Worth reporting to Itential.

The fix, one pattern in four workflows: every `sendCommand`/`sendConfig` is followed by an evaluation of its result
(`results[0].success`, or the envelope `status` for the multi-device `wf-show-all-v1`), and every evaluation's
failure reaches `workflow_end` through a note that publishes `device_error`. `tests/test_vault.py` holds every
workflow to it. Still to audit against the same rule: the integration and Configuration Manager tasks of
`wf-branch-vlan-v1` and `wf-compliance-report-v1`.

## What production has that dev does not (for step 5)

- The `netbox-latest` integration (the NX-OS pack's `NetBox:latest` model, made by hand, not managed by
  `integrations.yml`). Owner decision 2026-09-23: **it is brought under Vault.** `tasks/vault-external-integrations.yml`
  (listed in `vault.external_integrations`) replaces only its authentication value with the whole-header reference
  and writes every other property back unchanged; it never creates or deletes an instance. Tested on dev against a
  throwaway instance of the same kind (credential replaced, marker, server and TLS kept, second run unchanged, then
  deleted). It will use the token in Vault, the lab's main NetBox token: production's copy is `$ENC`, so whether the
  NX-OS pack was set up with the same token cannot be read back. S8.3a checks it wherever it exists.
- `servicenow-api` (ServiceNow is off on dev): the `$SECRET_services/servicenow $KEY_password` reference is untested;
  the cut-over needs a read-only ServiceNow call as a check.
- The `lab-hosts` inventory (3 Ubuntu hosts; empty on dev) and the `nxos` mocks: the host-node path has only run in
  unit tests.
- The Gateway runs on its own host, iag-01: `tasks/gateway-vault.yml`'s `docker exec` steps run there, not on the
  Platform host.
- Two Platform nodes (iap-02's container is stopped on purpose): both need the Vault environment.

## Bound addresses, measured on the dev tier (2026-09-23)

A temporary file audit device (enabled for the measurement, then disabled and its log removed) showed every request
from the dev VM's containers (the Platform's AppRole login and its NetBox read, the Gateway's device-password
read) arriving from **one address: the gateway of the vault-dev Docker network**. Docker relays traffic to a port
published on the same host, so the containers' own addresses never reach Vault. Consequences:

- **On dev, binding means "only from itential-dev"**, not "only from the Platform" or "only from the Gateway": both
  readers share the VM and arrive from the same address. That is still the boundary that matters. A valid secret
  ID used from any other machine is refused (S8.2e: HTTP 400 from the workstation).
- **The vault-dev network's subnet is pinned** (`vault.dev.docker_subnet`, 172.31.65.0/24, gateway 172.31.65.1), so
  the bound address is deliberate. Changing it means recreating the network once.
- Vault stores a single-host `token_bound_cidrs` entry **without its `/32`**, but keeps it on
  `secret_id_bound_cidrs`. `tasks/vault-config.yml` compares both sides without it; otherwise the roles would be
  rewritten on every run.
- **For production (step 3): the Vault service on k3s needs `externalTrafficPolicy: Local`.** With the default
  (`Cluster`), a request can be forwarded between nodes and arrive from a node address, and binding to iap-01/02
  and iag-01 would never match. Measure it the same way (temporary audit device) before binding production's roles.
- S8.2c now proves the policy boundary with short-lived tokens Vault issues with each reader's policy, because the
  AppRole logins themselves can no longer be used from the workstation.

## Audit of wf-compliance-report-v1 and wf-branch-vlan-v1 (2026-09-23, owner decisions)

- **`wf-compliance-report-v1` could report a false "compliant".** Its summary computed `compliant = not bad` over the
  reports that existed, so a run in which no device's configuration could be read (a sealed Vault) returned
  `"compliant": true, "devices_checked": 0`. Now `compliant` needs at least one device, every device must have been
  evaluated (some pass, error or warning), an unevaluated device is listed in `devices_not_checked`, and an empty run
  carries an `error`. Its ten Configuration Manager and runner calls, which could dead-end, and the "run not complete
  after the last attempt" case, which ended in error by design, now end cleanly with `report_error` (a read-only
  agent tool, the same rule as the config push). How Configuration Manager records a device whose configuration
  could not be read is not measured: dev has no compliance plans and production has no Vault yet.
- **`wf-branch-vlan-v1` keeps its designed error-end** (rollback on reject or failed push, then error). The one
  external call between the NetBox reservation and the push, `e6` (the ServiceNow work note), now rolls back on an
  error instead of dead-ending with the VLAN reserved. Calls before the reservation have changed nothing; calls after
  the push must not roll back, because the VLAN is live on the switch. Neither kind changes. Limit: with Vault
  sealed, the rollback itself (a NetBox delete) cannot read its credential either, so a sealed Vault between the
  reservation and the push still leaves the reservation. The caller waiting on an errored job (an agent, or a
  Lifecycle Manager action that stays `running`) is recorded, not solved: that belongs with a decision on its
  Lifecycle Manager semantics.
- Neither workflow can run on the dev tier (no compliance plans, no ServiceNow): the fixes are proven by tests and
  by the Platform accepting both on import (a workflow it disagrees with is left as a draft).

## Found while checking device configs (2026-09-24)

- **Both dev C8000v routers still accepted the vendor default `admin`/`admin`, stored in plain text.** vrnetlab creates
  `admin` with a type-0 `password`, and IOS-XE refuses a `secret` for a user that already has one, so the template's
  replacement line never applied. The unit test that "proved" it rendered the template, not the device. Fixed on both
  routers through Gateway `send-config` (`no username admin`, the `[confirm]` answered on its own line, the user
  re-created with the lab password as `secret 9`, saved) and in `clab/configs/c8000v.cfg.j2`. PID S10.7 now tries
  `admin`/`admin` on every node and passes only on a refusal. Production's 12 devices were checked the same day, GET
  only: each holds just the hashed `automation` user.
- **`send-config` reports `success: true` for lines the device rejects** (IOS-XE answered `% Invalid input detected`;
  measured). So `wf-config-push-v1`'s "config applied?" catches a Gateway error such as a sealed Vault, but not a
  router rejecting the lines. The workflow's comment and message now say so; detecting a rejection needs a check of
  the device's reply and is a separate decision.
- Device configs, and Configuration Manager's copies of them, still carry the automation account's **hash** (Arista
  sha512-crypt, IOS-XE type 9). Vault moves the password out of the automation platform, not out of the device.
  Removing the local account needs central device login (TACACS+), which belongs to the identity phase.

## Production Vault, step 3 (2026-09-24)

`make vault` installed Vault 2.0.4 (chart 0.34.1) in namespace `vault`: one server with Raft on a 2 Gi Longhorn
volume, a lab-CA certificate from cert-manager for `vault.lab.internal`, the in-cluster names and 10.100.0.41, and a
MetalLB LoadBalancer on 10.100.0.41 with `externalTrafficPolicy: Local` (`k8s/vault/values.yaml`). The owner ran
`make vault-init` (one key share, written with the root token to a mode-600 file outside the repo, nothing printed)
and `make vault-unseal` in their own terminal. Measured and decided along the way:

- **The readiness probe accepts sealed and uninitialised** (`/v1/sys/health?standbyok=true&sealedcode=204&uninitcode=204`).
  The chart's default (`vault status`) takes a sealed pod out of the Service, and a sealed Vault behind a VIP with
  no endpoints cannot be unsealed through the VIP.
- **The binding is proven on the real path, not with an audit device** (`make vault-config`, `vault-prod-config.yml`):
  a one-use, five-minute secret ID per reader host logs in from that host, and one per role from the workstation is
  refused. Result: iap-01 and iap-02 accepted for `itential-platform`, iag-01 for `itential-gateway`, the
  workstation refused for both. With `externalTrafficPolicy: Local` the hosts' own addresses reach Vault.
- **A refused login still spends a one-use secret ID**; destroying it afterwards answers 500 "failed to find accessor
  entry", which the clean-up treats as the outcome wanted.
- **The root token was used once and revoked** (`make vault-revoke-root`): a lookup with it answers 403 and it is gone
  from the init file, which now holds only the unseal key. Administrator work (the cut-over's secret IDs, the
  verify's policy, binding and seed checks) takes a fresh token from `vault operator generate-root` and the unseal
  key; without one those checks skip, never pass.
- **Snapshots**: `make vault-snapshot` writes `~/Backups/itential-enterprise-lab/vault/vault-raft-<ts>.snap` (mode 600,
  36 KB), and `make vault-config` takes one at the end.
- **Sealed signal**: a web check on `/v1/sys/health` (200 only when unsealed) feeds the blackbox-http probe and the
  Zabbix `lab-web-ui` scenario; `VaultSealed` fires after a minute (critical), notifying nobody until S7 has a
  receiver. `probe_success{ui="vault"}` = 1 after the apply.
- `verify/test-09a-vault.sh`: 7/7 with the root token, 4 pass + 3 skipped after the revoke.
- The Platform and the Gateway still hold their credentials. S8.3 and the switch of `vault_enabled` for production
  are the cut-over (step 5), approved separately.

## Rebuilt with an administrator login, then the cut-over, step 5 (2026-09-24)

**Vault 2.0 authenticates generate-root.** After step 3 revoked the root token, `sys/generate-root/attempt` answered
403 without a token (so did `sys/rekey`): Vault 2.0 authenticates those endpoint families unless the server config
lists them in `enable_unauthenticated_access`. Production was left with no administrator path. Enabling
unauthenticated generate-root was rejected (it weakens the server permanently); instead Vault was rebuilt (nothing
depended on it yet; everything in it comes from the repo and `.env`) with **an administrator login for the owner**:

- userpass login `lab-admin` (`versions.yaml` `vault.prod.admin`): the play makes the mount and the policy, the owner
  sets the password (`make vault-admin-user`), `make vault-login` writes a one-hour token to a mode-600 file and
  `make vault-logout` revokes it;
- **least privilege**: the readers' role IDs and secret IDs, the reader roles and their policies (not its own), the
  `lab/` secrets, snapshots, and reader-policy tokens only through the `verify-readers` token role. It cannot seal
  Vault, change auth methods or mounts, or widen itself;
- `make vault-revoke-root` **refuses unless the administrator login works**. Measured: with the init file hidden,
  `lab-admin` alone passed S8.2a-g and took a snapshot; then the root token was revoked (403).

**The cut-over** (`make vault-cutover`, the owner's login token read from its file, never printed):

- `vault_enabled: true` in both production sources, `itential/ha2/versions.yaml` (the HA2 plays) and
  `vars/itential-prod.yml` (the replay); `tests/test_vault.py` holds them equal, so no production target can put the
  plaintext back by leaving the switch out.
- Platform nodes: the AppRole credentials in each node's mode-600 `.env`, the `ITENTIAL_VAULT_*` client in the
  compose file, `NODE_EXTRA_CA_CERTS` pointing at the lab CA each node already mounts for MongoDB. iap-02 started with
  it, served its login page and was parked again, as designed.
- Gateway on iag-01: the lab CA and `SSL_CERT_DIR`, then `tasks/gateway-vault.yml` unchanged in substance (the
  Platform API is reached from iag-01).
- Later replays need no token: a reader keeps the role and secret ID it holds; one holding nothing stops the play.
- Found on the way: **a stray `/opt/itential/compose.override.yml` on iap-01** (dated 2026-09-11, a copy of the dev
  stack's override) broke `docker compose up` there; the first run stopped before touching the running Platform. The
  owner moved it aside (`compose.override.yml.stray-2026-09-11`) and the second run completed.
- Result: `verify/test-09a-vault.sh` **12/12** - S8.2a-g and S8.3a-e: every lab and lab-hosts node carries
  `$GATEWAYSECRET_(lab-automation-password)`, the NetBox adapter and both NetBox integrations carry `$SECRET_`
  references, the Gateway provider matches the oracle, NetBox reads through Vault, all 12 devices (`show clock`)
  and all 3 hosts (`hostname`) log in with the Vault-held password.
- Not covered by the cut-over: the `nxos` inventory (made by the NX-OS pack's own workflow) keeps its credentials.
- Roll back: the three plays with `-e vault_enabled=false`; the credentials stay in `.env` until Phase 9b.
- The full production verify after the cut-over (`verify/run.sh`, 2026-09-24 20:03 UTC) is green apart from two
  criteria unrelated to Vault that were already failing (S7.1: the NX-OS switches are not in Zabbix; S11.8: it still
  expects dev VM 205 retired, which ADR 0063 brought back): every workflow's device login through the alias, the
  ServiceNow change lifecycle through its `$SECRET_` reference, Lifecycle Manager, NetBox, the integrations and every
  agent, Claude-backed and local, pass.

## Amendment 2026-09-24: Phase 9b deferred (PID 1.35, owner decision)

Phase 9b (S8 criterion 4, `.env` reduced to the Proxmox token and the Vault address and unseal reference; criterion 5,
cert-manager issuing from the Vault PKI) is deferred, not scheduled. Phase 9a already delivers what Vault is for in
this lab: the Platform and the Gateway hold no credential, and they read them read-only through address-bound logins.
What 9b adds does not pay for itself here:

- `.env` lives on one workstation, is ignored by git and checked by gitleaks on every commit. Taking secrets off many
  people's machines and CI runners, the enterprise reason for the move, does not apply to a lab with one operator.
- Vault runs on k3s with one Raft replica on a single SSD. Made the only source, a lost disk would leave every service
  secret depending on a snapshot restore. With `.env` as the source, a lost Vault is rebuilt and re-seeded from it (`make vault-init`, `vault-unseal`,
  `vault-admin-user`, `vault-login`, `vault-config`, then `vault-cutover` for fresh secret IDs).
- Every `make` target would need an administrator login first (one-hour tokens), and the plays that build k3s could
  not read secrets from a Vault that runs on it.
- The lab CA already issues every certificate through cert-manager; a Vault PKI issuer would look the same to a
  client.

So `.env` stays the source and Vault a read-only copy seeded one way from it (decision 10 stands without an end
date); the LDAP bind password stays `$ENC`. `verify/test-09b-secrets.sh` is not written. Rotating the `automation`
password does not depend on 9b: change it on the devices and in `.env`, then `make vault-login` and
`make vault-config`, which rewrites only the values that differ.
