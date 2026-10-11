# 0081 — The Platform explains its workflows in conversation: platform-guide, one read-only tool over the describer

- **Status:** proposed (2026-10-11); built in the PR that carries this file; the eval set runs on the dev tier first
  (ADR 0063), then production
- **Date:** 2026-10-11
- **Amends:** ADR 0080 decision 7 (the agents, "after"): this is that decision, built
- **Related:** ADR 0046 (the agent fleet, a Claude agent with a local twin each), ADR 0075 (the agents infer and
  Itential checks them: here the opposite, by design), ADR 0079 (the local model), ADR 0080 (the describer), the
  NetClaw exploration (Slack as the front door, not yet built)

## Context

ADR 0080 gave the Platform two workflows that describe it: `Describe Workflow` (what a workflow does, from the
stored document) and `Describe Job` (what one run did, from the job record). Both answer with a Work Center card and
wait for Seen. The owner's next question (2026-10-11): an agent that has access to these and explains them in
natural language, so an engineer can ask "what does the BGP outage workflow do?" or "what did this job do?" and get
an answer in their own terms, without reading a card section by section.

What this is not: the agent does not read the workflow document, the canvas or the job record itself. The describer
(a deterministic walker, tested against the committed documents) already turns those into words that cannot drift
from what runs. The model's job is to tailor those words to the question: pick the part that answers it, keep the
counts and names exact, and say when the guide does not say.

What was measured before deciding:

- The card workflows cannot be an agent's tools: a workflow tool returns when the job ends, and `Describe Workflow`
  ends only when a person presses Seen. An agent handed it would hang until someone cleared Work Center.
- An agent's workflow tool returns the job's published variables. `Describe Workflow` publishes `described` (the
  HTML, the chart as SVG, the Markdown, the whole answer) and `describe_result` (the Gateway envelope with the same
  answer as a string): about 100 KB for the longest workflow, three copies of the guide. The local twin has 32k of
  context (ADR 0079).
- A request often names a workflow loosely ("the BGP one"). The service had no way to find the exact name; the
  Studio list answers every stored document in one GET.

## Decision

1. **One tool, no card: `Describe for an Agent`.** A reducing workflow (the pattern of `Run Show Command on a
   Device` and `Summarize Compliance Results`): inputs `what` (`list`, `workflow` or `job`) and `which` (the
   workflow's name, the job's id, or the word `all`), both as data behind the input gate; one `runService` of
   `platform-describe`; publishes `outcome` (one line), `guide` (Markdown) and `error`, and nothing else - the
   Gateway envelope stays on the task (`run_service(..., out_job=None)`). No `runCode`, no card, no manual task.
2. **The service gains `--action list` and `--which`.** The list reads every stored document (`GET
   /automation-studio/workflows?limit=200`) and answers one line per workflow: tasks, steps, stops for a person,
   the first sentence of its description; a document without step titles (a vendor pack) lists with no steps.
   `--which` is the one argument the tool passes whatever the action; `--name` and `--id` stay for the card
   workflows. The Gateway's `arg-format` turns each param key into `--key value`, so the tool sends exactly
   `action`, `which` and `timeout`.
3. **The agent `platform-guide`, with its local twin.** Profile `anthropic` (the twin `ollama-mac`, ADR 0079),
   project `lab-netops`, input `request`, tools: `Describe for an Agent` only. The prompt gives the three calls
   (list first when the name is loose), the shape of the answer (lead with the summary, three to eight sentences,
   counts and names exactly as the guide spells them, for a job the path and where it ended) and the rules: only
   from what the tool returned, never a workflow, step, task, count or time the guide does not give, at most three
   tool calls, never run or change anything. The twin's answers are capped at eight sentences with no tables.
4. **The eval set is the acceptance.** `scripts/eval-platform-guide.py` runs the same requests through both agents:
   a named workflow, a loose name (list then describe), where a workflow waits for a person, a workflow's inputs,
   what stops the governed push, the latest finished job, and a workflow that does not exist. The facts each answer
   must carry come from the committed documents (the same documents the Platform holds) and, for the job, from the
   walker over the job record; the tool discipline is read from the session's tool calls. A case passes when every
   fact is in the answer, nothing forbidden is, the only tool called is the tool, and the session completed. The
   verify (`test-06-flowai.sh`, S16.6 and S16.7) runs one case on each agent with the counts from the committed
   document.
5. **Where it is reached.** Agent Sessions and the API now (the eval and the verify use the API); NetClaw in Slack
   later, as ADR 0080 decision 7 said. Nothing is exposed through MCP in this ADR.

## Consequences

- The Platform answers questions about itself in conversation, and the words still cannot drift: the model only
  has the describer's text to work from, and the eval measures whether it kept to it.
- The pin lag (ADR 0080 decision 2): the service's `list` action and `--which` reach the Gateway one merge after the
  code; `Describe for an Agent` on the Platform fails with "unrecognized arguments" until the pin bump, as `Describe
  Job` did. The plays run after the bump, as before.
- One more agent and twin for the fleet verify to run; the twin costs no tokens, the Claude case costs one short
  session.
- `Describe for an Agent` is a workflow like any other: it has a guide page, it is listed by the list it serves.

## Alternatives considered

- **The card workflows as the agent's tools.** They wait on Seen: an agent's call would never return (measured
  shape, not measured by running it).
- **`runService` as a Gateway Service tool (`application:GatewayManager:runService`).** One tool for every service
  on the Gateway, including `lab-edge-push`; the fleet's rule is the one service, never `runService` (ADR 0074).
- **An MCP tool through FlowMCP.** Production only, and the service is not an MCP server. The `itential-mcp`
  contribution (ADR 0080) is still the better long-term shape for the list and the describe both.
- **Letting the model read the document.** It would be the agent, not the Platform, that describes; the counts and
  the branches would be the model's reading, not the walker's, and could not be tested once.
- **Two tools (one per action).** Cleaner for a reader, but the local twin handles one tool with two inputs better
  than a choice between tools (ADR 0046's measurements on tool-name bugs).
