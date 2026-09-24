# Enterprise lab — one-word verbs.
# `make up` builds the lab in phase order; `make verify` runs every test in verify/.
# Every target fails loud: no silent fallbacks.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

PHASES := oob-network platform network-topology itential flowai observability platform-ha2 config-secrets-code identity ddi containerlab firewall-track

.PHONY: help bootstrap lint test up verify vault-dev vault vault-status vault-init vault-unseal vault-config vault-snapshot vault-revoke-root vault-admin-user vault-login vault-logout vault-cutover discover netbox-enrich tokens agents-push observability-refresh plan-oob plan-platform plan-itential plan-platform-ha2 plan-clab \
	netbox-token-dev clab-dev dev-stack verify-dev prod-snapshot copilot-prod $(addprefix phase-,$(PHASES))

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

# The dev stack's Platform plays run with the dev overlay and a NetBox token that can only read (ADR 0063):
# the dev Platform's adapter, integration and runner all receive NETBOX_TOKEN, and a prototype on dev must
# never be able to reserve or delete a production VLAN. Fails loud rather than falling back to the full token.
DEV := -e @playbooks/vars/itential-dev.yml
define load_env_dev
	$(load_env) [ -n "$${NETBOX_DEV_RO_TOKEN:-}" ] || { echo "NETBOX_DEV_RO_TOKEN missing: run make netbox-token-dev first"; exit 1; }; export NETBOX_TOKEN=$$NETBOX_DEV_RO_TOKEN;
endef

plan-oob: ## Phase 2: show what tofu would change (read-only)
	@$(load_env) cd tofu/oob && tofu init -input=false >/dev/null && tofu plan -input=false

plan-platform: ## Phase 3: show what tofu would change (read-only)
	@$(load_env) cd tofu/platform && tofu init -input=false >/dev/null && tofu plan -input=false

plan-platform-ha2: ## Phase 8: show what tofu would change for the production environment (read-only)
	@$(load_env) cd tofu/platform-ha2 && tofu init -input=false >/dev/null && tofu plan -input=false

plan-itential: ## Phase 5: show what tofu would change for the dev stack VM (read-only)
	@$(load_env) cd tofu/itential && tofu init -input=false >/dev/null && tofu plan -input=false

plan-clab: ## Show what tofu would change for the Containerlab VM (read-only)
	@$(load_env) cd tofu/clab && tofu init -input=false >/dev/null && tofu plan -input=false

lint: ## Run every CI check locally
	pre-commit run --all-files

tokens: ## Anthropic spend of the last 7 days from the platform's session ledger against llm.budget (ADR 0049)
	verify/tokens.sh

netbox-enrich: ## NetBox enrichment derived from topology/enterprise.yaml (addresses, VRFs, racks, circuits, contexts; ADR 0048)
	$(load_env) cd ansible && ansible-playbook playbooks/netbox-enrich.yml

netbox-nxos: ## Mock Cisco NX-OS devices in NetBox only, for the Cisco NX-OS project's inventory workflow (STATE=absent to remove)
	$(load_env) cd ansible && ansible-playbook playbooks/netbox-nxos.yml $(if $(STATE),-e nxos_state=$(STATE))

# Monitoring is built in phase 7 and sizes itself from NetBox, so anything registered after that is
# invisible to it until this runs (ADR 0057). Idempotent. Deliberately NOT observability-devices.yml:
# those are governed pushes that raise a Work Center card per device and need a person.
observability-refresh: ## Make Zabbix and Prometheus catch up with the hosts NetBox now holds (ADR 0057)
	$(load_env) cd ansible && ansible-playbook playbooks/observability.yml
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml -i inventory/phase2.yml playbooks/observability-hosts.yml

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

