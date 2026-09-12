#!/usr/bin/env python3
"""Compare local models on what the FlowAI twins actually need: tool calls of the right SHAPE.

Not a leaderboard. Every case here is a failure this lab measured on a real agent session, so a model
that scores well is a model that would not have caused the incident:

  plain-string   `filter` must arrive as "br1". qwen2.5:7b sent {"site": "br1", "summary": "..."} -
                 the Platform does not type-check a workflow input, so the object reached the runner,
                 matched nothing, and the agent said "not in NetBox" about a device that exists.
  no-nulls       An operation with optional parameters must receive ONLY the one being used. A 7B model
                 filled every declared property with null and the Platform rejected the call
                 (invalid-tool-input) before it reached NetBox.
  right-tool     Two plausible tools, one correct. Picking the wrong one is a wasted round trip a
                 small model often cannot recover from.
  terse          "reply with the version string only" - the twins' prompts demand short answers, and
                 output tokens are wall-clock on a reasoning model.

Latency and output tokens matter as much as correctness: the Platform has an inference timeout, and a
reasoning model pays for every trace it emits (measured: qwen3:30b-a3b answered correctly in 52 s with
1820 output tokens, against 62 s and 195 tokens for a 7B on CPU).

    scripts/model-bakeoff.py                  # every installed model, 3 runs each
    scripts/model-bakeoff.py --runs 5 qwen3:8b qwen3.6:35b
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request

HOST = "http://127.0.0.1:11434"

DEVICES_TOOL = {
    "type": "function",
    "function": {
        "name": "wf-netbox-devices-v1",
        "description": "List NetBox devices, optionally filtered.",
        "parameters": {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "string",
                    "description": "One value: a device name (br2-sw01), a site slug (br1) or a "
                    "role slug (leaf). Empty string for every device.",
                }
            },
            "required": ["filter"],
        },
    },
}

# the shape that caused the original incident: eight optional strings, all nullable-looking
LIST_TOOL = {
    "type": "function",
    "function": {
        "name": "dcim_devices_list",
        "description": "List devices from NetBox.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Device name"},
                "site": {"type": "string", "description": "Site slug"},
                "role": {"type": "string", "description": "Role slug"},
                "platform": {"type": "string", "description": "Platform slug"},
                "status": {"type": "string", "description": "Status"},
                "tag": {"type": "string", "description": "Tag slug"},
                "q": {"type": "string", "description": "Free-text search"},
                "limit": {"type": "integer", "description": "Page size"},
            },
            "required": [],
        },
    },
}

SHOW_TOOL = {
    "type": "function",
    "function": {
        "name": "wf-show-command-v1",
        "description": "Run one show command on a lab device and return its parsed output.",
        "parameters": {
            "type": "object",
            "properties": {
                "device": {"type": "string", "description": "Inventory node name"},
                "command": {"type": "string", "description": "One show command"},
            },
            "required": ["device", "command"],
        },
    },
}

SYSTEM = (
    "You answer questions about a network lab using the tools provided. Call exactly one tool. "
    "Pass a tool only the inputs it needs: leave an unused input out of the call entirely and never "
    "set one to null."
)


def check_plain_string(call: dict) -> tuple[bool, str]:
    if call.get("name") != "wf-netbox-devices-v1":
        return False, f"called {call.get('name')}"
    v = (call.get("arguments") or {}).get("filter")
    if isinstance(v, str):
        return (v.strip().lower() == "br1"), f'filter={v!r}'
    return False, f"filter is {type(v).__name__}: {json.dumps(v)[:60]}"


def check_no_nulls(call: dict) -> tuple[bool, str]:
    if call.get("name") != "dcim_devices_list":
        return False, f"called {call.get('name')}"
    args = call.get("arguments") or {}
    nulls = [k for k, v in args.items() if v is None]
    extra = [k for k, v in args.items() if v is not None and k not in ("site", "limit")]
    if nulls:
        return False, f"nulls: {nulls}"
    if str(args.get("site", "")).lower() != "br1":
        return False, f"site={args.get('site')!r}"
    return (not extra), (f"extra: {extra}" if extra else "site only")


def check_right_tool(call: dict) -> tuple[bool, str]:
    if call.get("name") != "wf-show-command-v1":
        return False, f"called {call.get('name')}"
    args = call.get("arguments") or {}
    dev_ok = str(args.get("device", "")).lower() == "br2-sw01"
    return dev_ok, f"device={args.get('device')!r}"


CASES = [
    ("plain-string", [DEVICES_TOOL], "Which devices are at site br1?", check_plain_string),
    ("no-nulls", [LIST_TOOL], "List the devices at site br1.", check_no_nulls),
    ("right-tool", [DEVICES_TOOL, SHOW_TOOL],
     "What software version is br2-sw01 running? Use a show command.", check_right_tool),
]


def chat(model: str, tools: list, prompt: str, timeout: int) -> tuple[dict, float, int, str]:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        "tools": tools,
        "stream": False,
        "options": {"temperature": 0},
    }
    req = urllib.request.Request(
        f"{HOST}/api/chat", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    dt = time.time() - t0
    msg = d.get("message") or {}
    calls = msg.get("tool_calls") or []
    out_tokens = d.get("eval_count", 0)
    thinking = (msg.get("thinking") or "")
    call = (calls[0].get("function") or {}) if calls else {}
    return call, dt, out_tokens, thinking


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="*")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()

    with urllib.request.urlopen(f"{HOST}/api/tags", timeout=10) as r:
        installed = [m["name"] for m in json.loads(r.read())["models"]]
    models = args.models or [m for m in installed if "llava" not in m]

    print(f"{'model':26} {'case':13} {'pass':>5} {'median s':>9} {'out tok':>8}  note")
    print("-" * 96)
    summary: dict[str, dict] = {}
    for model in models:
        agg = {"pass": 0, "total": 0, "secs": [], "toks": [], "thinks": 0, "err": ""}
        for case_name, tools, prompt, check in CASES:
            oks, secs, toks, notes = [], [], [], []
            for _ in range(args.runs):
                try:
                    call, dt, tok, thinking = chat(model, tools, prompt, args.timeout)
                except Exception as e:  # a model that cannot do tools at all fails here
                    agg["err"] = f"{type(e).__name__}: {str(e)[:60]}"
                    oks, notes = [False], [agg["err"]]
                    break
                ok, note = check(call)
                oks.append(ok)
                secs.append(dt)
                toks.append(tok)
                notes.append(note)
                if thinking.strip():
                    agg["thinks"] += 1
            p = sum(oks)
            agg["pass"] += p
            agg["total"] += len(oks)
            agg["secs"] += secs
            agg["toks"] += toks
            med = statistics.median(secs) if secs else 0.0
            mtok = int(statistics.median(toks)) if toks else 0
            worst = next((n for o, n in zip(oks, notes) if not o), notes[0] if notes else "")
            print(f"{model:26} {case_name:13} {p}/{len(oks):>3} {med:9.1f} {mtok:8}  {worst[:34]}")
        summary[model] = agg
    print("-" * 96)
    print(f"{'model':26} {'score':>7} {'median s':>9} {'med out':>8}  thinking")
    for model, a in sorted(summary.items(), key=lambda kv: (-kv[1]["pass"], statistics.median(kv[1]["secs"] or [999]))):
        med = statistics.median(a["secs"]) if a["secs"] else 0.0
        mtok = int(statistics.median(a["toks"])) if a["toks"] else 0
        th = f"{a['thinks']}/{a['total']} runs emitted traces" if a["thinks"] else "none"
        print(f"{model:26} {a['pass']:3}/{a['total']:<3} {med:9.1f} {mtok:8}  {th}{'  ' + a['err'] if a['err'] else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
