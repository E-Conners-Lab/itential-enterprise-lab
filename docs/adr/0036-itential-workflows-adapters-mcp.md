# 0036 — Phase 5 automation content: NetBox adapter, Inventory Manager from NetBox, Gateway 5 device services, generated workflows, MCP

- **Status:** accepted
- **Date:** 2026-09-07
- **Related:** ADR 0035 (dev stack on one VM), ADR 0002 (two sources of truth), PID S4

## Context

PID S4 needs a NetBox adapter, device access through the gateway, a first
workflow with an approval task and a NetBox rollback, and an MCP server Claude
Code can reach. Platform 6.5.2's workflow documents (canvasVersion 3) are JSON
with engine-specific conventions that are not documented for hand authoring,
and the built-in local `admin` cannot own gateway resources (ADR 0035).

## Decision

- **Adapters** come from the itentialopensource GitLab at pinned tags
  (`adapter-netbox` v1.0.10, `adapter-servicenow` v3.0.11, `itential/versions.yaml`),
  cloned into the platform's custom services mount and installed with npm inside
  a throw-away platform-image container. Instances are created and configured
  through the API by `ansible/playbooks/itential.yml`; the NetBox token is stored
  encrypted by the platform (`ITENTIAL_ENCRYPTION_KEY`).
- **Device inventory** is the Inventory Manager inventory `lab`, generated from
  NetBox on every play run (active devices with a management address; platform
  `ios-xe` -> netmiko `cisco_ios`, `eos` -> `arista_eos`). Gateway 5's native
  `send-command` / `send-config` services run against it; no Ansible collections
  or netmiko scripts are needed for S4.3 and S4.4. The lab `automation`
  password sits in the node attributes until Vault (Phase 9): accepted SEC
  exception.
- **Workflows are generated**, not drawn: `itential/workflows/build.py` emits
  the three documents and records the engine contract it learned (adapter
  tasks address the model by export and the instance by `adapter_id`; `$var`
  references resolve only at the top level of a task's inputs, so nested JSON is
  built with `replace` + `parse`; outputs are published as job variables; task
  ids are hex). Documents are imported through Automation Studio on every play
  run and are held to the generator by `tests/test_itential.py`.
- **VID allocation** happens in NetBox's per-branch VLAN group (VID 11-99,
  created by the play); the next free VID is picked by a few lines of Python
  run by Gateway 5 (`runCode`) because the NetBox adapter strips the trailing
  slash NetBox's `available-vlans` endpoint requires. The VLAN is created
  `reserved`, approved in a `ViewData` task, pushed with `send-config`, then
  set `active`; a rejection or a device failure deletes the reservation and the
  job ends in error.
- **MCP**: `ghcr.io/itential/itential-mcp` v0.14.0 over streamable HTTP on
  `mcp.lab.internal:8000/mcp`, logging in as the LDAP admin; `.mcp.json` in the
  repo registers it for Claude Code.

## Consequences

- Adding a workflow means adding a function to the generator and a criterion to
  `verify/test-05-itential.sh`; no JSON is edited by hand.
- The Inventory Manager is derived state: NetBox stays the source of truth
  (ADR 0002); re-running the play re-syncs it.
- Gateway 4 stays deployed-not-running (ADR 0035); ~~Golden Config is out of
  scope until a phase needs it~~ (superseded 2026-09-07: Golden Config, compliance and
  device groups are built in Phase 6 on the InventoryBroker devices, ADR 0039/0040).
