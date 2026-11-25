---
name: itential-agent-project-migration
description: Use when moving Itential agent projects between instances (lab Platform to the Itential Cloud instance), when an import returns 409, when an imported project is missing or unreadable (403), when imported agents show missing tools or an unknown provider/model, or when deciding what to do with local-model (Ollama) twin agents.
---

# Agent project migration (export bundle -> another instance)

## Checks first (read-only)

1. Target version and reachability: you are on the right instance (`browser-automation-on-itential`).
2. Target's provider profile and model names: list them via the Model Registry (GET).
3. Target's workflows by NAME and uuid (GET). Workflow uuids differ per instance; names are the join key.
4. Whether a project of the same name already exists in the target (GET).

## Bundle format (what to expect)

Agents carry the provider as `profileName` / `modelName`, and tools as `referenceId` + `lastKnownName`. There are no
operators. Inspect the actual export before scripting against it; do not assume fields beyond these.

## Procedure

Work on a COPY of the export, never the original.

1. Remap provider: set each agent's `profileName`/`modelName` to the target's profile and model.
2. Remap workflow tools: for each workflow tool, find the target workflow with the same name and swap the uuid.
   Name not found: mark it for dropping.
3. Check every non-workflow tool (integration, adapter, gateway) with `POST /tools/bulk` (a read-style existence
   check; confirm the body shape in the target's API docs/sample before sending). Anything not found: mark it.
4. Drop only what does not exist in the target. Write the dropped list to a file next to the bundle: agent,
   tool name, reason. Give it to the user.
5. Skip local-model twins (agents bound to an Ollama model) when the target cannot reach Ollama. Cloud cannot reach
   the lab's. List the skipped twins in the same record.
6. Ask the user to approve the import (it writes to a shared instance), then Import via the UI with `file_upload`.

## Failure handling

- Importing under a name that exists returns 409. Rename the project in the bundle copy (suffix with the date or
  `-cloud`) and re-import. Don't fight the existing one.
- A failed import can leave a hidden orphan project that the user cannot read (403). Don't try to delete or reach
  it. Rename and go on, and tell the user the orphan exists so they can ask an admin to remove it.
- After an import, tools may show `authorized:false`: that is a role problem, see
  `itential-cloud-rbac-and-tool-access`.

## Don't

- Don't match workflows by uuid across instances.
- Don't drop a tool silently, or keep one pointing at a uuid that is not in the target.
- Don't import the original export file, and don't retry the same import more than twice.
- Don't try the platform's internal APIs to delete the orphan.

## Verify

Read the imported project back: every agent has the target profile/model, each tool resolves (no
`unauthorizedReferenceId`, no unknown reference), and the count of tools = exported - dropped, matching the drop record.
