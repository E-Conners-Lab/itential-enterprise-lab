# Enterprise lab — one-word verbs.
# `make up` builds the lab in phase order; `make verify` runs every test in verify/.
# Every target fails loud: no silent fallbacks.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

PHASES := oob-network platform network-topology itential ddi identity observability config-secrets-code panorama containerlab

.PHONY: help bootstrap lint up verify discover $(addprefix phase-,$(PHASES))

help: ## Show targets
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

bootstrap: ## Install local tooling checks + pre-commit hooks
	@for t in tofu ansible ansible-lint yamllint gitleaks pre-commit gh jq; do \
	  command -v $$t >/dev/null || { echo "MISSING: $$t (see README: Bootstrap)"; exit 1; }; done
	pre-commit install
	@echo "bootstrap OK"

lint: ## Run every CI check locally
	pre-commit run --all-files

discover: ## Phase 0: read-only inventory of Proxmox, EVE-NG, NetBox (writes verify/results/)
	verify/discover.sh

up: $(addprefix phase-,$(PHASES)) ## Bring the whole lab up in phase order (stops at first failure)

verify: ## Run every verification test in verify/ and record results
	verify/run.sh

# Phase targets are wired in as each phase lands. Until then they fail loud.
$(addprefix phase-,$(PHASES)):
	@echo "phase '$@' is not implemented yet (see docs/PID.md delivery plan)"; exit 1
