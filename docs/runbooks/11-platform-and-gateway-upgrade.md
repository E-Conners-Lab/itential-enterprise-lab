# 11 — Upgrading the Platform and the Gateway

Move a running lab from one Itential Platform and Gateway release to the next: dev first, then production,
with a rollback that is real. This chapter is the procedure that took Platform 6.5.2 to 6.6.0 and Gateway
5.5.2 to 5.5.3 on 2026-10-10 ([ADR 0078](../adr/0078-platform-6-6-0-and-gateway-5-5-3.md)), written so the
next bump is the same steps with different numbers.

Nothing here is new tooling. The pins, the fetch script, the plays, the snapshot and the verify suites are the
ones chapters 05 and 08 built; an upgrade is a pin change pushed through them in a fixed order.

---

## Before you start

### What must already be true

- Chapter 08 green: the production environment is up, the nightly MongoDB dump runs on the backup member, and
  `make prod-snapshot MODE=save` works from your workstation.
- Chapter 09 green if you keep a dev tier: the dev stack is where the irreversible parts are proved first.
- A session on Itential's registry: `aws sso login --profile ${ECR_PROFILE}`. The token lasts about twelve
  hours, so log in at the start, not when the pull is due.
- `make vault-login` done in your own terminal (chapter 08). The Gateway play's last section reads Vault with
  the administrator token to check the edge aliases; without it that section refuses and changes nothing, so
  you would have to run the play twice.
- Free space: each Platform image is about 5 GB loaded and 0.8 GB as a tarball; the Gateway about 1.1 GB and
  0.4 GB. Check the hypervisor's staging volume and each node's root disk.

### Read the release notes before you touch a pin

Itential publishes a changelog per release and a feature announcement per minor. Three kinds of entry matter
to this lab and are easy to miss in a long list:

- **Runtime changes.** 6.6.0 moved the Platform from Node 20 to Node 22. The images bundle the runtime, so
  nothing on the host changes, but the open-source adapters' `node_modules` were installed by the old image's
  `npm` and the play's `creates:` guard never reinstalls them. Delete them on dev before the play and let the
  new image rebuild them.
- **API tightening.** 6.6.0 began refusing an Operations Manager update that names `componentId` without
  `componentType`. The lab's trigger re-point tasks sent exactly that, and they run on every converge. Read
  the "enhancements" section for the word *validation*; each entry there is a request shape some play may
  still send.
- **Schema growth.** A new release can add collections on first start. The HA2 verify compares the newest
  dump's collection count with the live database, so the pre-upgrade dump fails that check until a post-upgrade
  dump exists. Expected; take one.

Record the version numbers the release carries in its long tag (the Platform tag names its ECM, FlowAI,
Gateway Manager and LCM versions) in the ADR, and note any disagreement with the changelog's component table
rather than resolving it.

### What the upgrade touches in the repo

| File | Change |
|---|---|
| `itential/versions.yaml` | `images.platform` and `images.gateway5` tag and digest; `stack.runner_image` name (it carries the Gateway version the `iagctl` binary came from, so a new name forces the rebuild) |
| `docs/image-manifest.md` | Section 3.5's "Pinned" column; `tests/test_itential.py` compares it with the pins |
| `ansible/playbooks/tasks/*.yml` | Whatever the release notes said the API now refuses |
| `docs/adr/` and `docs/PID.md` | One ADR for the bump, one history row |

The digests come from the registry listing, never from a tag:

```
aws ecr describe-images --profile ${ECR_PROFILE} --region us-east-2 \
  --repository-name automation-platform-config-lcm-flowai \
  --image-ids imageTag=6.6.0 --query 'imageDetails[0].[imagePushedAt,imageDigest,imageTags]'
```

When a release was pushed twice the same day, the build that carries the short tags (`6`, `6.6`, `6.6.0`) is
the release; an earlier one with different component versions in its long tag was superseded.

---

## The commands, in order

### 1. Pins, tests, lint

Edit the files in the table above on a branch, then:

```
.venv/bin/python -m pytest -q
pre-commit run --files itential/versions.yaml docs/image-manifest.md ansible/playbooks/tasks/*.yml
```

Both must be clean apart from checks that need a deployed AWS side (the AWS VPN target tests error when
nothing is deployed; confirm they error the same way on `main`).

### 2. Pull, verify, stage, load

```
images/fetch.sh itential            # pull both images, compare digests with versions.yaml, tarballs to the hypervisor
images/fetch.sh itential-load       # the dev VM
images/fetch.sh itential-load-ha2   # the production nodes, each only the image its role runs
```

Loading adds an image; nothing restarts. The old tarballs stay on the staging volume as the rollback copy.

### 3. Dev first

```
ssh ubuntu@<dev vm> 'sudo rm -rf /opt/itential/volumes/platform/adapters/adapter-*/node_modules'
make phase-flowai                   # itential.yml -> platform.yml -> flowai.yml
make verify-dev
```

`ansible/playbooks/itential.yml` rewrites the stack's `.env` with the new image names, recreates the
containers, rebuilds the runner from the new Gateway image and reinstalls the adapter dependencies with the
new image's `npm`. `ansible/playbooks/platform.yml` re-points every trigger at the re-imported workflows,
which is where an API tightening shows up. `ansible/playbooks/flowai.yml` needs the inference host; if it is
down the first two plays have already proved the upgrade.

After the Platform restarts, **restart the MCP container**: it holds a session token from the old process
and never logs in again, so every tool answers 401 until it does.