# Phase 5. NetBox registration first (inventory source), then the VM, then the images (staged on the host;
# a missing one needs `aws sso login --profile itential-ecr`), then the stack, adapters and workflows.
# Since ADR 0063 this is the dev stack (itential-dev, .65): every Platform play carries $(DEV) and the
# read-only NetBox token. It verifies with verify-dev, never verify/run.sh - that suite targets production
# and its S4.4 writes NetBox and EVE-NG devices.
# The prerequisites are Make targets, not recipe lines, so `make up` builds them first: every dev Platform play
# needs NETBOX_DEV_RO_TOKEN (load_env_dev) and the dev inventory names the running clab nodes. Both are idempotent.
phase-itential: netbox-token-dev clab-dev ## Phase 5: read-only NetBox token + clab (prerequisites) -> NetBox VM -> tofu apply -> resolver alias -> host (lab-CA cert, clab route) -> images -> dev stack + gateway + adapters + workflows (ADR 0063)
	$(load_env) cd ansible && ansible-playbook playbooks/netbox-vms.yml
	$(load_env) cd tofu/itential && tofu init -input=false >/dev/null && tofu apply -input=false -auto-approve
	$(load_env) cd ansible && ansible-playbook playbooks/oob-gw.yml --tags dns
	$(load_env_dev) cd ansible && ansible-playbook -i inventory/netbox.yml $(DEV) playbooks/itential-host.yml
	images/fetch.sh itential
	images/fetch.sh itential-load
	$(load_env_dev) cd ansible && ansible-playbook -i inventory/netbox.yml $(DEV) playbooks/itential.yml

# Phase 6 (PID S4c + S4d): after itential.yml re-imports the workflows, platform.yml wires Configuration
# Manager (Golden Config, compliance, device groups; more elements as they land) and flowai.yml re-resolves
# the agents' tool references (ADR 0038 rule). Order matters: itential -> platform -> flowai.
phase-flowai: ## Phase 6 on the dev stack: workflows re-imported -> applications wired to the clab devices -> ollama-mac profile + local agents (ADR 0063)
	$(load_env_dev) cd ansible && ansible-playbook -i inventory/netbox.yml $(DEV) playbooks/itential.yml
	$(load_env_dev) cd ansible && ansible-playbook -i inventory/netbox.yml $(DEV) playbooks/platform.yml
	$(load_env_dev) cd ansible && ansible-playbook -i inventory/netbox.yml $(DEV) playbooks/flowai.yml

vault-dev: ## Phase 9a: the dev tier's own Vault on itential-dev (dev secrets only, auto-unseal from the VM) -> KV, AppRoles, seeds (ADR 0065)
	$(load_env_dev) cd ansible && ansible-playbook -i inventory/netbox.yml $(DEV) playbooks/vault-dev.yml

# Phase 9a step 3 (ADR 0065): production's Vault on k3s. `make vault` installs it and never unseals it; the owner
# initialises it once in their own terminal (`make vault-init`: nothing printed, the key and root token land in a
# mode-600 file outside the repo) and unseals it after every pod restart (`make vault-unseal` asks for the key).
vault: ## Phase 9a: production's Vault on k3s (VIP from itential/versions.yaml, lab-CA TLS, Raft on Longhorn); never unseals
	$(load_env) cd ansible && ansible-playbook playbooks/vault.yml

vault-status: ## Production Vault: initialised / sealed / version (nothing secret)
	scripts/vault-prod.sh status

vault-init: ## Production Vault, once, in YOUR terminal: one unseal key + root token to a mode-600 file outside the repo
	scripts/vault-prod.sh init

vault-unseal: ## Production Vault: asks for the unseal key without echoing it (FROM_FILE=1 reads the init file)
	FROM_FILE=$(FROM_FILE) scripts/vault-prod.sh unseal

vault-config: ## Production Vault: KV, read-only AppRoles bound to their hosts, seeds from .env; proves each host can log in and this Mac cannot
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml playbooks/vault-prod-config.yml
	scripts/vault-prod.sh snapshot

vault-snapshot: ## Production Vault: a Raft snapshot to ~/Backups/itential-enterprise-lab/vault (mode 600, outside the repo)
	scripts/vault-prod.sh snapshot

