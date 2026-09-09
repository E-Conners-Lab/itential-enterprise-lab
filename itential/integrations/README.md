# Integration Models (PID S4d.4, ADR 0045)

`python itential/integrations/build.py` writes one OpenAPI 3 document per model from an operation list;
`--check` fails when a file differs (`tests/test_integrations.py`). `ansible/playbooks/tasks/integrations.yml`
validates and creates the models, creates the integrations (virtual adapters) with credentials from `.env`,
re-syncs the roles the models register, and asserts every operation is an authorized tool.

| Document | Model id | Integration | Operations |
|---|---|---|---|
| `lab-netbox.json` | `lab-netbox:1.0.0` | `netbox-api` (token from `NETBOX_TOKEN`) | six read operations: devices, device by id, interfaces, IP addresses, VLANs, sites |
| `lab-servicenow.json` | `lab-servicenow:1.0.0` | `servicenow-api` (basic auth `SNOW_USER`, host from `SNOW_INSTANCE`) | incidents (list, get, update), change requests (list, get) |

Tool ids: `integration:<title>%3A<version>:<instance>:<operationId>`; agent documents reference them as
`{reference: <operationId>, kind: integration, model: netbox|servicenow}`. Never call `/adapters/<name>/start`
on an integration (it is not a process; the call only flips its health to STOPPED).
