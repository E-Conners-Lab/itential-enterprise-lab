# 0021 — k3s platform stack: k3s v1.36.4+k3s1, Cilium 1.20.1, MetalLB 0.16.1, Longhorn 1.12.1, cert-manager 1.21.1, bundled Traefik 3.7.8, kube-vip, CloudNativePG 1.30.0 / PostgreSQL 18

- **Status:** accepted
- **Date:** 2026-09-06

## Context

Every containerised service needs a cluster with a CNI, LoadBalancer IPs,
storage, certificates and Postgres. ingress-nginx is retired (final release
2026-03-19, repo archived), so the k3s-bundled Traefik is the ingress. All
chosen versions are compatible with Kubernetes 1.36; the 1.36.4 k3s build is
ten days old but the 1.36 minor is mature (manifest 4.1).

## Decision

Pin: **k3s v1.36.4+k3s1** (flannel, kube-proxy, ServiceLB disabled; Traefik
kept), **Cilium 1.20.1** (kube-proxy replacement, Hubble), **MetalLB 0.16.1**
L2 pool 10.100.0.32-63, **Longhorn 1.12.1** (default 2 replicas, telemetry
volumes at 1), **cert-manager 1.21.1** with a lab root CA, **kube-vip** (tag
fixed in Phase 3) for the API VIP, **CloudNativePG 1.30.0** operator with
PostgreSQL 18 operand images, one CNPG cluster per application.

## Consequences

Fallbacks recorded: k3s v1.35.8+k3s1. Inferred image tags (MetalLB, CNPG,
kube-vip) are checked against the registries in Phase 3 before `helm install`.
Cilium's own LB-IPAM could replace MetalLB later; not now, to keep the stack
conventional.
