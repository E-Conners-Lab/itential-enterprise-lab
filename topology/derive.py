"""Derives every address the lab runs from topology/enterprise.yaml (ADR 0048).

Two consumers, one derivation: eve/build.py hands ``render_context`` to the startup-config templates,
and ansible/playbooks/netbox-enrich.yml reads ``interfaces`` (``python topology/derive.py`` prints it as
JSON). Link addresses follow one rule: the a-side takes the first usable address of the link prefix, the
b-side the second (a /31 has no network address, so .0 and .1).
"""

from __future__ import annotations

import ipaddress
import json
import sys
from pathlib import Path

import yaml

TOPO = Path(__file__).resolve().parent / "enterprise.yaml"
# the YAML (and EVE-NG) use short port names; NetBox and the templates use the device's own names
SHORT_TO_DEVICE = {
    "c8000v": (("Gi", "GigabitEthernet"),),
    "veos": (("Mgmt1", "Management1"), ("Eth", "Ethernet")),
    "pa-vm": (("eth", "ethernet"), ("mgmt", "management")),
}
SITE_ASN_KEY = {"wan": "isp", "dc1": "dc1_edge"}  # branches use their own site key


def load_topology() -> dict:
    return yaml.safe_load(TOPO.read_text())


def site_asn(routing: dict, site: str) -> int:
    return routing["asn"][SITE_ASN_KEY.get(site, site)]


def device_name(platform: str, iface: str) -> str:
    """The interface name the device itself uses: Gi2 -> GigabitEthernet2, Eth1 -> Ethernet1, Mgmt1 -> Management1."""
    for short, long in SHORT_TO_DEVICE.get(platform, ()):
        if iface.startswith(short) and not iface.startswith(long):
            return long + iface[len(short) :]
    return iface


def bare(address: str) -> str:
    """'10.1.2.3/24' -> '10.1.2.3'."""
    return address.split("/")[0]


def link_ends(link: dict) -> dict[str, dict]:
    """{node: {iface, ip, prefixlen, peer_node, peer_iface, peer_ip}} for a link with a prefix."""
    a_node, a_if = link["a"].split(":")
    b_node, b_if = link["b"].split(":")
    net = ipaddress.ip_network(link["prefix"])
    a_ip, b_ip = (net[0], net[1]) if net.prefixlen == 31 else (net[1], net[2])
    return {
        a_node: {
            "iface": a_if,
            "ip": str(a_ip),
            "prefixlen": net.prefixlen,
            "peer_node": b_node,
            "peer_iface": b_if,
            "peer_ip": str(b_ip),
        },
        b_node: {
            "iface": b_if,
            "ip": str(b_ip),
            "prefixlen": net.prefixlen,
            "peer_node": a_node,
            "peer_iface": a_if,
            "peer_ip": str(a_ip),
        },
    }


def node_links(topo: dict, name: str, *, with_prefix: bool = True) -> list[dict]:
    """This node's ends of the topology links (YAML order), bypass links included."""
    out = []
    for link in topo["links"]:
        if not (link["a"].startswith(name + ":") or link["b"].startswith(name + ":")):
            continue
        if with_prefix and "prefix" not in link:
            continue
        end = link_ends(link)[name] if "prefix" in link else None
        out.append({"link": link, "end": end})
    return out


def node_tunnels(topo: dict, name: str) -> list[dict]:
    """[{id, peer, ip, nbr, dst, ras}] for a WAN edge: dst is the peer's provider-facing address."""
    out = []
    for tunnel in topo["routing"].get("tunnels", []):
        ends = link_ends(tunnel)
        if name not in ends:
            continue
        mine = ends[name]
        peer_wan = _wan_end(topo, mine["peer_node"])
        out.append(
            {
                "id": int(mine["iface"].removeprefix("Tunnel")),
                "peer": mine["peer_node"],
                "ip": mine["ip"],
                "nbr": mine["peer_ip"],
                "dst": peer_wan["ip"] if peer_wan else None,
                "ras": site_asn(
                    topo["routing"], topo["nodes"][mine["peer_node"]]["site"]
                ),
            }
        )
    return sorted(out, key=lambda t: t["id"])


def _wan_end(topo: dict, name: str) -> dict | None:
    """The node's end of its link to the provider core (role isp-core)."""
    for item in node_links(topo, name):
        if topo["nodes"][item["end"]["peer_node"]]["role"] == "isp-core":
            return item["end"]
    return None


