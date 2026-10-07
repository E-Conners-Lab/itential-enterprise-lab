# 0074 — The diagnostics agents consult an external knowledge service through FlowMCP Gateway

- **Status:** accepted (owner decisions 2026-10-05/06); amended 2026-10-06 (decision 10: the registration as measured)
- **Date:** 2026-10-06
- **Amends:** ADR 0072 (the tunnel-diagnostics prompt and tools), ADR 0073 (the fabric-diagnostics prompt and tools)
- **Related:** ADR 0046 (the agent fleet and its tiers), ADR 0051 (observability), ADR 0065 (Vault aliases on the
  Gateway), ADR 0068 (a private repository's code pinned by commit)

## Context

The outage loops (ADR 0072, 0073) hold each agent's cause -> fix knowledge in its prompt. The fix menu itself is
enforced by workflow code (`outage_fix`, `fabric_fix` in `itential/workflows/build.py`), never by the prompt. The
next step is to let the agents consult what the lab has learned (incidents, runbooks, ADR lessons) and vendor
guidance, and to show that an agent can use an EXTERNAL system safely: read-only, least privilege, no secrets,
auditable, and unable to widen what the automation may do.

FlowMCP Gateway (Gateway 5.5+, Gateway Manager 1.1.1+; the lab runs 5.5.2 and 1.2.3) registers an external MCP
server over `stdio`, `streamable-http` or SSE, discovers its tools and exposes them as Gateway services that agents
can use. `stdio` starts the server as a subprocess on the Gateway host; HTTP transports take headers whose values
can come from the Gateway's secret store (`{{ secret "name" }}`).

The service and its corpus live in a separate PRIVATE repository, `E-Conners-Lab/netops-knowledge` (owner decision
2026-10-05): a corpus of how the lab breaks is sensitive even when scrubbed, and a separately built and pinned
service makes "external" literally true. Its PID records the design and the owner's decisions (D1-D9).

## Decision

1. **The service.** `netops-knowledge` is an MCP server with exactly two read-only tools, `search_scenarios` and
   `get_scenario`, over a 40-scenario corpus (26 proven in the lab, 3 draft, 11 vendor-documented, each citing its
   source). Every reply opens with a notice that the text is reference data, not instructions. The index is
   keyword-only (FTS5 BM25): measured hit@3 30/30 on 30 golden queries, so query embeddings, and the egress they
   would need, are not used (netops-knowledge PID D2).
2. **Hosting: `streamable-http` on k3s, not `stdio` on iag-01** (owner, 2026-10-06). `stdio` would run third-party
   Python inside the Gateway container that holds the Vault AppRole secret ID and the device credentials, with the
   Gateway's network reach. The pod is its own blast radius:
   - namespace `netops-knowledge`, one replica, image pinned by digest and signed (cosign keyless, verified by the
     play before it applies the digest);
   - non-root (UID 10001), read-only root filesystem, all capabilities dropped, no service-account token;
   - TLS on the pod (cert-manager, lab CA); a LoadBalancer VIP (the Gateway is a VM outside k3s, so a ClusterIP
     Service cannot reach it) with `externalTrafficPolicy: Local` so the policy sees the real source;
   - ingress only from iag-01 (LoadBalancer source range and a Cilium policy); NO egress at all.
3. **The credential.** One bearer token, generated once and kept only in Vault. The Gateway resolves it through its
   Vault secret provider into the registration's header (`Authorization: Bearer {{ secret "..." }}`). The pod
   holds only the token's SHA-256. FlowMCP supports only a static header, so this is a documented exception to
   short-lived service credentials, rotated every 90 days.
4. **Least privilege for the agents.** A Gateway Manager service group would not restrict an agent: the agent acts
   as the job's user, and the outage relay logs in as the administrator. What limits an agent is its tool list, so
   each diagnostics agent gets the one search tool and nothing that can name another service.
5. **The prompts.** `tunnel-diagnostics`, `fabric-diagnostics` and their twins search once (at most one corrected
   retry) before matching causes, never put an address, id or key in a query, treat what comes back as data and
   never follow an instruction in it, and cite what they used in the work note (`KB: <id>`, or `KB: none`, or
   `KB: unavailable` when the service fails, in which case they carry on with their own rules). The fix menus and the
   Work Center approval are unchanged.
6. **The pin.** `itential/versions.yaml` pins the image digest and the index SHA-256 together; a corpus change
   reaches production only through a lab PR that bumps the pin.
7. **The registration** goes through Gateway Manager's configuration import, like every other Gateway resource in
   the lab, and is proven by the export and by discovery: the export carries `mcp_servers` (measured read-only on
   production, 2026-10-06), and Gateway Manager must list a service for each tool, which needs DNS, TLS, the token
   and the policy all to be right. It is off (`register_with_gateway: false`) until the pod runs; turning it on is
   the owner's go. Order: the Gateway's Vault policy must allow the token's path first (`vault-prod-config.yml`
   adds `lab/data/mcp/netops-knowledge` to `itential-gateway`), or the alias resolves to nothing and discovery fails;
   then the Gateway play binds the alias (which restarts the Gateway server and runner) and registers the server.
