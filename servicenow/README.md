# servicenow/ — what the PDI needs for Phase 5 (S4b)

The ServiceNow side of `Add Branch VLAN` uses **stock** objects of a Personal Developer
Instance only, so it needs no update set (PID S4b.3, amended in PID 1.5); the AWS VPN
workflows add one custom record, kept here as field values. Rebuilding a reclaimed PDI is manual step 7, the AWS VPN change template
further down (the one custom record, ADR 0085), and the two records below, which exist in the
demo data of every PDI on the Zurich/Australia families:

| Object | Value | Used by |
|---|---|---|
| Standard change template | `Change VLAN on a Cisco switchport` (`b1c8d15147810200e90d87e8dee490f7`) | `POST /api/sn_chg_rest/change/standard/{id}` in task `d1` |
| Assignment group | `Network` (`287ebd7da9fe198100f92cc8d1d2154e`) | the change model refuses every state move without one |
| Integration user | `itential.integration` with `itil`, `snc_platform_rest_api_access`, `rest_api_explorer`, `snc_basic_auth_api_access` | `adapter-servicenow` basic auth (`.env` `SNOW_*`) |

## The one custom record: the Lab AWS VPN change template

Deploy, Hand Off and Tear Down AWS VPN (ADR 0085) open their standard change from a template the PDI's demo data does
not have. A rebuilt PDI needs it again, and its new sys_id goes into `itential/versions.yaml`
(`aws_vpn.change_request.template`), then `itential/workflows/build.py` and a production replay
(`platform-ha2-replay.yml`, which also points the `servicenow-api` integration at `SNOW_INSTANCE` from `.env`
and keeps the workflows and their triggers in step). The sys_id is the **record producer's**: read it with
`GET /api/sn_chg_rest/change/standard/template?sysparm_query=name=Lab AWS VPN change`, not from the proposal or the
template list (each has its own sys_id). S4b.3 checks the pinned id resolves to this template, active. Create it as a **Standard Change
Proposal** (`std_change_proposal_list.do`, New: the catalog item is desktop-only and the PDI shows no Change menu),
Request Approval, and approve it; approval creates the template.

| Field | Value |
|---|---|
| Category (catalog) | Network Standard Changes |
| Template name | Lab AWS VPN change |
| Template description, change Description | A change to the lab's AWS site-to-site VPN made by an Itential workflow (Deploy, Hand Off or Tear Down AWS VPN) after a person approved it in Itential Work Center. |
| Short description | AWS VPN change (itential-enterprise-lab) |
| Category, Assignment group, Impact | Network, Network, 3 - Low (Risk is not offered on a template) |
| Implementation plan | Itential applies the Terraform plan or router change exactly as approved in Work Center. |
| Backout plan | Tear Down AWS VPN, or the router change's revert timer; the job link in the work notes shows the run. |
| Test plan | Verify AWS VPN (tunnel, data plane, AWS monitor) after the change. |
| Justification (also the proposal's Business justification) | Keeps a ServiceNow record of every person-approved change to the lab's AWS VPN; the change itself is approved in Itential Work Center before it runs. |
| Risk and impact analysis | Low. A lab VPN with no production traffic; every change is shown as a plan and approved in Work Center first, and Tear Down AWS VPN removes it. |

The PDI requires Justification and Risk and impact analysis on a standard change template. The cloud tier's
ServiceNow integration sends a whole `Basic ...` header from Vault (`services/servicenow-cloud`), derived from the
integration password, so a new password means rebuilding that entry too; a new instance also means moving that
integration's server host.

State walk used by the workflow (standard change model): New `-5` -> Scheduled `-2` ->
Implement `-1` -> Review `0` -> Closed `3` (`close_code: successful`), work note on the table
API. `sys_audit` is not readable by a non-admin user, so the workflow records every state
ServiceNow returns in job variables and `verify/test-05-itential.sh` S4b.2 checks those plus
the change read back with display values.

`verify/` S4b.3 checks that this file exists and that the template and the group are present on
the instance (the two ids above), which is what "re-imports cleanly into a fresh PDI" reduces to
for the stock records; the custom template is checked by its pinned id. Reclamation: the owner logs in interactively at least every 10 days
(manual step 8); S4b.5 reports the admin's last login.