def _aggregate(topo: dict, site: str) -> dict | None:
    prefix = topo["sites"][site].get("aggregate")
    if not prefix:
        return None
    net = ipaddress.ip_network(prefix)
    return {"prefix": prefix, "net": str(net.network_address), "mask": str(net.netmask)}


def render_context(topo: dict, name: str) -> dict:
    """The per-node variables the startup-config templates render (every address in the lab)."""
    node = topo["nodes"][name]
    routing = topo["routing"]
    site = node["site"]
    addressing = node.get("addressing") or {}
    loopbacks = {x["name"]: x for x in addressing.get("loopbacks", [])}
    svis = {x["name"]: x for x in addressing.get("svis", [])}
    lo = bare(loopbacks["Loopback0"]["address"]) if "Loopback0" in loopbacks else None
    ctx: dict = {
        "lo": lo,
        "asn": None,
        "agg": _aggregate(topo, site),
        "isp_peer": None,
        "wan_ip": None,
        "tunnels": [],
        "lan": None,
        "ibgp": None,
        "isp_neighbors": [],
        "vtep": None,
        "transit_ip": None,
        "mlag": None,
        "svi10": None,
        "svi100": None,
        "evpn_peers": [],
    }
    if node["platform"] == "c8000v":
        ctx["asn"] = site_asn(routing, site)
        wan = _wan_end(topo, name)
        if wan:
            ctx["isp_peer"], ctx["wan_ip"] = wan["peer_ip"], wan["ip"]
        ctx["tunnels"] = node_tunnels(topo, name)
        if "lan" in addressing:
            lan = addressing["lan"]
            ctx["lan"] = {
                "net": str(ipaddress.ip_network(lan["prefix"]).network_address),
                "gw": bare(lan["gateway"]),
                "agg": ctx["agg"]["net"],
                "vlan": lan["vlan"],
            }
        for item in node_links(topo, name):
            peer = topo["nodes"][item["end"]["peer_node"]]
            if peer["role"] == "wan-edge" and peer["site"] == site:
                ctx["ibgp"] = item["end"]["peer_ip"]
        if node["role"] == "isp-core":
            ctx["isp_neighbors"] = [
                {
                    "ip": item["end"]["peer_ip"],
                    "as": site_asn(
                        routing, topo["nodes"][item["end"]["peer_node"]]["site"]
                    ),
                }
                for item in node_links(topo, name)
            ]
    elif node["platform"] == "veos" and node["role"] in ("spine", "leaf"):
        ctx["asn"] = (
            routing["asn"]["dc1_spine"]
            if node["role"] == "spine"
            else routing["asn"]["dc1_leaf"]
        )
        other = "leaf" if node["role"] == "spine" else "spine"
        ctx["evpn_peers"] = [
            bare(n["addressing"]["loopbacks"][0]["address"])
            for n in topo["nodes"].values()
            if n["role"] == other and n["site"] == site
        ]
        if "Loopback1" in loopbacks:
            ctx["vtep"] = bare(loopbacks["Loopback1"]["address"])
        if "Vlan4094" in svis:
            mlag = ipaddress.ip_interface(svis["Vlan4094"]["address"])
            peer = next(h for h in mlag.network.hosts() if h != mlag.ip)
            ctx["mlag"] = {"address": svis["Vlan4094"]["address"], "peer": str(peer)}
        if "Vlan10" in svis:
            ctx["svi10"] = {"virtual": svis["Vlan10"]["virtual"]}
        if "Vlan100" in svis:
            ctx["transit_ip"] = bare(svis["Vlan100"]["address"])
            ctx["svi100"] = {"virtual_ip": bare(svis["Vlan100"]["virtual_router"])}
    return ctx


