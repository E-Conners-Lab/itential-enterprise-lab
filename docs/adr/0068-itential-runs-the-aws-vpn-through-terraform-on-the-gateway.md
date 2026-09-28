# 0068 — Itential runs the AWS site-to-site VPN: Terraform on the Gateway, the approved plan is the applied plan

- **Status:** proposed (2026-09-26). Decisions 1, 3, 4, 8, 9 and 10 and the rule in decision 5 are the owner's (D1,
  D3 and D4 answered 2026-09-27); decision 6 waits on the dev probes (P1-P7)
- **Date:** 2026-09-26
- **Amends:** PID S13 and section 3 (amendment 1.36)
- **Related:** ADR 0029 (local tofu state for the lab's own modules, unchanged), ADR 0038 (Gateway 5 runner on
  glibc), ADR 0063 (dev tier first), ADR 0065 (Vault through the built-in clients, 9b deferred), ADR 0066 (every
  device task result is checked)

## Context

The AWS side and a manual runbook already exist outside this repo:

- `~/PycharmProjects/cloud-devops-pipeline` (local git only, 9554a86, 7 commits), `terraform/environments/dev`:
  a t3.micro strongSwan EC2 with an EIP and IMDSv2 (`modules/vpn`), the PSK as a `random_password` in Secrets
  Manager `vpn/onprem-psk`, and a Lambda monitor with two-metric alarming and SNS (`modules/vpn-monitor`). State
  is in S3 with a KMS CMK and a DynamoDB lock. It was written by Terraform 1.5.7.
- `~/PycharmProjects/Eve-NG_Agent` `docs/runbooks/deploy-cloud-aws.md` (a1a9273): apply, sync the outputs, seed
  NetBox, render the IPsec block for `dc-ce-1`, run Batfish, push with the PSK fetched at push time, then check
  four signals (the device, the `cloud_tunnel_health` MCP tool, the Lambda, a data-plane test).

A read-only check on 2026-09-26 (AWS describe calls in every region, `terraform state list`, state metadata
only):

| Fact | Evidence |
|---|---|
| Nothing is deployed | No instances, NAT gateways, load balancers or EIPs in any region; `state list` is empty |
| The S3 state is the real state and it is empty | Serial 33, 0 resources, the same lineage as the local `errored.tfstate` (serial 13, 50 resources). The local file is a stale leftover and still holds a PSK |
| The dev env has no ALB | The `alb` module exists but `environments/dev/main.tf` does not call it. The recurring cost is the NAT gateway, which `modules/vpc` always creates (about $32 a month while up) |
| `dc-ce-1` is not in this lab | Its counterpart is `dc1-wan01`: the DC1 WAN edge, AS 65100, C8000v 17.13.01a, active in NetBox, reachable |
| The Eve-NG tunnel used the management port | `tunnel_source: GigabitEthernet1`. Here Gi1 is in VRF `MGMT`, and `oob-gw` NATs 10.100.0.0/24 to the home LAN and the internet (ADR 0004) |
| `dc1-wan01` has two spare ports | 8 NICs; Gi2-Gi6 are cabled (Gi3 to `dc1-fw01`), Gi7 and Gi8 have no network (EVE-NG API, 2026-09-28). EVE-NG Pro cables a port the running node already has; adding a NIC needs the node stopped (owner) |
| EVE-NG's NAT cloud | `nat0` is 172.29.129.254/24 on the EVE-NG host, masqueraded out of `pnet0` (192.168.68.240) by nftables. Its udhcpd pool is .1-.253, handed out from the bottom (read on the host, 2026-09-28) |
| The home address is public | The home router's WAN address equals what `curl ifconfig.me` returns (owner, 2026-09-27): no carrier-grade NAT |
| The on-prem prefixes are the Eve-NG lab's | `modules/vpn` `onprem_cidrs` defaults to 10.1/16, 10.10/16 and others; the dev env does not expose the variable |
| The PSK recovery window blocks a quick re-apply | `recovery_window_in_days = 7` (the comment says 1 day) |

