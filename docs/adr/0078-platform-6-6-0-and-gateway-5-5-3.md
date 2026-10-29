# 0078 — Platform 6.6.0 and Gateway 5.5.3 replace 6.5.2 and 5.5.2

- **Status:** accepted (owner, 2026-10-10); amended 2026-10-29 (Platform 6.6.1, the `ViewData` correction)
- **Date:** 2026-10-10
- **Amends:** ADR 0020 (amendment: container images from Itential's private ECR), manifest 3.5
- **Related:** ADR 0035 (Gateway 5 is the gateway), ADR 0038 (the runner image), ADR 0053 (the production environment),
  ADR 0065 (Vault), ADR 0074 (FlowMCP on the Gateway)

## Context

Itential pushed two releases to the private ECR on 2026-10-07, listed on 2026-10-10 with the owner's SSO session
(`aws ecr describe-images --profile product-ecr-pull-all-497639811223`):

- `automation-platform-config-lcm-flowai` **6.6.0** (digest `sha256:ab18e774eb52…`, 826 MB; the short tags `6`, `6.6`
  and `6.6.0` all name `6.6.0-ecm-6.6.0-fai-6.6.0-gm-1.18.2-lcm-6.6.0-1`; an earlier push the same day carried
  `fai-1.2.0` and was superseded).
- `automation-gateway5` **5.5.3-amd64** (digest `sha256:e98c8b40c6e5…`, 381 MB; also tagged `5.5` and `stable-5-amd64`).

Platform 6.6.0 (changelog 2026-10-07: 21 enhancements, 44 bug fixes, 40 security fixes) brings Event Auditing (an
immutable who/what/when trail that records FlowAI agent actions beside human ones), an OpenTelemetry web server metrics
endpoint at `/metrics/webserver`, validation APIs across Configuration Manager, Gateway Manager, Inventory Manager, MOP,
Studio and Templates, deep validation on workflow import, and fixes the lab has brushed against: MongoDB oplog growth in
looped workflows, Redis rolling-restart handling, FlowAI tool discovery isolating a failing tool, a critical proxy-addr IP
spoofing fix in Work Center (CVE-2026-90711). Its one breaking change is **Node 20 to Node 22**: the images bundle Node 22
and nothing in the container can stay on 20. Operations Manager now **requires `componentType` whenever `componentId` or
`componentName` is sent** (ENG-26443), and manual-trigger `formId`/`formData` are validated at write time (ENG-26440).

Gateway 5.5.3 is a maintenance release with no configuration changes: `sshpass` in the container image (ENG-28306),
`iagctl login` writes `api.key` as 0600 and repairs an existing file (ENG-26731), the archived mapstructure replaced
(ENG-26702), grpc / x/crypto / mcp-go updated with a fix for SSH channel deadlocks against git remotes and devices
(ENG-27907).

Requirements otherwise unchanged: MongoDB 6.0 to 8.0 (the lab pins 7.0.40), Redis 7.0 to 7.4 (7.4.11), Python 3.11 in the
image. The published Gateway compatibility matrix stops at 5.4; the rule it states is that every Gateway 5.x works with
Platform 6.

Two things the notes do not settle, recorded as observed: the image tag says Gateway Manager **1.18.2** while the changelog's
component table says 1.2.4 (the tag is taken as the truth); and Itential documents the RPM upgrade only, so the container
procedure below is the lab's own (the same plays that built each tier).

## Decision (owner, 2026-10-10)

1. **Both images, dev first.** The pins move together: `images.platform` to `6.6.0` by digest, `images.gateway5` to
   `5.5.3-amd64` by digest, `stack.runner_image` to a `5.5.3` tag (its name carries the Gateway version the `iagctl` binary
   came from, so the runner is rebuilt). Dev proves the two things an image swap does not undo: the schema migration the
   Platform runs on first start, and the adapter's `node_modules` under Node 22 (deleted on dev before the play so the
   image's own npm reinstalls them; adapter-netbox has no native addons).
2. **iap-02 stays stopped.** The production Platform play runs with `--limit iap-01`; the standby keeps its exited 6.5.2
   container and its loaded 6.6.0 image until the owner decides to bring it up (the Gateway Manager single-connection
   finding, ADR 0053, is why it is stopped).
3. **The five trigger re-point tasks send `componentType`.** The PATCH that re-points an existing automation at a
   re-imported workflow (`tasks/aws-vpn-triggers.yml`, `outage-trigger.yml`, `golden-config.yml`, `mop.yml`,
   `aws-vpn-schedule.yml`) carried only `componentId` and `componentName`; 6.6.0 rejects that. The creates already sent it.
   This is the path every dev converge pair takes (workflows re-import with new IDs), so without it the first converge
   after the upgrade fails.
4. **Pre-upgrade dump, by hand.** The nightly dump on mongo-03 runs at 03:20Z; before the production swap the same script
   is run once more so the rollback copy is minutes old. Rollback is the previous pins and the two production plays; if
   6.5.2 refuses the migrated schema, the dump is restored first (ADR 0058's procedure).
5. **Order.** Repo edits and the test suite; `images/fetch.sh itential` (pull, digest check, tarballs on the Proxmox host),
   `itential-load` (dev) and `itential-load-ha2` (iap-01, iap-02, iag-01); dev converge pair and the dev verify suites;
   then on production: the dump, `make prod-snapshot MODE=save`, no running jobs in Operations Manager, the Platform play
   limited to iap-01, the Gateway play (runner rebuild, services re-imported), the verify suites, the snapshot compare.

## Consequences

- The Platform UI is unavailable for the minutes the container takes to come up on iap-01; the runner is down for its
  rebuild. Nothing else restarts.
- Two tarballs join `/srv/images/itential/` beside the 6.5.2 and 5.5.2 ones, which stay as the rollback copy.
- Every "measured on 6.5.2" note in the tests and plays is a measurement that may no longer hold; they are left as
  written and re-measured only where a verify suite disagrees.
- `/metrics/webserver` and Event Auditing are available and not yet used; wiring them into the observability stack is a
  later decision.

## As built (2026-10-10)

Measured on the day, dev first, then production:

- **Dev** (`make phase-flowai`): the stack play 136 ok / 13 changed, the platform play 54 ok / 2 changed, both failed=0;
  the Platform on 6.6.0 with Node 22.23.2, the Gateway on 5.5.3, the runner rebuilt; adapter-netbox's `node_modules`
  reinstalled by the new image's npm (no native addons). The MOP backup automation's re-point PATCH, now with
  `componentType`, succeeded against 6.6.0. The FlowAI play stopped at its Ollama pre-check (the Mac's inference
  host does not answer NAT'd lab VMs; unrelated, pre-existing since about 2026-10-08). Dev verify: S12.1 to S12.6
  and S12.9 pass once the MCP container is restarted, S8.2 and S8.3 pass.
- **Production**: dump at 16:57Z, snapshot saved, 1447 jobs and none running. The Platform play limited to iap-01:
  19 ok / 2 changed, the container healthy on 6.6.0 in **47 seconds** end to end. The Gateway play: 5.5.3 and the
  runner up in about two minutes, then its last section refused until the Vault administrator token was renewed
  (the saved token had expired; `make vault-login`, then 52 ok / 1 changed / failed=0). The MCP container on
  tools-01 restarted for its session (it had been answering 401 for days already, from an earlier Platform restart).
- **What 6.6.0 changed that the notes did not say:** four collections added to the `itential` database on first
  start (76 to 80; S11.9 red until a post-upgrade dump was taken), four roles added to the administrator group
  (192 to 196; the only difference the snapshot compare reports).
- **Verify on production:** test-05 11/12 (S4.1 6.6.0, device commands through the Gateway, Add Branch VLAN with
  approval, S4.7 MCP 6.6.0; the one red is the PDI's interactive-login age, owner action), test-08 7/7, test-06d
  5/5, test-06b green after the ServiceNow operation oracle caught up with `createIncident` (R6 drift since
  2026-10-05, S4d.4), test-06 green except S4c.5 (the Ollama path). k3s S2.3 is red because the workstation's
  kubeconfig names a LAN address the nodes no longer hold; the API answers on its OOB VIP. None of the red is the
  upgrade.
- **Found the same evening, not in the release notes:** a `ViewData` manual task cannot be opened in the 6.6.0 UI.
  Work Center and the legacy Operations Manager dialog both show "There was a problem loading this task", and the
  browser console shows the task view's federated bundle (`workflow_engine/task/ViewData/remoteEntry.js`) throwing
  `TypeError: Cannot read properties of undefined (reading 'call')` as Work Center's `federatedFrame` loads it;
  every chunk downloads with 200, a hard reload changes nothing. Push Configuration with Approval's card (task
  2a) is the one affected here; it still finishes through the API (`POST /operations-manager/jobs/<job>/tasks/2a/finish`,
  which is how `verify/test-06-flowai.sh` approves it). The branded InteractiveHTML cards of ADR 0077 are fine:
  a Push Configuration with Revert Timer card rendered in Work Center and its Reject button ended the job
  (`changed = false`). Reported to Itential as an owner action; the workaround is the API or a cancel.
- The procedure is runbook chapter 11.

## Amendment 2026-10-29 — Platform 6.6.1 replaces 6.6.0

Itential withdrew the 6.6.0 artifacts: the tag is gone from the ECR, and the staged tarball under
`/srv/images/itential/` is the only copy left (kept as the rollback). 6.6.1 was pushed on 2026-10-15, the same digest
as the descriptive tag `6.6.1-ecm-6.6.1-fai-1.2.0-gm-1.18.2-lcm-6.6.1-1` (Node 22), and is pinned by digest in
`itential/versions.yaml`. A 6.6.1.1 exists only as a cloud release, with no image to pull. Gateway 5.5.3 is still the
newest Gateway, so its pin stays.

The 6.6.1 notes tighten Operations Manager validation (ENG-26440 manual trigger `formId`/`formData`, ENG-26441
`componentName`-only updates, ENG-26442 read access requiring write on `createAutomation`) and fix Run Service
(ENG-28541, ENG-28436). The dev converge, which re-points every trigger, passed without a refusal; the three stay
watch items for production.

**Dev, as built 2026-10-29:** the image loaded on dev and both production nodes (image ID equal everywhere); the dev
converge (`itential.yml` → `platform.yml` → `flowai.yml`) ran 05:23–05:33Z with no failed or ignored task; MCP
restarted; `make verify-dev` passed 33 checks with 0 failures (test-12a 7, test-05b 7 incl. S12.2 and S12.9 on 6.6.1,
test-09a-dev 13, test-16 6).

**Correction to the 2026-10-10 `ViewData` finding above.** The card was never broken by the release. The browser
had cached old chunks of the task view. The Platform serves them with `Cache-Control: public, max-age=0`, a fixed
`Last-Modified` of 1985-10-26 08:15 UTC and a weak ETag built from size and that time (`W/"af17-7438674ba0"` is
44823 bytes plus that timestamp). So a chunk that changed between releases but kept its byte length revalidates
with 304, and the browser keeps the old code; a hard reload does not reach chunks the federated frame loads later.
Proven on 2026-10-29 with a Push Configuration with Approval card, rejected each time, nothing pushed:
- Production on 6.6.0: seven of the cached `ViewData` chunks differed from the server's, each with the same
  length. The card failed as recorded. With only those seven chunks refreshed, it rendered, and its Reject
  button ended the job on the rejected path.
- Dev on 6.6.1: three cached chunks were stale, with the same result once refreshed.

The InteractiveHTML cards chosen because of the finding (ADR 0077, ADR 0085's fix-by-hand task) still work. Only
the stated reason is wrong. Runbook chapter 11 now treats the browser cache as the first suspect and asks for
cleared site data after every upgrade.

Production follows from `main` after this amendment merges, by runbook chapter 11.
