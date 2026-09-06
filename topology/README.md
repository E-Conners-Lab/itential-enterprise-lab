# topology/ — single YAML that drives EVE-NG and NetBox

The YAML here is the source for both the EVE-NG lab (nodes, networks, links,
startup configs, built via the EVE-NG REST API) and the NetBox objects (sites,
devices, VMs, prefixes, IPs, VLANs). Both consumers read the same file so they
cannot drift from each other. Schema lands in Phase 1 with the PID.
