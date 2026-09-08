# JSON Forms (PID S4d.3, ADR 0044)

Generated documents: `python itential/forms/build.py` writes one `<name>.json` per form from a field list;
`--check` fails when a file differs (`tests/test_lcm.py`). `ansible/playbooks/tasks/lcm.yml` creates a
missing form with `POST /json-forms/forms` and replaces a changed one with `PUT /json-forms/forms/<id>`.

| Form | Used by | Fields |
|---|---|---|
| `lab-branch-vlan-approval` | `wf-branch-vlan-v1` task `4a` (`ShowJsonForm`) | read-only branch, VLAN id, VLAN name, switch, NetBox reservation, status; `decision` approve/reject (default reject) |

Measured on 6.5.2: the task view is `/json-forms/task/ShowJsonForm`, `form_id` takes the form name,
`instance_data` must be a top-level `$var` object, the submitted form comes back as `export`, and the
pending task is a Work Center item whose resolved incoming variables the Work Center API returns.