def interfaces(topo: dict) -> dict[str, list[dict]]:
    """Every addressed interface per network device for NetBox: name, address (with mask), vrf, description, kind.
    Physical link ends (kind link), loopbacks, SVIs, tunnels and the branch LAN gateway on its bypass port."""
    firewalls = bool(topo["lab"].get("firewalls", True))
    out: dict[str, list[dict]] = {}
    for name, node in topo["nodes"].items():
        if node["platform"] not in ("c8000v", "veos"):
            continue
        ctx = render_context(topo, name)
        rows: list[dict] = []
        addressing = node.get("addressing") or {}
        for lo in addressing.get("loopbacks", []):
            rows.append(
                {
                    "name": lo["name"],
                    "kind": "loopback",
                    "address": lo["address"],
                    "vrf": lo.get("vrf"),
                    "description": lo.get("description", ""),
                    "anycast": bool(lo.get("anycast")),
                }
            )
        for svi in addressing.get("svis", []):
            row = {
                "name": svi["name"],
                "kind": "svi",
                "address": svi.get("address"),
                "virtual": svi.get("virtual"),
                "virtual_router": svi.get("virtual_router"),
                "vrf": svi.get("vrf"),
                "description": svi.get("description", ""),
            }
            rows.append(row)
        for item in node_links(topo, name, with_prefix=False):
            link, end = item["link"], item["end"]
            if link.get("bypass") and firewalls:
                continue
            peer_end = link["b"] if link["a"].startswith(name + ":") else link["a"]
            mine = (link["a"] if link["a"].startswith(name + ":") else link["b"]).split(
                ":"
            )[1]
            if (
                not firewalls
                and topo["nodes"][peer_end.split(":")[0]]["role"] == "firewall"
            ):
                continue  # ports facing a deferred firewall stay unconfigured
            description = f"to {peer_end}" + (
                " (firewall bypass)" if link.get("bypass") else ""
            )
            row = {
                "name": device_name(node["platform"], mine),
                "seed_name": mine,
                "kind": "link",
                "description": description,
                "address": None,
                "vrf": None,
                "peer": peer_end,
            }
            if end:
                row["address"] = f"{end['ip']}/{end['prefixlen']}"
                peer_role = topo["nodes"][end["peer_node"]]["role"]
                if node["platform"] == "c8000v" and peer_role == "isp-core":
                    row["vrf"] = "WAN"
                if node["platform"] == "veos" and link.get("bypass"):
                    row["vrf"] = "PROD"
            elif link.get("vlan") and ctx["lan"]:
                row["address"] = addressing["lan"]["gateway"]
            rows.append(row)
        for t in ctx["tunnels"]:
            mode = (
                "FlexVPN sVTI"
                if topo["routing"]["wan_tunnel_mode"] == "ipsec"
                else "GRE-over-IPsec"
            )
            rows.append(
                {
                    "name": f"Tunnel{t['id']}",
                    "kind": "tunnel",
                    "address": f"{t['ip']}/30",
                    "vrf": None,
                    "description": f"{mode} to {t['peer']}",
                    "peer": t["peer"],
                }
            )
        out[name] = rows
    return out


def ports(topo: dict) -> dict[str, str]:
    """Every link end in the YAML's short form -> the device's own interface name (for the seed play's cables)."""
    out = {}
    for link in topo["links"]:
        for end in (link["a"], link["b"]):
            node, iface = end.split(":")
            out[end] = device_name(topo["nodes"][node]["platform"], iface)
    return out


def bgp_neighbors(topo: dict, name: str) -> list[dict]:
    """[{neighbor, remote_as, vrf, afi, description}] the device's startup config carries (the templates' rules)."""
    node = topo["nodes"][name]
    routing = topo["routing"]
    asn = routing["asn"]
    firewalls = bool(topo["lab"].get("firewalls", True))
    ctx = render_context(topo, name)
    out: list[dict] = []

    def add(neighbor, remote_as, vrf=None, afi="ipv4", description=""):
        out.append(
            {
                "neighbor": neighbor,
                "remote_as": remote_as,
                "vrf": vrf,
                "afi": afi,
                "description": description,
            }
        )

    links = [
        item
        for item in node_links(topo, name)
        if not (item["link"].get("bypass") and firewalls)
        and (firewalls or topo["nodes"][item["end"]["peer_node"]]["role"] != "firewall")
    ]
    if node["platform"] == "c8000v":
        if node["role"] == "isp-core":
            for n, item in zip(ctx["isp_neighbors"], links, strict=True):
                add(n["ip"], n["as"], description=item["end"]["peer_node"])
            return out
        for t in ctx["tunnels"]:
            add(t["nbr"], t["ras"], description=f"{t['peer']} via Tunnel{t['id']}")
        for item in links:
            peer = item["end"]["peer_node"]
            role = topo["nodes"][peer]["role"]
            if role == "firewall":
                add(item["end"]["peer_ip"], asn["dc1_fw"], description=peer)
            elif role == "leaf":
                add(
                    item["end"]["peer_ip"],
                    asn["dc1_leaf"],
                    description=f"{peer} (firewall bypass)",
                )
        if ctx["ibgp"]:
            add(ctx["ibgp"], asn["dc1_edge"], description="iBGP dc1 edge peer")
        if ctx["isp_peer"]:
            add(
                ctx["isp_peer"],
                asn["isp"],
                vrf="WAN",
                description="provider (transport only)",
            )
    elif node["platform"] == "veos" and node["role"] in ("spine", "leaf"):
        other_as = asn["dc1_leaf"] if node["role"] == "spine" else asn["dc1_spine"]
        for item in links:
            if item["link"].get("bypass"):
                continue
            add(item["end"]["peer_ip"], other_as, description=item["end"]["peer_node"])
        for lo in ctx["evpn_peers"]:
            add(lo, other_as, afi="evpn", description="EVPN")
        if node["role"] == "leaf":
            for item in links:
                if item["link"].get("bypass"):
                    add(
                        item["end"]["peer_ip"],
                        asn["dc1_edge"],
                        vrf="PROD",
                        description=f"{item['end']['peer_node']} (firewall bypass)",
                    )
            if firewalls:
                add(
                    routing["firewall_transit_gateway"],
                    asn["dc1_fw"],
                    vrf="PROD",
                    description="firewall pair (active) on the transit VLAN",
                )
    return out


