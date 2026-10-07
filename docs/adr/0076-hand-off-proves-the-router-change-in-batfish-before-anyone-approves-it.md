# 0076 — Hand Off AWS VPN proves the router change in Batfish before anyone approves it

- **Status:** accepted (owner, 2026-10-06)
- **Date:** 2026-10-06
- **Amends:** PID S13 (R7: wording and acceptance criteria)
- **Related:** ADR 0068 (Hand Off AWS VPN and the rendered block), ADR 0038 (the Gateway runner), ADR 0065 (Vault),
  ADR 0070 (the AWS VPN on production), ADR 0073 (a pinned cloud-devops-pipeline service on the shared account)

## Context

R7 on the S13 roadmap says: "model the new VRF and ACL before the router change and prove the AWS side reaches only
the allowed prefixes". The wording is off: `Hand Off AWS VPN` creates no VRF. `Tunnel10` sits in the global table with
`tunnel vrf INET`, and INET already exists. What the block changes is the firewall (the zone pairs between INSIDE, AWS
and self, the two ACLs the class maps match), the internet port's ACL (`INET-IN` entries 20 and 30 become the peer's
UDP 500 and 4500) and the routing (the static route to the VPC with its Null0 backup, a BGP `network`). Today a person
approves that block from its text on the Work Center card. Nothing proves, before the push, what the router will do
with it.

The block is rendered only by cloud-devops-pipeline `itential/lab_edge_render.py` from the pinned target in
`itential/versions.yaml` and the deployment's outputs; `lab-edge-push` re-renders and refuses on a SHA-256 mismatch.
The natural gate is between Hand Off's 5e (the block's SHA-256 and masked text are on the job) and 6a (the card).

The lab already has Batfish experience in `~/PycharmProjects/networkops` (`core/digital_twin.py`). Its approach is
reused, not its code: that project runs `batfish/batfish:latest` unpinned, its differential check answers a generic
gained/lost question, and its change workflow is fail-open with no verdict shown to anyone.

Three read-only probes (2026-10-06) decided the design:

- **P1, parse fidelity** (a pinned `batfish/batfish` on the Mac, pybatfish 2025.7.7.2423, the intended config plus
  the block rendered with a fake public peer):
  - The block appended as pushed does not model: `no 20`, `no 30` and the re-added entries fall out of the ACL
    context. The candidate must be the block *applied* the way IOS applies it (entries 20 and 30 replaced in place,
    everything else appended). P2 showed the running config has exactly that shape.
  - The zone-based firewall between interface zones is modeled. Flows from the VPC entering `Tunnel10` are denied by
    the INSIDE zone policy to every lab destination; flows from inside reach the VPC only when sourced from the pinned
    lab prefixes; the branches (via Tunnel1) and 10.103.0.0/16 are denied. `INET-IN` is exact: the peer's UDP 500 and
    4500 accepted, any other source denied, the pre-change config denies the peer too. AWS to the MGMT address is
    NO_ROUTE.
  - Not modeled: the **`self` zone** (an undefined reference; VPC sources reach the router's own addresses as
    ACCEPTED while ZP-AWS-SELF drops them), **`tunnel vrf`** and **`prf sha384`** (unrecognized, so no IPsec session
    ever establishes in the model; on a single-node snapshot `Tunnel10` is still active and routes through it
    resolve), and the ACL `fragments` clause (ignored).
  - A whole-lab snapshot from `topology/generated/configs` brings up 10 of 20 BGP sessions: the WAN overlay's
    Tunnel1/2 fail IKE in the model, so Batfish cannot prove the VPC prefix stays off the branches.
  - `differentialReachability` refuses entering-flow queries with default headers (sources must be given as
    0.0.0.0/0) and returns one example flow per ingress interface, so "nothing else changed" must be asked over the
    header space outside the expected gains and come back empty. `Tunnel10` does not exist in the base snapshot, so
    flows entering it are plain reachability on the candidate.
  - The candidate's parse warnings are the base's 11 plus 10 known IKEv2 and fvrf lines; none touch forwarding.
- **P2, the router today** (through `Run Show Command on a Device` on production): the model and the router agree on
  every point Batfish can answer. The static VPC route via `Tunnel10`; `INET-IN` 20 and 30 permit the live peer with
  real match counters; four zone pairs as rendered; `Tunnel10` in zone AWS. Both PM-AWS-DROP policies show 0 drops
  (nothing from AWS has tried). The branch router is advertised 11 prefixes and no VPC prefix; dc1-leaf01 receives it.
- **P3, hosts:** tools-01 has 7.2 GB of 7.9 GB free, ufw inactive, Compose v5.5.1, and iag-01 reaches it. The clab VM
  has 3.3 GB of 20 GB free (four QEMU VMs hold 16 GB). iag-01 is the runner itself. k3s was excluded by the roadmap
  note (requests already 19.1 of ~24 GB). Batfish at a 2 GB heap needs about 3 GB.

## Decision (owner, 2026-10-06)

