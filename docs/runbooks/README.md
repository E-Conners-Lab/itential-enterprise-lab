# Runbooks

How to build this lab, one track per chapter. Each chapter has the same five sections — *Before you start*,
*The commands, in order*, *What "done" looks like*, *Verification*, *Troubleshooting* — and a table of the
versions it was tested against.

Read [00 — Prerequisites](00-prerequisites.md) first. It carries the fill-in table that every other chapter
references: no chapter names a real address, account or token, so fill that table in once and substitute as
you read.

| | Chapter | What you get |
|---|---|---|
| 00 | [Prerequisites](00-prerequisites.md) | Hardware, the accounts you must supply, `.env`, the fill-in table |
| 01 | [Out-of-band network and addressing](01-oob-network-and-addressing.md) | The gateway VM, DNS, DHCP, the address plan, and why `vmbr0` is never touched |
| 02 | [k3s and platform services](02-k3s-platform-services.md) | k3s, MetalLB, Traefik, cert-manager and the lab CA, Longhorn, CloudNativePG |
| 03 | [NetBox as the source of truth](03-netbox-source-of-truth.md) | NetBox, the seed, the enrichment, and NetBox as the Ansible inventory |
| 04 | [The EVE-NG topology](04-eve-ng-topology.md) | Twelve network devices from a YAML description, cabled and configured |
| 05 | [The Itential dev stack](05-itential-dev-stack.md) | Platform, Gateway, LDAP, adapters, inventories and workflows on one VM |
| 06 | [Platform applications](06-platform-applications.md) | Golden Config, compliance, MOP, Lifecycle Manager, Integration Models, FlowAI agents |
| 07 | [Observability](07-observability.md) | Zabbix, Prometheus, Grafana, Loki, gNMIc, and the official Itential dashboard |
| 08 | [Production HA and migration](08-production-ha2-and-migration.md) | The eleven-VM HA environment, the replay, the cut-over, retiring the dev stack |

## Chapters 00-04 and 07 need no commercial licence

They build a complete network-automation lab: an out-of-band network, a Kubernetes cluster, NetBox, a
twelve-device nested topology and a full observability stack. Chapters 05, 06 and 08 need access to
Itential's container registry.

## What these are not

They are not a substitute for the vendors' documentation, and they are not a promise that a command will
behave the same on a different version — see each chapter's *Tested versions* table. The
[ADRs](../adr/) carry the reasoning behind each decision, and [`docs/PID.md`](../PID.md) carries the plan
the phases follow. Where a chapter says something surprising, the ADR it cites says why.

The *Troubleshooting* sections are the reason these exist. Most of what is in them cost hours to find, and
almost all of it failed **silently** — a task reported complete that returned nothing, a health check that
lied because the image had no `curl`, a cluster that could never reach quorum so no failover ever ran.
Those are the entries worth reading before you need them.
