# 0049 — The Anthropic key is the company key: a $15-a-week budget, metered on the platform, guarded in the verifies

- **Status:** accepted (owner instruction 2026-09-09)
- **Date:** 2026-09-09
- **Related:** ADR 0037 (local LLMs alongside Claude), ADR 0046 (the fleet with local twins; the 638k-token raw-report tool), PID Domain 7 (amendment 1.14)

## Context

On approving PR #17 the owner said the `ANTHROPIC_API_KEY` in `.env` is his company key and that its cost should
stay around **$15 a week**; the in-lab Ollama models exist for that reason. Measured on 2026-09-09 through the
Agent Session Manager (every session document carries `provider`, `modelVersion`, `startedAt`,
`totalInputTokens`, `totalOutputTokens`): the week since 2026-09-02 spent ~2.13M Anthropic input tokens
(~$4.50 at the claude-sonnet-5 list prices of $2 / $10 per MTok), of which one compliance session took 963k
(the raw compliance-report tool, since replaced by `wf-compliance-report-v1`) and lab-netops 824k over 49
sessions of Phase 6 development. A full `test-06` run costs ~155k input tokens (~$0.31), `test-06c` ~54k.

## Decision

- **The platform is the ledger.** `verify/tokens.sh` sums the last 7 days (or `DAYS=n`) per provider and model
  from the session documents and prices the Anthropic ones with `itential/versions.yaml` `llm.budget`
  (`weekly_usd: 15`, list prices recorded with their date). `make tokens` prints it; the three largest sessions
  are named so a runaway shows up.
- **The verifies guard the budget.** `test-06` and `test-06c` (the ones that start Anthropic sessions) call
  `verify/tokens.sh --check` after login and refuse to run once the week's spend is at or over the budget;
  `ANTHROPIC_VERIFY=force` overrides for a deliberate exception. Iteration uses `ONLY=` subsets or the
  `-local` twins; a full Anthropic verify runs once per PR.
- **Local first where the criterion allows it.** New agent criteria are written for the `ollama-lab` twin unless
  the criterion is about Claude itself (S4c.2-4, S4c.6, S4d.5a-e). Which profile the agents default to for
  ad-hoc MCP use stays the owner's call (today `anthropic`).

## Consequences

- No Anthropic spend outside sessions the platform records (the verifies never call the API directly), so the
  meter is complete; the Console remains the authority for the invoice.
- A session that runs away costs at most what one tool result can carry; `wf-show-all-v1`'s 1500-character cap
  and `wf-compliance-report-v1` exist for that reason (ADR 0046).
- Rejected: a CSV ledger written by the verifies (misses every other session); a hard rate limit on the key
  (not available per key from the lab).