What docs.itential.com says about Terraform (read 2026-09-27; the Gateway 5 index lists every page):

- **Gateway 5.3 and later runs Terraform through executable services.** An *executable object* names a binary on
  the execution nodes, and the documentation's own example is `iagctl create executable-object terraform-latest
  --exec-command /usr/local/bin/terraform`. An *executable service* runs a file from a gateway repository with
  that object. It takes `--set` arguments shaped by `--arg-format` (default `--{{.Key}} {{.Value}}`), an optional
  decorator, and secrets from the Gateway's secret store injected as environment variables
  (`--secret name=...,type=env,target=ENV_VAR`). It returns stdout, stderr, the return code and the run time
  ([5.3.0 release notes](https://docs.itential.com/itential-gateway/release-notes/530-feature-announcement),
  [executable services](https://docs.itential.com/itential-gateway/executable-services-overview),
  [`iagctl create service executable`](https://docs.itential.com/itential-gateway/iagctl/create-service-executable),
  [`iagctl create executable-object`](https://docs.itential.com/itential-gateway/iagctl/create-executable-object)).
  The executable must already exist on the node ("The executable file must exist on your system at the specified
  path"). The overview mentions allowed-argument restrictions and path validation; the documented CLI has only
  `--exec-command`, `--description` and `--tag`.
- **An executable object can also replace the binary of an OpenTofu service** ("OpenTofu — Path to the OpenTofu
  executable on disk"). Pointing it at `terraform` is not what the documentation describes.
- **Gateway 4 had a Terraform module engine** (discovery from `properties.yml` paths, decoration, init, plan,
  apply, destroy and validate over REST, with the state as a local `.tfstate` in the module directory on the
  Gateway: [Terraform integration](https://docs.itential.com/itential-gateway/4/terraform-integration)). Gateway 4
  is staged but not deployed in this lab (ADR 0035). Gateway 5 does not carry that engine forward; the executable
  service replaces it.

Two more facts about Gateway 5 decide how Terraform runs:

- The native `opentofu-plan` service runs `tofu init` and then `tofu apply` or `tofu destroy`. It clones a
  gateway repository, and remote state is set with `--backend-config`. Its documentation describes no saved
  plan that a later apply reuses
  ([create](https://docs.itential.com/itential-automation-gateway/iagctl/create-service-opentofu-plan),
  [run](https://docs.itential.com/itential-gateway/iagctl/run-service-opentofu-plan)).
- The lab's runner image (ADR 0038) is Debian with Python 3.12 and `iagctl`. It has no `tofu` and no
  `terraform` binary. Whether the stock image provides `tofu` is not documented.

## Decision

1. **Terraform, not OpenTofu (owner, 2026-09-26: "can we not just use terraform?"), through a Gateway 5
   executable service (5.3+, documented for Terraform).** The runner image gets Terraform 1.5.7 (the version
   that wrote the state, and the last MPL-licensed release), pinned with its SHA-256 in `itential/versions.yaml`.
   The executable object `terraform-1-5-7` points at it.
   - A bare `terraform <args>` run cannot do decision 2. That needs `init`, then `plan -out`, the plan file kept
     past the run, and `apply` of that same file, and each run starts from a fresh checkout.
   - So the executable service `terraform-run` runs a small wrapper committed next to the Terraform
     (`itential/terraform-run.py`, run by a Python 3.12 executable object on the runner). The wrapper calls the
     pinned binary for `plan`, `apply` and `destroy` against `terraform/environments/dev`. P7 checks how the
     Gateway builds the command line.
   - The native `opentofu-plan` service is not used. It re-plans at apply time, and it would move a Terraform
     state to OpenTofu.
   - The lab's own modules stay on OpenTofu with local state (ADR 0029). That is a different code base with a
     different state.
2. **The approved plan is the applied plan.** `plan` runs `terraform plan -out`. It stores the plan file in the
   state bucket under `plans/<job id>.tfplan`, encrypted with the same CMK, and returns only a summary (add,
   change and destroy counts plus resource addresses) and the plan file's SHA-256. A Work Center approval shows
   the summary. `apply` downloads the plan file, checks the hash and runs `terraform apply <plan file>`.
   Terraform refuses a stale plan, so a state change between approval and apply ends the job without applying
   anything. A rejected or applied plan file is deleted. A plan file can hold sensitive values, so it never
   reaches the Platform.
3. **Every AWS secret lives in Vault and nowhere else in the lab (owner, 2026-09-26: "i want the aws secrets
   to be a part of my vault. no plaintext passwords anywhere").** This is stricter than the rest of the lab,
   where `.env` stays the source (9b deferred, ADR 0065). No AWS value is written to `.env`, a file, a job
   document, a log or a screen.
   - The IAM user `itential-terraform` gets only the actions the dev env needs. Its IAM writes are limited to a
     path and a permissions boundary.
   - The owner creates its access key and writes it straight into Vault KV `lab/aws/terraform` in one pipe, so
     the secret key is never displayed or saved:
     `aws iam create-access-key ... | jq '{access_key_id, secret_access_key}' | vault kv put lab/aws/terraform -`.
     This runs under a `make vault-login` token; `lab-admin` can write `lab/*`.
   - The Gateway reads the key through its built-in `vault` provider and hands it to `terraform-run` as
     environment variables for one execution (P1).
   - A later workflow rotates the key: it creates a new key, writes a new Vault version and deletes the old
     key.
   - The Vault AWS secrets engine (short-lived STS credentials) would be better, but no current identity can
     enable it. `lab-admin` has only `read` on `sys/mounts` (`vault-prod-config.yml`) and cannot widen itself,
     and the root token is revoked. It also needs a Gateway path other than the KV-only built-in provider, and
     egress from the Vault pod to AWS.
4. **The edge is `dc1-wan01`, with its own internet port (owner, D1, 2026-09-27: option B).** A real edge does
   not run a VPN over its management port, so the tunnel does not use Gi1 or `oob-gw`.
   - **The port:** `GigabitEthernet7` on EVE-NG's NAT cloud `nat0`, in a new front-door VRF `INET`, at
     172.29.129.250/24 with a default route in `INET` to the EVE-NG host (.254). The address is at the top of
     the host's DHCP pool, which hands out from .1, and nothing else in the lab uses `nat0`. Gi3 is taken (it
     faces `dc1-fw01`), and Gi7 already exists on the running node, so cabling it needs no restart.
   - **The path:** Gi7 → `nat0` → the EVE-NG host masquerades to 192.168.68.240 → the home router NATs to the
     public address → AWS. There are two NAT layers, the same count as through `oob-gw`, so the tunnel uses
     NAT-T (UDP 4500).
   - **An inbound ACL** on Gi7 admits only IKE (UDP 500 and 4500), ESP and the ICMP replies the router's own
     checks need. It is tightened to the EIP when the tunnel is built.
   - **The tunnel:** `Tunnel10`, a static VTI in the pattern of Eve-NG's `ipsec_tunnel.j2`, with an IKEv2
     profile that has `match fvrf INET` and matches only the EIP. Its inner side is in the global table, with
     static routes to the VPC. The branch profile `LAB` matches in `fvrf WAN`, so the two do not overlap.
   - The on-prem prefixes passed to AWS are 10.101.0.0/16 (DC1) and 10.103.255.0/24 (the WAN loopbacks, for
     the data-plane probe).
   - The port is a topology change and goes straight to production (owner, 2026-09-28: "Dev is for Itential
     work only"). The workflows still go to the dev tier first (ADR 0063). One AWS deployment exists at a time:
     the secret name and resource names are fixed, so dev and production never have a tunnel up at once.
5. **Vault is the source of the PSK; Terraform never sees it.** With Terraform 1.5.7, the module's
   `random_password` sits in the state in clear text. The state is encrypted at rest, but anyone who can pull
   the state can read the value, which is how `errored.tfstate` came to hold one. The new flow:
   - The deploy workflow generates the PSK on the runner.
   - It writes the PSK to Vault `lab/aws/vpn-psk` through a write-only AppRole `itential-aws-psk-writer`. Its
     policy allows only `create`/`update` on that one path, and it is bound to iag-01. Its secret ID is itself
     in Vault, read through the Gateway's read-only provider.
   - It copies the PSK to Secrets Manager with `put-secret-value`. strongSwan still reads it there at boot.
     Terraform manages only the secret's container (`ignore_changes` on versions).
   - `vpn-bootstrap` waits for a value to exist, and `null_resource.strongswan_ready` moves out of the apply
     into the workflow. Otherwise the apply would wait on a strongSwan that waits on the PSK.
   - The router stores the key as type 6 (`password encryption aes`). The owner types the master key once and
     keeps it in Vault. A Configuration Manager backup therefore never holds the PSK in clear.
   - A test fails any job output, rendered file or log line that contains a Vault or Secrets Manager value.
6. **The push goes through the broker with a Vault reference (after P3).** The rendered IPsec block carries
   `$GATEWAYSECRET_aws-vpn-psk`, an alias on the built-in `vault` provider. Configuration Manager `sendConfig`
   sends the block, and the Gateway resolves the reference at push time. That is the broker path whose replies
   the lab already checks (ADR 0066). If P3 shows that `sendConfig` does not resolve references in config text,
   the fallback is a Gateway python-script service. It reads the same alias, substitutes it in memory, pushes
   with Netmiko and redacts the key line in its result.
7. **Verify with four independent signals and fail if they disagree.** (a) The device: `show crypto ikev2 sa`
   READY and `show crypto ipsec sa` counters rising. (b) The Eve-NG `cloud_tunnel_health` parser, ported as a
   Gateway service. (c) The AWS side: invoke the Lambda, and CloudWatch `TunnelEstablished` Maximum = 1. (d) The
   data plane: a ping from `dc1-wan01` Loopback0 to the strongSwan private address through the tunnel. If any
   two signals disagree, the job ends with a reason, not a warning.
8. **Teardown runs in reverse.** It removes the on-prem block (and checks it is gone), then runs a
   plan-approve-apply `destroy`, then removes the NetBox objects. The recovery-window problem is fixed at the
   source: `recovery_window_in_days = 0` (owner, 2026-09-27: "I like the idea of deleting it when it tears
   down"). The value is regenerated on every apply, so there is nothing to recover and nothing for the owner
   to type: each deploy writes a new PSK version to Vault (`vault kv get lab/aws/vpn-psk` shows the current one,
   and KV v2 keeps the earlier ones). A force-delete step in the workflow is not needed.
9. **`cloud-devops-pipeline` goes to a private GitHub repository first (owner, D4, 2026-09-27).** The Gateway
   clones services from a repository, and the changes above need review like this repo's. gitleaks found
   nothing in the 7 commits. State, plan and tfvars files are ignored and were never committed.
   - The commit author is rewritten from the Mac's host address to `elliot@thetech-e.com` before the push.
   - The two uncommitted files (`docs/phase2-runbook.md`, `app/api/uv.lock`) are reviewed, and the owner
     decides whether to keep or drop them.
   - The Gateway needs its own read-only credential for the private repository (a deploy key on that one
     repository), and under decision 3 it lives in Vault. How a Gateway 5 repository takes its credential is
     checked in the Itential documentation when `terraform-run` is built.
10. **The NAT gateway is a switch (owner, D3, 2026-09-27).** `modules/vpc` gets `enable_nat_gateway`, default
    `true`, so the pipeline's own behaviour does not change. The Deploy workflow takes it as an input, so a run
    turns the NAT gateway on or off through the same plan, approval and apply ("I want to make sure that this
    is something that I can turn up and turn off at any time I want"). Whether the ECS tasks sit in private
    subnets, and so lose egress while it is off, is checked when the change is written.

## Owner decisions (D) and dev probes (P)

- **D1** Resolved by the owner, 2026-09-27: its own internet port on EVE-NG `nat0` in VRF `INET`, not VRF `MGMT`
  on Gi1 (decision 4). The port is Gi7, because Gi3 is cabled to `dc1-fw01`.
- **D2** Resolved by the owner, 2026-09-26: decision 3.
- **D3** Resolved by the owner, 2026-09-27: `enable_nat_gateway`, default true, switched per run (decision 10).
- **D4** Resolved by the owner, 2026-09-27: a private repo `E-Conners-Lab/cloud-devops-pipeline`, the author
  rewritten first, and the Gateway's access to it set up (decision 9).
- **P1** Does a `$GATEWAYSECRET_` alias resolve into a python-script service's environment or decorated input?
- **P2** Does the runner reach STS, S3 and Secrets Manager through `oob-gw`?
- **P3** Does `sendConfig` resolve a `$GATEWAYSECRET_` reference inside the config text (decision 6)?
- **P4** Does IKEv2 with NAT-T come up through two NAT layers (the EVE-NG host, then the home router)? The home
  address is public (owner, 2026-09-27); whether it stays stable between deployments is still open.
- **P5** Does a Work Center approval gate the apply, and does a rejection end the job cleanly (ADR 0066)?
- **P7** How does an executable service build the command line (`<exec-command> <filename> <args>`)? Does its
  `--secret` take a value from the built-in `vault` provider, or only from the Gateway's own store? This decides
  whether decision 3 needs P1's `$GATEWAYSECRET_` route.
- **P6** Does a write-only AppRole (`create`/`update`, no `read`) let the runner write `lab/aws/vpn-psk` while
  the Gateway's reader role still resolves it?

## Alternatives rejected

- **The native `opentofu-plan` service, or the same service with its binary swapped for `terraform`.** Covered
  in decision 1: it re-plans at apply time and moves the state to OpenTofu. The swap is not what the documentation
  describes.
- **Gateway 4's Terraform engine.** It keeps the state as a local file on the Gateway, and it means deploying the
  Gateway 4 that ADR 0035 deferred.
- **Running Terraform from the Platform host or the Mac.** The Gateway is the lab's execution layer. Anything
  else puts AWS credentials somewhere that 9a took credentials away from.
- **Terraform reading the PSK through the `vault` provider, or taking it as a sensitive variable.** Both write
  the value into the state.
- **The PSK in Secrets Manager only, fetched at push time (the Eve-NG design).** It works, but the value would
  live outside Vault. It also needs either a second AWS credential for the itential/assets provider plugin or a
  push outside the broker.
- **Seeding the AWS key from `.env` like the other lab secrets.** It breaks the owner's rule for AWS secrets.

## Consequences

- The runner image grows by one pinned binary. A Terraform upgrade is a pin change with a licence check, because
  releases after 1.5.7 are BSL.
- `cloud-devops-pipeline` needs changes in `modules/vpn`: `onprem_cidrs` exposed by the dev env,
  `recovery_window_in_days`, no `random_password` and no secret version, a `vpn-bootstrap` that waits for the
  value, and `strongswan_ready` removed. D3 is separate. The dev env's other modules (ECR, ECS, RDS) are applied
  with it, as they are today.
- Plaintext AWS or PSK material that exists today outside this design, for the owner to clear: the admin user's
  static key in `~/.aws/credentials` on the Mac (IAM Identity Center would remove it), `errored.tfstate`, and
  the noncurrent S3 state versions.
- The owner still deletes `terraform/environments/dev/errored.tfstate` and `apply.tfplan` by hand. Noncurrent
  S3 state versions keep old PSKs from destroyed deployments. Those secrets are deleted, but a
  noncurrent-version expiry rule would remove the copies.
- Every apply costs money: about $1 a day with the NAT gateway, cents without it, plus the fixed $1 a month for
  the CMK. The `vpn-lab-monthly` budget ($5) stays.
