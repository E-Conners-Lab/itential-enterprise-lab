# 0081 — The Platform explains its workflows in conversation: platform-guide, one read-only tool over the describer

- **Status:** proposed (2026-10-11); built in #174, pinned by #175, converged on dev and production the same day; the eval set measured on both tiers (below): Claude 8/8 on production, the twin 8/8 on dev and on production once the judge accepted every honest way of saying a workflow is not there; decision 6 (the Slack front door's workflow, `Ask Platform Guide`) added 2026-10-11
- **Date:** 2026-10-11
- **Amends:** ADR 0080 decision 7 (the agents, "after"): this is that decision, built
- **Related:** ADR 0046 (the agent fleet, a Claude agent with a local twin each), ADR 0075 (the agents infer and
  Itential checks them: here the opposite, by design), ADR 0079 (the local model), ADR 0080 (the describer), the
  NetClaw exploration (Slack as the front door), the department-agents delivery (the Slack bot this front door
  rides: one channel, one agent, one service account, the answer on a Work Center card)

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
5. **Where it is reached.** Agent Sessions and the API (the eval and the verify use the API), and Slack through
   decision 6. Nothing is exposed through MCP in this ADR.
6. **The Slack front door: `Ask Platform Guide`** (added 2026-10-11). The owner's Slack bot (department-agents:
   no language model in the bot, a channel maps to one agent and one service account, the answer comes back from a
   Work Center card the bot finds by the job's id) is the front door NetClaw was going to be. The Platform side is
   one more workflow of the lab's own: `request` (plain words, 1-2000 characters) as data behind the input gate;
   `runAgent` of `platform-guide` as the Platform (the automation's group access decides who may ask, as it does
   for the department Ask automations; the marker `__AGENT_ID:platform-guide__` is filled at import, and the dev
   overlay fills it with the twin's id, since dev creates only the twins); the answer on a small branded card with
   one button, laid out as the bot reads it (an eyebrow it drops, the answer's own lines, one foot line), and
   published as `answer` with `outcome` and `error`. An agent that did not run still gets a card, so the asker
   hears why. The automation `Ask Platform Guide` and the endpoint `ask-platform-guide` come from the same play task
   the outage loops use, on both tiers. The bot's side is the delivery's: a channel entry, a service account in its
   own group, and the automation's group access for that group.

## Consequences

- The Platform answers questions about itself in conversation, and the words still cannot drift: the model only
  has the describer's text to work from, and the eval measures whether it kept to it.
- The pin lag (ADR 0080 decision 2): the service's `list` action and `--which` reach the Gateway one merge after the
  code; `Describe for an Agent` on the Platform fails with "unrecognized arguments" until the pin bump, as `Describe
  Job` did. The plays run after the bump, as before.
- One more agent and twin for the fleet verify to run; the twin costs no tokens, the Claude case costs one short
  session.
- `Describe for an Agent` is a workflow like any other: it has a guide page, it is listed by the list it serves.
- The answer in Slack is the agent's text as the Platform's own card holds it: the same words a person sees in
  Work Center, nothing the bot adds. The card's `</think>` cut is the twin's known leak (ADR 0079), applied once.
- The card is open to whoever can see Work Center items with no group, like the describe cards; the asker's
  identity and group are on the automation and the trigger, where the Platform records them.

## Measured (2026-10-11, pin 02742f5)

The tool first, by direct runs on production: `what list` answered 29 workflows, 15 of them stopping for a person
(the new tool among them); `what workflow` for `Diagnose Fabric BGP Outage` published a 17 KB guide and the summary;
`what workflow` for `Launch Rockets` ended with `error` "no workflow named 'Launch Rockets' on this Platform" and
no guide. The gate refused `what` "list all" with an empty `which` before anything ran (a probe's shell did the
word-splitting wrong; the refusal named both inputs).

Then the eval set, the same eight requests on each agent (`verify/results/*-platform-guide-eval.json`):

| Agent | Tier | Score | A case | Tokens a case |
|---|---|---|---|---|
| platform-guide (Claude) | production | 8/8 | 8-25 s | 5-18k in, 0.2-1.3k out |
| platform-guide-local (qwen3.8:27b) | dev | 7/8, 8/8 rescored | 37-119 s | 3.5-14k in, 0.25-0.75k out |
| platform-guide-local | production | 7/8, 8/8 rescored | 33-111 s | 3.5-13k in, 0.25-0.77k out |

What the answers did: every case called `Describe for an Agent` and nothing else, at most two calls; the loose name
("the BGP outage workflow") went list first, then the exact name; the counts and the step names came back as the
guide spells them; the job case gave the path with the branches answered and where the run ended; the missing
workflow was answered as missing by all three runs, with nothing invented. The twin's one "miss" on each tier was the
judge, not the twin: it wrote "does not include one called Launch Rockets ... I cannot describe it", which the first
phrase list did not cover; the list was widened and `--rescore` re-judged the stored answers (the `-rescored.json`
files beside the originals). The dev overlay creates only the twin (the profiles are narrowed to the Mac's Ollama),
so Claude was measured on production only.

Two things to carry forward: the twin reads the whole guide (13k tokens for the longest) and takes about two
minutes on it, well inside its context but slow for a chat; a shorter `guide` for the agent (the summary, the steps
and where it waits, without the walk-through) is the next lever if the twin is the front door. And the tool's output
is read from the session's `TOOL_CALLED` events by name; the Platform records the call's inputs, which is what the
tool-discipline score reads.

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
