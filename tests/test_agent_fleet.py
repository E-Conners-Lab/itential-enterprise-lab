"""PID S4d element 5 (ADR 0046): the agent fleet (netbox-sot, device-ops, compliance, diagnostics, remediation) with a
local twin each under itential/agents/, tiered by tool kind. These tests hold the documents, the play, the verify script
and the PID to each other. They run in CI with no lab access."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
AGENTS = ROOT / "itential" / "agents"
VERSIONS = ROOT / "itential" / "versions.yaml"
AGENT_TASK = ROOT / "ansible" / "playbooks" / "tasks" / "flowai-agent.yml"
VERIFY = ROOT / "verify" / "test-06-flowai.sh"
PID = ROOT / "docs" / "PID.md"
ADR = ROOT / "docs" / "adr" / "0046-agent-fleet-tiered-autonomy.md"

FLEET = ("netbox-sot", "device-ops", "compliance", "diagnostics", "remediation")
DEVICE_WRITE_TOOLS = {
    "send-config",
    "Push Configuration with Approval",
    "Add Branch VLAN",
    "Remove Branch VLAN",
}

def _file(name: str) -> str:
    """A workflow's file in itential/workflows/: its name in lowercase with dashes (ADR 0067)."""
    return name.lower().replace(" ", "-") + ".json"



@pytest.fixture(scope="module")
def docs() -> dict:
    return {p.stem: yaml.safe_load(p.read_text()) for p in AGENTS.glob("*.yaml")}


@pytest.fixture(scope="module")
def versions() -> dict:
    return yaml.safe_load(VERSIONS.read_text())


def tool_names(doc: dict) -> set[str]:
    return {t["reference"] for t in doc["tools"]}


def test_every_fleet_agent_exists_on_claude_with_a_local_twin(
    docs: dict, versions: dict
) -> None:
    profiles = {p["name"] for p in versions["llm"]["profiles"]}
    for name in FLEET:
        assert name in docs and f"{name}-local" in docs, (
            f"{name} needs a Claude document and a local twin"
        )
        assert (
            docs[name]["profile"] == "anthropic"
            and docs[f"{name}-local"]["profile"] == "ollama-mac"
        )
        assert docs[name]["project"] == docs[f"{name}-local"]["project"] == "lab-netops"
        assert {docs[name]["profile"], docs[f"{name}-local"]["profile"]} <= profiles
        # The old cap was three, justified as "a 7B model loses its way with more" - a measured
        # property of qwen2.5:7b on four CPU cores, not a law. Re-measured on gemma4:26b (ADR 0062):
        # netbox-sot-local went from two tools to four and got BETTER, answering a six-clause question
        # 6/6 with no wasted call, where at two tools it burned one and returned a false "not in
        # NetBox". The cap stays, because tool schemas cost input tokens on every request (that same
        # question went 10.5k -> 18.6k) and an unbounded list is how a twin ends up holding the wide
        # reads that caused the timeout of ADR 0059 - but it is now set from measurement, not from a
        # model nobody runs. Raise it again the same way: measure first, then move the number.
        assert 1 <= len(docs[f"{name}-local"]["tools"]) <= 6, (
            f"{name}-local: a local twin gets one to six tools (ADR 0062 - re-measure before raising)"
        )
        assert len(docs[f"{name}-local"]["tools"]) <= len(docs[name]["tools"])


