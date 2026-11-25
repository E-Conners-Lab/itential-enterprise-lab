---
name: itential-gateway5-and-private-networks
description: Use when deciding how an Itential Cloud instance reaches private lab devices or services, when a gateway cluster id or integration "cluster" setting is in question, when reachability from cloud to the lab is being assumed, or when planning inbound firewall/port exposure for Gateway 5.
---

# Gateway 5 and private networks

## Facts

- Gateway 5 dials OUT to the platform's Gateway Manager with mTLS. Nothing needs to be exposed inbound to the lab.
  Don't plan or request inbound port forwards for it.
- Cluster ids differ by instance: the lab's gateway cluster is `lab`; the cloud's is `homelab`. Don't copy one
  into the other's config.
- An integration instance with no custom cluster runs on the default gateway cluster. Check what the instance
  actually has set rather than assuming.

## Checks first (read-only)

1. Gateway Manager on the instance: which clusters exist, are they connected (GET).
2. The integration instance's cluster setting (GET). Empty = default cluster.
3. Which gateway the default cluster is, and whether it can see the target (it may not).

## Test reachability by running something

Don't reason about it; measure it. Run a read-only workflow job that calls the target through the integration and
read the HTTP status the job reports:

1. Pick an existing read-only workflow (a GET-style lookup). Start the job (get approval first on a shared
   instance; it is a write to job history).
2. Read the job result: HTTP status and error text. 200 with data = reachable. 502/504/timeout = not routed.
   401/403 = reachable but credentials or roles are wrong (not a network problem).
3. If the instance has no custom cluster and the call fails, set the cluster on that integration instance only after
   showing the user what changes and how to revert, then re-test the same job.

Beware 200 with an HTML body: that is a service answering, but not the right one (see
`servicenow-pdi-silent-failure`).

## Don't

- Don't declare something reachable or unreachable from ping, DNS, or an architecture guess. Two earlier guesses
  were wrong until a job was run.
- Don't assume `lab` and `homelab` are interchangeable, or expose inbound ports.
- Don't store gateway certificates or tokens in the repo or print them.

## Verify

A read-only job through the intended cluster returns the expected 2xx and body shape, and the cluster setting you
found/changed is recorded in your report.
