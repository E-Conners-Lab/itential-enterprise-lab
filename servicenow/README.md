# servicenow/ — what the PDI needs for Phase 5 (S4b)

The ServiceNow side of `wf-branch-vlan-v1` uses **stock** objects of a Personal Developer
Instance only; nothing was customised, so there is no update set to export (PID S4b.3,
amended in PID 1.5). Rebuilding a reclaimed PDI is manual step 7 plus the two records below,
which exist in the demo data of every PDI on the Zurich/Australia families:

| Object | Value | Used by |
|---|---|---|
| Standard change template | `Change VLAN on a Cisco switchport` (`b1c8d15147810200e90d87e8dee490f7`) | `POST /api/sn_chg_rest/change/standard/{id}` in task `d1` |
| Assignment group | `Network` (`287ebd7da9fe198100f92cc8d1d2154e`) | the change model refuses every state move without one |
| Integration user | `itential.integration` with `itil`, `snc_platform_rest_api_access`, `rest_api_explorer`, `snc_basic_auth_api_access` | `adapter-servicenow` basic auth (`.env` `SNOW_*`) |

State walk used by the workflow (standard change model): New `-5` -> Scheduled `-2` ->
Implement `-1` -> Review `0` -> Closed `3` (`close_code: successful`), work note on the table
API. `sys_audit` is not readable by a non-admin user, so the workflow records every state
ServiceNow returns in job variables and `verify/test-05-itential.sh` S4b.2 checks those plus
the change read back with display values.

`verify/` S4b.3 checks that this file exists and that the template and the group are present on
the instance (the two ids above), which is what "re-imports cleanly into a fresh PDI" reduces to
when nothing is customised. Reclamation: the owner logs in interactively at least every 10 days
(manual step 8); S4b.5 reports the admin's last login.
