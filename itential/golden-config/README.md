# Golden Config documents (PID S4d.1, ADR 0040)

One directory per Golden Config tree, keyed by the platform `deviceType` that selects the
configuration parser (`cisco-ios`, `arista-eos`; the inventory's netmiko names `cisco_ios` /
`arista_eos` are mapped in `itential/versions.yaml` `golden_config.trees`).

- `base.gc` — the OS baseline in Golden Config template syntax, literal text. Goes on the tree's
  `base` node, so every device inherits it. Prefixes: none = required/warning, `<e/>` =
  required/error, `<i/>` = required/info, `{d/}` = disallowed, `{/regex/}` = pattern.
- `device.j2` — the per-device leaf, rendered by `ansible/playbooks/tasks/golden-config.yml` for
  every NetBox device of that platform (`device` = the NetBox device object, `mgmt_ip` = its
  primary address without the prefix length, `mgmt_if` = the interface that carries it). This is
  the NetBox intent the compliance plan checks: hostname and the management interface.

Tree layout: `base/<site>/<device>`; the site node carries the `site-<slug>` device group, the
device leaf carries the device. Detection only: a violation is fixed through `Push Configuration with Approval`
with a Work Center approval, never by Golden Config itself.
