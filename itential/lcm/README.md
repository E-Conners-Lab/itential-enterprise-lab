# Lifecycle Manager documents (PID S4d.3, ADR 0043)

One file per resource model, applied by `ansible/playbooks/platform.yml` (`tasks/lcm.yml`):

| File | Model | Actions |
|---|---|---|
| `branch-vlan.yaml` | `branch-vlan` (branch, vid, vlan_name, switch, netbox_vlan_id, status) | Create -> `Add Branch VLAN`, Delete -> `Remove Branch VLAN` |

Rules learned on 6.5.2 (memory `itential-platform-lessons`): actions may name their workflow (the name is
stored and resolved at run time, so re-imports keep the model valid); a model update is a PUT of the full
document; the action workflow runs as a child job of a `resource:action` wrapper job and receives the
inputs plus `instance`; its `instance` job variable becomes the stored data; instance import is the bare
`{name, description, instanceData}` document validated against the schema. Instances are named
`<branch>-<vlan_name>`; the play imports one per NetBox VLAN in the branch groups and never retires one.
