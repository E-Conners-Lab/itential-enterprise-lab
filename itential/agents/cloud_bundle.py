#!/usr/bin/env python3
"""Build Agent Project import bundles for another Platform (an Itential Cloud instance) from itential/agents/*.yaml.

The repo is the source of truth (ADR 0086). The lab's own play (`flowai.yml`) creates agents through the API of the
lab's Platform; this script is the other direction: it writes the same agents as the bundle files that the Platform's
"Import Agent Project" accepts, so a second Platform reads from the repo instead of from the first one.

    python itential/agents/cloud_bundle.py --workflows cloud-workflows.json \
        --rename lab-netops=lab-netops-cloud --out build/cloud-bundles

What it changes on the way, and why:
  * provider: a bundle names the profile and model, which differ per Platform (default anthropic-homelab / claude-sonnet-5-5);
  * workflow tools: a workflow tool is `workflow:<uuid>` and a uuid belongs to one Platform, so each is resolved BY NAME
    from --workflows, a JSON object {workflow name: uuid} read from the target Platform;
  * local-model twins (profile ollama-*) are skipped: a cloud Platform cannot reach the lab's Ollama (--include-local keeps them);
  * tools the target does not have (unmapped workflows, MCP tools) are dropped and reported, never silently (--strict fails instead).

It writes only under --out and never talks to a Platform. Nothing in a bundle is a secret: tool references are names.
Do not commit the --workflows file: its uuids are specific to one instance.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
AGENTS = ROOT / "itential" / "agents"
VERSIONS = ROOT / "itential" / "versions.yaml"

BUNDLE_VERSION = 1
# The importer rejects a bundle without the metadata its own exports carry ("Not a valid agent project file", measured on
# Platform 6.7.0). The ids are derived from the names, so a rebuild writes byte-identical files; the date is a constant.
BUNDLE_CREATED = "2026-01-01T00:00:00.000Z"
BUNDLE_CREATED_BY = {"provenance": "Custom", "username": "itential-enterprise-lab"}
_NS = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/E-Conners-Lab/itential-enterprise-lab/agent-bundles")
DEFAULT_PROFILE = "anthropic-homelab"
DEFAULT_MODEL = "claude-sonnet-5-5"

# The play's own mapping (ansible/playbooks/tasks/flowai-agent.yml): a Gateway Manager tool is an application method.
GATEWAY_METHODS = {"send-command": "sendCommand", "send-config": "sendConfig", "run-service": "runService"}

# The documents carry no project description (the play never sets one); these are the ones the lab's Platform shows.
PROJECT_DESCRIPTIONS = {
    "lab-netops": "Network Operations for Itential Enterprise Lab",
    "skills-lab": "The skills eval suite's agents (ADR 0084); documents in itential/agents/",
}


def stable_id(*parts: str) -> str:
    return str(uuid.uuid5(_NS, "/".join(parts)))


def load_documents(agents_dir: Path = AGENTS) -> list[dict]:
    docs = []
    for path in sorted(agents_dir.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text())
        doc["_file"] = path.name
        docs.append(doc)
    return docs


def is_local(doc: dict) -> bool:
    """A local-model twin: its profile is an Ollama one (ollama-mac today)."""
    return str(doc.get("profile", "")).startswith("ollama")


def tool_reference(tool: dict, models: dict, workflows: dict[str, str], mcp_cluster: str | None) -> tuple[str | None, str]:
    """(referenceId or None when the target lacks it, display name). Mirrors the play's resolution."""
    kind, ref = tool["kind"], tool["reference"]
    if kind == "workflow":
        uuid = workflows.get(ref)
        return (f"workflow:{uuid}" if uuid else None), ref
    if kind == "integration":
        m = models[tool["model"]]
        return f"integration:{m['title']}%3A{m['version']}:{m['instance']}:{ref}", ref
    if kind == "application":
        return f"application:{tool['app']}:{ref}", ref
    if kind == "gateway-service":
        return f"application:GatewayManager:{GATEWAY_METHODS[ref]}", GATEWAY_METHODS[ref]
    if kind == "mcp-tool":
        # an MCP tool is one service of a FlowMCP server registered on a Gateway cluster; absent unless the caller names one
        if mcp_cluster:
            return f"gatewayService:{mcp_cluster}:python-script:{tool['server']}_{ref}", ref
        return None, f"{tool['server']}_{ref}"
    raise SystemExit(f"{tool.get('reference')}: unsupported tool kind {kind!r} for a bundle")