vault-admin-user: ## Production Vault, once, in YOUR terminal: set the administrator login's password (root token from the init file)
	scripts/vault-prod.sh admin-user

vault-login: ## Production Vault, in YOUR terminal: log in as the administrator; a short-lived token to a mode-600 file outside the repo
	scripts/vault-prod.sh login

vault-logout: ## Production Vault: revoke the administrator token and delete its file
	scripts/vault-prod.sh logout

vault-revoke-root: ## Production Vault: revoke the root token - refuses unless the administrator login has been proven (make vault-login)
	scripts/vault-prod.sh revoke-root

# Phase 9a step 5 (ADR 0065): production reads its credentials from Vault. Needs the owner's administrator login
# (make vault-login, in their own terminal): the token issues the Platform's and the Gateway's secret IDs and is read
# from its file, never printed. Order matters: the Platform's own Vault client, then the Gateway's provider, then the
# replay that swaps every credential for a reference. The Platform on iap-01 and the Gateway restart on the way.
# Roll back: the same three plays with -e vault_enabled=false (the credentials are still in .env until Phase 9b).
VAULT_ADMIN_FILE ?= $(HOME)/.config/itential-enterprise-lab/vault-admin-token
vault-cutover: ## Phase 9a step 5: the Platform and the Gateway read their credentials from Vault (needs make vault-login)
	@test -s $(VAULT_ADMIN_FILE) || { echo "log in as the administrator first: make vault-login (in your own terminal)"; exit 1; }
	$(MAKE) prod-snapshot MODE=save
	$(load_env) export VAULT_TOKEN=$$(tr -d '\n' < $(VAULT_ADMIN_FILE)); cd ansible \
	  && ansible-playbook -i inventory/netbox.yml playbooks/platform-ha2-platform.yml \
	  && ansible-playbook -i inventory/netbox.yml playbooks/platform-ha2-gateway.yml \
	  && ansible-playbook -i inventory/netbox.yml -e @playbooks/vars/itential-prod.yml playbooks/platform-ha2-replay.yml
	verify/test-09a-vault.sh
	scripts/vault-prod.sh snapshot

# ADR 0063: the Copilot sandbox. Order matters: the read-only NetBox token before any dev Platform play, the
# Containerlab devices before the dev inventory that names them. prod-snapshot MODE=save before and
# MODE=compare after is the proof that none of it reached production.
netbox-token-dev: ## A view-only NetBox user and a write_enabled=false token for the dev stack, persisted to .env (ADR 0063)
	$(load_env) cd ansible && ansible-playbook playbooks/netbox-token-dev.yml

clab-dev: ## Containerlab VM -> route on oob-gw -> images (C8000v and vEOS from EVE-NG) -> Docker + containerlab + vrnetlab -> dev topology -> verify (ADR 0063)
	$(load_env) cd ansible && ansible-playbook playbooks/netbox-vms.yml
	$(load_env) cd tofu/clab && tofu init -input=false >/dev/null && tofu apply -input=false -auto-approve
	$(load_env) cd ansible && ansible-playbook playbooks/oob-gw.yml --tags routes
	images/fetch.sh c8000v
	images/fetch.sh veos
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml playbooks/clab-host.yml
	images/fetch.sh clab-load
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml playbooks/clab-host.yml
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml playbooks/clab-dev.yml
	CLAB_DEV_ONLY=1 verify/test-12a-clab-dev.sh

dev-stack: phase-itential phase-flowai verify-dev ## The whole dev stack; phase-itential brings netbox-token-dev and clab-dev first (ADR 0063)

verify-dev: ## Dev tier only: Containerlab topology and the dev stack; never part of make verify, so a torn-down dev stack cannot turn it red
	verify/test-12a-clab-dev.sh
	verify/test-05b-dev-copilot.sh
	verify/test-09a-vault-dev.sh

