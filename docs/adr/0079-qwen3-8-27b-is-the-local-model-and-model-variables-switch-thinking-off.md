# 0079 — qwen3.8:27b is the local model, and the profile's Model Variables switch its thinking off

- **Status:** accepted (owner, 2026-10-10)
- **Date:** 2026-10-10
- **Amends:** ADR 0061 (the prompt picks the model: gemma4:26b)
- **Related:** ADR 0060 (inference runs on the Mac Mini), ADR 0046 (the twins), ADR 0059 (a tool call must end
  the job), ADR 0030 amendment 2026-10-10 (the Mac leaves the router's return path)

## Context

ADR 0061 chose `gemma4:26b` for one reason: thinking. Every candidate scored 9/9 on the tool-call shapes
that caused real incidents here, so correctness did not separate them; the cost of reasoning traces did
(6-8x on the answer turn), and the only way to switch them off that the Platform let us use was
`/no_think` as the first prompt line, which only gemma obeys. The ADR said it outright: if Itential ever
exposes provider options on a Model Registry profile, `think: false` becomes available and the Qwen
family comes back into contention.

It has. A profile's `models[]` entry carries an optional **Model Variables** JSON (`modelVariables` in
the update API), which the Platform stores as-is and passes to the provider's request at runtime
(Model Registry docs, "on-prem profiles"). That is the Ollama chat request's top level, where `think`
lives.

The owner asked for `qwen3.8:27b` (Qwen's current dense 27B: 256K context, tools, vision, thinking on by
default) in place of gemma. The Mac already held the model; Homebrew's Ollama `0.23.3` could not load it
("unable to load model" on a fully pulled blob), and `0.40.2` can.

## Measurements (2026-10-10, Ollama 0.40.2, Apple M4 Pro, `scripts/model-bakeoff.py --runs 3`)

The Qwen family still ignores the prompt directive and still honours the API flag. One trivial
generation, `qwen3.8:27b`:

| request | thinking chars | output tokens |
|---|---|---|
| plain | 69 | 20 |
| `/no_think` first line | 70 | 20 |
| `think: false` | **0** | 2 |

The bake-off, which is the tool-call shapes that caused this lab's incidents, with and without the flag
(`--no-think` sends `think: false`, exactly what a `modelVariables` entry does in a session):

| model | thinking | score | median s | median out tokens | traces |
|---|---|---|---|---|---|
| `gemma4:26b` | on | 9/9 | 2.5 | 149 | 9/9 runs |
| `gemma4:26b` | off | 9/9 | **0.6** | 19 | none |
| `qwen3.8:27b` | on | 9/9 | 6.7 | 62 | 9/9 runs |
| `qwen3.8:27b` | off | 9/9 | 4.3 | 31 | none |

Both are correct on every run. With thinking off, qwen3.8 emits no traces and the fewest tokens of any
candidate measured here, and takes about seven times longer than gemma on a tool call: it is a dense 27B
against gemma's 26B-A4B mixture, so each token costs more. 4.3 s is far inside the Platform's inference
timeout; the twins' answer turns were 1-2 s on gemma and will be a few seconds here.

## Decision

1. **`qwen3.8:27b` is the local model** (`ollama-mac` in `itential/versions.yaml`), the owner's call,
   taken with the speed cost above in view.
2. **The profile model carries `model_variables: {think: false}`.** `tasks/flowai-assets.yml` sends it as
   `modelVariables` in the profile PATCH and treats a live profile whose variables differ as drift, so a
   session that starts emitting traces again is a converge away from fixed. A Qwen pin without it is
   refused by `tests/test_flowai.py`.
3. **`/no_think` stays as every twin's first prompt line** (reversed the same evening, see "As built, the
   directive" below)**.** It costs nothing on Qwen and it is what a
   gemma fallback still needs; the test that holds it stands.
4. **`scripts/model-bakeoff.py --no-think` is the way to re-take this.** ADR 0061's rule stands: a
   measurement, re-run when a model or an Ollama version changes.

## Consequences

- The twins' tool calls get slower by a few seconds each and their replies get shorter; the verify
  records both (S4c.5, S4d.5f carry the response time).
- Ollama on the Mac is `0.40.2` now (recorded in the manifest, not pinned: Homebrew). Any future
  `brew upgrade ollama` moves the Cellar path, which the macOS firewall allows by path (ADR 0060's
  trap); this upgrade did not trip it, but the check is `curl` from a lab VM after every upgrade.
- The profile PATCH reissues the model id and orphans every bound agent until the play re-binds them
  (ADR 0061 amendment); converging this change is therefore `flowai.yml`, never a hand PATCH.
- Not changed: the twins' prompts and tool caps, which ADR 0061 left as a separate decision to make
  against measurements. A stronger model is the reason to revisit them, not the occasion.

## As built (2026-10-10, production)

The replay converged production (`failed=0`, nine twins bound to `qwen3.8:27b`, `modelVariables`
`{"think": false}` on the profile). The local twins then ran: 7 passed, 4 failed, and one failure was
new and the model's. `tunnel-diagnostics-local` ended `FAILED` with *"ollama model invocation failed:
no user query found in messages"* on its fourth turn, right after a 35 kB show-command result landed on
a 5.7k-token conversation. Reproduced directly against Ollama: when a conversation outgrows `num_ctx`,
Ollama drops the **oldest** messages and protects only the last one, so in a tool loop the user's
question is the first thing to go. gemma's template answered the pruned conversation anyway (which is a
fair candidate for why the twins "invented causes on healthy" devices); qwen3.8's template checks for
a user turn and refuses. The Platform sends no `num_ctx` of its own (`"options":{}` on the wire), so the
Mac's `OLLAMA_CONTEXT_LENGTH` is the only place the limit lives: raised from 16384 to 32768 in
`scripts/mac-ollama.sh`, held by `tests/test_agent_fleet.py`. A 14.6k-token prompt fits either way;
the failing shape needs the headroom.

