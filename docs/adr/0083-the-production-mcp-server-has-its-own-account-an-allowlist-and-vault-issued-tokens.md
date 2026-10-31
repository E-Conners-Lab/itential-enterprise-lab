# 0083 — The production MCP server has its own account, an allowlist, and Vault-issued tokens

- **Status:** accepted (owner, 2026-10-18). Decisions 1-4 land first, in their own change; 5-8 follow once the owner's
  one-time Vault root ceremony is done.
- **Date:** 2026-10-18
- **Related:** ADR 0036 (the MCP server), ADR 0039 amendment (the tools hidden for returning node credentials), ADR 0053
  (tools-01), ADR 0063 (svc-copilot, the service-account pattern reused here), ADR 0065 (Vault), ADR 0076 (the Batfish
  allowlist pattern), ADR 0082 (gbac as the boundary for automations)

## Context

Production's MCP server (`itential-mcp` on tools-01, `mcp.lab.internal:8000`) had two holes, found while fixing its
idle-hour 401 (PR #183):

- **Anyone who reached the port could use it, with no login.** `ITENTIAL_MCP_SERVER_AUTH_TYPE` was `none`. The home router
  routes 10.100.0.0/14 to oob-gw, and oob-gw's forward chain admits all of 192.168.68.0/22, so any device on the home
  network reached it: phones, TVs, guests. Its logs showed one real client, the Mac Mini.
- **Whoever got in was the platform administrator.** It logged in as `admin@itential`, so every caller could do anything
  (push device configuration, delete inventories, restart adapters), and the Platform's audit trail showed `admin` for
  every MCP call.

`itential-mcp` has one Platform identity per server process: it can check who is calling, but it cannot act as that caller
on the Platform (its changelog says so). So what any client can do is decided by the server's own account, and who may
call is decided in front of it. The owner wants it usable from the Mac, a personal laptop and possibly a work laptop, and
from the phone, and wants the credentials in a secret manager with a way back in if Vault is shut down.

## Decision

1. **Its own account: `svc-mcp`, builder without administration.** An LDAP account in group `mcp-builders`
   (`tasks/ldap-service-account.yml`, the svc-copilot pattern, `svc_kind mcp`). It holds every role on the Platform
   except identity, sessions and the server itself (the dev copilot-builders exclusions), and adapter and application
   control: `Adapters/admin` can update, stop or delete the LDAP adapter every login goes through, so it could lock every
   user out (measured on production 6.6.0). `itential/mcp/roles.yaml` is the oracle. The password is generated once and
   kept in `.env` (`SVC_MCP_PROD_PASSWORD`) like the other service accounts.
2. **The server hides the tools that account cannot use.** The six adapter and application start/stop/restart tools join
   the two that return node credentials in `ITENTIAL_MCP_SERVER_EXCLUDE_TAGS`. A hidden tool is refused if called, not
   only left out of the list.
3. **The group reaches what the plays made, next to admin_group.** On 6.6.0 the Gateway cluster, its service group, each
   agent (operators), each agent project (members), each automation (gbac) and each inventory (groups) filter by group
   membership, so roles alone see none of them (measured: a role-only account saw 0 of 1 gateway, 0 of 28 agents, 19 of
   22 automations). `tasks/mcp-access.yml` adds the group to the plays' gateway, service group, agents and agent project;
   `automation_access.read.prod` names it; agent updates keep it. 6.6.0's Inventory Manager cannot change an existing
   inventory's groups (its OpenAPI offers GET and DELETE only, no role holds an update method), so the lab inventories are
   created with it and the two existing ones are recreated once (owner's choice over a database edit). Objects the plays
   did not make keep their own access: the department agents, automations and project, and the hand-imported Studio
   projects.
4. **An allowlist on the port.** A DOCKER-USER chain on tools-01 (`mcp-access.sh.j2`, the Batfish pattern) admits only
   `tools.mcp_allow`: one /32 per client machine, each with a DHCP reservation. A phone reaches it through Remote Control
   of Claude Code on one of those machines, so it needs no entry.
5. **Every caller presents a Vault-issued token.** `ITENTIAL_MCP_SERVER_AUTH_TYPE=jwt`: the server checks each request's
   bearer JWT against Vault's published keys (`/v1/identity/oidc/.well-known/keys`), issuer and audience. Vault's identity
   token role `itential-mcp` signs one-hour tokens with its own key, rotated daily. Each client machine has its own AppRole
   (`mcp-client-<device>`), whose only policy reads that token, so one machine can be revoked alone; its role and secret
   IDs live in that machine's Keychain. Claude Code's `headersHelper` fetches a fresh token on every connection. The image
   moves to v0.14.1 first, for the PyJWT advisory GHSA-ffc3-869f-jxw9.
6. **TLS on the port.** A lab-CA certificate from cert-manager for `mcp.lab.internal`, so a token is never sent in clear.
7. **Break-glass without Vault.** A compose override staged on tools-01 recreates the server with no authentication,
   published on 127.0.0.1 only, reached through an SSH tunnel to tools-01: whoever can use it already holds root there.
   Leaving break-glass is the tools play or a plain `docker compose up -d mcp`.
8. **One root ceremony.** The lab-admin login cannot create the signing key or the token role (root was revoked, ADR
   0065), so the owner generates root once from the unseal key for decision 5. The same ceremony raises the lab-admin
   token to 8h/24h (owner decision 2026-10-04), and root is revoked again.

## Consequences

- Everything any MCP client does is bounded by `svc-mcp`: it can build and push changes, but cannot change who has access,
  end sessions, reconfigure the server or stop the login adapter. The Platform's audit trail names `svc-mcp`, not `admin`.
- One identity for all clients remains: the Platform cannot tell the Mac from a laptop. Vault's audit (which AppRole minted
  a token) is where that lives.
- `svc-mcp` cannot edit automations (write gbac stays admin_group) or reach the department and hand-imported objects.
- A new client machine is a DHCP reservation, one line in `tools.mcp_allow` and a tools play run, plus its own AppRole once
  decision 5 lands. Working away from home needs a route into the lab that does not exist today.
- If Vault is down, no new token can be minted and the server refuses callers once its cached keys expire (one hour):
  break-glass is the way in, and the rest of the lab does not depend on the MCP server.

## Measured (production 6.6.0, 2026-10-18)

- Role catalogue: 201 roles. The identity and server administration roles and `Adapters/admin` (createAdapter,
  deleteAdapter, stopAdapter, updateAdapter, ...) and `Applications/admin` (stopApplication, ...) are the exclusions.
- svc-copilot (roles only) against the administrator: gateways 0/1, service groups 0/1, operable agents 0/28, agent
  projects 0/2, automations 19/22, workflows 38/48, templates 216/218.
- Inventory Manager: no update route (`PUT`/`PATCH /inventory_manager/v1/inventories/{id}` answer "Cannot PUT"); deleting
  an inventory removes its nodes (scratch inventory, created and deleted).
- `itential-mcp` v0.14.0 auth: `none`, `jwt` (FastMCP JWTVerifier: JWKS URI or static key, issuer, audience; JWKS cached
  one hour and re-fetched on an unknown key id), `oauth_proxy`; no per-caller passthrough to the Platform.
- Vault's OIDC issuer is its pod address (`https://10.42.x.x:8200/v1/identity/oidc`) until the ceremony sets it to
  `https://vault.lab.internal:8200`.

## Alternatives considered

- **Read-only or operator account.** Read-only loses `run_command`; operator needs a role set measured from scratch. The
  owner chose builder without administration: production is the owner's own test bed.
- **A static shared token.** Simplest, but a long-lived password with manual rotation. Vault tokens expire in an hour and
  revoke per machine.
- **Keycloak single sign-on (`oauth_proxy`).** The enterprise end state, but Keycloak is not deployed and v0.14.0's
  `oauth_proxy` ignores its provider type, maps the userinfo URL to revocation and keeps client registrations inside the
  container.
- **An auth gateway in front (agentgateway, oauth2-proxy, Kong).** Per-tool access by group claim and request logging;
  more than one owner needs.
- **Editing the inventories in MongoDB.** The smallest change, but it bypasses the application; recreating them uses only
  supported APIs and keeps the group in the plays.
- **The Platform's OAuth service accounts for the server's login.** ipsdk supports client credentials, but on 6.5.2 such an
  account could not join an LDAP group and its device search failed (runbook 09).

## Amendment 2026-10-31: how decision 5 is built (Vault side)

- **The client machines are records.** `topology/ipam.yaml` `home_lan.mcp_clients` holds each machine's address and its
  router reservation (both are inside the DHCP pool, like `inference_host`); `tools.mcp_allow` must name exactly them
  (`tests/test_mcp_access.py`), so an allowlisted address no reservation holds fails the build.
- **Tokens** (`itential/versions.yaml` `vault.mcp_tokens`): key and role `itential-mcp`, RS256, one-hour tokens, the key
  rotated daily and a retired public key published for a day. The role's `client_id` is set, not generated, so the
  server's audience is known before the role exists.
- **One AppRole per machine**, `mcp-client-<hostname>`, its secret IDs and tokens bound to that machine's /32. Its only
  policy, `itential-mcp-client`, reads `identity/oidc/token/itential-mcp`; a login token lasts five to ten minutes. The
  lab-admin login may write these (an `itential-*` policy and AppRole roles), so they need no root token:
  `make vault-mcp-tokens` runs this part alone and proves it from the Mac (its own role logs in, the laptop's is refused).
- **The identity provider** (issuer, signing key, token role) is written only by a token Vault says may write it
  (`sys/capabilities-self`). Decision 8's root ceremony was impossible: lab-admin's token is refused `generate-root`
  (measured 2026-10-31: 403, capabilities `deny`; Vault 2.0 authenticates it, ADR 0065), and root was revoked. The owner
  chose a **temporary setup role** instead (`make vault-mcp-setup`, run once 2026-10-31 18:50Z): with rights it already
  held, lab-admin made a policy and an AppRole usable once, from the Mac only, for fifteen minutes; that token set the
  issuer, the key and the role and rewrote lab-admin's policy, and then the role, its token and the policy were removed
  (an `always` block). lab-admin now keeps the key and the role (not the issuer, not token minting) and may run
  `generate-root`, which still needs the owner's unseal key: the way back to root decision 8 assumed.
- **Correction to ADR 0065:** lab-admin *can* widen itself. It writes `itential-*` policies and AppRole roles and can log
  in through a role bound to the Mac, so it can grant itself anything; no ACL can stop an administrator that writes
  policies. Treat its password as a full administrator's.
- **Delivery** (`scripts/mcp-client.sh`, stock macOS only; no secret in argv, a file or a here-string): the Mac enrols
  itself (`make mcp-enrol NAME=mac-mini`). The laptop's role and secret IDs arrive response-wrapped, single use, valid
  ten minutes (owner decision 2026-10-31, over a lab-admin login on the laptop): `make mcp-wrap NAME=laptop` in the
  owner's terminal, then `scripts/mcp-client.sh unwrap` on the laptop; a wrap destroys the machine's older secret IDs, so
  each machine holds one. `make mcp-revoke NAME=<machine>` stops that machine's logins, but a token it already took stays
  valid until it expires (at most an hour): the server checks signatures, not Vault, and rotating the key does not help,
  since a retired public key stays published for a day.
- **Proven 2026-10-31:** the Mac's own role logs in from the Mac and the laptop's is refused there (HTTP 400); the Mac,
  enrolled, gets a token through `scripts/mcp-client.sh token` (Claude Code's headersHelper) in 0.3 s, whose RS256
  signature verifies against Vault's published keys, with `iss https://vault.lab.internal:8200/v1/identity/oidc`,
  `aud itential-mcp` and an hour's life. The server does not ask for it yet: that is the server-side change.

## Amendment 2026-10-31: decisions 5 to 7, server side

- **The server** (`itential/ha2/tools.compose.yml.j2`): `ITENTIAL_MCP_SERVER_AUTH_TYPE=jwt` with Vault's JWKS URI,
  issuer, audience and RS256, all derived from `vault.mcp_tokens`; it fetches the keys with the lab CA it already
  mounts (`SSL_CERT_FILE`) and caches them an hour. itential-mcp v0.14.1 serves TLS itself (uvicorn's certificate and
  key, `ITENTIAL_MCP_SERVER_CERTIFICATE_FILE` / `_PRIVATE_KEY_FILE`, read from its v0.14.1 source), so decision 6 needs
  no proxy.
- **The certificate** comes from the lab CA on k3s as the Platform hosts' do: the tools play's first play issues it for
  tools-01's own name, its alias `mcp.lab.internal` and its address, and the tools play installs it (the key root's,
  mode 0600: the container runs as root) and restarts the server when it changes, since Compose recreates a container
  only when its own definition changes.
- **Proof on the real path**, in the tools play and verify S4.7/S4.7d: from the Mac, an initialize with no token gets
  401, and one with the Mac's token (`scripts/mcp-client.sh token`, exactly as Claude Code's headersHelper runs it) gets
  200; a token Vault never signed gets 401 and plain HTTP gets no answer.
- **Clients**: `.mcp.json` names `https://mcp.lab.internal:8000/mcp` and `headersHelper: scripts/mcp-client.sh token`.
  Claude Code runs the helper on every connection and again after a 401, so the hourly token renews itself; it trusts
  the operating system's store, where the lab CA is already a trusted root on the Mac (`NODE_EXTRA_CA_CERTS` where it
  is not). Credential-named environment variables are stripped from a project helper, which reads only the Keychain.
- **Break-glass** (decision 7): `itential/ha2/tools.compose.breakglass.yml`, staged beside the compose file, recreates
  the server with no token and no TLS on tools-01's loopback (`ports: !override`), reached through an SSH tunnel; the
  tools play's own `docker compose up -d` uses the normal file only, so a run leaves break-glass.