def test_tiered_autonomy_by_tool_kind(docs: dict) -> None:
    # read-only tiers hold no device-writing tool at all; remediation's only write is the governed push
    for name in ("netbox-sot", "device-ops", "compliance", "diagnostics"):
        for variant in (name, f"{name}-local"):
            assert not (tool_names(docs[variant]) & DEVICE_WRITE_TOOLS), (
                f"{variant} must not write to devices"
            )
    assert tool_names(docs["netbox-sot"]) == {
        "dcim_devices_list",
        "dcim_devices_retrieve",
        "dcim_interfaces_list",
        "ipam_ip_addresses_list",
        "ipam_vlans_list",
        "dcim_sites_list",
        # S4e (ADR 0048): the enriched objects
        "dcim_racks_list",
        "circuits_circuits_list",
        "ipam_asns_list",
        "ipam_vrfs_list",
        "ipam_prefixes_list",
    }
    assert all(
        t["kind"] == "integration" and t["model"] == "netbox"
        for t in docs["netbox-sot"]["tools"]
    )
    assert {
        "Run Show Command on a Device",
        "Run Show Command on All Devices",
        "send-command",
        "dcim_devices_list",
    } == tool_names(docs["device-ops"])
    for name in ("device-ops-local", "lab-netops-local"):
        assert "Run Show Command on All Devices" in tool_names(
            docs[name]
        ) and "send-command" not in tool_names(docs[name]), (
            f"{name}: the fleet-wide read is a workflow; a raw gateway tool makes a small model invent node names (measured)"
        )
    assert "Summarize Compliance Results" in tool_names(
        docs["compliance"]
    ) and "Summarize Compliance Results" in tool_names(docs["remediation"])
    assert not (
        {"searchCompliancePlanInstances", "getJSONComplianceReportsByBatch"}
        & tool_names(docs["compliance"])
    ), (
        "the raw report tools cost an agent 638k tokens (measured): the summarising workflow replaces them"
    )
    assert all(
        t.get("app") == "ConfigurationManager"
        for t in docs["compliance"]["tools"]
        if t["kind"] == "application"
    )
    assert "updateIncident" in tool_names(
        docs["diagnostics"]
    ) and "listIncidents" in tool_names(docs["diagnostics"])
    assert "Run Show Command on a Device" in tool_names(docs["diagnostics"]), (
        "diagnostics reads the device before it proposes"
    )
    for variant in ("remediation", "remediation-local"):
        writes = tool_names(docs[variant]) & DEVICE_WRITE_TOOLS
        assert writes == {"Push Configuration with Approval"}, (
            f"{variant}: the governed push is the only write tool"
        )
        assert "updateIncident" not in tool_names(docs[variant])


def test_instructions_state_the_guardrails(docs: dict) -> None:
    assert (
        "never remediate" in docs["compliance"]["instructions"].lower()
        or "never push" in docs["compliance"]["instructions"].lower()
    )
    d = docs["diagnostics"]["instructions"]
    assert (
        "Proposed fix:" in d
        and "one work note" in d.lower()
        and "Never push configuration" in d
    )
    for cause in (
        "hostname",
        "ntp server vrf MGMT 10.100.0.1",
        "show vlan",
        "BGP",
        "no shutdown",
    ):
        assert cause in d, f"diagnostics lacks the known root cause {cause!r}"
    r = docs["remediation"]["instructions"]
    assert (
        "Work Center" in r
        and "Push Configuration with Approval" in r
        and "single configuration line" in r
    )
    assert "never bypass" in r.lower()
    for name in FLEET:
        assert "{{request}}" in docs[name]["instructions"] and docs[name][
            "input_schema"
        ]["required"] == ["request"]


