# Handoff — the agents on the Itential Cloud instance, and the change-risk reviewer (2026-11-25)

Paste the block below into a new Claude Code session opened in this repo.

---

Continue the itential-enterprise-lab project. This repository is the source of truth for the agents and workflows; the owner
also runs an Itential Cloud instance (Platform 6.7.0) that reads from it, and works from two machines through GitHub, so
`git fetch` and check ahead/behind before reading or writing anything.

Read, in this order:
1. This file.
2. ADR 0086 (the repo builds the bundles for a second Platform) and ADR 0087 (the change-risk reviewer).
3. `itential/agents/cloud_bundle.py` (its docstring) and `itential/agents/change-risk-reviewer.yaml`.
4. `docs/workflows/get-change-evidence.md`.

## Where it stands (2026-11-25)

| Pull request | State |
|---|---|
| #240 the link check no longer fails on the retired Rocky 9.8 image | merged |
| #238 `cloud_bundle.py` (ADR 0086) | open, all checks green except `links`, which a re-run clears now that #240 is on `main` |
| #239 the change-risk reviewer and `Get Change Evidence` (ADR 0087) | open, stacked on #238; retarget it to `main` once #238 merges |

Merging is the owner's call. The owner's GitHub token needed the `workflow` scope to push a change to `.github/workflows`
(done on 2026-11-25 with `gh auth refresh -s workflow`).

## What is on the cloud instance (not in git)

- **Agent projects:** `lab-netops-cloud` (13 agents, the Claude ones; the `ollama-mac` twins are skipped because the cloud
  cannot reach the lab's Ollama), `skills-lab` (1), `business-agents` (4, no tools: their department workflows are not
  there), `change-risk-reviewer` (1), and `AutoCon Demo` (1, built outside this repo).
- **Workflows:** the lab's, imported by name, plus `Get Change Evidence`. **Four exist only on the cloud and nowhere in git:**
  `Ask Network Summary Agent`, `Get Device Status`, `Get Network Summary`, `List ServiceNow Incidents` (with the
  `AutoCon Demo` project, about 80 KB in all). Export them if the instance is to be rebuilt.
- **Model profile:** `anthropic-homelab` (claude-sonnet-5-5), with its key already set.
- **Integrations:** `netbox-api`, `servicenow-api` and `typesafe-api`, all running through the cloud's default Gateway 5
  cluster `homelab`. NetBox answers through it (24 devices); nothing is exposed inbound, the gateway dials out.
- **A hidden orphan:** a project named `lab-netops` holding only `plan-reviewer`, left by a failed first import. The
  owner's account has no read right on it, so a cloud admin has to remove it. Import under another name meanwhile.

## How to put the repo's agents on the cloud again

1. Read the workflow names and uuids from the target (workflow uuids belong to one instance), as `{name: uuid}` JSON kept
   outside git.
2. `python itential/agents/cloud_bundle.py --workflows <map.json> --rename lab-netops=<new name>` (add `--agent NAME` to
   build one agent). It lists every tool it had to drop; `--strict` fails on any.
3. Studio -> Import Workflow for a workflow; Agent Projects -> Import Agent Project for a bundle. The importer previews
   the agents before it writes, so a bundle can be checked by cancelling.
4. For a prompt-only change, patch the agent's `prompt.instructions` and compare a SHA-256 of the stored text with the repo's.

## Things that cost time, so they are written down

- Integration tools read `authorized: false` until each model's `admin` role is on the user's group, **and** the user has
  logged out and in; an agent imported before that keeps its tools as `unauthorizedReferenceId` until each is removed
  and added again, in two calls (the same reference in both is a 400).
- `PATCH /authorization/groups/<id>` replaces the whole role list: send the old list plus the additions, and read it back.
- A transition table holds one edge per target, so a `failure` and an `error` edge to the same task overwrite each other.
  A query that finds nothing is a `failure`; a task that throws is an `error`.
- A sleeping ServiceNow developer instance answers HTTP 200 with a page. Check the content, not the status.
- An integration response carries ServiceNow's headers, a session cookie among them. Publish extracted fields only.
- The browser tool blocks output that looks like cookie or query-string data; return counts and booleans.

## Not done

- `Get Change Evidence` and the reviewer have run on the cloud only against the instance's sample change `CHG0000001`
  (names no device). Nothing has exercised a change that names a lab device, a collision, a related incident or a live read.
- Open changes and open incidents are capped at 25 each and the evidence does not say it was cut.
- Neither has been imported to the lab Platform (`itential.lab.internal`).
- The reviewer's answer can still open with a short preamble before `Risk:`.
- Eight lessons-learned skills were written outside this repo and are uncommitted (see the owner's notes); they need review
  before they are added here.
- The Rocky pin is still 9.8; moving to 9.9 is its own decision.

## Owner rules

- Claude never types or asks for a password, key or token: the owner signs in.
- Read-only first; ask before any write to a shared instance, and say how to undo it.
- Nothing is merged, and no permission or setting is changed, unless the owner says so.
- When a guess turns out wrong, say so and correct the record.
