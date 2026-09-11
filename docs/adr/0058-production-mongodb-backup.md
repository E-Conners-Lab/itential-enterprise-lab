# 0058 — The production MongoDB is backed up nightly by `mongodump` on a secondary, and the verify restores it

- **Status:** accepted (PR #30, 2026-09-11) — the nightly dump runs and S11.9 restores it
- **Date:** 2026-09-11
- **Related:** ADR 0053 (the production environment), ADR 0031 (CNPG backs up to Garage; phase 9 copies backups off-host), ADR 0002, PID S11 amendment 1.24

## Context

The owner intends to use this environment to test Itential marketplace integrations and assets, and accepts
doing that on the production-shaped environment rather than standing a sandbox back up — **on the condition
that there is rollback.**

There is, for everything the repo owns. A bad asset is `git revert` plus `make replay-platform-ha2`, and that
is not theoretical: on 2026-09-10 three workflows were broken on production by a conversion and restored from
the committed documents within minutes.

There is nothing for the database underneath it. Measured 2026-09-11: no `mongodump`, no snapshot job, no
backup task anywhere in `ansible/playbooks/platform-ha2-*.yml`. The three-member replica set is
**availability, not backup** — it replicates a bad write to all three members in milliseconds.

The inconsistency is stark, because the lab already backs up the things it considers valuable:

| Database | Backup | Proven by |
|---|---|---|
| NetBox (PostgreSQL) | nightly `pg_dump` + media, 7-day rotation | S0.2 fails if it is older than 24 h |
| Zabbix and platform-db (CloudNativePG) | scheduled base backups and WAL to Garage | S2.5 requires a completed `Backup` |
| **Production MongoDB** | **none** | — |

MongoDB is where the entire Platform estate lives: every workflow document, job and job history, Lifecycle
Manager instance, Work Center item, agent session and integration instance — and whatever a marketplace asset
creates. It is the one database whose loss cannot be replayed from the repo, because the repo owns assets,
not history.

Measured while designing: the official `mongo:7.0` image ships `mongodump` and `mongorestore` (tools 100.18.0)
at `/usr/bin`, so this needs no extra image.

## Decision

1. **`mongodump` nightly, in the container, on a secondary.** The dump runs on the **last** replica-set member
   in the oracle, connected to its own `mongod` with `--host localhost:<port>` — a direct connection, where a
   replica-set URI would let the driver discover the set and send the dump to the **primary**, defeating the
   point of running it on a secondary — and `--readPreference=secondary` besides. Dumping from a secondary keeps the read load off the member the Platform writes through, and
   the member is chosen from `itential/ha2/versions.yaml` rather than named in a script, so it follows the
   oracle if the topology changes.

2. **A dedicated `backup` account with MongoDB's built-in `backup` role**, password generated once into `.env`
   beside the other three. Not `admin`: a credential that exists to read everything nightly should not also be
   able to write everything, and the Deployer's account list is extended rather than reused.

3. **Seven-day rotation into `/var/backups/mongodb`, gzipped, one archive per run** — deliberately the same
   shape as the NetBox backup this repo already runs, so there is one pattern to learn and one place it is
   wrong if it is wrong.

4. **The verify restores it — into a throw-away `mongod`, never into the replica set.** S11.9 does not
   merely assert that a recent file exists; that is a claim about a filename. It starts a standalone
   container from the pinned image with a 0.25 GB cache on the backup member, pipes the newest archive into
   it, compares the restored collection count against the live `itential` database, and removes the
   container.

   The first version of this check restored into a throw-away *database on the production replica set*, and
   that was wrong twice over. A full copy of the database doubles its storage, and on these 4 GB members it
   took **all three `mongod` processes down** (2026-09-11; the set re-formed on its own and the copies were
   dropped, but the Platform lost its database for about a minute). **A backup check must not risk the thing
   it is backing up.** Restoring into a clean instance is also closer to what a real recovery does: prove the
   archive reconstitutes a database somewhere else, which is the question worth answering.

   Two properties of the restore, both found by running it rather than reasoning about it: the archive is a
   file on the *host* — the dump redirects the container's stdout into it — so it must be piped back in on
   stdin, because `--archive=<host path>` inside the container silently restores nothing; and a restore is a
   write, so aiming it at the member holding the archive fails with `NotWritablePrimary` after reporting
   every collection restored. Neither is visible without attempting a restore.

5. **Off-host copy is deferred to phase 9**, which already owns exactly this for the Garage backups (ADR 0031)
   and for NetBox's. Today the archive sits on the VM it was taken from, which protects against a bad write,
   a bad asset and a bad migration — the failure modes this element exists for — and not against losing that
   VM. Stating the limit is part of the decision rather than an omission from it.

## Consequences

- Rollback becomes true rather than half-true: an asset that mangles Platform objects is recoverable to the
  previous night, and the element that made this urgent — marketplace testing — can proceed.
- One more generated secret in `.env`, and one more account on the replica set.
- The restore check makes S11.9 slower than a file-existence assertion and worth every second of it: it
  found, in one sitting, that the archive has to be piped rather than pathed, that a restore needs a primary,
  and that a 4 GB replica set will not absorb a second copy of its own database.
- The oracle's `priority` does not make the last member permanently a secondary - it decides the *initial*
  election, and a later failover can leave the backup member as primary. That is harmless for a dump (the
  load argument weakens, the correctness does not) and it is why the restore never assumes which member is
  writable.
- A dump is a point-in-time copy of a replica-set member, not a cluster-consistent snapshot with an oplog
  tail. For this lab that is the right trade; a production deployment that needed point-in-time recovery
  would use `--oplog` and a continuous archive, and that is a bigger element than this one.
