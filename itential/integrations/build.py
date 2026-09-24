#!/usr/bin/env python3
"""Generate the OpenAPI documents for the lab's Integration Models into itential/integrations/ (PID S4d.4, ADR 0045).

An Integration Model is an OpenAPI 3 document the platform turns into a virtual adapter whose operations become
agent tools. NetBox publishes 322 paths and ServiceNow none, so both documents are written here from a short
operation list: every operation an agent should be able to call, with the handful of parameters that matter.
`python itential/integrations/build.py` writes the files, `--check` exits 1 when a file on disk differs
(tests/test_integrations.py). The model id on the platform is `<info.title>:<info.version>` (versions.yaml).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
VERSIONS = yaml.safe_load((HERE.parent / "versions.yaml").read_text())
MODELS = VERSIONS["integrations"]["models"]

# no bare `true` literals anywhere in these documents: Ollama rejects a chat request whose tool schemas carry one (memory lesson)
OBJ = {"type": "object"}
PAGE = {
    "type": "object",
    "properties": {
        "count": {"type": "integer"},
        "next": {"type": ["string", "null"]},
        "previous": {"type": ["string", "null"]},
        "results": {"type": "array", "items": OBJ},
    },
}
SNOW_RESULT = {
    "type": "object",
    "properties": {"result": {"type": "array", "items": OBJ}},
}
SNOW_ONE = {"type": "object", "properties": {"result": OBJ}}


def q(name: str, description: str, *, typ: str = "string", multi: bool = False) -> dict:
    """A query parameter; multi = repeated ?name=a&name=b (NetBox filters), style form / explode true.

    NetBox's filters are all repeatable, and every one of them was declared `multi=True` for the agent
    tool schemas in phase 6 (ADR 0045). S4f (ADR 0054) put the workflows on the same operations and that
    turned out to matter: a workflow passes values by reference, and a `$var` resolves at the top level
    of a task input but **not inside an array** - so an array-typed filter given `["$var.job.device"]`
    completes, sends the literal text `$var.job.device`, and returns nothing. Measured 2026-09-10.
    The lab filters by one value everywhere, so single-value is the default and `multi` stays available
    for a caller that genuinely needs a repeated filter."""
    schema = {"type": "array", "items": {"type": typ}} if multi else {"type": typ}
    p = {
        "name": name,
        "in": "query",
        "required": False,
        "description": description,
        "schema": schema,
    }
    if multi:
        p.update({"style": "form", "explode": True})
    return p


def path_param(name: str, description: str, typ: str = "string") -> dict:
    return {
        "name": name,
        "in": "path",
        "required": True,
        "description": description,
        "schema": {"type": typ},
    }


def op(
    operation_id: str,
    summary: str,
    params: list[dict],
    response: dict,
    security: str,
    *,
    body: dict | None = None,
    success: str = "200",
) -> dict:
    # A create answers 201 and a delete 204: declaring 200 for them would be a document that does not
    # describe the API, and the platform reads these codes when it maps the response.
    ok: dict = {"description": "success"}
    if success != "204":
        ok["content"] = {"application/json": {"schema": response}}
    o = {
        "operationId": operation_id,
        "summary": summary,
        "description": summary,
        "parameters": params,
        "responses": {
            success: ok,
            "default": {"description": "error"},
        },
        "security": [{security: []}],
    }
    if body is not None:
        o["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": body}},
        }
    return o


def model(
    key: str, description: str, paths: dict, security_scheme: dict, server_url: str
) -> dict:
    m = MODELS[key]
    return {
        "openapi": "3.0.3",
        "info": {
            "title": m["title"],
            "version": str(m["version"]),
            "description": description,
            # S4f.1 (ADR 0054 decision 4): where this document came from, so a regeneration is
            # reproducible and a drift is visible. kind=openapi was trimmed from a published
            # specification; kind=documentation was written from a vendor reference by hand.
            "x-spec-source": m["spec"],
        },
        "servers": [{"url": server_url}],
        "paths": paths,
        "components": {"securitySchemes": {m["auth"]: security_scheme}},
    }


# --- NetBox: the source of truth, read-only ---------------------------------------------------------------
NB_LIMIT = q("limit", "Page size (NetBox default 50)", typ="integer")
NB_Q = q("q", "Free-text search")


def netbox() -> dict:
    a = MODELS["netbox"]["auth"]
    s = MODELS["netbox"]["server"]
    paths = {
        "/api/dcim/devices/": {
            "get": op(
                "dcim_devices_list",
                "List devices; filter by name, site, role, platform, status or tag",
                [
                    q("name", "Device name"),
                    q("site", "Site slug"),
                    q("role", "Role slug"),
                    q("platform", "Platform slug"),
                    q("status", "Status (active, planned, ...)"),
                    q("tag", "Tag slug"),
                    NB_Q,
                    NB_LIMIT,
                ],
                PAGE,
                a,
            )
        },
        "/api/dcim/devices/{id}/": {
            "get": op(
                "dcim_devices_retrieve",
                "One device by NetBox id",
                [path_param("id", "NetBox device id", "integer")],
                OBJ,
                a,
            )
        },
        "/api/dcim/interfaces/": {
            "get": op(
                "dcim_interfaces_list",
                "List interfaces; filter by device or name",
                [
                    q("device", "Device name"),
                    q("name", "Interface name"),
                    NB_Q,
                    NB_LIMIT,
                ],
                PAGE,
                a,
            )
        },
        "/api/ipam/ip-addresses/": {
            "get": op(
                "ipam_ip_addresses_list",
                "List IP addresses; filter by device, address or interface",
                [
                    q("device", "Device name"),
                    q("address", "Address with prefix length"),
                    q("interface", "Interface name"),
                    NB_Q,
                    NB_LIMIT,
                ],
                PAGE,
                a,
            )
        },
        "/api/ipam/vlans/": {
            "get": op(
                "ipam_vlans_list",
                "List VLANs; filter by site, group, name, vid or status",
                [
                    q("site", "Site slug"),
                    q("group", "VLAN group slug"),
                    q("name", "VLAN name"),
                    q("vid", "802.1Q id", typ="integer"),
                    q("status", "Status"),
                    NB_Q,
                    NB_LIMIT,
                ],
                PAGE,
                a,
            )
        },
        "/api/dcim/sites/": {
            "get": op(
                "dcim_sites_list",
                "List sites",
                [
                    q("name", "Site name"),
                    q("slug", "Site slug"),
                    NB_LIMIT,
                ],
                PAGE,
                a,
            )
        },
        # S4e (ADR 0048): the enriched objects
        "/api/dcim/racks/": {
            "get": op(
                "dcim_racks_list",
                "List racks; filter by site or name (devices carry rack and position)",
                [
                    q("site", "Site slug"),
                    q("name", "Rack name"),
                    NB_LIMIT,
                ],
                PAGE,
                a,
            )
        },
        "/api/circuits/circuits/": {
            "get": op(
                "circuits_circuits_list",
                "List circuits; filter by site (termination), provider or cid",
                [
                    q("site", "Site slug of a termination"),
                    q("provider", "Provider slug"),
                    q("cid", "Circuit id"),
                    NB_LIMIT,
                ],
                PAGE,
                a,
            )
        },
        "/api/ipam/asns/": {
            "get": op(
                "ipam_asns_list",
                "List autonomous system numbers; filter by site or asn",
                [
                    q("site", "Site slug"),
                    q("asn", "AS number", typ="integer"),
                    NB_LIMIT,
                ],
                PAGE,
                a,
            )
        },
        "/api/ipam/vrfs/": {
            "get": op(
                "ipam_vrfs_list",
                "List VRFs (route targets included); filter by name",
                [q("name", "VRF name"), NB_LIMIT],
                PAGE,
                a,
            )
        },
        "/api/ipam/prefixes/": {
            "get": op(
                "ipam_prefixes_list",
                "List prefixes; filter by site, vrf, role, vlan_vid or within",
                [
                    q("site", "Site slug"),
                    q("vrf", "VRF name"),
                    q("role", "Prefix role slug"),
                    q("vlan_vid", "VLAN id", typ="integer"),
                    q("within", "Parent prefix"),
                    NB_LIMIT,
                ],
                PAGE,
                a,
            )
        },
    }
    # --- S4f (ADR 0054): the writes the workflows used to make through adapter-netbox. Every path keeps
    # its trailing slash, which is the whole point: adapter-netbox 1.0.10 strips it, which is why
    # available-vlans was unreachable and the journal entries went out from the runner (ADR 0048).
    paths["/api/ipam/vlans/"]["post"] = op(
        "ipam_vlans_create",
        "Reserve a VLAN (Add Branch VLAN: created status=reserved, set active once the switch takes it)",
        [],
        OBJ,
        a,
        body={
            "type": "object",
            "required": ["vid", "name"],
            "properties": {
                "vid": {"type": "integer", "description": "VLAN id"},
                "name": {"type": "string"},
                "site": {"type": "integer", "description": "Site id"},
                "group": {"type": "integer", "description": "VLAN group id (the branch group)"},
                "status": {"type": "string", "description": "reserved | active"},
            },
        },
    )
    paths["/api/ipam/vlans/{id}/"] = {
        "patch": op(
            "ipam_vlans_partial_update",
            "Change a VLAN in place (Add Branch VLAN: reserved -> active once the push succeeded)",
            [path_param("id", "NetBox VLAN id", "integer")],
            OBJ,
            a,
            body={"type": "object", "properties": {"status": {"type": "string"}, "name": {"type": "string"}}},
        ),
        "delete": op(
            "ipam_vlans_destroy",
            "Delete a VLAN (Add Branch VLAN rollback and Remove Branch VLAN)",
            [path_param("id", "NetBox VLAN id", "integer")],
            OBJ,
            a,
            success="204",
        ),
    }
    paths["/api/extras/journal-entries/"] = {
        "post": op(
            "extras_journal_entries_create",
            "Write a journal entry on an object (the governed VLAN workflows record the push on the switch)",
            [],
            OBJ,
            a,
            body={
                "type": "object",
                "required": ["assigned_object_type", "assigned_object_id", "comments"],
                "properties": {
                    "assigned_object_type": {"type": "string", "description": "e.g. dcim.device"},
                    "assigned_object_id": {"type": "integer"},
                    "kind": {"type": "string", "description": "info | success | warning | danger"},
                    "comments": {"type": "string"},
                },
            },
            success="201",
        )
    }

    return model(
        "netbox",
        "NetBox, the lab's source of truth: device, interface, address, VLAN, site, rack, circuit, ASN, VRF and prefix queries, "
        "plus the VLAN and journal writes the governed workflows make (PID S4d.4/S4e/S4f, ADR 0045/0048/0054)",
        paths,
        {
            "type": "apiKey",
            "in": "header",
            "name": "Authorization",
            "description": "Token <NetBox API token>",
        },
        f"{s['protocol']}://{s['host']}:{s['port']}",
    )


# --- ServiceNow PDI: incidents and change requests (the Table API) -----------------------------------------
SN_QUERY = q(
    "sysparm_query", "Encoded query, e.g. number=INC0000060 or active=true^priority=1"
)
SN_LIMIT = q("sysparm_limit", "Maximum records", typ="integer")
SN_FIELDS = q("sysparm_fields", "Comma-separated fields to return")
SN_DISPLAY = q(
    "sysparm_display_value",
    "true returns display values (state names, user names) instead of sys_ids",
)


CHANGE_BODY = {
    "type": "object",
    "properties": {
        "state": {"type": "string"},
        "work_notes": {"type": "string"},
        "close_code": {"type": "string"},
        "close_notes": {"type": "string"},
        "assignment_group": {"type": "string"},
        "short_description": {"type": "string"},
    },
}


def servicenow() -> dict:
    a = MODELS["servicenow"]["auth"]
    incident_update = {
        "type": "object",
        "properties": {
            "work_notes": {"type": "string"},
            "comments": {"type": "string"},
            "state": {"type": "string"},
            "assignment_group": {"type": "string"},
        },
    }
    paths = {
        "/api/now/table/incident": {
            "get": op(
                "listIncidents",
                "List incidents (Table API)",
                [SN_QUERY, SN_LIMIT, SN_FIELDS, SN_DISPLAY],
                SNOW_RESULT,
                a,
            )
        },
        "/api/now/table/incident/{sys_id}": {
            "get": op(
                "getIncident",
                "One incident by sys_id",
                [path_param("sys_id", "Incident sys_id"), SN_FIELDS, SN_DISPLAY],
                SNOW_ONE,
                a,
            ),
            "patch": op(
                "updateIncident",
                "Update an incident: work notes, comments, state, assignment group",
                [path_param("sys_id", "Incident sys_id"), SN_FIELDS],
                SNOW_ONE,
                a,
                body=incident_update,
            ),
        },
        "/api/now/table/change_request": {
            "get": op(
                "listChangeRequests",
                "List change requests (Table API)",
                [SN_QUERY, SN_LIMIT, SN_FIELDS, SN_DISPLAY],
                SNOW_RESULT,
                a,
            )
        },
        "/api/now/table/change_request/{sys_id}": {
            "get": op(
                "getChangeRequest",
                "One change request by sys_id",
                [path_param("sys_id", "Change sys_id"), SN_FIELDS, SN_DISPLAY],
                SNOW_ONE,
                a,
            )
        },
    }
    # S4f (ADR 0054): the change-request writes Add Branch VLAN and Push Configuration with Approval reached through
    # the adapter's genericAdapterRequest. The Change API (/sn_chg_rest) is a different surface from the
    # Table API above: it is what applies a standard-change template and enforces the change model's
    # state order, which is why the state walk cannot simply PATCH the table record.
    paths["/api/sn_chg_rest/change/standard/{template_sys_id}"] = {
        "post": op(
            "createStandardChange",
            "Open a standard change from a PDI template (the change model requires an assignment group)",
            [path_param("template_sys_id", "sys_id of the standard change template")],
            SNOW_ONE,
            a,
            body={
                "type": "object",
                "properties": {
                    "short_description": {"type": "string"},
                    "assignment_group": {"type": "string", "description": "sys_id of the group"},
                },
            },
            success="200",
        )
    }
    paths["/api/sn_chg_rest/change/standard/{sys_id}"] = {
        "patch": op(
            "updateStandardChange",
            "Move a standard change through its states (-2 Scheduled, -1 Implement, 0 Review, 3 Closed)",
            [path_param("sys_id", "sys_id of the change request")],
            SNOW_ONE,
            a,
            body=CHANGE_BODY,
        )
    }
    paths["/api/now/table/change_request/{sys_id}"]["patch"] = op(
        "updateChangeRequest",
        "Write to the change's table record (the work note carrying the NetBox reservation)",
        [path_param("sys_id", "sys_id of the change request"), SN_FIELDS],
        SNOW_ONE,
        a,
        body=CHANGE_BODY,
    )

    return model(
        "servicenow",
        "ServiceNow PDI: incidents and change requests through the Table API (PID S4d.4, ADR 0045)",
        paths,
        {
            "type": "http",
            "scheme": "basic",
            "description": "the integration user (SNOW_USER / SNOW_PASSWORD)",
        },
        "https://example.service-now.com",
    )  # the instance's server carries the real PDI host from .env


def main(check: bool) -> int:
    rc = 0
    for doc in (netbox(), servicenow()):
        out = HERE / f"{doc['info']['title']}.json"
        text = json.dumps(doc, indent=2) + "\n"
        if check:
            if not out.exists() or out.read_text() != text:
                print(
                    f"{out.relative_to(HERE.parent.parent)} differs from build.py; run python itential/integrations/build.py"
                )
                rc = 1
            continue
        out.write_text(text)
        n = sum(len(v) for v in doc["paths"].values())
        print(
            out.relative_to(HERE.parent.parent),
            len(doc["paths"]),
            "paths,",
            n,
            "operations",
        )
    return rc


if __name__ == "__main__":
    sys.exit(main("--check" in sys.argv))
