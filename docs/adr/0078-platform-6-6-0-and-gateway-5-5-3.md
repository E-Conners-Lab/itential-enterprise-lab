# 0078 — Platform 6.6.0 and Gateway 5.5.3 replace 6.5.2 and 5.5.2

- **Status:** accepted (owner, 2026-10-10)
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

## As built

*(filled in after the production converge: measured times, verify results, anything 6.6.0 changed that the notes did not say)*
