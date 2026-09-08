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


def field(key: str, title: str, *, typ: str = "string", read_only: bool = True, enum: list[str] | None = None,
          required: bool = False, default: str | None = None, description: str = "") -> dict:
    """One struct item; customKey is the property name in schema and the key of the submitted export."""
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
        if f["readOnly"]:
            prop["readOnly"] = True
            ui[k] = {"ui:readonly": True}
        else:
            ui[k] = {"ui:placeholder": f["placeholder"] or "Enter a value"}
        properties[k] = prop
    ui["ui:order"] = [f["customKey"] for f in fields] + ["*"]  # the platform adds this on save; emitting it keeps re-runs idempotent
    return {
        "name": name,
        "description": description,
        "struct": {"type": "array", "items": fields},  # "object" renders empty
        "schema": {"title": name, "description": description, "type": "object",
                   "required": [f["customKey"] for f in fields if f["required"]], "properties": properties},
        "uiSchema": ui,
        "bindingSchema": {},
        "validationSchema": {},
        "tags": [],
        "version": "2020.1",
    }


# --- lab-branch-vlan-approval: the approval task of wf-branch-vlan-v1 (ShowJsonForm on task 4a) --------------
# The workflow fills the read-only context from its job variables (the same object Lifecycle Manager stores
# as the instance, ADR 0043); the approver only sets the decision. Default reject: an untouched form pushes nothing.
def branch_vlan_approval() -> dict:
    return form(VERSIONS["forms"]["approval"],
                "Approve or reject a branch VLAN change reserved in NetBox by wf-branch-vlan-v1 (PID S4d.3, ADR 0044)",
                [field("branch", "Branch", description="Branch site (br1, br2)"),
                 field("vid", "VLAN id", typ="number", description="802.1Q id chosen from the branch VLAN group in NetBox"),
                 field("vlan_name", "VLAN name"),
                 field("switch", "Switch", description="Inventory node that receives 'vlan <id> / name <name>' on approval"),
                 field("netbox_vlan_id", "NetBox reservation (VLAN object id)", typ="number",
                       description="The VLAN object reserved in NetBox; deleted again on reject"),
                 field("status", "NetBox status", description="reserved until the switch is configured"),
                 field("decision", "Decision", read_only=False, enum=["approve", "reject"], required=True, default="reject",
                       description="approve pushes the VLAN to the switch and activates the reservation; reject rolls it back")])


def main(check: bool) -> int:
    rc = 0
    for doc in (branch_vlan_approval(),):
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
