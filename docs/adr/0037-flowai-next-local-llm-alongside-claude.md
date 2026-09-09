# 0037 — FlowAI agents are the next phase; local LLMs (Ollama) run alongside Claude

- **Status:** accepted
- **Date:** 2026-09-07
- **Amends:** ADR 0008 (phase order), PID 1.6 (new S4c, phase table)

## Context

The lab exists to run the EVE-NG topology through Itential automations. After
Phase 5 the Platform, Gateway 5, the NetBox and ServiceNow adapters, the
Inventory Manager and the MCP server are live, and the FlowAI applications
(Agent Projects, Agent Execution Engine, Agent Session Manager, Tool Registry,
Model Registry) ship inside the `automation-platform-config-lcm-flowai` image.
DDI, identity, observability, config/secrets and Panorama (ADR 0008 Phases 6-10)
harden or observe the lab; none of them is a prerequisite for agents. The owner
wants agents now, with local models available next to Claude. The Proxmox host
has no GPU (Matrox G200 only, Xeon Gold 6154 with AVX-512); the owner's Mac Mini
is an M4 Pro with 64 GB and Ollama already installed.

## Decision

- **Phase order** becomes 5 Itential, **6 FlowAI agents**, 7 DDI, 8 identity,
  9 observability, 10 config/secrets/code, 11 Panorama, 12 Containerlab.
  Windows Server AD is deferred with identity; the dev-stack OpenLDAP keeps
  providing `admin@itential` until then.
- **LLM providers as Model Registry profiles**, keys in `.env` only:
  1. `anthropic` with `claude-sonnet-5` as the default agent model
     (`ANTHROPIC_API_KEY`).
  2. `ollama` **in the lab**: an `ollama/ollama` container on the itential VM
     (pinned image and model tags in `itential/versions.yaml`, models on a
     Docker volume), CPU inference on the VM's 8 vCPU. Small instruct models
     only (7-8 B, 4-bit); this is the always-on, reproducible local option and
     its RAM is measured by the Phase 5 S4.6 method.
  3. `ollama` **on the Mac Mini** (M4 Pro): the fast local option for interactive
     demos, reached from the platform at the Mac's LAN address through oob-gw's
     NAT. Optional: it is the owner's workstation, not lab infrastructure, so no
     acceptance criterion depends on it.
- **Agents and tools are code**: agent projects, tool registrations and provider
  profiles are created by `ansible/playbooks/flowai.yml` from documents in
  `itential/agents/`, the same way workflows are generated and imported
  (ADR 0036). Tools come from what Phase 5 built: NetBox adapter methods,
  Gateway 5 `send-command`/`send-config` on the `lab` inventory, and the
  `wf-*` workflows exposed as tools (the VLAN change keeps its human approval).
- **ServiceNow Integration Model** (OpenAPI upload + instance with the
  integration user) is added in this phase for agent tool use; the adapter
  stays for workflows.

## Consequences

- PID 1.6 adds S4c with its own acceptance criteria and `verify/test-06-flowai.sh`;
  the later phases keep their specs, only their numbers move.
- No new VM: the Ollama container lives on VM 205; if the S4.6-style
  measurement shows pressure, the first lever is the VM's 24 -> 32 GB (budget
  headroom 17 GB), applied by PR.
- Provider keys are write-only in the platform and never leave `.env`; the Mac
  endpoint carries no key.
