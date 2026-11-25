# 0086 — The repo builds the agent bundles for a second Platform; the cloud instance reads from the repo

- **Status:** accepted
- **Date:** 2026-11-25
- **Related:** ADR 0037 (agents as code), ADR 0046 (the agent fleet), ADR 0084 (the skills-lab project), `itential/agents/push.py`

## Context

The agents live in `itential/agents/*.yaml` and the lab's play (`flowai.yml`) creates them on the lab's own Platform
through its API. The owner now also runs an Itential Cloud instance (Platform 6.7.0) and wants the same agents there,
with this repository as the only place they are defined, worked on from either of two machines through GitHub.

The first copy to the cloud was made by exporting each project from the lab's Platform and importing it on the cloud.
That made the lab's Platform, not the repo, the source of the cloud's agents, and it needed three edits by hand on
every pass. Measured on 2026-11-25 against 6.7.0:

- an export names the provider by profile and model name, and a cloud has its own profile (`anthropic-homelab`);
- a workflow tool is `workflow:<uuid>` and the uuid belongs to one Platform, so each is re-resolved by name;
- the lab-only tools (the MCP knowledge service, department workflows) do not exist there and must be dropped, not left dangling;
- the importer rejects a bundle without the metadata its own exports carry (`_id`, `created`, `createdBy`):
  "Not a valid agent project file";
- importing under a project name that exists answers 409, and a failed import can leave a project its importer cannot read.

## Decision

`itential/agents/cloud_bundle.py` (`make cloud-agents`) builds the import bundles from the agent documents and
`itential/versions.yaml`, resolving integration, application and Gateway Manager tools the way the play does and workflow
tools by name from a mapping file read from the target. It skips the local-model twins, lists every tool it had to drop
(`--strict` fails on any), derives its ids from the names so a rebuild is byte-identical, and writes only under `build/`.
The mapping file and the bundles are instance-specific and are never committed.

The bundles are imported through the Platform's own Import Agent Project. The script never talks to a Platform and holds no credential.

## Consequences

- A prompt or tool change is made once, in the YAML, and reaches both Platforms: `push.py` or the play for the lab,
  `make cloud-agents` and an import for the cloud.
- A built bundle matched the bundles exported from the live lab agent for agent, field for field (14 of 14), so the
  documents and the lab have not drifted; a drift now shows up as a diff.
- The import is still a manual step in the UI. A Platform-API push for the cloud needs a service account (client
  credentials, as ADR 0080's amendment describes) and is a separate decision.
- `business-agents` is not in this repository (it belongs to `itential-deliveries/department-agents`) and is not built here.
- Rejected: keeping the cloud copy as an export of the lab's Platform (two sources of truth, hand edits each time).
