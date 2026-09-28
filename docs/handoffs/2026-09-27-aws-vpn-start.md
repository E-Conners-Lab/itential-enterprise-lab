# Handoff — track `aws-vpn` (PID S13) start: design committed, nothing built (2026-09-27)

Paste the block below into a new Claude Code session opened in this repo (the `itential-builder` plugin and the
`itential-docs` MCP server are installed at user scope).

---

Continue the itential-enterprise-lab project, track `aws-vpn`: Itential runs the existing AWS site-to-site VPN
(`~/PycharmProjects/cloud-devops-pipeline`, `terraform/environments/dev`) end to end. The track replaces the manual
runbook of `~/PycharmProjects/Eve-NG_Agent` (`docs/runbooks/deploy-cloud-aws.md`).

The branch `phase-aws-vpn/design` holds the design only: PID 1.36 (new S13, a phase-table row), ADR 0068
(**proposed**), a README row, and `tests/test_aws_vpn.py`, which took over the PID version pin from
`tests/test_vault.py`. It sits on main after PR #67 (PID 1.35), which merged on 2026-09-27. D1, D3 and D4 were
answered the same day (ADR 0068): the edge gets its own internet port, Gi7 on EVE-NG `nat0` in VRF `INET`.

Read, in this order:
1. This file.
2. ADR 0068.
3. `docs/PID.md`: S13 and amendment 1.36.
4. ADR 0065 (Vault and its built-in clients, and the 9b deferral).
5. ADR 0038 (the runner image).
6. `itential/gateway5-runner/Dockerfile`.
7. `ansible/playbooks/vault-prod-config.yml` (what `lab-admin` can and cannot do).
8. `topology/configs/c8000v.j2` (the existing IKEv2 block).

## Owner rules for this track (2026-09-26)
- **Terraform, not OpenTofu.** Terraform 1.5.7 runs as a Gateway 5 executable service `terraform-run`, a service
  type Itential documents for Terraform (Gateway 5.3+), with the binary pinned in the runner image.
  - A wrapper does `plan -out`, stores the plan file encrypted in the state bucket, waits for a Work Center
    approval, then applies exactly that file.
  - The native `opentofu-plan` service is not used: it re-plans at apply time.
- **Every AWS secret lives in Vault only.** Nothing goes in `.env` or any file.
  - The IAM key of `itential-terraform` is piped straight into KV `lab/aws/terraform`.
  - Vault is the PSK's source (`lab/aws/vpn-psk`). Secrets Manager holds a copy for strongSwan, and the PSK leaves
    the Terraform state.
  - `tests/test_aws_vpn.py` fails if an AWS secret appears in `.env.example`.
- **No `terraform apply`/`destroy`, `git push` or device config push without Elliot's explicit go.** Anything that
  could not be checked is marked UNVERIFIED.

## Found by the read-only check (2026-09-26)
- **AWS:** nothing is deployed in any region. The S3 state is the real state and it is empty. The local
  `errored.tfstate` and `apply.tfplan` in `cloud-devops-pipeline` are stale; the owner deletes them.
- **Costs:** the dev env has no ALB. The only recurring cost is the NAT gateway, which `modules/vpc` always
  creates.
- **The edge:** `dc-ce-1` is not in this lab. The edge is `dc1-wan01` (AS 65100).
  - The front door is proposed as VRF `MGMT` on Gi1, which reaches the internet through `oob-gw` (open decision
    D1).
  - `onprem_cidrs` must become this lab's prefixes, and `recovery_window_in_days` becomes 0.
- **Vault:** `lab-admin` writes `lab/*` and creates AppRoles, but cannot enable mounts. The Vault AWS secrets
  engine is therefore out for now.

## Next
1. Owner answers:
   - **D1:** front door on MGMT/Gi1, or a new Gi3 on EVE-NG `nat0`.
   - **D3:** an `enable_nat_gateway` switch.
   - **D4:** push `cloud-devops-pipeline` to a private repo, with its commit author fixed first.
2. PR plan:
   1. This design.
   2. The `cloud-devops-pipeline` changes: the PSK out of Terraform, `onprem_cidrs`, the recovery window, the
      IAM user.
   3. `terraform-run` and `Deploy AWS VPN` on the dev tier first (probes P1, P2, P5-P7).
   4. The handoff: NetBox seed, the IPsec block with `$GATEWAYSECRET_aws-vpn-psk` through the broker, type-6 PSK;
      `clab-rtr1`, then `dc1-wan01` (P3, P4).
   5. `Verify AWS VPN` (four signals that must agree) and `Tear Down AWS VPN`.
   6. Later: `cloud_tunnel_health` as a read-only tool for `lab-netops`.
3. Next ADR is 0069, next PID amendment 1.37.

## Open items carried (outside this track)
- `c8000v.j2` uses the device password as the branch-tunnel IKE PSK, and the template has no `password encryption
  aes`. It is probably in clear in `show run` and in the Configuration Manager backups (UNVERIFIED on a device).
- `ruff` is not on the workstation PATH, so the ruff hook fails silently. The NetBox MCP server rejects its token
  ("Invalid v2 token").