def test_show_all_workflow_fans_out_deterministically(versions: dict) -> None:
    import json

    assert versions["workflows"]["show_all"] == "Run Show Command on All Devices"
    wf = json.loads(
        (ROOT / "itential" / "workflows" / "run-show-command-on-all-devices.json").read_text()
    )
    tasks = wf["tasks"]
    names = [t.get("name") for t in tasks.values()]
    assert (
        "getDevicesFiltered" in names and "join" in names and "setObjectKey" in names
    ), "device list -> selector -> merged parser input"
    send = [t for t in tasks.values() if t.get("name") == "sendCommand"]
    assert (
        len(send) == 1
        and send[0]["variables"]["incoming"]["inventory"].startswith("$var.")
        and send[0]["variables"]["incoming"]["commands"].startswith("$var.")
    )
    code = [t for t in tasks.values() if t.get("name") == "runCode"]
    assert len(code) == 1 and set(code[0]["variables"]["incoming"]["packages"]) >= {
        "genie==26.8",
        "ntc-templates==9.2.0",
    }
    assert "LIMIT = 1500" in code[0]["variables"]["incoming"]["code"], (
        "per-device output capped for a small model's context"
    )
    assert set(wf["outputSchema"]["properties"]) >= {
        "devices",
        "devices_checked",
        "results",
        "parser_errors",
    }
    assert wf["inputSchema"]["required"] == ["command"]
    override = (ROOT / "itential" / "compose.override.yml").read_text()
    # The context length used to be pinned on the lab's Ollama container. ADR 0060 moved inference to
    # the Mac Mini, whose Ollama is Homebrew-managed and not configured from this repo, so the pin
    # moved with it: the requirement is the same (a twelve-device result must fit), but it is now the
    # Mac's OLLAMA_CONTEXT_LENGTH, set by scripts/mac-ollama.sh.
    mac_script = (ROOT / "scripts" / "mac-ollama.sh").read_text()
    assert "OLLAMA_CONTEXT_LENGTH" in mac_script and "16384" in mac_script, (
        "the local model must hold a twelve-device result: set OLLAMA_CONTEXT_LENGTH=16384 on the "
        "Mac's ollama LaunchAgent (scripts/mac-ollama.sh)"
    )


def test_play_resolves_application_tools() -> None:
    text = AGENT_TASK.read_text()
    assert "item.kind == 'application'" in text and "'application:' ~ item.app" in text


def test_verify_pid_and_adr_cover_s4d5() -> None:
    text = VERIFY.read_text()
    for name in FLEET:
        assert re.search(rf'check "S4d\.5[a-z]? {re.escape(name)}', text), (
            f"verify 06 lacks the {name} check"
        )
    assert "netbox-sot-local" in text, "one local twin runs in the verify"
    assert re.search(r'check "S4d\.5g ', text) and "Run Show Command on All Devices" in text, (
        "the fleet-wide read on the local generalist"
    )
    for s in (
        "Push Configuration with Approval",
        "devcmd.py",
        "service-now.com",
        "comments_and_work_notes",
        "close_code",
    ):
        assert s in text, f"verify 06 S4d.5 lacks {s}"
    assert "| 1.11 |" in PID.read_text() and ADR.exists()


INTEGRATIONS = ROOT / "itential" / "integrations"


def integration_params() -> dict[str, dict[str, str]]:
    """operationId -> {parameter name: declared JSON Schema type}, over every Integration Model."""
    import json

    out: dict[str, dict[str, str]] = {}
    for spec in sorted(INTEGRATIONS.glob("*.json")):
        doc = json.loads(spec.read_text())
        for operations in doc["paths"].values():
            for op in operations.values():
                if isinstance(op, dict) and "operationId" in op:
                    out[op["operationId"]] = {
                        p["name"]: p["schema"].get("type", "string")
                        for p in op.get("parameters", [])
                    }
    return out


def test_no_instruction_shows_a_list_filter_for_a_single_valued_parameter(
    docs: dict,
) -> None:
    """S4f turned every NetBox filter from an array into a single value, because a workflow's
    `$var` does not resolve inside an array (ADR 0054, itential/integrations/build.py). The prompts
    kept teaching the array form, so a model that followed them sent name ["br1-sw01"] into a
    string parameter and the Platform rejected the call as invalid-tool-input. Measured
    2026-09-11 on netbox-sot-local."""
    params = integration_params()
    for name, doc in sorted(docs.items()):
        text = doc["instructions"]
        for tool in doc["tools"]:
            if tool.get("kind") != "integration":
                continue
            for param, typ in params.get(tool["reference"], {}).items():
                if typ == "array":
                    continue
                assert not re.search(rf"\b{re.escape(param)}\s*\[", text), (
                    f"{name}: instructions show {param} as a list, but "
                    f"{tool['reference']} declares it {typ}"
                )


