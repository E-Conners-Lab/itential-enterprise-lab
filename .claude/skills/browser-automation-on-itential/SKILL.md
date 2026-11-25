---
name: browser-automation-on-itential
description: Use when driving the Itential Platform UI (https://itential.lab.internal or the Itential Cloud instance) through the Chrome extension, when sidebar clicks fail or navigate nowhere, when the tool output says it was blocked or filtered, when a fetch to a localhost relay fails, or when the platform session is stale or logged out.
---

# Browser automation on Itential

## Checks first

1. `tabs_context_mcp` to learn the session state. The extension cannot see the user's existing tabs: create your own
   tab group with `tabs_create_mcp` and work only there. Never navigate, reload, or close the user's tabs.
2. Confirm you are on the right instance (lab `itential.lab.internal` vs the cloud one) before any action.
3. Probe the session with a read: from page JS, `fetch('/health/status', {credentials:'include'})` and return
   `r.status` only. If 401/redirect-to-login, the session is stale (see Credentials).

## Procedure: call the platform's own API from page JavaScript

Use `javascript_tool` on a platform page. This is far more reliable than the sidebar, whose clicks failed repeatedly.

```js
const r = await fetch('/<api path>', {credentials: 'include'});
const j = await r.json();
({status: r.status, count: (j.results || j.data || j).length})
```

Return counts, booleans, and names, not raw dumps. Read the endpoint's shape from a small sample first. Do reads
first; any POST/PATCH/DELETE follows the `working-agreement` skill (state the change, get approval).

## Moving data in and out

- A page cannot POST to a localhost relay (blocked). Do not build one.
- Use the UI's own Export (download a file) and Import with `file_upload` against the import dialog's file input.
- Keep exported bundles outside the repo (scratchpad) when they could hold anything sensitive.

## Credentials

- Never type, paste, or ask for a username, password, token, or MFA code.
- Stale session: ask the user to log out and back in themselves, then re-probe. Role or group changes also need a
  fresh login before they show up (see `itential-cloud-rbac-and-tool-access`).

## The output filter

The tool layer blocks results that look like cookie or query-string data (`a=b&c=d`, `token=`, `Set-Cookie`).
When a result is blocked, do not try to bypass it by encoding, splitting, or renaming. Re-ask for something that
cannot be sensitive: a boolean, a count, an id list, a name. If the data truly cannot be had that way, tell the user.

## Don't

- Don't use the user's existing tabs, or leave your own open at the end (close the ones you made).
- Don't keep retrying sidebar clicks; after two failures switch to the API call.
- Don't read, print, or relay cookies, tokens, or `document.cookie`.
- Don't trigger `alert`/`confirm` dialogs (they freeze the extension); avoid Delete buttons with confirmations.
- Don't loop on a failing browser action; after 2-3 attempts report what you tried.

## Verify

The API call returns the expected status and count, and (for imports) the item shows up via a follow-up GET, not
just a success toast.