def device_context(topo: dict, name: str) -> dict:
    """The device's local config context for NetBox: BGP (ASN, router-id, neighbours) and the per-device VRF RDs."""
    node = topo["nodes"][name]
    ctx = render_context(topo, name)
    out: dict = {"management": {"vrf": "MGMT", "address": node["mgmt_ip"]}}
    if ctx["asn"]:
        out["bgp"] = {
            "asn": ctx["asn"],
            "router_id": ctx["lo"],
            "neighbors": bgp_neighbors(topo, name),
        }
    vrfs = {}
    if node["platform"] == "c8000v" and node["role"] != "isp-core":
        vrfs["WAN"] = {"rd": f"{ctx['asn']}:1"}
    if node["role"] == "leaf":
        vrfs["PROD"] = {
            "rd": f"{ctx['lo']}:{topo['vrfs']['PROD']['vni']}",
            "vni": topo["vrfs"]["PROD"]["vni"],
        }
    if vrfs:
        out["vrfs"] = vrfs
    return out


def contexts(topo: dict) -> dict:
    """Config contexts: lab-wide, per site, per platform (rendered by NetBox onto every device) and per device (local)."""
    lab = topo["lab"]
    sites = {}
    for slug, site in topo["sites"].items():
        entry: dict = {"site": slug, "aggregate": site.get("aggregate")}
        gateways = []
        for n in topo["nodes"].values():
            if n["site"] != slug:
                continue
            lan = n.get("addressing", {}).get("lan")
            if lan:
                gateways.append(
                    {
                        "vlan": lan["vlan"],
                        "gateway": bare(lan["gateway"]),
                        "prefix": lan["prefix"],
                    }
                )
            for svi in n.get("addressing", {}).get("svis", []):
                if svi.get("virtual"):
                    gateways.append(
                        {
                            "vlan": int(svi["name"].removeprefix("Vlan")),
                            "gateway": bare(svi["virtual"]),
                            "vrf": svi.get("vrf"),
                        }
                    )
        if gateways:
            entry["gateways"] = sorted(
                {g["gateway"]: g for g in gateways}.values(), key=lambda g: g["vlan"]
            )
        sites[slug] = entry
    platforms = {
        "ios-xe": {
            "automation_user": "automation",
            "management_vrf": "MGMT",
            "parser": "genie",
        },
        "eos": {
            "automation_user": "automation",
            "management_vrf": "MGMT",
            "parser": "textfsm",
        },
    }
    return {
        "lab": {
            "domain": lab["domain"],
            "dns": lab["dns"],
            "ntp": lab["ntp"],
            "management_gateway": lab["mgmt_gateway"],
            "asn": topo["routing"]["asn"],
        },
        "sites": sites,
        "platforms": platforms,
        "devices": {
            name: device_context(topo, name)
            for name, n in topo["nodes"].items()
            if n["platform"] in ("c8000v", "veos")
        },
    }