1. **The host is tools-01.** Batfish joins `itential/ha2/tools.compose.yml.j2` (`platform-ha2-tools.yml`): the
   `batfish/batfish` image pinned **by digest** in `itential/versions.yaml` (never a tag), `JAVA_TOOL_OPTIONS=-Xmx2g`
   with a 3 GB memory limit, no persistent volume (a snapshot lives only for the answer). Batfish's API has no
   authentication, so port 9996 is published on 10.100.0.81 only and a DOCKER-USER allowlist admits iag-01 alone (the
   clab host's pattern); 9997 is not published. Never k3s, never the clab VM (measured).
2. **The candidate is the router as it is, with the block applied.** A new cloud-devops-pipeline Gateway service
   `batfish-check` (python-script, pybatfish pinned, on the shared device account like `lab-edge`) reads the running
   config the way the precheck reads (the same session module), **scrubs** every key and password line to a marker
   before anything leaves the runner (`pre-shared-key`, `secret`, `password`, `key config-key`, SNMP communities; the
   scrubbed line count is reported), applies the block semantically (`INET-IN` 20 and 30 replaced in place, the rest
   appended; a test asserts every line of the block is accounted for), sends the base and the candidate to Batfish,
   reads the answers and deletes both snapshots. The intended config in `topology/generated` is not the source: it
   proves intent, not the device.
3. **Seven checks, each pass or fail, five simulated and two structural.** Simulated: (a) *parses* - the candidate's
   warnings are the base's plus an exact allow-list of the IKEv2 and fvrf lines, nothing else unrecognized; (b) *AWS
   reaches no lab address* - no flow from the VPC entering `Tunnel10` succeeds to any lab, WAN or management
   destination; (c) *the lab reaches AWS only from the pinned prefixes* - with the search inverted, no source outside
   `lab_prefixes` succeeds to the VPC; (d) *INET-IN* - the peer's UDP 500 and 4500 are accepted, any other source to
   the front door is denied, and nothing else on the internet port changed; (e) *nothing else gained or lost* -
   differential reachability per ingress interface, outside the expected gains (the pinned prefixes to the VPC, the
   peer's two flows, anything to `Tunnel10`'s own /30), is empty. Structural, labelled on the card as text checks that
   Batfish cannot simulate: (f) ZP-AWS-SELF carries PM-AWS-DROP in the candidate (the `self` zone); (g) the
   NO-AWS-VPC prefix-list stays outbound on all three WAN and iBGP neighbours (the WAN overlay). The live post-push
   read (ADR 0068) remains the data-plane truth for (f) and (g).
4. **Where it sits and what it does with the answer.** A new Hand Off section between 5e and 6a runs `batfish-check`
   with the same inputs the render took; the service re-renders and refuses when its SHA-256 differs from the one on
   the job, so the verdict is about the exact block the push will send. A verdict with **every check passed** goes on
   the card. A verdict with **any check failed** ends Hand Off before the card with nothing sent, the failing checks
   and their example flows in the job (a wrong block is not a thing a person should be able to approve over). When
   Batfish is **unreachable, times out or cannot answer** (the service's own failure or a non-zero exit), the card
   shows "not proven" with the reason and approval stays possible (owner: fail-open for the unavailable case, since
   the push's own prechecks and the post-push read still guard the change).
5. **Scope: dc1-wan01 only**, single-node snapshots. The whole lab from the generated configs would not prove more
   (P1: the WAN overlay does not model), and the one extra claim it could cover is read live from the router today.
6. **The drill proves the checks can fail.** `batfish-check` takes a `--drill` mode that corrupts the candidate in a
   known way after the scrub: `unzoned-inside` (an INSIDE membership stripped), `acl-any` (`AWS-FROM-LAB` permits
   any source), `inet-open` (`INET-IN` 20 from any). A workflow `Drill Batfish Gate` runs the same check section with
   each mode and expects its matching check to fail; the section is held identical to Hand Off's by a test (no child
   jobs). Widening `lab_prefixes` in the inputs is not a drill: it would widen the oracle with the block.
7. **Pins and tests.** `pybatfish==2025.7.7.2423` in the runner's requirements; `batfish-check` pinned by
   cloud-devops-pipeline commit in `itential/versions.yaml` like `fabric-bgp`; the image digest next to it.
   cloud-devops-pipeline tests: the scrubber against fixtures that hold every secret shape the router prints, the
   apply function against the rendered block, the question set against recorded Batfish answers. Lab tests: the pins,
   the compose entry and its limits, Hand Off's new tasks and edges, the identical-copies test with the drill.
8. **Delivery.** One cloud-devops-pipeline PR (the service and its tests), one lab PR (compose and play, the Hand Off
   section, the drill workflow, the pins, this ADR and the PID amendment), then the production converge pair (memory:
   plays directly, NETBOX_API exported, the Vault token renewed first), then the proof on production: a Hand Off on
   dc1-wan01 with the verdict passing on the card, and the three drill modes failing. The measured answer time and
   memory go into an amendment here.

## Consequences

- A person approving the router change sees, for the first time, what the router will do with the block rather than
  its text: five simulated and two structural answers, computed on the device as it is that minute.
- Two places now hold a scrubbed copy of the running config for a few seconds: the runner's temporary directory and
  the Batfish container's memory on tools-01. The leak sweep (S13 criterion 2) gains the Batfish host.
- One more container on tools-01 (about 3 GB of its 8 GB) and one more pinned service to bump when
  cloud-devops-pipeline moves.
- Batfish's blind spots are written down: the `self` zone, the front-door VRF, IKEv2 proposal lines, fragments. The
  card labels the structural checks as text checks so nobody reads them as simulation.
- The gate is fail-closed on a wrong block and fail-open on a missing Batfish. A later owner decision can close the
  second once the service has a run history.
- Verification: `tests/test_aws_vpn.py` (pins, compose, the Hand Off section, the drill's identical copy),
  cloud-devops-pipeline's own tests, and `verify/test-13a-aws-vpn.sh` gains two R7 cases (a Hand Off on production shows the
  verdict; the drill fails each check).
