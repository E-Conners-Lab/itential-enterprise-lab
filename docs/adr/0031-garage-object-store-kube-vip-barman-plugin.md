# 0031 — Garage 2.4.0 is the in-cluster object store; CNPG backs up through the Barman Cloud plugin 0.15.0; kube-vip 1.2.3 holds the API VIP

- **Status:** accepted
- **Date:** 2026-09-06

## Context

PID S2 requires a CloudNativePG backup to land in a Longhorn-backed bucket,
which means an S3-compatible store in the cluster. MinIO was the reflex
choice, but `minio/minio` was archived on 2026-04-25 (source-only, no
binaries, console removed) and `minio/operator` on 2026-03-20. RustFS has no
stable release; SeaweedFS is far heavier than a lab needs. CloudNativePG 1.30
still accepts the in-tree `barmanObjectStore` but has deprecated it since 1.26
in favour of the Barman Cloud plugin. The Kubernetes API needs a VIP that does
not depend on the CNI being up (Cilium replaces kube-proxy, so the ClusterIP
does not work until Cilium runs). Sources: `docs/image-manifest.md` 4.1.

## Decision

- **Garage v2.4.0** (`docker.io/dxflrs/garage:v2.4.0`, AGPL-3.0) as a single-node
  StatefulSet with `--single-node --default-bucket`, 2 GiB metadata and 50 GiB
  data on Longhorn, secrets generated once by the play and held only in the
  cluster.
- **Barman Cloud plugin v0.15.0** installed from its release manifest into
  `cnpg-system`; every CNPG `Cluster` archives WAL and takes scheduled base
  backups to Garage through an `ObjectStore` resource. The in-tree
  `barmanObjectStore` is not used.
- **kube-vip v1.2.3** as a static pod on each server (ARP mode, control-plane
  only, `svc_enable=false`), authenticating with the node's local k3s
  kubeconfig so it needs no ClusterIP. MetalLB owns every LoadBalancer.

## Consequences

- No MinIO anywhere; anything later that needs S3 (Gitea, NetBox media,
  etcd snapshots) points at Garage.
- Garage is single-replica by design (one physical disk, ADR 0007/0021);
  losing its volume loses backups, which is why Phase 9 copies them off-host.
- The plugin adds a CRD and a controller to keep upgraded alongside CNPG.