def build_bundles(
    docs: list[dict],
    models: dict,
    workflows: dict[str, str],
    *,
    profile: str = DEFAULT_PROFILE,
    model: str = DEFAULT_MODEL,
    include_local: bool = False,
    rename: dict[str, str] | None = None,
    mcp_cluster: str | None = None,
    only: set[str] | None = None,
) -> tuple[dict[str, dict], list[tuple[str, str, list[str]]]]:
    """({output project name: bundle}, [(project, agent, dropped tool names)])."""
    rename = rename or {}
    bundles: dict[str, dict] = {}
    dropped_report: list[tuple[str, str, list[str]]] = []
    for doc in sorted(docs, key=lambda d: (d["project"], d["name"])):
        if is_local(doc) and not include_local:
            continue
        if only and doc["name"] not in only:
            continue
        project = rename.get(doc["project"], doc["project"])
        bundle = bundles.setdefault(
            project,
            {
                "_id": stable_id(project),
                "name": project,
                "description": PROJECT_DESCRIPTIONS.get(doc["project"], f"{doc['project']} agents"),
                "agentProjectBundleVersion": BUNDLE_VERSION,
                "created": BUNDLE_CREATED,
                "createdBy": dict(BUNDLE_CREATED_BY),
                "agents": [],
            },
        )
        tools, dropped = [], []
        for tool in doc.get("tools") or []:
            ref_id, name = tool_reference(tool, models, workflows, mcp_cluster)
            if ref_id is None:
                dropped.append(name)
            else:
                tools.append({"referenceId": ref_id, "lastKnownName": name})
        bundle["agents"].append(
            {
                "_id": stable_id(project, doc["name"]),
                "name": doc["name"],
                "description": doc["description"],
                "instructions": doc["instructions"],
                "inputSchema": doc["input_schema"],
                "created": BUNDLE_CREATED,
                "createdBy": dict(BUNDLE_CREATED_BY),
                "provider": {"profileName": profile, "modelName": model},
                "tools": tools,
            }
        )
        dropped_report.append((project, doc["name"], dropped))
    return bundles, dropped_report


def parse_rename(items: list[str]) -> dict[str, str]:
    out = {}
    for item in items:
        old, sep, new = item.partition("=")
        if not sep or not old or not new:
            raise SystemExit(f"--rename wants old=new, got {item!r}")
        out[old] = new
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workflows", type=Path, help="JSON {workflow name: uuid} read from the target Platform; without it every workflow tool is dropped")
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "cloud-bundles")
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--rename", action="append", default=[], metavar="OLD=NEW", help="rename a project (repeatable)")
    ap.add_argument("--include-local", action="store_true", help="keep the ollama-* twins")
    ap.add_argument("--agent", action="append", default=[], metavar="NAME", help="build only this agent (repeatable)")
    ap.add_argument("--mcp-cluster", help="Gateway cluster id that serves the MCP tools; without it they are dropped")
    ap.add_argument("--strict", action="store_true", help="exit 1 when any tool had to be dropped")
    args = ap.parse_args(argv)

    versions = yaml.safe_load(VERSIONS.read_text())
    workflows = json.loads(args.workflows.read_text()) if args.workflows else {}
    bundles, report = build_bundles(
        load_documents(), versions["integrations"]["models"], workflows,
        profile=args.profile, model=args.model, include_local=args.include_local,
        rename=parse_rename(args.rename), mcp_cluster=args.mcp_cluster, only=set(args.agent) or None,
    )
    unknown = set(args.agent) - {a["name"] for b in bundles.values() for a in b["agents"]}
    if unknown:
        raise SystemExit(f"no such agent document (or a local twin without --include-local): {', '.join(sorted(unknown))}")

    args.out.mkdir(parents=True, exist_ok=True)
    for project, bundle in bundles.items():
        (args.out / f"{project}.json").write_text(json.dumps(bundle, indent=2, ensure_ascii=False) + "\n")

    n_dropped = 0
    print(f"{'project':<18} {'agent':<22} {'tools':>5}  dropped")
    for project, agent, dropped in report:
        kept = len(next(a for a in bundles[project]["agents"] if a["name"] == agent)["tools"])
        n_dropped += len(dropped)
        print(f"{project:<18} {agent:<22} {kept:>5}  {', '.join(dropped) if dropped else '-'}")
    print(f"\n{sum(len(b['agents']) for b in bundles.values())} agents in {len(bundles)} bundles -> {args.out}")
    if n_dropped:
        print(f"{n_dropped} tool(s) dropped because the target lacks them (listed above)", file=sys.stderr)
        if args.strict:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