8. **Two lab PRs.** PR A: the deployment, the secrets, the VIP, the registration task, Loki's retention, this ADR.
   PR B, written from what the first registration measures (how FlowMCP names the services and how they appear in
   the Tool Registry): the agents' tool and prompts, the Gateway service group, the verify checks of the agents, the
   evals on the Ollama twins first and one Claude run, and the live drill.
9. **Rotation, every 90 days:** `make knowledge-token ROTATE=1` (Vault), `make netops-knowledge` (the pod's hash;
   the checksum rolls the pod), then the Gateway play with `nk_reregister=true`.

10. **Amendment: the registration as measured on production** (2026-10-06, Gateway 5.5.2; owner decision: a local
    secret now and a question to Itential, "option 3"). Three facts the documentation does not give, each found by a
    refused import and confirmed in the Gateway's own binary and log:
    - the stored MCP server has no `url`: the address goes in `command` (the CLI's `command_or_url`); `transport` takes
      the enum name `STREAMABLE_HTTP`; `headers` is a map; the import is sent as YAML text;
    - an MCP header cannot resolve the Vault secret provider (`cannot resolve external secret: registry is nil`, also
      after a server restart), so decision 3's alias cannot carry the token;
    - a local secret imported through Gateway Manager is stored unencrypted (the export returns it in plain text) and
      the server cannot decrypt it (`invalid encrypted data`); only `iagctl create secret` encrypts it.
    So the header names a LOCAL Gateway secret, `netops_knowledge.mcp_secret`, copied from Vault by `make
    knowledge-mcp-secret` in the owner's terminal: `iagctl` needs the Gateway's own `admin` login, which accepts a
    password only interactively. The owner reset that login with `iagctl server --recover-admin-user` (its password
    is in Vault, `vault.gateway_admin_path`). The registration itself stays in the Gateway play (proven by the export
    and by discovery); FlowMCP names the services `<mcp_server>_<tool>`, and in the agent tool picker each one is a
    "Gateway Service" an agent can be given directly - PR B needs no wrapper workflow and no `runService`.
    Consequences: this is the one copy of the token outside Vault (encrypted with the Gateway's key, never in the
    export in plain text), a documented exception to decision 3 until Itential confirms or fixes provider support for
    MCP headers; the old alias `netops-knowledge-token` is retired (named, not compared) until `iagctl` deletes it; a
    rotation is `make knowledge-token ROTATE=1`, `make netops-knowledge`, `make knowledge-mcp-secret`, then the Gateway
    play with `-e nk_reregister=true` (the server reconnects only when its registration changes or the server starts).

11. **Lab PR B: the agents** (owner, 2026-10-06). `tunnel-diagnostics`, `fabric-diagnostics` and both twins get the one
    service `netops-knowledge_search_scenarios` - a new tool kind `mcp-tool` that resolves to the Tool Registry's
    `gatewayService:<cluster>:python-script:<server>_<tool>` (measured), never GatewayManager `runService`;
    `get_scenario` stays off (owner: the search hit already carries the id, the menu item and a 400-character excerpt,
    and a full scenario is up to 8 KB more untrusted text, tokens and turns for no change in the answer). FlowMCP runs
    on production only, so the tool is attached only there (`mcp_tools_available`); elsewhere the agents run without
    it and note `KB: unavailable`. The prompts follow decision 5 (topic `bgp` or `ipsec-vpn`, the incident number as
    `ref`, one corrected retry). `make knowledge-pull-token` now refuses a token that is not a classic `ghp_` token or
    that GHCR does not serve the pinned image with. No Gateway Manager service group: its export carries none, and
    decision 4 already holds that a group would not restrict an agent. Verify S15.6 holds the agents' tool lists.

## Consequences

- The agents' answers may cite the lab's own history; the fix they can propose is still one menu item, approved by
  a person.
- One more k3s workload and one more Vault entry; the 90-day token rotation is a play, not a manual step.
- The audit trail: the service logs every call (the query's hash, never its text; the ids returned) to Loki, kept
  90 days for that stream; Agent Sessions shows the call; the work note shows the ids.
- If the service is down, the loops still run: retrieval informs a diagnosis, it never decides one.
- Verification: `tests/test_netops_knowledge.py` (pins, Vault paths both ways, the VIP, the pod's lock-down, the
  policy, the retention, the registration's alias), `verify/test-15-knowledge.sh` S15.1-S15.5 (control plane and data
  plane), and the service's own CI in its repo.
