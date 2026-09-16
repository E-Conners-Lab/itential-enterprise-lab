# 00 — Prerequisites

What you need before chapter 01, and the one table every later chapter reads from.

This lab builds an enterprise network-automation environment: an out-of-band network, a k3s cluster,
NetBox as the source of truth, a nested EVE-NG topology of twelve real network devices, the Itential
Platform (first as a single-VM dev stack, then as a production HA environment), and a full observability
stack. Everything is built by `make` targets from documents in the repo, and every phase ends with a
verification script that proves it.

**Read this chapter fully before buying or installing anything.** Two of the requirements — the Itential
container registry and the network devices' images — are the ones people get stuck on, and neither can be
worked around later.

---

## Before you start

### Hardware

The lab was built and tested on one server. Nothing in the repo assumes that server specifically, but the
shape matters: you need a hypervisor that can hold eleven production VMs plus a nested virtualisation host,
and enough RAM that the nested topology is not fighting the VMs for it.

| | Tested on | What actually matters |
|---|---|---|
| Hypervisor | Dell R640, 2× Xeon Gold 6154 (72 threads), 314 GB RAM, 1.6 TB thin LVM | Enough for the allocation below, plus headroom for the host |
| vCPU allocated | 95 of a 108 ceiling (1.5:1 oversubscription) | Oversubscription is fine; these VMs are mostly idle |
| RAM allocated | 296 GB of a 296 GB ceiling | **RAM is the binding constraint.** Do not oversubscribe it |
| Disk allocated | 1,455 GB thin of a 1.5 TB ceiling | Thin provisioning; watch *usage*, not allocation |
| Nested lab | EVE-NG host, 21 nodes, 47 vCPU, 111 GB | Can be a VM on the same hypervisor or its own machine |

If you have less, the lab still works — build fewer branch sites, or skip the production HA environment in
chapter 08 and stop at the dev stack in chapter 05. `docs/resource-budget.md` is the live version of this
table and is checked against the hypervisor by the verification scripts, so amend it if you change sizes.

### Workstation

A Mac or Linux machine that can reach the hypervisor. Everything runs from here; nothing is installed on
the hypervisor by hand.

```
tofu ansible ansible-lint yamllint gitleaks pre-commit gh jq python3 helm kubectl cilium
```

`make bootstrap` checks for all of them, creates `.venv`, installs the Python and Ansible dependencies and
the pre-commit hooks. It fails loudly and names anything missing.

### Accounts you must supply yourself

These cannot be automated. Each one is a login you create, and the repo never stores the credentials —
only references to them in `.env`.

| Account | Needed for | Cost | Notes |
|---|---|---|---|
| **Itential container registry** | chapters 05, 06, 08 | commercial | The blocking one. Images come from a private ECR; you need an AWS profile with pull access. Without it you can build chapters 00–04 and 07 but no Itential |
| **ServiceNow PDI** | chapter 05 (optional) | free | A Personal Developer Instance. Reclaimed if you do not log in interactively every 10 days |
| **Anthropic API key** | chapter 06 (optional) | pay per use | For the FlowAI agents. A local Ollama model is the free alternative and the lab runs both |
| **Cisco / Arista accounts** | chapter 04 | free | To download the router and switch images. Both require accepting a EULA in a browser |
| **Infoblox / Palo Alto** | later phases | free eval | Only if you extend past chapter 08 |

`docs/manual-steps.md` is the complete list of things a human must do, with the reason each one resists
automation. Read it once before you start — it is short, and it is the honest account of what "fully
automated" does not cover.

### The fill-in table

**Every later chapter references these placeholders and never a real value.** Fill this in once, keep it
with your `.env`, and substitute as you read. The lab's own `10.100.0.0/24` addresses are used literally
throughout the chapters — they are private and invented here, so they are safe to copy as-is.

| Placeholder | Yours | Example | What it is |
|---|---|---|---|
| `${HOME_LAN}` | ______________ | `192.168.1.0/24` | The network your hypervisor and workstation already sit on |
| `${HOME_ROUTER}` | ______________ | `192.168.1.1` | Its default gateway — you add one static route here, and nothing else |
| `${OOB_GW_LAN_IP}` | ______________ | `192.168.1.120` | A free static address on `${HOME_LAN}` for the lab's gateway VM |
| `${PVE_HOST}` | ______________ | `192.168.1.161` | The hypervisor's management address |
| `${EVE_HOST}` | ______________ | `192.168.1.240` | The EVE-NG host's management address |
| `${NETBOX_HOST}` | ______________ | `192.168.1.110` | NetBox's address before it moves onto the OOB network |
| `${ECR_ACCOUNT}` | ______________ | `123456789012` | The AWS account id of the Itential registry |
| `${ECR_PROFILE}` | ______________ | `itential-ecr` | Your local AWS profile name for it |
| `${SNOW_INSTANCE}` | ______________ | `dev123456` | Your ServiceNow PDI, without `.service-now.com` |
| `${LAB_DOMAIN}` | ______________ | `lab.internal` | The lab's internal DNS zone. Change it only if it collides with something you own |

> **Why parameterised.** These are the values that identify *your* environment. Keeping them out of the
> chapters means the runbooks can be shared, and means nobody accidentally publishes their home network or
> a registry account id. The verification tests fail if a real address, account id or token appears in any
> chapter — the safety is enforced, not remembered.

### `.env`

`.env` holds every credential and is never committed — `.gitignore` excludes it and a `gitleaks` pre-commit
hook blocks a token that slips into any other file. Copy the template and fill in what you have:

```
cp .env.example .env
```

