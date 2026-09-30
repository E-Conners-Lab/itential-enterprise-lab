#!/bin/sh
# clab-aws-twin: the AWS box's strongSwan end, for Hand Off / Verify against clab-rtr1 (itential-enterprise-lab step 6).
# Mirrors the AWS box's boot (user_data.sh.tftpl): the XFRM interface and its routes (vpn-xfrm.sh), the firewall, then
# swanctl.conf with the key put in at start (vpn-render.sh). Values come from /etc/aws-twin/twin.env, rendered by
# clab-dev.yml from clab/versions.yaml; the connection template is cloud-devops-pipeline's swanctl.conf.tftpl at the
# pinned commit, with every ${...} already filled and only $PSK left.
set -eu
. /etc/aws-twin/twin.env

# containerlab adds the data link after the container starts
until ip link show eth1 >/dev/null 2>&1; do sleep 1; done
ip addr replace "$TWIN_ADDR" dev eth1
ip link set eth1 up
ip route replace default via "$VPC_GATEWAY" dev eth1

# the route-based tunnel: the XFRM interface bound to the child SA's if_id, the AWS end's inner address, the lab routes
ip link add "xfrm$IF_ID" type xfrm dev eth1 if_id "$IF_ID"
ip addr replace "$TUNNEL_LOCAL_INNER" dev "xfrm$IF_ID"
ip link set "xfrm$IF_ID" mtu 1400 up
for cidr in $LAB_PREFIXES; do ip route replace "$cidr" dev "xfrm$IF_ID"; done

# the box itself: loopback, replies, IKE and ESP from the lab's front door only, ICMP from the lab through the tunnel
iptables -A INPUT -i lo -j ACCEPT
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A INPUT -s "$PEER" -p udp -m multiport --dports 500,4500 -j ACCEPT
iptables -A INPUT -s "$PEER" -p esp -j ACCEPT
for cidr in $LAB_PREFIXES; do iptables -A INPUT -i "xfrm$IF_ID" -s "$cidr" -p icmp -j ACCEPT; done
iptables -A INPUT -i "xfrm$IF_ID" -s "$TUNNEL_REMOTE_INNER" -p icmp -j ACCEPT
iptables -P INPUT DROP
iptables -P FORWARD DROP

# the key, as vpn-render.sh: never world-readable, the same character check
umask 077
PSK=$(cat /etc/aws-twin/psk)
if ! printf '%s' "$PSK" | grep -Eq '^[A-Za-z0-9._+/=-]{32,}$'; then
  echo "twin: the key is not 32+ characters of [A-Za-z0-9._+/=-]" >&2
  exit 1
fi
sed "s|\$PSK|$PSK|" /etc/aws-twin/swanctl.conf.template > /etc/swanctl/swanctl.conf
unset PSK

/usr/lib/ipsec/charon &
charon=$!
until swanctl --stats >/dev/null 2>&1; do sleep 1; done
swanctl --load-all
wait "$charon"
