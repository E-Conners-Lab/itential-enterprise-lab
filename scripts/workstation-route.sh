#!/usr/bin/env bash
# Per-host access to the lab from the home LAN when the home router has no static route.
# Adds: route 10.100.0.0/14 via oob-gw's LAN leg, and (macOS) a scoped resolver for lab.internal.
# Idempotent. Needs sudo. Not persistent across reboot on macOS: re-run after a reboot, or put the
# route on the router (docs/manual-steps.md 1b) which is the recommended way.
set -euo pipefail
GW=${OOB_GW_LAN:-192.168.68.120}
NET=10.100.0.0/14
case "$(uname -s)" in
  Darwin)
    if netstat -rn -f inet | grep -q '^10.100/14 '; then echo "route $NET present"; else sudo route -n add -net "$NET" "$GW" >/dev/null && echo "route $NET via $GW added"; fi
    sudo mkdir -p /etc/resolver
    printf 'nameserver %s\n' "$GW" | sudo tee /etc/resolver/lab.internal >/dev/null && echo "resolver for lab.internal -> $GW"
    ;;
  Linux)
    ip route show "$NET" | grep -q . && echo "route $NET present" || { sudo ip route add "$NET" via "$GW" && echo "route $NET via $GW added"; }
    echo "DNS: add 'DNS=$GW' + 'Domains=~lab.internal' to a systemd-resolved drop-in, or use resolvectl"
    ;;
  *) echo "Windows (admin PowerShell): route -p add 10.100.0.0 mask 255.252.0.0 $GW ; Add-DnsClientNrptRule -Namespace .lab.internal -NameServers $GW"; exit 1 ;;
esac
# the first packets after a route change can be lost while ARP for the gateway settles: retry
# macOS ping -W is milliseconds, Linux ping -W is seconds
case "$(uname -s)" in Darwin) W=2000 ;; *) W=2 ;; esac
for _ in 1 2 3 4 5; do ping -c 1 -W "$W" 10.100.0.1 >/dev/null 2>&1 && { echo "oob-gw 10.100.0.1 reachable"; exit 0; }; sleep 1; done
echo "10.100.0.1 unreachable after 5 tries: is oob-gw up?"; exit 1
