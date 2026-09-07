# 0039 — Configuration Manager devices come from Inventory Manager through Device Broker (Gateway 5 only)

- **Status:** accepted
- **Date:** 2026-09-07
- **Amends:** ADR 0035 (Gateway 4 stays staged, not deployed)

## Context

Configuration Manager (backups, Golden Configuration, compliance plans, device groups),
command templates and the MCP device tools (`get_devices`, `run_command`,
`backup_device_configuration`) read devices from the Platform's Device Broker. On the
dev stack this answered "Provider is not available": nothing registered a provider.
Gateway 4 is the classic provider; the owner chose Gateway 5 as the only gateway
(ADR 0035) and asked on 2026-09-07 for every Platform application to be wired to the
EVE-NG lab, reopening the question of whether Gateway 4 had to come back.

Platform 6 ships `@itential/adapter-inventory_manager`, a built-in adapter that
publishes Inventory Manager nodes to Device Broker and routes `get-config`,
`set-config`, `run-command` and `is-alive` through the inventory's broker actions to
Gateway 5 ([device broker support](https://docs.itential.com/itential-platform/inventory-manager/device-broker-support)).
Measured 2026-09-07: one adapter instance made all 12 inventory nodes appear in
Configuration Manager with `ostype` = the node's `itential_platform` (`cisco_ios`,
`arista_eos`, the names Configuration Manager's parsers use); `GET
/configuration_manager/devices/br1-sw01/configuration` returned the running config,
a backup landed, and the MCP `run_command` tool answered `show vlan 11`, all through
the Gateway 5 runner (ADR 0038).

## Decision

- `ansible/playbooks/itential.yml` creates the adapter instance **InventoryBroker**
  (type `InventoryManager`; the name `InventoryManager` is taken by the application)
  with `inventories: [lab]` and `prepend_inventory_name: false`, so device names in
  Configuration Manager equal the NetBox and inventory names, and asserts that
  Configuration Manager lists every inventory node.
- **Gateway 4 stays staged and undeployed.** Every Platform application that needs
  devices gets them from Inventory Manager through this adapter; the NetBox ->
  Inventory Manager sync (itential.yml) remains the single source of device access.

## Consequences

- Golden Configuration trees, compliance plans, device groups and command templates
  can be built against the lab devices (S4d, PID 1.8) without any new gateway.
- Node credentials are visible in Device Broker output (as they are in Inventory
  Manager): Phase 10 (secrets) moves them to OpenBao-backed references.
- Rejected: enabling the `gateway4` profile (second gateway, netmiko device
  sync, the 4.x security-patch line) only to feed Configuration Manager.