# ALLOW=path is the compare allowlist: without it a compare after `make copilot-prod` always fails (test-05b S12.8
# derives the allowlist from itential/copilot/roles.yaml). prod-snapshot.py also passes with an empty section (a
# renamed endpoint answers an empty list), so snapshot-sanity.py then refuses the fingerprint just written.
prod-snapshot: ## GET-only fingerprint of production: MODE=save before a dev build, MODE=compare [ALLOW=path] after (ADR 0063)
	@[ "$(MODE)" = save ] || [ "$(MODE)" = compare ] || { echo "MODE=save or MODE=compare"; exit 1; }
	@[ -z "$(ALLOW)" ] || [ "$(MODE)" = compare ] || { echo "ALLOW applies to MODE=compare only"; exit 1; }
	$(load_env) .venv/bin/python verify/prod-snapshot.py --$(MODE)$(if $(ALLOW), --allow $(ALLOW),)
	.venv/bin/python verify/snapshot-sanity.py

# The one production-side step of ADR 0063, run on its own and only with the owner's approval: svc-copilot
# and the copilot-readonly group with its read roles. Additive; touches no existing group or role.
copilot-prod: ## svc-copilot on production: LDAP user, 3 custom read roles, copilot-readonly group (ADR 0063)
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml playbooks/copilot-access.yml

# A prompt-only change: PATCH prompt.instructions on agents that already exist, touching no tool
# binding (itential/agents/push.py). `make phase-flowai` is still the path after a workflow import,
# because a re-imported workflow gets a new uuid and the tool references have to be re-resolved.
agents-push: ## Push itential/agents/*.yaml prompts to the Platform (CHECK=1 to diff only)
	$(load_env) .venv/bin/python itential/agents/push.py $(if $(CHECK),--check,) $(AGENT)

# Phase 7 (PID S7, ADR 0051). The stack and its Zabbix configuration on k3s, the agents on every Ubuntu machine
# (NetBox inventory for the VMs and EVE-NG endpoints, phase2.yml for the two pre-existing machines), then the
# governed device pushes (one Push Configuration with Approval job per router/switch; the owner approves the Work Center cards).
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
phase-platform-ha2: ## Phase 8: NetBox VMs -> tofu apply -> Docker hosts -> MongoDB replica set -> Redis + Sentinel -> Platform nodes + nginx -> tools + directory -> the administrator -> Gateway 5 -> verify
	$(load_env) cd ansible && ansible-playbook playbooks/netbox-seed.yml
	$(load_env) cd ansible && ansible-playbook playbooks/netbox-vms.yml
	$(load_env) cd tofu/platform-ha2 && tofu init -input=false >/dev/null && tofu apply -input=false -auto-approve
	$(load_env) cd ansible && ansible-playbook playbooks/oob-gw.yml --tags dns
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml playbooks/platform-ha2-hosts.yml
	images/fetch.sh itential-load-ha2
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml playbooks/platform-ha2-mongodb.yml
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml playbooks/platform-ha2-redis.yml
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml playbooks/platform-ha2-platform.yml
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml playbooks/platform-ha2-tools.yml
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml playbooks/platform-ha2-identity.yml
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml playbooks/platform-ha2-gateway.yml
	$(MAKE) observability-refresh
	verify/run.sh

# Phase 8, S11.5 (ADR 0055): the phase 5-7 assets replayed onto production from the shared task files. The
# overlay selects the production target, the local administrator, the API role re-sync and tools-01's Ollama;
# without it the same plays run against the dev-stack. Idempotent: re-run it right before the cut-over.
replay-platform-ha2: ## Phase 8 S11.5: replay every phase 5-7 Platform asset onto the production environment
	$(load_env) cd ansible && ansible-playbook -i inventory/netbox.yml -e @playbooks/vars/itential-prod.yml playbooks/platform-ha2-replay.yml
	verify/run.sh

# Later phases are wired in as each lands. Until then they fail loud.
$(addprefix phase-,$(filter-out oob-network platform network-topology itential flowai observability platform-ha2,$(PHASES))):
	@echo "phase '$@' is not implemented yet (see docs/PID.md delivery plan)"; exit 1
