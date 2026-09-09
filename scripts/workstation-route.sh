#!/usr/bin/env bash
# Per-host access to the lab from a workstation on the home LAN: a route for 10.100.0.0/14 that
# beats a VPN client's 10/8 capture, and (macOS) a scoped resolver for lab.internal.
#
# The route points at the HOME ROUTER (which carries the static route to oob-gw, manual step 1b),
# NOT at oob-gw's LAN leg: oob-gw returns every reply through the router (ADR 0030) so the
# router's stateful firewall sees both halves of a flow. A route straight to oob-gw makes the
# path asymmetric and the router drops replies intermittently (observed 2026-09-07).
# Idempotent. Needs sudo. Not persistent across reboot on macOS: re-run after a reboot.
set -euo pipefail
GW=${LAB_NEXT_HOP:-192.168.68.1}   # home router
DNS=${OOB_GW_LAN:-192.168.68.120}  # oob-gw answers lab.internal
NET=10.100.0.0/14
case "$(uname -s)" in
  Darwin)
    cur=$(netstat -rn -f inet | awk '$1=="10.100/14"{print $2}')
    if [ "$cur" = "$GW" ]; then echo "route $NET via $GW present"
    else [ -n "$cur" ] && sudo route -n delete -net "$NET" >/dev/null; sudo route -n add -net "$NET" "$GW" >/dev/null && echo "route $NET via $GW added"; fi
    sudo mkdir -p /etc/resolver
    printf 'nameserver %s\n' "$DNS" | sudo tee /etc/resolver/lab.internal >/dev/null && echo "resolver for lab.internal -> $DNS"
    ;;
  Linux)
    ip route show "$NET" | grep -q . && echo "route $NET present" || { sudo ip route add "$NET" via "$GW" && echo "route $NET via $GW added"; }
    echo "DNS: add 'DNS=$DNS' + 'Domains=~lab.internal' to a systemd-resolved drop-in, or use resolvectl"
    ;;
  *) echo "Windows (admin PowerShell): route -p add 10.100.0.0 mask 255.252.0.0 $GW ; Add-DnsClientNrptRule -Namespace .lab.internal -NameServers $DNS"; exit 1 ;;
esac
# the first packets after a route change can be lost while ARP for the gateway settles: retry
# macOS ping -W is milliseconds, Linux ping -W is seconds
case "$(uname -s)" in Darwin) W=2000 ;; *) W=2 ;; esac
for _ in 1 2 3 4 5; do ping -c 1 -W "$W" 10.100.0.1 >/dev/null 2>&1 && { echo "oob-gw 10.100.0.1 reachable"; exit 0; }; sleep 1; done
echo "10.100.0.1 unreachable after 5 tries: is oob-gw up?"; exit 1