```
ssh ubuntu@<dev vm> 'cd /opt/itential && docker compose restart mcp'
```

### 4. Production

Three things before the swap, in this order:

```
ssh ubuntu@<backup member> 'sudo /usr/local/sbin/mongo-backup.sh'   # a dump from minutes ago, not last night's
make prod-snapshot MODE=save
```

and a read of Operations Manager for jobs that are `running` or `paused`. The outage loops start from alerts,
so a job can appear at any time; a Platform restart under a running job leaves it to be cancelled by hand.

Then the two plays. The Platform play is limited to the active node when the standby is parked (chapter 08):
`docker compose up -d` on the standby would start it.

```
cd ansible
ansible-playbook -i inventory/netbox.yml playbooks/platform-ha2-platform.yml --limit <active node>
ansible-playbook -i inventory/netbox.yml playbooks/platform-ha2-gateway.yml
```

The Gateway play needs `NETBOX_API` exported (or it matches no hosts and exits 0) and `VAULT_TOKEN` from
`make vault-login`. Then the MCP container on the tools VM, for the same reason as on dev:

```
ssh ubuntu@<tools vm> 'cd /opt/itential && docker compose restart mcp'
```

### 5. Post-upgrade dump, verify, compare

```
ssh ubuntu@<backup member> 'sudo /usr/local/sbin/mongo-backup.sh'
verify/test-05-itential.sh
verify/test-06b-platform.sh
verify/test-06d-integrations.sh
verify/test-08-platform-ha2.sh
verify/test-06-flowai.sh
verify/test-09a-vault.sh
make prod-snapshot MODE=compare
```

### 6. Record

Fill the ADR's "as built" section with the measured times and whatever the release changed that the notes
did not say, then the PR.

---

## What "done" looks like

- `/health/server` through the load balancer reports the new release, every application and adapter running.
- The Gateway cluster is connected on the new version and a device command runs through it.
- Every trigger still points at a workflow (the re-point PATCH succeeded on the new API).
- The MCP server answers `get_health` with the new version.
- The snapshot compare shows only what the release itself changed (6.6.0: four roles added to the
  administrator group, nothing else).
- A post-upgrade dump exists and restores to the live collection count.

Measured on 2026-10-10: the Platform container swap on the active node took 47 seconds end to end; the
Gateway play, including the runner rebuild, about two minutes; the whole day, dev and production with every
verify, under three hours.

---

## Verification

| Suite | Criteria that prove the upgrade |
|---|---|
| `verify/test-05b-dev-copilot.sh` | S12.2 (the dev Platform runs the pinned version, Gateway connected), S12.9 (the dev MCP answers with it) |
| `verify/test-05-itential.sh` | S4.1 (production runs the pinned version, Gateway connected), S4.7 (the production MCP answers with it) |
| `verify/test-08-platform-ha2.sh` | S11.4 (the active node serves through the load balancer, the standby is parked), S11.9 (the newest dump restores to the live collection count) |
| `verify/test-06b-platform.sh` | S4d.6 (the Gateway reaches hosts after the runner rebuild) |

The version string every one of these compares against is read from `itential/versions.yaml`, so a verify
that still passes after a pin change is proof, not habit.

---

## Troubleshooting

**Every MCP tool answers `401 Unauthorized ... does not correspond with an active session` after the
upgrade.** The MCP container logged in to the old Platform process and keeps that token; the new process does
not know it. Restart the container. On production this had already happened days before the upgrade for the
same reason (a Platform restart), so check it after *any* Platform restart, not only an upgrade.

**The Gateway play fails `Each edge alias's Vault entry exists before it is bound` with every item censored.**
The administrator token is missing or expired: the task reads Vault with it. Run `make vault-login` in your
own terminal, then the play again. The task is first in its file on purpose: a refusal issues no secret ID
and imports nothing, so the retry is clean.

**S11.9 fails with `restored N collections, live itential has N+4`.** The release added collections on first
start and the newest dump predates it. Take a dump now and rerun; keep the pre-upgrade dump as the rollback.

**`make prod-snapshot MODE=compare` reports the administrator group's role count changed.** New applications
or features carry new roles, which the Platform grants to its administrator group on upgrade. Four on 6.6.0.
Record the number in the ADR; it is not drift.

**A trigger re-point returns 400 on the new release.** The release tightened an Operations Manager request
shape (6.6.0: `componentType` must accompany `componentId`). Compare the PATCH body with the POST body in the
same task file; the create usually already sends the field the update omits.

**The adapter fails to load after a Node major bump.** Its `node_modules` were built by the old runtime and the
play's `creates:` guard skips the install while the directory exists. Delete the directory and rerun.

**`flowai.yml` fails on `Every Ollama endpoint answers`.** The inference host, not the upgrade. The first two
plays of the dev converge have already proved the Platform; fix the host separately.

---

## Tested

| Component | Version |
|---|---|
| Itential Platform | 6.5.2 → 6.6.0 (Node 20 → 22; FlowAI 6.6.0, Gateway Manager 1.18.2 per the image tag, LCM 6.6.0) |
| Itential Gateway 5 | 5.5.2-amd64 → 5.5.3-amd64; runner rebuilt from its `iagctl` |
| MongoDB | 7.0.40 (unchanged; 6.0 to 8.0 supported) |
| Redis | 7.4.11 (unchanged; 7.0 to 7.4 supported) |
| Dev tier | the Copilot dev stack of chapter 09 |
| Production | the eleven-VM environment of chapter 08, standby parked |