Fill in what a chapter needs when you reach it, not all at once. Most values are **generated for you** on
first run and written back to `.env` — the Itential encryption key, the MongoDB keyfile, every database
password. You supply only what comes from outside: the hypervisor token, the vendor logins, and the
passwords you choose.

| Fill in now | Generated for you later |
|---|---|
| `PROXMOX_VE_ENDPOINT`, `PROXMOX_VE_API_TOKEN` | `ITENTIAL_ENCRYPTION_KEY`, `MONGO_KEYFILE` |
| `EVE_HOST`, `EVE_USERNAME`, `EVE_PASSWORD` | `MONGO_*_PASSWORD`, `REDIS_*_PASSWORD` |
| `AUTOMATION_PASSWORD` (you choose it) | `NETBOX_TOKEN` (after NetBox is up) |
| `ECR_AWS_PROFILE` (chapter 05) | |

---

## The commands, in order

```
git clone https://github.com/E-Conners-Lab/itential-enterprise-lab
cd itential-enterprise-lab
make bootstrap                    # tooling check, .venv, pre-commit hooks
cp .env.example .env              # then fill in the hypervisor and EVE-NG values
make test                         # the unit tests, no lab access needed
make discover                     # read-only: what your hypervisor and EVE-NG already have
```

Then, on your workstation, two one-time steps that need `sudo` and cannot be automated from the repo —
both are in `docs/manual-steps.md` as steps 6b and 6c:

```
sudo scripts/workstation-route.sh                                    # route to the lab and its resolver
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain docs/lab-root-ca.crt         # macOS: trust the lab CA
```

The second one only matters from chapter 02 onward, once there is a lab CA to trust — but do it early and
you will never see a certificate warning.

---

## What "done" looks like

`make bootstrap` prints `bootstrap OK` and nothing else. It takes under a minute.

`make test` runs the unit tests — they need no lab access and check that the repo's documents agree with
each other (the address plan against the IP-plan table, the resource budget against the declared VM sizes).
A few hundred tests, about two seconds. **If these fail on a fresh clone, stop** — something is wrong with
the clone or the Python environment, not with your lab.

`make discover` writes a read-only inventory to `verify/results/`. It changes nothing. Read it: it tells you
what your hypervisor already has, which VM ids are taken, and whether EVE-NG is reachable.

---

## Verification

There is no `verify/test-00-*.sh` — this chapter installs nothing in the lab. Its verification is that the
next chapter can start:

```
make test          # the repo agrees with itself
make discover      # the hypervisor and EVE-NG answer
```

Both green means chapter 01 will run. `verify/run.sh` runs every phase's verification and is what you use
from chapter 02 onward.

---

## Troubleshooting

**`make bootstrap` says `MISSING: helm@3`.** The repo pins Helm 3 explicitly because Helm 4 changed
behaviour the k3s chapter depends on. On macOS: `brew install helm@3`. The check looks for it at
`/opt/homebrew/opt/helm@3/bin/helm`; set `HELM3_BIN` in `.env` if yours is elsewhere.

**`make test` fails on a fresh clone with import errors.** The tests run from `.venv`, not your system
Python. Use `make test`, or `.venv/bin/python -m pytest tests/ -q` — not a bare `pytest`.

**Every lab HTTPS page says "Not Secure" with `https` struck through.** The lab CA is not trusted by your
browser. Importing the certificate is **not** enough — macOS stores a certificate and *trusts* it as two
separate things, and a root with no trust setting signs nothing a browser will accept. Run the
`add-trusted-cert` command above, or in Keychain Access set *Trust → Always Trust*, then restart the
browser. To prove it worked, run `curl https://itential.${LAB_DOMAIN}/login` **without** `--cacert`: a 200
means the system trust store accepts the chain. This is manual step 6c, and it went unnoticed in this repo
for five phases because every internal check passes `--cacert` explicitly and so never exercised the
system trust store.

**`make discover` cannot reach the hypervisor.** Check `PROXMOX_VE_ENDPOINT` includes the scheme and port
(`https://${PVE_HOST}:8006`) and that `PROXMOX_VE_API_TOKEN` is the full `user@realm!tokenid=uuid` string.
Set `PROXMOX_VE_INSECURE=true` if the hypervisor still presents its self-signed certificate.

**A VPN client on your workstation swallows the lab.** Several corporate VPN clients capture all of
`10.0.0.0/8`, which includes the lab. `scripts/workstation-route.sh` installs a more specific route via
`${HOME_ROUTER}` — via the *router*, never straight at the lab gateway, or return traffic takes a path the
gateway does not expect and the connection hangs rather than failing.

**You do not have Itential registry access.** Chapters 00–04 and 07 build a complete network-automation
lab without it: OOB network, k3s, NetBox, a twelve-device EVE-NG topology, and the full observability
stack. Chapters 05, 06 and 08 are the ones that need the images. Nothing later depends on your having run
them in order except that they assume the earlier chapters' output exists.

---

## Tested versions

Everything below was running when this chapter was last verified. A different version very likely works;
these are what was actually tested, so a difference is worth suspecting when something behaves oddly.

| Component | Version |
|---|---|
| Proxmox VE | 9.2 |
| EVE-NG | Pro 6.5, Ubuntu 20.04 base |
| OpenTofu | 1.10.x, provider `bpg/proxmox` 0.112.0 |
| Ansible | core 2.19 |
| Python | 3.12 (`.venv`) |
| Helm | 3 (pinned; **not** 4) |
| Workstation | macOS 15 (Linux works; the CA trust command differs) |

---

**Next:** [01 — Out-of-band network and addressing](01-oob-network-and-addressing.md)
