# Enterprise lab — one-word verbs.
# `make up` builds the lab in phase order; `make verify` runs every test in verify/.
# Every target fails loud: no silent fallbacks.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

PHASES := oob-network platform network-topology itential ddi identity observability config-secrets-code panorama containerlab

.PHONY: help bootstrap lint test up verify discover plan-oob plan-platform $(addprefix phase-,$(PHASES))

help: ## Show targets
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

bootstrap: ## Install local tooling checks, .venv (pytest, pynetbox) + pre-commit hooks
	@for t in tofu ansible ansible-lint yamllint gitleaks pre-commit gh jq python3 helm kubectl cilium; do \
	  command -v $$t >/dev/null || { echo "MISSING: $$t (see README: Bootstrap)"; exit 1; }; done; [ -x /opt/homebrew/opt/helm@3/bin/helm ] || { echo "MISSING: helm@3 (brew install helm@3)"; exit 1; }; for t in true; do \
	  command -v $$t >/dev/null || { echo "MISSING: $$t (see README: Bootstrap)"; exit 1; }; done
	[ -d .venv ] || python3 -m venv .venv
	.venv/bin/pip install -q -r requirements-dev.txt
	ansible-galaxy collection install -r requirements.yml >/dev/null
	pre-commit install
	@echo "bootstrap OK"

test: ## Unit tests that need no lab access (ipam.yaml vs ip-plan.md, ...)
	.venv/bin/python -m pytest -q tests/

# Every lab-touching target loads .env into the environment (never committed).
define load_env
	set -a; . ./.env; set +a; export NETBOX_API=$$NETBOX_URL;
endef

plan-oob: ## Phase 2: show what tofu would change (read-only)
	@$(load_env) cd tofu/oob && tofu init -input=false >/dev/null && tofu plan -input=false

plan-platform: ## Phase 3: show what tofu would change (read-only)
	@$(load_env) cd tofu/platform && tofu init -input=false >/dev/null && tofu plan -input=false

lint: ## Run every CI check locally
	pre-commit run --all-files

discover: ## Phase 0: read-only inventory of Proxmox, EVE-NG, NetBox (writes verify/results/)
	verify/discover.sh

up: $(addprefix phase-,$(PHASES)) ## Bring the whole lab up in phase order (stops at first failure)

verify: ## Run every verification test in verify/ and record results
	verify/run.sh

# Phase 2. Order matters: the host play mints the API token that tofu needs; tofu builds
# oob-gw before its guest play; NetBox is seeded before anything reads it (ADR 0002).
phase-oob-network: ## Phase 2: host prep -> tofu apply -> oob-gw -> EVE pnet1 -> NetBox OOB/seed/token -> EVE password -> verify
	$(load_env) cd ansible && ansible-playbook playbooks/pve-host-prep.yml
	$(load_env) cd tofu/oob && tofu init -input=false >/dev/null && tofu apply -input=false -auto-approve
	$(load_env) cd ansible && ansible-playbook playbooks/oob-gw.yml
	$(load_env) cd ansible && ansible-playbook playbooks/eve-oob.yml
	$(load_env) cd ansible && ansible-playbook playbooks/netbox-oob.yml
	$(load_env) cd ansible && ansible-playbook playbooks/netbox-seed.yml
	$(load_env) cd ansible && ansible-playbook playbooks/netbox-token.yml
	$(load_env) cd ansible && ansible-playbook playbooks/eve-password.yml
	verify/run.sh

# Phase 3. NetBox registration first (inventory source), then VMs, then the cluster, then the workloads.
phase-platform: ## Phase 3: NetBox VMs -> tofu apply -> k3s cluster -> Cilium/MetalLB/Longhorn/cert-manager/CNPG -> verify
	$(load_env) cd ansible && ansible-playbook playbooks/netbox-vms.yml
	$(load_env) cd tofu/platform && tofu init -input=false >/dev/null && tofu apply -input=false -auto-approve
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml playbooks/k3s-cluster.yml
	$(load_env) cd ansible && ansible-playbook playbooks/k8s-platform.yml
	verify/run.sh

# Later phases are wired in as each lands. Until then they fail loud.
$(addprefix phase-,$(filter-out oob-network platform,$(PHASES))):
	@echo "phase '$@' is not implemented yet (see docs/PID.md delivery plan)"; exit 1
