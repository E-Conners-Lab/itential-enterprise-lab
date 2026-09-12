# 0060 — Local inference runs on the Mac Mini; the lab runs none

- **Status:** accepted (owner instruction, 2026-09-11)
- **Date:** 2026-09-11
- **Related:** ADR 0037 (FlowAI agents; in-lab Ollama, Mac optional), ADR 0046 (agent fleet, the twins), ADR 0053 (the production environment), ADR 0059 (a tool call must end the job), PID S4c/S4d.5

## Context

ADR 0037 put Ollama in the lab and made the Mac Mini an *optional* fast path, so the lab stayed
self-contained and nothing in `verify/` depended on a machine the owner might switch off. That held
until the lab was measured under load.

`tools-01` is 4 vCPU, 7.8 GiB, **no GPU**. Its Ollama container was capped at 6 GiB and ran at
**91%** of that cap. On 2026-09-11, mid-verify:

```
22:42:15  llama-server completion error: Post "http://127.0.0.1:37871/completion": EOF
22:42:33  task 0   <- the model reloaded from scratch: the runner had died
```

The session that happened to be in flight (`diagnostics-local`, S4d.5j) ended `FAILED` with **zero
tokens**, and the Platform reported *"ollama model invocation failed: model runner has unexpectedly
stopped, this may be due to resource limitations"*. It was the criterion's first run, so it looked
like a new test defect; it was the host.

The speed was the deeper problem. Measured the same day: five NetBox device objects (17.9 kB) plus
tool schemas made a 7,823-token prompt, and `qwen2.5:7b` on those four cores ingests at **~22
tokens/sec** - about 350 seconds, past the Platform's inference timeout (ADR 0059 context). Every
design decision for the twins - three tools each, a reducing workflow, six fields per device - was
shaped by a model that was slow because it had no GPU.

The Mac Mini has Apple Silicon with Metal, already serves nine models including `qwen3:30b-a3b` (a
MoE with ~3B active parameters: faster *and* stronger than the 7B), and sits on the same home LAN.
Routing from the lab already worked through oob-gw; only the macOS firewall was dropping TCP 11434,
because it allows binaries by resolved path and the running server is the Homebrew one, not
`/Applications/Ollama.app`.

The owner's instruction: **no inference on the server; the Mac Mini is the inference machine.**

## Decision

1. **No lab host runs Ollama.** The service and its volume leave `itential/compose.override.yml` and
   `itential/ha2/tools.compose.yml.j2`; the `ollama` compose profile, the `OLLAMA_IMAGE` pin and
   `tools.ollama_port` go with them. Both plays now `docker rm -f ollama` so a dormant container
   cannot linger holding 6 GiB of a 7.8 GiB VM. `tests/test_flowai.py` fails if any of it returns -
   a dormant service definition is how this comes back.
2. **One local profile, `ollama-mac`, and it is not optional.** `ollama-lab` is gone; the six `-local`
   twins move to `ollama-mac`. `lab-netops-mac` is deleted: it existed as "the optional fast local
   path", which is now what every twin is, and it carried the raw gateway tool the fleet rule
   (ADR 0046) keeps away from small models.
3. **The endpoint is a name, never an address.** `ollama.lab.internal` moves from an alias on
   `tools-01` to the Mac, declared once in `topology/ipam.yaml` as `home_lan.inference_host` and
   rendered into unbound by `oob-gw.yml`. The Mac's address is **inside the router's DHCP pool**, so
   it is only stable while the router holds a reservation: `dhcp_reservation: true` records that, and
   `tests/test_ipam.py` refuses a pool address without it. **The reservation was set on the main
   router and confirmed by the owner on 2026-09-11**, so the address is now a property of the router
   rather than an assumption - but the field and its test stay, because the requirement is what they
   assert and the failure mode returns the moment the reservation does not.
4. **An unreachable Mac fails the play loudly.** There is no in-lab fallback *by design*, so
   `flowai.yml` asserts the endpoint answers and carries the pinned model before touching an agent,
   and names every cause in the failure message: Mac asleep, `ollama serve` down, firewall, or a moved
   reservation. The probe retries - a cold first connection has been seen to time out and then
   succeed immediately, twice.
5. **The context-length pin follows the inference.** `OLLAMA_CONTEXT_LENGTH=16384` (a twelve-device
   result must fit, ADR 0046) moves from the container to the Mac's LaunchAgent in
   `scripts/mac-ollama.sh`, and `tests/test_agent_fleet.py` checks it there.

## Consequences

- The twins get a GPU and a far better model; the crash class in the Context section goes away; ~6 GiB
  returns to `tools-01`.
- **The lab is no longer self-contained, and `verify` now requires the Mac awake.** ADR 0037's
  premise is reversed knowingly: a sleeping Mac is a red suite, not a skipped criterion. The failure
  messages say so, so it reads as a missing dependency rather than a lab fault.
- Two manual steps exist that no play can perform, both recorded in `scripts/mac-ollama.sh`. The
  router's DHCP reservation is **done** (2026-09-11). The macOS firewall allowance is done but cannot
  be locked in the same way: macOS keys it to the resolved binary path, so a `brew upgrade ollama`
  changes the Cellar version and the allowance has to be re-added. That one remains the standing
  manual dependency.
- Not addressed: the twins' prompts were tuned for a 7B model - three tools each, heavy reduction. A
  30B MoE may not need those constraints, but that is a behavioural change to make with measurements,
  not with this migration.