def test_every_agent_with_integration_tools_forbids_a_null_filter(docs: dict) -> None:
    """A 7B model fills every declared property, null included, and a null fails validation before
    the call leaves the Platform (invalid-tool-input, measured 2026-09-11). Each prompt has to say
    to leave an unused filter out of the call."""
    for name, doc in sorted(docs.items()):
        if not any(t.get("kind") == "integration" for t in doc["tools"]):
            continue
        text = doc["instructions"].lower()
        assert "null" in text, f"{name}: instructions never forbid a null filter"


WORKFLOWS = ROOT / "itential" / "workflows"
REDUCING_DEVICE_TOOL = "List Devices from NetBox"


def test_the_local_twins_read_inventory_through_the_reducing_workflow() -> None:
    """A 7B model on CPU cannot be handed raw NetBox device objects: 52 fields each, five devices at br1
    are 17.9 kB, and the prompt reached 7,823 tokens - ~350 s of ingestion at ~22 tokens/sec on tools-01,
    past the Platform's inference timeout, reported as "ollama model invocation failed" while Ollama was
    still working (measured 2026-09-11). List Devices from NetBox reduces the list on the runner first. The
    Claude agents keep the raw operations: they need the full objects and ingest them in a second."""
    docs = {p.stem: yaml.safe_load(p.read_text()) for p in AGENTS.glob("*.yaml")}
    for name, doc in sorted(docs.items()):
        if doc["profile"] != "ollama-mac":
            continue
        tools = tool_names(doc)
        assert "dcim_devices_list" not in tools, (
            f"{name}: a local twin must not read the raw device list; use {REDUCING_DEVICE_TOOL}"
        )
        if REDUCING_DEVICE_TOOL in tools:
            assert REDUCING_DEVICE_TOOL in doc["instructions"], (
                f"{name}: holds {REDUCING_DEVICE_TOOL} but never tells the model to run it"
            )


def test_the_reducing_workflow_passes_no_filter_to_the_integration() -> None:
    """The filters are workflow inputs, matched in Python. The operation is called with `limit` only, so
    there is no optional filter for a model to fill with null and no array for a `$var` to fall into
    (ADR 0054)."""
    import json

    wf = json.loads((WORKFLOWS / _file(REDUCING_DEVICE_TOOL)).read_text())
    calls = [
        t for t in wf["tasks"].values() if t.get("name") == "dcim_devices_list"
    ]
    assert len(calls) == 1, f"{REDUCING_DEVICE_TOOL} should read the device list exactly once"
    incoming = calls[0]["variables"]["incoming"]
    assert set(incoming) == {"adapter_id", "limit"}, (
        f"the operation takes adapter_id and limit only, got {sorted(incoming)}"
    )
    # One input: Operations Manager refuses a job start unless EVERY declared input is supplied
    # (measured 2026-09-11: site alone -> 500, metadata.error ["name", "role"]), so three declared
    # filters would mean three keys on every call from a 7B model - the fragility this avoids.
    assert set(wf["inputSchema"]["properties"]) == {"filter"}
    assert wf["inputSchema"]["required"] == ["filter"]


def test_the_reducing_workflow_survives_a_filter_that_is_not_a_string() -> None:
    """Measured 2026-09-11: qwen2.5:7b sent `filter` as {"site": "br1", "summary": "Devices at site
    br1"}. The Platform does not type-check a workflow input, so it reached the runner intact, the
    code stringified the dict, nothing matched, and the agent answered "not in NetBox" - confidently
    wrong, which is worse than an error. The runner coerces and flags it instead."""
    import importlib.util
    import json
    import subprocess
    import sys

    spec = importlib.util.spec_from_file_location("b", ROOT / "itential" / "workflows" / "build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    devices = [
        {"name": "br1-sw01", "site": {"slug": "br1"}, "role": {"slug": "leaf"},
         "platform": {"slug": "eos"}, "status": {"value": "active"},
         "primary_ip4": {"address": "10.100.0.165/24"}},
        {"name": "br2-sw01", "site": {"slug": "br2"}, "role": {"slug": "leaf"},
         "platform": {"slug": "eos"}, "status": {"value": "active"}, "primary_ip4": None},
    ]

    def run(f: object) -> dict:
        r = subprocess.run(
            [sys.executable, "-c", build.DEVICES_CODE],
            input=json.dumps({"devices": devices, "filter": f}),
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)

    assert run("br1")["count"] == 1 and run("br1")["matched"] == "site"
    nested = run({"site": "br1", "summary": "Devices at site br1"})
    assert nested["count"] == 1, "the object the model actually sent must still find br1"
    assert nested["filter_was_not_a_string"] is True
    assert run(["br2-sw01"])["count"] == 1, "a list must coerce too"
    assert run("")["count"] == 2, "empty means every device"
    miss = run("nope-99")
    assert miss["count"] == 0 and not miss.get("error"), "a genuine miss is not an error"
    assert run({"summary": "nothing usable"}).get("filter_was_not_a_string") is True


