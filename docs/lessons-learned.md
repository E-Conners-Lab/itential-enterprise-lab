# Lessons learned

Problems met while building and running the lab, what caused them, and what changed so they do not come back. Each
entry names where the full record lives: an ADR, a verify criterion or a PR. The README describes the lab as it is;
this file is how it got there. Newest first.

## Vault and secrets

**Revoking Vault's root token left no administrator path** (2026-09-24, ADR 0065).
Vault 2.0 authenticates the `sys/generate-root` and `sys/rekey` endpoint families, which earlier versions left open
(they answer 403 without a token unless the server lists them in `enable_unauthenticated_access`). The root token was
revoked on the assumption that `generate-root` and the unseal key could always issue a new one. Vault was rebuilt,
since nothing depended on it yet, with a least-privilege administrator login (`lab-admin`), and
`make vault-revoke-root` now refuses unless that login has been proven. Test a recovery path before removing the one
you have.

**A refused AppRole login still spends a one-use secret ID** (2026-09-24, ADR 0065).
Destroying the secret ID afterwards answers 500 "failed to find accessor entry". The binding test treats that as the
expected outcome.

**The Vault chart's default readiness probe makes a sealed Vault unreachable** (2026-09-24, ADR 0065).
It runs `vault status`, which fails while sealed, so the pod leaves the Service and the VIP has no endpoint to unseal
through. `k8s/vault/values.yaml` probes `/v1/sys/health` with sealed and uninitialised mapped to success.

**An integration field resolves a `$SECRET_` reference only when it is the whole value** (2026-09-23, ADR 0065).
`Token $SECRET_...` was stored verbatim. Vault holds the whole `Authorization` value (`header`, with the `Token `
prefix) and the integration references it alone.

**Gateway 5's configuration import accepts unknown fields without an error** (2026-09-23, ADR 0065).
Every import of the Vault provider is proven by exporting the configuration and comparing it.

## Production environment

**A stray dev-stack Compose override on a production Platform node** (2026-09-24, ADR 0065).
`/opt/itential/compose.override.yml` on one Platform node was a copy of the dev stack's override, which Compose merges
automatically; `docker compose up` failed there, while the running container was untouched. It was moved aside. The
dev stack and production share the `/opt/itential` path, so a dev play must never be pointed at a production host.

**The nginx health check probed an address nginx did not listen on** (2026-09-23, PR #59).

## Networking

**Frames tagged VLAN 1 were dropped by the VMs, cutting off streamed replies** (2026-09-24, verify S1.7).
The home mesh access point adds 802.1Q VLAN 1 tags to some frames on its wired ports. The Proxmox host bridge
`vmbr0` was not VLAN-aware, so it passed the tagged frames to the VMs, whose kernels discarded them as not addressed to
them. Short replies got through and streamed ones stopped midway: local-model agent sessions failed with
`fetch failed` or `terminated`. No firewall or interface counter moved; kernel drop tracing (`skb:kfree_skb` with
reason `OTHERHOST`, the trace clock set to `mono` so it lines up with a packet capture) found it. `vmbr0` is now
VLAN-aware and strips VLAN 1 before the VMs, and S1.7 records that configuration.

**A workstation VPN client routed home-LAN replies into its tunnel** (2026-09-24).
The client had accepted a subnet route for the home LAN advertised by a peer that was offline, and some replies to the
lab gateway went into the tunnel. Route acceptance was turned off on that workstation.

**Local model contention on the inference host** (2026-09-24, ADR 0061).
Another application on the same Ollama host loaded a large model on a schedule, evicting the agents' model mid
session. The inference host serves one model at a time; other consumers were stopped.

## Workflows and agents

**Renamed workflows left verify checks matching fragments of the old names** (2026-09-24, PR #63, ADR 0067).
Three agent checks failed although the agents used the right workflows, and two checks for a forbidden write tool
could never fail again, because they matched a fragment of the old name. Tool checks now read the names from
`itential/versions.yaml`, and `tests/test_workflow_names.py` fails if a retired name appears outside the record.

**Shell code split workflow names that contain spaces** (2026-09-24, ADR 0067).
Names passed unquoted, or looped over as words, broke. Names are quoted wherever the shell passes them and read one
per line.

**Workflow names with spaces work as local-model tools, but only for global workflows** (2026-09-24, ADR 0067).
Studio project workflows get tool names like `@<id>: <name>`, which the local model cannot call. Agent workflows stay
global.

**A device task could report success on a failure** (2026-09-23, ADR 0066).
Gateway 5 finishes `sendCommand` and `sendConfig` as `success` when it returns a JSON-RPC error envelope, and
`send-config` reports `success: true` for lines the device refused. A task without an edge for its failure left the
job in a retryable `error` state, so a calling agent or Lifecycle Manager action waited forever. Every device result
is now checked and every failure ends the job with a reason.

## Verification

**The Proxmox UI reorders `/etc/network/interfaces` when it saves** (2026-09-24, PR #63).
S1.7 extracted the `vmbr0` stanza by position and broke; it now extracts `nic1` and `vmbr0` by name.

**`dig` ignores macOS per-domain resolvers** (2026-09-24).
`lab.internal` resolves through `/etc/resolver/lab.internal`, which `dig` does not read. Verify checks resolve names
through the system resolver.

**Escaped quotes inside Python f-strings in shell one-liners** (2026-09-24).
`\"` inside a single-quoted `python -c` expression is a syntax error. The scripts use `%` formatting or unescaped
nested quotes instead.
