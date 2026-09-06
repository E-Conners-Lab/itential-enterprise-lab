# 0001 — Record architecture decisions as ADRs

- **Status:** accepted
- **Date:** 2026-09-06

## Context

This lab is rebuilt from scratch on a single host and will be automated end to
end. Choices about images, versions, networks, and tooling are easy to forget
and expensive to re-derive. The kickoff brief requires that anything one would
ask "why did we do it this way?" about is written down.

## Decision

Every non-trivial decision, including every image version choice, gets a
numbered ADR in `docs/adr/` using `0000-template.md`. ADRs are immutable once
accepted; a change is a new ADR that supersedes the old one.

## Consequences

Slightly more writing per decision. In exchange, the repo is the project
source of truth and PR reviews can point at a decision instead of a chat log.
