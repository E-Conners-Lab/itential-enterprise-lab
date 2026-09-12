# 0061 — The only lever the Platform gives us is the prompt, so the prompt picks the model: gemma4:26b

- **Status:** accepted (2026-09-11)
- **Date:** 2026-09-11
- **Related:** ADR 0037 (FlowAI agents), ADR 0046 (the twins), ADR 0059 (a tool call must end the job), ADR 0060 (inference leaves the lab)

## Context

ADR 0060 moved inference to the Mac Mini and took `qwen3:30b-a3b` because it was already pulled and
is a strong model. The first real session on it was correct but **no faster than the 7B on CPU had
been**: 52 s against 62 s. The reason was in the token counts - output went **195 → 1820**. It is a
reasoning model, and the GPU was spending its speed on thinking traces.

`scripts/model-bakeoff.py` was written to settle this with measurement rather than reputation. Every
case in it is a failure this lab actually had, so a good score means "would not have caused the
incident": a plain string where an object was sent (PR #32), an operation whose optional parameters
were filled with nulls (the `invalid-tool-input` that started that work), and a choice between two
plausible tools.

**Correctness turned out not to be the differentiator.** Every candidate - `qwen3:8b`,
`qwen3:30b-a3b`, `gemma4:26b`, `qwen3.6:35b` - scored **9/9**. Those bugs were specific to
`qwen2.5:7b`; any current model on the Mac is a large upgrade.

What separates them is thinking, on the answer turn (model reads a tool result, writes the reply):

| model | thinking on | thinking off |
|---|---|---|
| `gemma4:26b` | 9.9 s, 137 tok | **1.6 s, 39 tok** |
| `qwen3:8b` | 12.9 s, 358 tok | 1.8 s, 32 tok |
| `qwen3.6:35b` | 19.4 s, 251 tok | 2.3 s, 32 tok |
| `qwen3:30b-a3b` | 22.9 s, 362 tok | 5.7 s, **347 tok** |

All five answered correctly. Two things stand out. Thinking costs **6-8x**. And the model ADR 0060
deployed is the worst of the set: with thinking disabled `qwen3:30b-a3b` still emitted 347 tokens and
its answer *began* `"Okay, the user is asking which devic..."` - it leaks the reasoning into the
content instead of the thinking field, so the operator pays for it twice.

Three ways exist to disable thinking, and only one is available to us:

| method | result |
|---|---|
| Ollama API `think: false` | works for every model - but **the Platform does not send it**; a real agent session emitted 1820 output tokens |
| Modelfile `PARAMETER think false` | **not a valid parameter**: `Error: unknown parameter 'think'` |
| `/no_think` in the prompt | **only `gemma4` obeys it.** Measured: gemma 294 → 0 thinking chars; `qwen3:8b` stayed at 703, `qwen3:30b-a3b` at 1175 |

The agent prompt is the one thing this repo owns end to end. That makes it the deciding constraint.

## Decision

1. **`gemma4:26b` is the local model.** It is the fastest on the tool call (2.6 s median), the leanest
   on the answer turn (39 tokens), scores 9/9 on the incident shapes, and - decisively - is the only
   candidate whose thinking can be switched off through the only interface we control.
2. **`/no_think` is the first line of every `ollama-mac` prompt**, held by a test. First line
   specifically: a directive buried mid-prompt is not reliably honoured.
3. **`scripts/model-bakeoff.py` stays in the repo.** This decision is a measurement, not an opinion,
   and it will need re-taking when a model or an Ollama version changes. Re-running it is the way to
   re-take it.

## Consequences

- The twins answer in seconds rather than tens of seconds, and stop emitting reasoning into replies.
- The choice is now coupled to a model family's prompt behaviour, which is a weaker guarantee than an
  API flag. If Itential exposes provider options on a Model Registry profile, `think: false` becomes
  available and the Qwen models come back into contention - re-run the bake-off then.
- Not addressed: the twins' prompts still carry constraints shaped by a 7B model on CPU (three tools
  each, heavy reduction). A 26B model may not need them, but relaxing those is a behavioural change to
  make against measurements, not alongside a model swap.


## Amendment 2026-09-11 — the profile IS updatable; the "create-only" finding was my wrong body shape

This ADR shipped saying Model Registry profiles cannot be updated, because
`PATCH /model-registry-service/profiles/<id>` answered 200 and changed nothing. That conclusion was
wrong and the play built on it was more destructive than it needed to be.

The `itential-builder:flowagent` skill documents the real contract: the body is wrapped in
`{"update": {...}}`, and every `models[]` entry needs **both** `name` and `enabled` (create takes
`name` alone). What I sent was `{"profile": {...}}` - which is the **create** wrapper - and then a bare
`{"models": [...]}`. The API answers **200 and silently ignores an unrecognised body**, which reads
exactly like "updates are not supported". Verified after loading the skill:

```
PATCH {"update":{"models":[{"name":"gemma4:26b","enabled":true},{"name":"qwen3:8b","enabled":true}]}}
-> 200, and the profile then listed both models
```

**The hazard the skill does not mention, and it is the important part.** Rewriting `models[]`
**reissues each model's UUID**, and an agent's `provider.model` *is* that UUID. So a profile PATCH
silently orphans every agent bound to it: measured, `agentCount` went **6 → 0** and all six twins
stopped resolving their model until each was re-PATCHed with the new id. Any profile update must be
followed by re-resolving `model_ids` and re-binding every agent - which `flowai-assets.yml` already
does further down, so the fix was ordering, not new machinery.

`flowai-assets.yml` now updates a drifted profile in place instead of deleting it. Deleting worked, but
it is an **irreversible hard delete** of a profile that agents depend on, and the skill documents
`GET /model-registry-service/profiles/{id}/agent-impact` for checking what breaks first - an endpoint
this repo did not know about and did not call before deleting a profile with six agents attached.

The wider lesson is about method rather than this API: the Itential skills document these services, and
reverse-engineering an undocumented shape by probing produced a confident, wrong, written-down
conclusion. Check `itential-builder:flowagent` before probing the Agent Project, Model Registry, Tools
or Session Manager services.