DEVICE_WORKFLOWS = ("Run Show Command on a Device", "Push Configuration with Approval")


def test_a_device_the_inventory_lacks_ends_the_job_instead_of_hanging_the_agent() -> None:
    """Measured 2026-09-11: device-ops-local invented the device "R1"; Gateway 5 answered 404
    "Missing nodes - Inventory 'lab': [R1]", the job dead-ended ("Job has no available transitions")
    and the agent session was STILL RUNNING 18 minutes later, holding Ollama's single slot. A job
    that ends in error at least returns; one with no path to workflow_end never does. Every task that
    sends to a device needs a failure edge that reaches the end and publishes device_error."""
    import json

    for name in DEVICE_WORKFLOWS:
        wf = json.loads((WORKFLOWS / _file(name)).read_text())
        tasks, tr = wf["tasks"], wf["transitions"]
        senders = [
            tid for tid, t in tasks.items()
            if t.get("name") in ("sendCommand", "sendConfig")
            and "inventory" in (t.get("variables", {}).get("incoming") or {})
        ]
        assert senders, f"{name}: no device-sending task found"
        for tid in senders:
            edges = tr.get(tid, {})
            # It must be `error`, not `failure`. A Gateway task that 404s lands in state `error` and a
            # `failure` edge never fires for it: measured 2026-09-11, the job said "5a could have led to
            # the workflow end task, but did not" with the failure edge present and correct.
            fail = [b for b, e in edges.items() if e.get("state") == "error"]
            assert fail, (
                f"{name}: {tid} ({tasks[tid]['name']}) has no `error` transition; a device the "
                "inventory lacks dead-ends the job and hangs the calling agent for ever "
                "(a `failure` edge does not fire for an errored Gateway task)"
            )
            # and that branch must actually reach the end
            seen, stack = set(), list(fail)
            while stack:
                a = stack.pop()
                if a in seen:
                    continue
                seen.add(a)
                stack += list(tr.get(a, {}))
            assert "workflow_end" in seen, (
                f"{name}: {tid}'s failure branch never reaches workflow_end"
            )
        assert "device_error" in wf["outputSchema"]["properties"], (
            f"{name}: publishes no device_error for the agent to report"
        )


def test_no_twin_is_told_never_to_invent_a_name_without_a_way_to_look_one_up() -> None:
    """device-ops-local's prompt said "you never invent a device name" while its only tools ran show
    commands - it had no source of real names, so the instruction was unfollowable and the model
    invented "R1" (measured 2026-09-11). A prompt that forbids inventing a name has to be paired with
    a tool that supplies them."""
    docs = {p.stem: yaml.safe_load(p.read_text()) for p in AGENTS.glob("*.yaml")}
    lookups = {"List Devices from NetBox", "dcim_devices_list", "Run Show Command on All Devices"}
    for name, doc in sorted(docs.items()):
        text = doc["instructions"].lower()
        # "never invent device output" (lab-netops-mac) is a different instruction: it forbids
        # fabricating command results, not names, and that agent does constrain devices to the
        # inventory. Only the name form needs a tool that supplies real names.
        if not re.search(r"invent (a |any )?(device |node )?names?\b", text):
            continue
        held = tool_names(doc)
        assert held & lookups, (
            f"{name}: forbids inventing a device name but holds no tool that returns real ones "
            f"({sorted(held)})"
        )


