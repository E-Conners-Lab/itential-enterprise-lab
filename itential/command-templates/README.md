# Command templates (PID S4d.2, ADR 0042)

One document per Configuration Manager OS type, created and updated through the MOP API by
`ansible/playbooks/tasks/mop.yml` (`POST /mop/createTemplate`, `POST /mop/updateTemplate/<name>`,
`createAnalyticTemplate`, `updateAnalyticTemplate/<id>`). Names come from `itential/versions.yaml` `mop`.

- `command_template` — show commands with pass/fail rules (MOP rule syntax: `contains`, `!contains`,
  `RegEx`, `!RegEx`; a `RegEx` rule is the bare pattern, a `/pattern/` wrapper never matches on 6.5.2
  (measured); `flags.multiline` anchors `^`/`$` per line). Every command is a `show`: MOP never pushes
  configuration.
- `analytic_template` — pre/post comparison of the same commands: a value extracted before a change
  must equal the value after it. Rule `type: regex` (bare pattern with one capture group) extracts
  and compares the values; `type: matches` does not compare captures on 6.5.2 (measured).

Rule design notes: Platform 6.5.2 does not store `ignoreWarnings`, so every rule is an `error`. The
EOS BGP check accepts either an established peer or "% BGP inactive" (the L2-only branch switches,
`no ip routing`) while a stuck peer (`Idle`/`Active`/`Connect`) fails everywhere. The topology runs
no OSPF (ADR 0034).
