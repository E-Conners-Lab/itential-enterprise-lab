---
name: itential-cloud-rbac-and-tool-access
description: Use when an Itential agent can't be launched due to unresolved tool access, tools show authorized:false or unauthorizedReferenceId, integration tools (NetBox, ServiceNow, Vault) are missing from the tool registry, PATCH /authorization/groups returns 403, or role assignments were just changed and nothing seems to have taken effect.
---

# Itential Cloud RBAC and agent tool access

## Checks first (read-only)

1. Which user and group? GET the user's groups; note the group id.
2. Does the role exist? List roles and look for the per-model `admin` roles (provenance like `lab-netbox:1.0.0`).
   Missing means the integration model isn't installed; that's a different problem.
3. Read the group: `GET /authorization/groups/<id>` and save `assignedRoles` to a file. This is your backup and
   the base for the full list.
4. Tool state: integration tools show `authorized:false` until their `admin` roles are on the user's group.

## Assign roles (this REPLACES the list)

`PATCH /authorization/groups/<id>` with `{"updates":{"assignedRoles":[{"roleId":"..."}, ...]}}` REPLACES
`assignedRoles`. Sending only the additions removes everything else.

1. Build the body = every existing `roleId` from step 3 plus the new role ids. Show the user the before/after
   role names and how to roll back (PATCH the saved list).
2. Get approval, then PATCH (shared instance; see `working-agreement`).
3. Read the group back and diff against saved + additions. If anything is missing, restore from the backup at once
   and tell the user.
4. 403 means the user lacks the Authorization method. Stop and report. Don't work around it with another
   account, another endpoint, or the admin's session.

## Make it take effect

The session must be refreshed before the tool registry reflects the new roles: ask the user to log out and in
themselves (never ask for credentials). Then re-read the tool list; `authorized` should be true.

## Agents imported before the roles existed

They keep tools as `unauthorizedReferenceId` with `authorized:false`. Fix per agent with TWO separate PATCHes on the
agent: first `removeTools` (the stale references), then `addTools` (the same tools by real reference). The same
reference in both arrays in one call is a 400. Confirm exact field shapes against one agent's GET before patching.

## Don't

- Don't send a partial `assignedRoles` list.
- Don't put the same reference in `removeTools` and `addTools` of one request.
- Don't test whether a role "worked" before the user has refreshed their session.
- Don't use an admin's credentials or token you were not given; don't print tokens.

## Verify

Group read-back equals the saved list plus the additions; after re-login, the integration tools show
`authorized:true`; the agent launches without "unresolved tool access".