def asns(topo: dict) -> list[dict]:
    """The autonomous systems and the site each belongs to (RIR private)."""
    site_of = {
        "isp": "wan",
        "dc1_edge": "dc1",
        "dc1_spine": "dc1",
        "dc1_leaf": "dc1",
        "dc1_fw": "dc1",
    }
    return [
        {
            "asn": number,
            "site": site_of.get(key, key),
            "description": key.replace("_", " "),
        }
        for key, number in topo["routing"]["asn"].items()
    ]


def fhrp_groups(topo: dict) -> list[dict]:
    """Shared virtual-router addresses (EOS VARP) as FHRP groups with the member SVIs."""
    groups: dict[str, dict] = {}
    for name, node in topo["nodes"].items():
        for svi in node.get("addressing", {}).get("svis", []):
            if svi.get("virtual_router"):
                g = groups.setdefault(
                    svi["virtual_router"],
                    {
                        "address": svi["virtual_router"],
                        "vrf": svi.get("vrf"),
                        "name": f"{node['site']}-{svi['name'].lower()}-varp",
                        "members": [],
                    },
                )
                g["members"].append({"device": name, "interface": svi["name"]})
    return list(groups.values())


def racks(topo: dict) -> list[dict]:
    """One location and rack per site; every node of the site racked top down in rack_order, one U each."""
    order = {role: n for n, role in enumerate(topo["rack_order"])}
    out = []
    for site, spec in topo["racks"].items():
        names = sorted(
            (name for name, node in topo["nodes"].items() if node["site"] == site),
            key=lambda n: (order[topo["nodes"][n]["role"]], n),
        )
        devices = [
            {"name": name, "position": spec["u_height"] - idx, "face": "front"}
            for idx, name in enumerate(names)
        ]
        out.append(
            {
                "site": site,
                "location": spec["location"],
                "rack": spec["rack"],
                "u_height": spec["u_height"],
                "devices": devices,
            }
        )
    return out


def circuits(topo: dict) -> list[dict]:
    """The circuits on links[].circuit: Z at the provider port (a-side, WAN site), A at the customer edge (b-side)."""
    out = []
    for link in topo["links"]:
        cid = link.get("circuit")
        if not cid:
            continue
        spec = topo["circuits"][cid]
        a_node, a_if = link["a"].split(":")
        b_node, b_if = link["b"].split(":")
        out.append(
            {
                "cid": cid,
                "provider": topo["providers"][spec["provider"]]["name"],
                "provider_slug": spec["provider"],
                "type": spec["type"],
                "commit_rate_kbps": spec.get("commit_rate_kbps"),
                "description": spec.get("description", ""),
                "a": {
                    "site": topo["nodes"][b_node]["site"],
                    "device": b_node,
                    "interface": device_name(topo["nodes"][b_node]["platform"], b_if),
                },
                "z": {
                    "site": topo["nodes"][a_node]["site"],
                    "device": a_node,
                    "interface": device_name(topo["nodes"][a_node]["platform"], a_if),
                },
            }
        )
    return out


if __name__ == "__main__":
    topology = load_topology()
    per_device = interfaces(topology)
    flat = [
        dict(row, device=device)
        for device, items in per_device.items()
        for row in items
    ]
    port_map = ports(topology)
    renames = {k: v for k, v in port_map.items() if k.split(":")[1] != v}
    json.dump(
        {
            "interfaces": per_device,
            "rows": flat,
            "ports": port_map,
            "renames": renames,
            "contexts": contexts(topology),
            "asns": asns(topology),
            "asns_by_site": {
                site: sorted(a["asn"] for a in asns(topology) if a["site"] == site)
                for site in topology["sites"]
            },
            "fhrp_groups": fhrp_groups(topology),
            "racks": racks(topology),
            "circuits": circuits(topology),
            "providers": topology.get("providers", {}),
            # the play's lookups in one fixed order: groups, assignments, one per group address, one per member SVI
            "fhrp_lookups": [
                "ipam/fhrp-groups/?limit=100",
                "ipam/fhrp-group-assignments/?limit=100",
            ]
            + [
                f"ipam/ip-addresses/?address={g['address']}"
                for g in fhrp_groups(topology)
            ]
            + [
                f"dcim/interfaces/?device={m['device']}&name={m['interface']}"
                for g in fhrp_groups(topology)
                for m in g["members"]
            ],
        },
        sys.stdout,
        indent=1,
    )
