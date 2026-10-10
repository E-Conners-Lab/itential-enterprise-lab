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
3. **`/no_think` stays as every twin's first prompt line.** It costs nothing on Qwen and it is what a
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
