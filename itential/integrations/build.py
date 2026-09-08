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

OBJ = {"type": "object", "additionalProperties": True}
PAGE = {"type": "object", "properties": {"count": {"type": "integer"}, "next": {"type": ["string", "null"]},
                                          "previous": {"type": ["string", "null"]}, "results": {"type": "array", "items": OBJ}}}
SNOW_RESULT = {"type": "object", "properties": {"result": {"type": "array", "items": OBJ}}}
SNOW_ONE = {"type": "object", "properties": {"result": OBJ}}


def q(name: str, description: str, *, typ: str = "string", multi: bool = False) -> dict:
    """A query parameter; multi = repeated ?name=a&name=b (NetBox filters), style form / explode true."""
    schema = {"type": "array", "items": {"type": typ}} if multi else {"type": typ}
    p = {"name": name, "in": "query", "required": False, "description": description, "schema": schema}
    if multi:
        p.update({"style": "form", "explode": True})
    return p


def path_param(name: str, description: str, typ: str = "string") -> dict:
    return {"name": name, "in": "path", "required": True, "description": description, "schema": {"type": typ}}


def op(operation_id: str, summary: str, params: list[dict], response: dict, security: str, *, body: dict | None = None) -> dict:
    o = {"operationId": operation_id, "summary": summary, "description": summary, "parameters": params,
         "responses": {"200": {"description": "success", "content": {"application/json": {"schema": response}}},
                       "default": {"description": "error"}},
         "security": [{security: []}]}
    if body is not None:
        o["requestBody"] = {"required": True, "content": {"application/json": {"schema": body}}}
    return o


def model(key: str, description: str, paths: dict, security_scheme: dict, server_url: str) -> dict:
    m = MODELS[key]
    return {"openapi": "3.0.3",
            "info": {"title": m["title"], "version": str(m["version"]), "description": description},
            "servers": [{"url": server_url}],
            "paths": paths,
            "components": {"securitySchemes": {m["auth"]: security_scheme}}}


# --- NetBox: the source of truth, read-only ---------------------------------------------------------------
NB_LIMIT = q("limit", "Page size (NetBox default 50)", typ="integer")
NB_Q = q("q", "Free-text search")


def netbox() -> dict:
    a = MODELS["netbox"]["auth"]
    s = MODELS["netbox"]["server"]
    paths = {
        "/api/dcim/devices/": {"get": op("dcim_devices_list", "List devices; filter by name, site, role, platform, status or tag",
                                         [q("name", "Device name", multi=True), q("site", "Site slug", multi=True), q("role", "Role slug", multi=True),
                                          q("platform", "Platform slug", multi=True), q("status", "Status (active, planned, ...)", multi=True),
                                          q("tag", "Tag slug", multi=True), NB_Q, NB_LIMIT], PAGE, a)},
        "/api/dcim/devices/{id}/": {"get": op("dcim_devices_retrieve", "One device by NetBox id", [path_param("id", "NetBox device id", "integer")], OBJ, a)},
        "/api/dcim/interfaces/": {"get": op("dcim_interfaces_list", "List interfaces; filter by device or name",
                                            [q("device", "Device name", multi=True), q("name", "Interface name", multi=True), NB_Q, NB_LIMIT], PAGE, a)},
        "/api/ipam/ip-addresses/": {"get": op("ipam_ip_addresses_list", "List IP addresses; filter by device, address or interface",
                                              [q("device", "Device name", multi=True), q("address", "Address with prefix length", multi=True),
                                               q("interface", "Interface name", multi=True), NB_Q, NB_LIMIT], PAGE, a)},
        "/api/ipam/vlans/": {"get": op("ipam_vlans_list", "List VLANs; filter by site, group, name, vid or status",
                                       [q("site", "Site slug", multi=True), q("group", "VLAN group slug", multi=True), q("name", "VLAN name", multi=True),
                                        q("vid", "802.1Q id", typ="integer", multi=True), q("status", "Status", multi=True), NB_Q, NB_LIMIT], PAGE, a)},
        "/api/dcim/sites/": {"get": op("dcim_sites_list", "List sites", [q("name", "Site name", multi=True), q("slug", "Site slug", multi=True), NB_LIMIT], PAGE, a)},
    }
    return model("netbox", "NetBox, the lab's source of truth: read-only device, interface, address, VLAN and site queries (PID S4d.4, ADR 0045)",
                 paths, {"type": "apiKey", "in": "header", "name": "Authorization", "description": "Token <NetBox API token>"},
                 f"{s['protocol']}://{s['host']}:{s['port']}")


# --- ServiceNow PDI: incidents and change requests (the Table API) -----------------------------------------
SN_QUERY = q("sysparm_query", "Encoded query, e.g. number=INC0000060 or active=true^priority=1")
SN_LIMIT = q("sysparm_limit", "Maximum records", typ="integer")
SN_FIELDS = q("sysparm_fields", "Comma-separated fields to return")
SN_DISPLAY = q("sysparm_display_value", "true returns display values (state names, user names) instead of sys_ids")


def servicenow() -> dict:
    a = MODELS["servicenow"]["auth"]
    incident_update = {"type": "object", "properties": {"work_notes": {"type": "string"}, "comments": {"type": "string"},
                                                        "state": {"type": "string"}, "assignment_group": {"type": "string"}}}
    paths = {
        "/api/now/table/incident": {"get": op("listIncidents", "List incidents (Table API)", [SN_QUERY, SN_LIMIT, SN_FIELDS, SN_DISPLAY], SNOW_RESULT, a)},
        "/api/now/table/incident/{sys_id}": {
            "get": op("getIncident", "One incident by sys_id", [path_param("sys_id", "Incident sys_id"), SN_FIELDS, SN_DISPLAY], SNOW_ONE, a),
            "patch": op("updateIncident", "Update an incident: work notes, comments, state, assignment group",
                        [path_param("sys_id", "Incident sys_id"), SN_FIELDS], SNOW_ONE, a, body=incident_update)},
        "/api/now/table/change_request": {"get": op("listChangeRequests", "List change requests (Table API)", [SN_QUERY, SN_LIMIT, SN_FIELDS, SN_DISPLAY], SNOW_RESULT, a)},
        "/api/now/table/change_request/{sys_id}": {"get": op("getChangeRequest", "One change request by sys_id",
                                                             [path_param("sys_id", "Change sys_id"), SN_FIELDS, SN_DISPLAY], SNOW_ONE, a)},
    }
    return model("servicenow", "ServiceNow PDI: incidents and change requests through the Table API (PID S4d.4, ADR 0045)",
                 paths, {"type": "http", "scheme": "basic", "description": "the integration user (SNOW_USER / SNOW_PASSWORD)"},
                 "https://example.service-now.com")  # the instance's server carries the real PDI host from .env


def main(check: bool) -> int:
    rc = 0
    for doc in (netbox(), servicenow()):
        out = HERE / f"{doc['info']['title']}.json"
        text = json.dumps(doc, indent=2) + "\n"
        if check:
            if not out.exists() or out.read_text() != text:
                print(f"{out.relative_to(HERE.parent.parent)} differs from build.py; run python itential/integrations/build.py")
                rc = 1
            continue
        out.write_text(text)
        n = sum(len(v) for v in doc["paths"].values())
        print(out.relative_to(HERE.parent.parent), len(doc["paths"]), "paths,", n, "operations")
    return rc


if __name__ == "__main__":
    sys.exit(main("--check" in sys.argv))
