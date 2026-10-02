#!/usr/bin/env python3
"""Generate the JSON Forms documents into itential/forms/ (PID S4d.3, ADR 0044).

A form is four cooperating schemas (struct for rendering, schema for validation, uiSchema for widgets,
bindingSchema for live data); the builder skill warns that they drift silently, so every form here comes
from one field list. `python itential/forms/build.py` writes the files, `--check` exits 1 when a file on
disk differs (tests/test_lcm.py). tasks/lcm.yml POSTs a new form as is and PUTs it inside {options} when the
struct, schema or uiSchema on the platform differ.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
VERSIONS = yaml.safe_load((HERE.parent / "versions.yaml").read_text())
# the Deploy AWS VPN IPv4 check; tests/test_aws_vpn.py holds it equal to itential/workflows/build.py IPV4
IPV4 = r"^(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])){3}$"


def field(key: str, title: str, *, typ: str = "string", read_only: bool = True, enum: list[str] | None = None,
          required: bool = False, default: str | None = None, description: str = "", pattern: str | None = None,
          max_length: int | None = None) -> dict:
    """One struct item; customKey is the property name in schema and the key of the submitted export. pattern and
    max_length go to the schema only (they validate the submitted value)."""
    item = {"nodeId": f"node-{key}", "type": typ, "title": title, "description": description, "placeholder": "",
            "required": required, "readOnly": read_only, "binding": False, "rel": "item", "targetPointer": "/default",
            "customKey": key}
    if enum:
        # struct enums are {id, label, value} objects; schema enums are flat strings (builder skill, confirmed)
        item.update({"rel": "collection", "targetPointer": "/enum", "placeholder": "Select",
                     "enum": [{"id": f"enum-{key}-{v}", "label": v, "value": v} for v in enum],
                     "enumNames": [{"id": f"name-{key}-{v}", "label": v, "value": v} for v in enum]})
    if default is not None:
        item["default"] = default
    if pattern is not None:
        item["_pattern"] = pattern
    if max_length is not None:
        item["_maxLength"] = max_length
    return item


def form(name: str, description: str, fields: list[dict]) -> dict:
    properties, ui = {}, {}
    for f in fields:
        k = f["customKey"]
        prop = {"type": f["type"], "title": f["title"], "_id": f"/properties/{k}", "description": f["description"]}
        if f.get("enum"):
            prop["enum"] = [e["value"] for e in f["enum"]]
            prop["enumNames"] = [e["label"] for e in f["enumNames"]]
        if "default" in f:
            prop["default"] = f["default"]
        if "_maxLength" in f:
            prop["maxLength"] = f["_maxLength"]
        if "_pattern" in f:
            prop["pattern"] = f["_pattern"]
        if f["readOnly"]:
            prop["readOnly"] = True
            ui[k] = {"ui:readonly": True}
        else:
            ui[k] = {"ui:placeholder": f["placeholder"] or "Enter a value"}
        properties[k] = prop
    ui["ui:order"] = [f["customKey"] for f in fields] + ["*"]  # the platform adds this on save; emitting it keeps re-runs idempotent
    struct_items = [{k: v for k, v in f.items() if not k.startswith("_")} for f in fields]
    return {
        "name": name,
        "description": description,
        "struct": {"type": "array", "items": struct_items},  # "object" renders empty
        "schema": {"title": name, "description": description, "type": "object",
                   "required": [f["customKey"] for f in fields if f["required"]], "properties": properties},
        "uiSchema": ui,
        "bindingSchema": {},
        "validationSchema": {},
        "tags": [],
        "version": "2020.1",
    }


# --- lab-branch-vlan-approval: the approval task of Add Branch VLAN (ShowJsonForm on task 4a) --------------
# The workflow fills the read-only context from its job variables (the same object Lifecycle Manager stores
# as the instance, ADR 0043); the approver only sets the decision. Default reject: an untouched form pushes nothing.
def branch_vlan_approval() -> dict:
    return form(VERSIONS["forms"]["approval"],
                "Approve or reject a branch VLAN change reserved in NetBox by Add Branch VLAN (PID S4d.3, ADR 0044)",
                [field("branch", "Branch", description="Branch site (br1, br2)"),
                 field("vid", "VLAN id", typ="number", description="802.1Q id chosen from the branch VLAN group in NetBox"),
                 field("vlan_name", "VLAN name"),
                 field("switch", "Switch", description="Inventory node that receives 'vlan <id> / name <name>' on approval"),
                 field("netbox_vlan_id", "NetBox reservation (VLAN object id)", typ="number",
                       description="The VLAN object reserved in NetBox; deleted again on reject"),
                 field("status", "NetBox status", description="reserved until the switch is configured"),
                 field("decision", "Decision", read_only=False, enum=["approve", "reject"], required=True, default="reject",
                       description="approve pushes the VLAN to the switch and activates the reservation; reject rolls it back")])


# --- lab-deploy-aws-vpn: the manual trigger of Deploy AWS VPN (ADR 0068 step 5) -------------------------------
# The same three inputs the branded page sends to the API trigger; the NAT gateway defaults off (owner 2026-09-29).
def deploy_aws_vpn() -> dict:
    return form(VERSIONS["forms"]["deploy_aws_vpn"],
                "Deploy the AWS side of the lab's site-to-site VPN: Terraform plans it, a Work Center approval shows the plan (PID S13, ADR 0068)",
                [field("onprem_public_ip", "Your public IP address", read_only=False, required=True,
                       description="The public IPv4 address the tunnel comes from (curl ifconfig.me)",
                       pattern=IPV4, max_length=15),
                 field("enable_nat_gateway", "NAT gateway", read_only=False, enum=["false", "true"], required=True,
                       default="false", description="true also builds the NAT gateway (about $1 a day more); the VPN does not need it"),
                 # required: the Platform refuses a start without it; the same bounds as the workflow's check (WEB-01)
                 field("change_note", "Change note", read_only=False, required=True, max_length=280,
                       description="Why, shown to the approver")])


# --- lab-hand-off-aws-vpn / lab-verify-aws-vpn: the manual triggers of Hand Off and Verify AWS VPN (step 9) -----------
# One input, the target, from the open targets (itential/versions.yaml aws_vpn.targets): the same list as the workflows'
# input gates and the endpoint triggers' schemas.
OPEN_TARGETS = sorted(n for n, t in VERSIONS["aws_vpn"]["targets"].items() if t["window"] == "open")


def target_form(key: str, description: str) -> dict:
    return form(VERSIONS["forms"][key], description,
                [field("target", "Lab edge router", read_only=False, enum=OPEN_TARGETS, required=True,
                       default=OPEN_TARGETS[0] if OPEN_TARGETS else None,
                       description="The router whose side of the AWS VPN this runs on (open targets only)")])


def hand_off_aws_vpn() -> dict:
    return target_form("hand_off_aws_vpn", "Hand the lab edge router its side of the AWS VPN: render, Work Center "
                       "approval, push with the key from Vault, saved only once the tunnel is up (PID S13, ADR 0068)")


def verify_aws_vpn() -> dict:
    return target_form("verify_aws_vpn", "Verify the AWS VPN without changing anything: the router, the AWS monitor "
                       "where it applies and a ping through the tunnel must all say up (PID S13, ADR 0068)")


def tear_down_aws_vpn() -> dict:
    return target_form("tear_down_aws_vpn", "Tear the AWS VPN down: the router's AWS block removed under a revert timer "
                       "and proved gone, then the AWS side destroyed - each after its own Work Center approval (R2)")


def main(check: bool) -> int:
    rc = 0
    for doc in (branch_vlan_approval(), deploy_aws_vpn(), hand_off_aws_vpn(), verify_aws_vpn(), tear_down_aws_vpn()):
        out = HERE / f"{doc['name']}.json"
        text = json.dumps(doc, indent=2) + "\n"
        if check:
            if not out.exists() or out.read_text() != text:
                print(f"{out.relative_to(HERE.parent.parent)} differs from build.py; run python itential/forms/build.py")
                rc = 1
            continue
        out.write_text(text)
        print(out.relative_to(HERE.parent.parent), len(doc["struct"]["items"]), "fields")
    return rc


if __name__ == "__main__":
    sys.exit(main("--check" in sys.argv))
