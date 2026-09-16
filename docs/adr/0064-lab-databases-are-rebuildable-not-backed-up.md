# 0064 — The lab's CloudNativePG databases are rebuildable and are not backed up

- **Status:** accepted (owner decision, 2026-09-16)
- **Date:** 2026-09-16
- **Amends:** ADR 0031 (its "every CNPG `Cluster` archives WAL and takes scheduled base backups to Garage" is withdrawn; Garage and the plugin stay), ADR 0051 decision 3 (`zabbix-db` has no WAL archiving, no nightly base backup and no Garage credentials, and is 12 Gi), PID S2.5 (amendment 1.31)
- **Related:** ADR 0058 (the production MongoDB backup is VM-local and is not affected), ADR 0057 (monitoring follows the estate), PID S7

## Context

Both lab CloudNativePG clusters archived WAL continuously and took a nightly base backup to Garage through the
Barman Cloud plugin, with a 14-day retention policy, and Garage is one 20 GiB Longhorn volume.

The incident, 2026-09-15 to 2026-09-16:

- Garage filled to **0 B available**: bucket `cnpg-backups` held **28.1 GiB** of objects plus **922 MiB** of
  unfinished multipart uploads. Fourteen days of nightly base backups of two clusters plus continuous WAL did
  not fit in 20 GiB, and nothing sized the retention against the volume.
- From **2026-09-15 16:21 UTC** `barman-cloud-wal-archive` failed with "No space left on device". PostgreSQL
  keeps every WAL segment it has not archived, so WAL piled up on `zabbix-db`'s 10 GiB volume.
- At **2026-09-16 04:20 UTC** that volume filled. CNPG set the phase "Not enough disk space", postgres went
  into CrashLoopBackOff (180 restarts), and the Zabbix server was down for about **15 hours**.
- **Nothing alerted.** The monitoring was what broke, and the kube-prometheus-stack Alertmanager routes every
  alert to `lab-null` (ADR 0051: no notification channel in S7).

What the two databases hold:

| Cluster | Holds | Rebuilt from |
|---|---|---|
| `observability/zabbix-db` | Zabbix history and trends | Configuration (hosts, templates, triggers, macros, users) is rebuilt from the repo by `ansible/playbooks/observability.yml`; only the metric history is not |
| `cnpg-system/platform-db` | an **empty** `app` database (7.8 MB), phase-3 scaffolding for S2.5 | `ansible/playbooks/k8s-platform.yml` |

The backups were protecting metric history and an empty database, and they were the cause of the only outage
either database has had.

## Decision

The lab's CloudNativePG databases are rebuildable and are **not backed up**.

1. Both manifests (`k8s/observability/manifests/zabbix-db.yaml`, `k8s/platform/manifests/cnpg-platform-db.yaml`)
   lose their `ObjectStore`, the Cluster's `plugins:` entry for `barman-cloud.cloudnative-pg.io` and their
   `ScheduledBackup`.
2. Removing a document from a manifest does not delete the live object, and the plays' `state: present` sends a
   JSON merge patch, which never removes a key it omits (and the Clusters carry no last-applied annotation for
   `apply: true` to diff against). So each play removes the live objects explicitly and idempotently, in this
   order: delete the `ScheduledBackup`; remove `/spec/plugins` from the Cluster with a JSON patch when present;
   delete the `ObjectStore`; delete the namespace's copy of the `garage-s3` Secret, which only the ObjectStore
   read. The Cluster is applied after the cleanup.
3. `zabbix-db` storage goes **10 Gi to 12 Gi**. The live volume had to grow once so that postgres could start on
   a full disk; with archiving off, PostgreSQL recycles its WAL and the size does not need to keep growing.
4. **Garage stays**, with an empty bucket, available for a future use that is sized first. The Barman Cloud
   plugin stays installed: its CRD is what lets the `ObjectStore` cleanup resolve on every run, and removing it
   is a separate decision.
5. S2.5 changes from "a completed `Backup` newer than 48 h" to "healthy, `pg_isready`, and no
   `ScheduledBackup`, `ObjectStore`, plugin reference, Barman sidecar or WAL backlog in any namespace".
   `tests/test_cnpg_no_backups.py` holds the manifests and plays to the same.

## What a rebuild loses, and what it does not

| Lost | Not lost |
|---|---|
| Zabbix history and trends (metric history; Prometheus keeps its own 10 days independently) | Every Zabbix host, template, trigger, macro and user: `observability.yml` re-creates them |
| The `platform-db` `app` database, which is empty | The production MongoDB and its nightly `mongodump`, VM-local (ADR 0058) |
| Any point-in-time recovery of a lab CNPG database | NetBox's nightly `pg_dump` (S0.2), which is not on CNPG |

## Alternatives rejected

- **Keep the nightly backups with a short retention (say 2-3 days).** Still continuous WAL archiving, which is
  exactly what turns a full object store into a full database volume; retention only moves the day it happens,
  and it still protects nothing that is not rebuilt from the repo.
- **A bigger Garage volume.** Longhorn is already over-provisioned at 200 % across three nodes on one physical
  disk (ADR 0031, `docs/resource-budget.md`); more space postpones the same failure and buys backups of data
  nobody needs back.

## Consequences

- Garage's bucket `cnpg-backups` is empty once the live recovery empties it (including the incomplete multipart
  uploads), and stays in the cluster unused.
- There is no point-in-time recovery for any lab CNPG database. A later phase that puts data on CNPG that
  cannot be rebuilt from the repo (Gitea, ADR 0027; Keycloak, ADR 0022) must decide its own backup and size it
  against its target, not inherit one.
- The production MongoDB backup (ADR 0058) is unchanged.
- The live cluster needs a one-off recovery the plays cannot do safely on a crashlooping database (grow the
  PVC, remove the objects, empty the bucket); the owner approves and runs it.
- `platform-db-initial` and any other `Backup` objects that were not owned by a `ScheduledBackup` remain as
  records of backups that no longer exist; the recovery deletes them.
- **Follow-up, not done here:** an alert on Garage and PVC free space and on CNPG readiness would have caught
  this a day early. kube-prometheus-stack's default `KubePersistentVolumeFillingUp` rule is enabled and very
  likely fired (not verified), but Alertmanager's only receiver is `lab-null`. The gap is a notification
  channel plus a CNPG readiness alert, a change to S7 in its own right.
