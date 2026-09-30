#!/bin/sh
# clab-aws-nat: the 1:1 NAT in front of the twin, as an EIP in front of the AWS box (itential-enterprise-lab step 6).
# Everything addressed to the "EIP" goes to the twin, and the twin leaves as the EIP. Values from /etc/aws-twin/nat.env;
# IP forwarding is set by containerlab (sysctls in the topology).
#   nat.sh          the container's start: set up, then stay up
#   nat.sh reload   from `docker exec`: set up again in place (idempotent; a restart would lose containerlab's links)
set -eu
. /etc/aws-twin/nat.env
until ip link show eth1 >/dev/null 2>&1 && ip link show eth2 >/dev/null 2>&1; do sleep 1; done
ip addr flush dev eth1
ip addr add "$OUTSIDE_ADDR" dev eth1
ip addr flush dev eth2
ip addr add "$INSIDE_ADDR" dev eth2
ip link set eth1 up
ip link set eth2 up
iptables -t nat -F PREROUTING
iptables -t nat -F POSTROUTING
iptables -t nat -A PREROUTING -i eth1 -d "$EIP" -j DNAT --to-destination "$TWIN_IP"
iptables -t nat -A POSTROUTING -o eth1 -s "$TWIN_IP" -j SNAT --to-source "$EIP"
[ "${1:-start}" = reload ] && exit 0
exec sleep infinity