def test_the_verify_runs_every_local_twin(docs: dict) -> None:
    """The twins cost no provider tokens, so there was never a budget reason for four of the five to be
    the untested ones - and that is exactly where the unfollowable prompt survived (device-ops-local,
    measured 2026-09-11). Every ollama-mac document runs in the verify."""
    text = VERIFY.read_text()
    for name, doc in sorted(docs.items()):
        if doc["profile"] != "ollama-mac":
            continue
        assert re.search(rf"run_agent {re.escape(name)}\b", text), (
            f"{name} is never exercised by verify/test-06-flowai.sh"
        )


def test_every_twin_suppresses_thinking_in_its_prompt(docs: dict) -> None:
    """ADR 0061. The Platform does not send Ollama's `think: false` (a real session emitted 1820 output
    tokens) and `PARAMETER think false` is not a Modelfile parameter, so the prompt is the only lever.
    Measured with scripts/model-bakeoff.py: on the answer turn, thinking costs 6-8x - gemma4:26b goes
    9.9s/137 tokens to 1.6s/39. It must be the FIRST line: a directive buried mid-prompt is not
    reliably honoured."""
    for name, doc in sorted(docs.items()):
        if doc["profile"] != "ollama-mac":
            continue
        first = doc["instructions"].splitlines()[0].strip()
        assert first == "/no_think", (
            f"{name}: first prompt line is {first!r}, not /no_think - the twin will emit reasoning "
            "traces the operator pays for in wall-clock"
        )


def test_the_inference_host_holds_one_model_at_a_time() -> None:
    """ADR 0060 moved inference to the Mac but left the lab container's guards behind. Measured
    2026-09-12: a benchmark loaded qwen3.6:35b-32k (27.0 GB) beside gemma4:26b (25.8 GB), OLLAMA_KEEP_ALIVE
    pinned both, and 52.8 GB of 64 GB left Ollama thrashing between runners - an agent request failed
    with "ollama model invocation failed: fetch failed". Two models is what broke it, so the cap is one.
    The context length is checked here too: both are properties of the inference host that no play can
    set, so the script is the only place they are declared."""
    script = (ROOT / "scripts" / "mac-ollama.sh").read_text()
    assert "OLLAMA_MAX_LOADED_MODELS</key><string>1<" in script, (
        "the Mac must hold one model at a time: a second large model beside the fleet's model has "
        "already caused a failed agent run"
    )
    assert "OLLAMA_CONTEXT_LENGTH</key><string>16384<" in script
    assert "OLLAMA_HOST</key><string>0.0.0.0:11434<" in script, (
        "bound to loopback only, the lab cannot reach it at all"
    )


def test_the_mac_script_does_not_hardcode_the_model() -> None:
    """The script's default said qwen3:30b-a3b for a day after ADR 0061 chose gemma4:26b, so re-running
    it pulled 18 GB of the wrong model (measured 2026-09-12). The model is read from versions.yaml so
    the setup script and the Platform profile cannot disagree."""
    import yaml as y

    script = (ROOT / "scripts" / "mac-ollama.sh").read_text()
    versions = y.safe_load((ROOT / "itential" / "versions.yaml").read_text())
    model = next(p["model"] for p in versions["llm"]["profiles"] if p["provider"] == "ollama")
    # the assignment itself, not prose: the comments deliberately name both models to record the history
    lines = script.splitlines()
    i = next((n for n, ln in enumerate(lines) if re.match(r"\s*MODEL=", ln)), None)
    assert i is not None, "mac-ollama.sh no longer assigns MODEL"
    block = "\n".join(lines[i : i + 5])  # the assignment spans a few lines
    assert model not in block, (
        f"{model} is hardcoded in the MODEL= assignment; read it from versions.yaml instead"
    )
    assert "versions.yaml" in block, "the MODEL default must come from versions.yaml"
