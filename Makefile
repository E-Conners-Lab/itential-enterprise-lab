# Enterprise lab — one-word verbs.
# `make up` builds the lab in phase order; `make verify` runs every test in verify/.
# Every target fails loud: no silent fallbacks.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

PHASES := oob-network platform network-topology itential flowai observability platform-ha2 config-secrets-code identity ddi containerlab firewall-track

.PHONY: help bootstrap lint test up verify discover netbox-enrich tokens plan-oob plan-platform plan-itential plan-platform-ha2 $(addprefix phase-,$(PHASES))

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

plan-platform-ha2: ## Phase 8: show what tofu would change for the production environment (read-only)
	@$(load_env) cd tofu/platform-ha2 && tofu init -input=false >/dev/null && tofu plan -input=false

plan-itential: ## Phase 5: show what tofu would change (read-only)
	@$(load_env) cd tofu/itential && tofu init -input=false >/dev/null && tofu plan -input=false

lint: ## Run every CI check locally
	pre-commit run --all-files

tokens: ## Anthropic spend of the last 7 days from the platform's session ledger against llm.budget (ADR 0049)
	verify/tokens.sh

netbox-enrich: ## NetBox enrichment derived from topology/enterprise.yaml (addresses, VRFs, racks, circuits, contexts; ADR 0048)
	$(load_env) cd ansible && ansible-playbook playbooks/netbox-enrich.yml

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

# Phase 4 (PID S3, ADR 0034). NetBox receives the topology before EVE-NG is built (ADR 0002); `plan`
# fails loud on an image missing from EVE-NG (C8000v/vEOS folders per ADR 0032/0033, the Ubuntu golden
# image per docs/manual-steps.md step 15, Windows 11 from the one-time `images/fetch.sh microsoft` +
# `images/build-win11.sh`, which imports itself). `apply` is idempotent and uploads the startup configs;
# `export` writes topology/generated/eve-nodes.yaml, the MAC table the oob-gw DHCP reservations render;
# the endpoint play waits for the guests to boot. `eve/build.py push-configs` wipes and restarts nodes,
# so it is a deliberate re-push after a topology/configs change, never part of `make up`. Add `--waves`
# to `start` once lab.firewalls is true (PA-VM boot storm, PID E3).
phase-network-topology: ## Phase 4: NetBox topology -> EVE-NG plan/apply -> start -> node MAC export -> oob-gw DHCP reservations -> Linux endpoints -> verify
	$(load_env) cd ansible && ansible-playbook playbooks/netbox-topology.yml
	$(load_env) cd ansible && ansible-playbook playbooks/netbox-enrich.yml
	$(load_env) .venv/bin/python eve/build.py plan
	$(load_env) .venv/bin/python eve/build.py apply
	$(load_env) .venv/bin/python eve/build.py start
	$(load_env) .venv/bin/python eve/build.py export
	$(load_env) cd ansible && ansible-playbook playbooks/oob-gw.yml
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml playbooks/lab-endpoints.yml
	verify/run.sh

# Phase 5. NetBox registration first (inventory source), then the VM, then the images (needs an
# SSO session: `aws sso login --profile itential-ecr`), then the stack, adapters and workflows.
phase-itential: ## Phase 5: NetBox VM -> tofu apply -> resolver alias -> host (Docker, lab-CA cert) -> images from ECR -> dev-stack + gateways + adapters + workflows -> verify
	$(load_env) cd ansible && ansible-playbook playbooks/netbox-vms.yml
	$(load_env) cd tofu/itential && tofu init -input=false >/dev/null && tofu apply -input=false -auto-approve
	$(load_env) cd ansible && ansible-playbook playbooks/oob-gw.yml --tags dns
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml playbooks/itential-host.yml
	images/fetch.sh itential
	images/fetch.sh itential-load
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml playbooks/itential.yml
	verify/run.sh

# Phase 6 (PID S4c + S4d): after itential.yml re-imports the workflows, platform.yml wires Configuration
# Manager (Golden Config, compliance, device groups; more elements as they land) and flowai.yml re-resolves
# the agents' tool references (ADR 0038 rule). Order matters: itential -> platform -> flowai.
phase-flowai: ## Phase 6: workflows re-imported -> Platform applications wired to the lab -> Ollama + provider profiles + agents -> verify
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml playbooks/itential.yml
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml playbooks/platform.yml
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml playbooks/flowai.yml
	verify/run.sh

# Phase 7 (PID S7, ADR 0051). The stack and its Zabbix configuration on k3s, the agents on every Ubuntu machine
# (NetBox inventory for the VMs and EVE-NG endpoints, phase2.yml for the two pre-existing machines), then the
# governed device pushes (one wf-config-push-v1 job per router/switch; the owner approves the Work Center cards).
phase-observability: ## Phase 7: NetBox seed (phase labels, released addresses) -> resolver aliases -> stack + Zabbix config -> agents on every Ubuntu machine -> device pushes (approvals) -> verify
	$(load_env) cd ansible && ansible-playbook playbooks/netbox-seed.yml
	$(load_env) cd ansible && ansible-playbook playbooks/oob-gw.yml --tags dns
	$(load_env) cd ansible && ansible-playbook playbooks/observability.yml
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml -i inventory/phase2.yml playbooks/observability-hosts.yml
	$(load_env) cd ansible && ansible-playbook playbooks/observability-devices.yml
	verify/run.sh

# Phase 8 (PID S11, ADR 0053): the production Itential environment in Itential's HA2 shape. NetBox first
# (the inventory source), then the VMs, then the Docker hosts, then the databases, then the Platform nodes and
# the load balancer, then Gateway 5 and the tools VM. The cut-over (itential.lab.internal -> iap-lb) and the
# retirement of VM 205 are separate owner-approved steps, not part of this target.
phase-platform-ha2: ## Phase 8: NetBox VMs -> tofu apply -> Docker hosts -> MongoDB replica set -> Redis + Sentinel -> Platform nodes + nginx -> Gateway 5 + tools -> verify
	$(load_env) cd ansible && ansible-playbook playbooks/netbox-vms.yml
	$(load_env) cd tofu/platform-ha2 && tofu init -input=false >/dev/null && tofu apply -input=false -auto-approve
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml playbooks/platform-ha2-hosts.yml
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml playbooks/platform-ha2-mongodb.yml
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml playbooks/platform-ha2-redis.yml
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml playbooks/platform-ha2-platform.yml
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml playbooks/platform-ha2-gateway.yml
	verify/run.sh

# Later phases are wired in as each lands. Until then they fail loud.
$(addprefix phase-,$(filter-out oob-network platform network-topology itential flowai observability platform-ha2,$(PHASES))):
	@echo "phase '$@' is not implemented yet (see docs/PID.md delivery plan)"; exit 1