Also measured: prompt processing on the dense 27B runs at roughly 95 tokens/s without a cache hit, so
a cold 15k-token turn is about 150 s. `fabric-diagnostics-local` completed (9 turns) but outran the
verify's 300 s session wait, under gemma as well as here; that limit is the verify's, not the model's.

## As built, later the same day: temperature 0

The refusal in `remediation-local` (S4d.5k) was intermittent after the prompt fixes: dev refused five times
in a row, production wrote the refusal sentence and emitted the tool call in the same turn, from an agent
document that is identical apart from ids. That is sampling, and the Platform gives one sampling control.
Measured on the wire from the gateway: a top-level `temperature` in Model Variables is mapped into
Ollama's `options` (`{"options":{"temperature":0}}`), while a nested `options: {...}` is replaced by the
Platform's own empty object. `ollama-mac` now carries `temperature: 0`, so every twin decodes greedily and
the same prompt and tools give the same answer on both tiers; `tests/test_flowai.py` holds it and refuses a
nested `options`. The bake-off already ran at temperature 0, so its numbers describe this configuration.

## As built, the directive: `/no_think` leaves the twins' prompts

With `think: false` on the profile, the `/no_think` first line did harm instead of nothing: qwen3.8 answered
the directive by emitting a think-close token, and the Platform's streamed reply showed it as a literal
`</think>` after the first sentence, then the sentence again (the owner saw it in Agent Sessions; a direct
Ollama call with the same prompt is clean, so it is the streaming path). Decision 3 is reversed: the nine
local prompts carry no directive, the fabric twin's prompt is now identical to the Claude prompt, and
`tests/test_agent_fleet.py` refuses a `/no_think` first line. A gemma fallback would need the line back,
which is the one thing that fallback now costs.

## As built, the next morning: thinking ON after all

Removing the directive cleaned the first-turn replies, but two twins (netbox-sot-local, device-ops-local)
still ended with `</think>` in the next production run, both on the turn after a tool result. Reproduced
offline with the exact body the Platform sends (captured on the gateway), streamed or not: with
`think: false`, Ollama 0.40.2's qwen3.8 renderer lets the model write `br2\n</think>\n\nbr2`. A stop
sequence on the tag does nothing (it is a special token), and the trigger is prompt-sensitive line by
line: dropping one unrelated sentence from the netbox prompt removes it, dropping another keeps it. That
rules out prompt fixes. With `think: true` every captured turn came back clean, the reasoning in its own
field, tool calls and the refusal unchanged, and at temperature 0 the traces stayed between 37 and 331
characters. Decision 2 is therefore reversed: `model_variables: {think: true, temperature: 0}`. The cost
is the extra output tokens of the traces (dev: 480-600 per session against about 260), which the
Platform shows under "Agent Reasoning" rather than in the conclusion. A minimal reproduction for Ollama
is in the session notes; the lab's own measurement is the one above.

## Amendment (owner, 2026-10-21): Ollama 0.40.7 and a 131072 context

The Mac's Ollama moves from `0.40.2` to `0.40.7` (Homebrew stable on 2026-10-21) and
`OLLAMA_CONTEXT_LENGTH` from 32768 to 131072, so agent harnesses that carry their own system prompt and
tool definitions (Claude Code, Codex) have room on top of the twins' own needs. What changed the picture
since the As-built section:

- `qwen3.8:27b` runs on Ollama's MLX runner on this Mac (`ollama ps` shows `RUNNER mlx`). Per Ollama's
  source, the MLX runner never truncates history: a request past the limit fails with an explicit error
  instead of silently dropping the user's turn. The dropped-turn failure above belongs to the llama.cpp
  runner (Ollama issue #17778).
- Only 16 of the model's 64 layers keep a KV cache (64 KiB per token), so 131072 is about 8 GiB on top of
  the loaded model, well inside 64 GB with one model resident.
- Ollama advises at least 64K for agent and coding harnesses; Qwen advises at least 128K for this model
  family.

The cost is unchanged per token: a longer prompt still reads at about 95-110 tokens/s cold. The firewall
check after the Homebrew upgrade (Consequences above) still applies.

Measured after the upgrade (2026-10-21):

- `ollama show qwen3.8:27b` reports renderer `qwen3.8`, parser `qwen3.5`, template source
  `renderer_parser` (built in, no custom template); `ollama ps` shows `CONTEXT 131072`, `RUNNER mlx`.
  The model digest is unchanged (`47951e4a6661`).
- From oob-gw, `curl http://ollama.lab.internal:11434/api/version` answers HTTP 200 `{"version":"0.40.7"}`:
  the new Cellar path did not trip the macOS firewall.
- `verify/agent-probe.sh platform-guide-local` completed on production in 51 s with one read-only tool
  call and a correct answer.
- A 9,123-token prompt sent with `num_ctx` 4096 returned HTTP 200 and was read in full
  (`prompt_eval_count` 9123, about 112 tokens/s): on the MLX runner the context setting is advisory.
  Anything that must cap a conversation has to do it itself.
