# 0045 — Integration Models for NetBox and ServiceNow: generated OpenAPI documents, roles re-synced, operations as agent tools

- **Status:** accepted
- **Date:** 2026-09-08
- **Related:** ADR 0036 (adapters), ADR 0037 (agents and tools), PID S4d.4

## Context

PID S4d.4 wants the ServiceNow PDI and NetBox as Integration Models (OpenAPI documents the platform turns
into virtual adapters) registered as agent tools, with an agent reading an incident and a device through
them. The open-source adapters stay for the workflows (ADR 0036). Measured on 2026-09-08 with a throwaway
model (memory `itential-platform-lessons`):

- A model is `PUT /integration-models/validation` then `POST /integration-models` with `{model: <OpenAPI 3
  document>}`; its id is `<info.title>:<info.version>`; the read of a model returns the credential template
  the security scheme implies (`{tokenAuth: {value}}` for an apiKey header, `{BasicAuth: {username, password}}`
  for http basic). An integration is `POST /integrations` with the platform's service document (`name`,
  `model "@itential/adapter_<title>:<version>"`, `type Adapter`, `virtual true`, `properties.type
  "<title>:<version>"`, `properties.properties` with `authentication`, `server`, `tls`, `variables`,
  `version`); `PUT /integrations/<name>/properties` updates it. It is not a process: `PUT
  /adapters/<name>/start` fails with a path error and leaves its health `STOPPED`, and the operations
  still execute.
- A workflow task calls an operation with `app = "<title>:<version>"` (the model's export) and `adapter_id
  = <instance>`; the result is `{ok, url, status, headers, data}`.
- Tool discovery registers every operation as `integration:<title>%3A<version>:<instance>:<operationId>`,
  but the model also registers roles `<title>:<version>` admin/get, and until they are on the admin's account
  the tools are hidden from the list and "not authorized" on a read. The same role re-sync `itential.yml` runs
  after an adapter install fixes it.
- NetBox's own OpenAPI document is 322 paths and 14 MB (369 filter parameters on the device list); the PDI
  publishes none. An agent needs a handful of read operations with a handful of parameters.

## Decision

- **Documents generated, not copied.** `itential/integrations/build.py` writes `lab-netbox.json` (six read
  operations: devices, one device, interfaces, IP addresses, VLANs, sites, each with the useful filters as
  repeated query parameters) and `lab-servicenow.json` (incident list, one incident, incident update with
  work notes, change list, one change) from an operation list; `--check` holds the files to the generator.
  The ServiceNow document carries a placeholder server; the instance carries the PDI host from `.env`.
- **`tasks/integrations.yml`** (element 4 of `platform.yml`): validate and create each model (update in place
  through `PUT /integration-models` when the operations changed), create each integration once and write its
  server and credentials from `.env` on every run like the adapter instances, re-sync the model roles to
  `admin_group` and the LDAP admin, log in again, run tool discovery and assert every declared operation is an
  authorized tool. Never the adapter start route.
- **Agent tools:** `tasks/flowai-agent.yml` resolves `kind: integration` with `model: <key>` to the
  registry id above. `lab-netops` gets the six NetBox reads and the four ServiceNow reads in place of the two
  adapter methods; `updateIncident` is registered but given to no agent until the diagnostics agent of S4d.5.
- **Verification** S4d.4 in `verify/test-06b-platform.sh`: models with the declared operations, instances with
  the declared server, every tool authorized, then two `lab-netops` sessions: the site and role of br1-sw01
  (NetBox is the second source) and the short description of INC0000060 (the PDI's Table API is the second
  source), each session's tool calls being integration operations and never adapter methods; tokens printed.

## Consequences

- Two integrations run inside the platform's integration worker pool (`integration_thread_count`), no new
  container. The NetBox token and the PDI password are stored encrypted by the platform, sourced from `.env`.
- `platform.yml` reports the two property writes as changed on every run (credentials are always re-applied).
- Rejected: uploading NetBox's full schema (hundreds of tools, most of them writes); pointing the
  integrations at the adapters' methods (the point is the OpenAPI path); a separate agent for the acceptance
  (the fleet of S4d.5 takes the tools over from `lab-netops`).
