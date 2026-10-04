# 0070 — The AWS VPN runs on production; the dev tier gives up its AWS access, and AWS work goes straight to production

- **Status:** accepted (owner, 2026-10-04)
- **Date:** 2026-10-04
- **Amends:** PID S13 (placement, R8 pulled forward), ADR 0068 (which Platform owns the single AWS state), ADR 0063
  (dev first, for AWS work)
- **Related:** ADR 0055 (the replay onto production), ADR 0065 (Vault through the built-in clients)

## Context

R1 (Hand Off and Verify), R2 (Tear Down), build step 12 (the leak sweep, test-13a) and R2b (the timed teardown) were
built and proven on the dev tier. Every step added another dev-first proof before production saw anything. The owner,
2026-10-04: "it just seems like we keep adding things on to test and I really should have thought out the dev
environment ... If I could skip dev and go with all the stuff, I probably would." He wants production ready now, so
that FlowAI agents and further AWS workflows are built where they will run.

Only one Platform can own the deployment: the resource names are fixed, and there is one Terraform state. The live
tunnel was deployed by the dev tier, and its key is in the dev Vault only.

## Decision

1. **One switch, `aws_vpn.tier: prod` (itential/versions.yaml).** The tier it names gets:
   - the AWS VPN's triggers, forms and the hourly schedule (`platform.yml`);
   - `terraform-run`, `aws-vpn-psk` and the edge services on its Gateway (`gateway-terraform-run.yml`, renamed from
     `dev_services` to `edge_services`);
   - the edge aliases (`gateway-vault.yml`, renamed from `dev_gateway_aliases` to `edge_gateway_aliases`).

   The other tier's converge removes every AWS VPN trigger and automation (`tasks/aws-vpn-retire.yml`), so nothing
   there can start a Deploy or a Tear Down. Its workflows and forms stay, inert.
2. **Production gets its own secrets, never copies.** Each is written by the owner's make targets with `TIER=prod`:
   `aws-key`, `deploy-key` and `edge-account` (now tier-aware). A key or password is never moved between Vaults. The
   router's account line changes to production's password (`make edge-account-line TIER=prod`).
3. **The deployment's key is bound after the first Deploy.** Its two aliases are marked `after_deploy`: until the
   first Deploy AWS VPN on the tier has written `aws/vpn-psk`, they stay unbound and `lab-edge-push` leaves their
   secrets out. A Gateway converge after that Deploy binds them, before Hand Off. Any other missing entry still
   refuses the converge.
4. **The cutover tears down from dev and redeploys from production** (owner's choice: about 20 minutes without the
   tunnel, and a fresh key that only production's Vault holds). In this order:
   1. This PR merges. The owner runs `make vault-login`, `make aws-key TIER=prod`, `make deploy-key TIER=prod` and
      `make edge-account TIER=prod`. The router is not touched yet.
   2. Production converges: the replay plus the Gateway play. The dev tier is not converged yet: Tear Down needs its
      automations and its router account.
   3. Tear Down AWS VPN from the dev page or form: the router's block, then the AWS destroy.
   4. The owner pastes production's account line on dc1-wan01 (`make edge-account-line TIER=prod`).
   5. Deploy from production's page, then a Gateway converge to bind the key, then Hand Off and Verify.
   6. The dev tier converges, which retires its AWS VPN. The owner deletes the dev tier's IAM access key and the dev
      Vault's `aws/*` entries. That is the hard cut-off: no credential, no access.
5. **The twin's window is closed.** clab-rtr1 lives on the dev host. It stays in versions.yaml for its render and
   twin tests, but it is no target any more.
6. **The branded page is served by production's load balancer** at `/portal/deploy-aws-vpn/`. It has the same
   headers as the dev tier's portal, and the same origin as the Platform.
7. **AWS work goes straight to production from now on.** That covers new AWS workflows, Gateway services and FlowAI
   agents. Tests first, Work Center approvals for every write, and a production snapshot compare (with an allow-list
   for what a change adds) stay as the safety net. Topology changes already went straight to production
   (2026-09-28). Dev-first remains for other Itential work.

## Consequences

- S13.2a-d move to `verify/test-09a-vault.sh`, production's Vault test. S13.2d runs only on the AWS VPN's tier.
- `verify/test-13a-aws-vpn-dev.sh` sweeps the dev tier, which no longer runs the AWS VPN. Production's
  `test-13a-aws-vpn.sh` follows in its own PR: production's Vault and log locations differ (HA2: the Platform nodes,
  iag-01).
- The hourly schedule runs on production. Each run is one Terraform state read.
- R8 is done in substance with the cutover. What remains of it is production's test-13a.
- Going back to dev is the same switch, `aws_vpn.tier: dev`, and the same cutover in reverse, with fresh dev keys.

## Amendment 2026-10-04: what the cutover taught

- **A retired tier keeps its aliases.** The Gateway's configuration import replaces aliases by name but never removes
  one it is not sent. Gateway 5.5 deletes an alias only through `iagctl` in client mode, which needs the Gateway's
  own admin login (server mode refuses: measured on dev). The owner chose to accept and name them:
  - `vault.retired_gateway_aliases` (the twin's), and, on the tier that does not run the AWS VPN, its edge aliases,
    are left out of the converge's check and of S8.3b, and listed by name;
  - any other unexpected alias still fails;
  - their Vault entries are deleted, so they resolve to nothing.
- **Every export read now waits out a Gateway restart.** Production's converge failed twice on a 503 right after the
  Gateway server restarted: the first run generated the Gateway's secrets key, and the run that bound the
  deployment's key. All four export reads in `gateway-vault.yml` and `gateway-terraform-run.yml` now retry for a
  minute.
- **Production's Vault policy had to catch up.** It predated the deploy key's path, and S13.2b's 403 found it.
  `make vault-config` re-applies the policies from versions.yaml. It belongs in any first promotion of a Gateway
  service that reads a new path.
- **test-13a runs on production:** `verify/test-13a-aws-vpn.sh`. It reads the Platform nodes' and the Gateway VM's
  logs, and production's Vault with the owner's administrator token. The dev script is gone.
- **The cutover took about 27 minutes of tunnel downtime** (06:10-06:37Z):
  - Tear Down `4543f328` (dev);
  - Deploy `d5430afb`, Hand Off `04ca2dfd` and Verify `1a5ca206` (production).
