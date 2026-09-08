# 0042 — Pre/post check command templates (MOP, read-only) per OS and nightly backups through a forEach workflow

- **Status:** accepted
- **Date:** 2026-09-07
- **Related:** ADR 0039 (devices through InventoryBroker), ADR 0040 (platform.yml, schedule triggers), PID S4d.2

## Context

S4d.2 wants pre/post check templates per vendor (version, interfaces up, routing neighbours) with
pass/fail rules, and a nightly backup of every device. The Platform's MOP application holds command
templates (show commands + rules evaluated per device) and analytic templates (a value extracted
before a change must equal the value after it); both reach devices through Device Broker, so the
InventoryBroker path of ADR 0039 serves them without a gateway change (measured 2026-09-07:
`POST /mop/RunCommand` on br1-sw01 answered through Gateway 5). Configuration Manager's
`backUpDevice` task takes one device; a workflow loop is the WorkFlowEngine `forEach` task (the
`loop` transition starts an iteration, a body task with no outgoing transition returns to the
forEach, `success` fires when the array is exhausted), the pattern of Itential's own "Backup
Configuration" workflow in the builder-skills assets. The topology runs eBGP everywhere and no OSPF
(ADR 0034).

## Decision

- **Documents** `itential/command-templates/<deviceType>.yaml`, one per Golden Config OS type, each
  with a `command_template` (`lab-<type>-checks`) and an `analytic_template` (`lab-<type>-prepost`);
  `tasks/mop.yml` creates them once and replaces them when the document differs. Every command is a
  `show`; MOP never pushes configuration (the builder-skills rule).
- **Rules** are bare regex patterns (the builder-skills `/pattern/` form never matches on 6.5.2,
  measured with a throwaway template; `flags.multiline` works), literal, no `<!variables!>` (a missing variable makes MOP skip the command and count
  it as passed), and every rule is an `error`: 6.5.2 does not store `ignoreWarnings` (measured), so a
  warning-severity rule would fail the template like any other. BGP: a peer in
  `Idle|Active|Connect|OpenSent|OpenConfirm` fails on every device; EOS additionally needs either an
  established peer or `% BGP inactive` (the L2-only branch switches), so a fabric node with a stuck
  peer fails and a branch switch passes. IOS-XE checks `show bgp all summary` (global and the WAN VRF
  in one output). OSPF is not checked: none is configured.
- **Analytic templates** compare the software version and the management/loopback address before and
  after with `type: regex` rules (a capture group extracted from each side and compared; `type: matches`
  ignores the capture on 6.5.2, measured); verify 06b runs the command template twice and the analytic
  template over the two results.
- **Backups**: `wf-backup-all-v1` (no inputs) reads the device list from Configuration Manager
  (`getDevicesFiltered`, so a device added to NetBox is included on the next run), then `forEach` ->
  `backUpDevice` with the broker. An Operations Manager schedule trigger runs it at 02:30 UTC daily,
  half an hour before the compliance plan (03:00), same mechanism as ADR 0040.
- **Verification** S4d.2: each command template on one device per vendor with every rule evaluated
  (no `missing_parameters`), the analytic template green on a pre/post pair, the schedule trigger
  present and enabled, one run of the backup workflow, and for all 12 devices the newest backup's
  text equal to `show running-config` over direct SSH after dropping comment and header lines.

## Consequences

- `itential.yml` re-imports `wf-backup-all-v1` like every `wf-*`; `platform.yml` re-points the
  automation at the new id (PATCH).
- Backups accumulate in MongoDB (12 a night, a few KB each); retention is a Phase 9 concern when
  Oxidized and Gitea take over configuration history.
- Rejected: one template per role (six documents for two vendors); `<!version!>` as a template
  variable (silent pass when unset); one backup automation per device (12 triggers, formData not
  persisted anyway).
