#!/usr/bin/env python3
"""The guide to a workflow, from its document alone.

docs/workflows/<file>.md says what a workflow does from start to end: how it runs, its inputs and what it publishes,
the steps in canvas order with every branch, where it waits for a person, what it touches, and what the job says when
it stops. Everything comes from the exported document (what build.py writes and the play imports): a task's
`description` is its step (build.py `step`, written `short: what it does`), its `summary` the canvas label, the
transitions the arrows. The renderer needs nothing but the standard library, so the same code can run on the Gateway
runner against a document read from the Platform API (a Describe Workflow workflow) and say the same thing.

  python describe.py rotate-aws-vpn-key.json     # one guide on stdout
  python describe.py --write                      # docs/workflows/: a page per guided workflow and the index
  echo '{"document": {...}}' | python describe.py # the runner contract: JSON in, {"markdown", "html", ...} out

The last form is what Describe Workflow runs on the Gateway runner (a runCode task carries this file verbatim, the
document on stdin): the same walker, the same words, with an HTML rendering of the guide for a Work Center card.

tests/test_workflow_docs.py keeps the pages equal to what the committed documents render.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, OrderedDict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
DOCS = ROOT / "docs" / "workflows"
START, END = "workflow_start", "workflow_end"
PLATFORM_APPS = ("WorkFlowEngine", "GatewayManager")
OTHER = "other tasks: not in any step"  # a task whose description is still its summary (build.py gave it no step)

# Every workflow in versions.yaml gets a guide. The hand-written lines under "How it runs" are the one thing a
# document cannot say about itself; a workflow with none says only what versions.yaml knows (runs_lines).
NOTES = {
    "Rotate AWS VPN Key": [
        "The same rotation, task for task, is inside Rotate AWS VPN Key Monthly: that copy runs from a daily "
        "schedule and rotates on the 1st of the month only.",
    ],
    "Rotate AWS VPN Key Monthly": [
        "The rotation itself is Rotate AWS VPN Key's, task for task; only the due check in front of it is this "
        "workflow's own.",
    ],
    "Tear Down Expired AWS VPN": [
        "The teardown itself is Tear Down AWS VPN's, without its two cards: the end time approved on Deploy's card "
        "is the approval.",
    ],
    "Hand Off AWS VPN": [
        "Verify AWS VPN's checks run inside it after the push, task for task.",
    ],
}


def guided(versions: dict) -> list:
    """The workflows with a guide: every one versions.yaml names, in its order."""
    return list(versions["workflows"].values())


def file_name(name: str) -> str:
    """build.file_name: the document's file is its name in lowercase with dashes."""
    return name.lower().replace(" ", "-") + ".json"


# --- the graph ----------------------------------------------------------------------------------------------------
def edges(doc: dict) -> list:
    """[(src, dst, state)] in document order."""
    return [(a, b, e["state"]) for a, tos in doc["transitions"].items() for b, e in tos.items()]


def rows(doc: dict) -> dict:
    """Each node's row: the longest path from workflow_start with the back edges (loops) set aside. The canvas is
    laid out by the same rule, so the guide reads in the order the canvas draws."""
    succ: dict = {}
    for a, b, _ in edges(doc):
        succ.setdefault(a, []).append(b)
    state, back = {}, set()

    def classify(n: str) -> None:
        state[n] = 1
        for d in succ.get(n, []):
            if d not in state:
                classify(d)
            elif state[d] == 1:
                back.add((n, d))
        state[n] = 2

    classify(START)
    order, seen = [], set()

    def topo(n: str) -> None:
        seen.add(n)
        for d in succ.get(n, []):
            if (n, d) not in back and d not in seen:
                topo(d)
        order.append(n)

    topo(START)
    row = {START: 0}
    for n in reversed(order):
        for d in succ.get(n, []):
            if (n, d) not in back:
                row[d] = max(row.get(d, 0), row[n] + 1)
    return row


def steps(doc: dict) -> list:
    """[(title, [task ids])]: a step's tasks in row order; the steps in the order they finish (the row of their last
    task), so a step that catches failures from everywhere comes after the steps it catches."""
    row = rows(doc)
    tasks = {k: t for k, t in doc["tasks"].items() if k not in (START, END)}
    groups: dict = OrderedDict()
    for tid in sorted(tasks, key=lambda k: (row.get(k, 10**6), k)):
        t = tasks[tid]
        title = t.get("description") or ""
        if not title or title == t.get("summary"):
            title = OTHER
        groups.setdefault(title, []).append(tid)
    return sorted(groups.items(), key=lambda kv: (max(row.get(k, 10**6) for k in kv[1]), kv[0]))


# --- words ----------------------------------------------------------------------------------------------------------
def label(t: dict, state: str) -> str:
    if state == "success":
        return "yes" if t["name"] == "evaluation" else "then"
    if state == "error":
        return "could not run"
    if state == "loop":
        return "for each item"
    return "no" if t["name"] == "evaluation" else "failed"


def tag(t: dict) -> str:
    """What a task reaches beyond the Platform's own engine; empty for a query, an evaluation, a string."""
    inc = t["variables"]["incoming"]
    if t.get("type") == "manual":
        return "waits for a person"
    if t["app"] == "GatewayManager":
        if t["name"] == "runService":
            return "Gateway service `%s`" % inc.get("serviceName")
        if t["name"] == "runCode":
            return "Python on the Gateway runner"
        return "Gateway `%s`" % t["name"]
    if t["app"] not in PLATFORM_APPS:
        return "%s `%s`" % (t["app"], t["name"])
    return ""


def var(ref) -> str:
    """A reference in words: `$var.job.error` is the job variable error, `$var.4c.object` is task 4c's object."""
    if not isinstance(ref, str) or not ref.startswith("$var."):
        return json.dumps(ref) if not isinstance(ref, str) else '"%s"' % ref
    _, owner, *path = ref.split(".")
    name = ".".join(path)
    return "the job variable `%s`" % name if owner == "job" else "task %s's `%s`" % (owner, name)


def short(title: str) -> str:
    return title.split(":")[0]


def sentence(s: str) -> str:
    return s[0].upper() + s[1:]


# --- the sections -----------------------------------------------------------------------------------------------------
def ref(dst: str, here: int, where: dict, doc: dict) -> str:
    if dst == END:
        return "done"
    out = doc["tasks"][dst]["summary"]
    return out if where[dst] == here else "%s (step %d)" % (out, where[dst])


def task_line(tid: str, nxt, here: int, where: dict, doc: dict) -> str:
    t = doc["tasks"][tid]
    head = "`%s` %s" % (tid, t["summary"])
    if tag(t):
        head += " — " + tag(t)
    branches = []
    for dst, e in doc["transitions"].get(tid, {}).items():
        if e["state"] == "success" and dst == nxt:
            continue  # the arrow to the next line needs no words
        branches.append("%s → %s" % (sentence(label(t, e["state"])), ref(dst, here, where, doc)))
    if not doc["transitions"].get(tid):
        looped = any(e["state"] == "loop" for tos in doc["transitions"].values() for d, e in tos.items() if d == tid)
        branches.append("No arrow out: back to the loop for the next item" if looped
                        else "No arrow out: the job ends in error here, by design")
    if not branches:
        return "- " + head
    stop = "" if head.endswith(("?", ".")) else "."
    return "- " + head + stop + " " + ". ".join(branches) + "."


def walk_through(doc: dict, steps_: list, where: dict) -> list:
    out = []
    for n, (title, ids) in enumerate(steps_, 1):
        out += ["### %d. %s" % (n, title), ""]
        for i, tid in enumerate(ids):
            out.append(task_line(tid, ids[i + 1] if i + 1 < len(ids) else None, n, where, doc))
        out.append("")
    return out


def waits(doc: dict, where: dict) -> list:
    out = []
    for tid, t in doc["tasks"].items():
        if t.get("type") != "manual":
            continue
        inc = t["variables"]["incoming"]
        bits = []
        if t["name"] == "ViewData":
            bits.append('a Work Center task headed "%s"' % inc.get("header"))
            shows = [var(inc[k]) for k in ("message", "body") if inc.get(k)]
            if shows:
                bits.append("showing " + " and ".join(shows))
            bits.append("buttons %s / %s" % (inc.get("btn_success"), inc.get("btn_failure")))
        else:
            bits.append("the view `%s`" % t.get("view"))
        groups = t.get("groups") or []
        bits.append("for the groups " + ", ".join(groups) if groups else "no group set, so anyone who can open the task")
        out.append("- `%s` %s (step %d): %s." % (tid, t["summary"], where[tid], "; ".join(bits)))
    return out or ["Nothing: it runs through without a person."]


def touches(doc: dict) -> list:
    tasks = [t for k, t in doc["tasks"].items() if k not in (START, END)]
    services = Counter(t["variables"]["incoming"].get("serviceName") for t in tasks
                       if t["app"] == "GatewayManager" and t["name"] == "runService")
    code = sum(1 for t in tasks if t["app"] == "GatewayManager" and t["name"] == "runCode")
    adapters = Counter((t["app"], t["name"]) for t in tasks if t["app"] not in PLATFORM_APPS)
    manual = sum(1 for t in tasks if t.get("type") == "manual")
    out = []
    if services:
        out.append("- Gateway services: " + ", ".join("`%s` (%d)" % kv for kv in sorted(services.items())))
    if code:
        out.append("- Python on the Gateway runner: %d tasks" % code)
    for (app, name), n in sorted(adapters.items()):
        out.append("- %s `%s` (%d)" % (app, name, n))
    if manual:
        out.append("- Work Center: %d task%s" % (manual, "" if manual == 1 else "s"))
    out.append("- Everything else is the Platform's own engine: queries, evaluations, strings, job variables.")
    return out


def publishers(doc: dict, job_var: str) -> list:
    """The tasks that write the job variable, with what they write."""
    out = []
    for tid, t in doc["tasks"].items():
        if tid in (START, END):
            continue
        inc = t["variables"]["incoming"]
        for ref_ in t["variables"]["outgoing"].values():
            if ref_ != "$var.job." + job_var:
                continue
            if t["name"] == "makeData":
                what = inc.get("input")
            elif t["name"] == "query":
                what = "`%s` of %s" % (inc.get("query"), var(inc.get("obj")))
            elif t["name"] == "replace":
                what = "the template %s with `%s` filled in by %s" % (var(inc.get("str")), inc.get("substr"),
                                                                      var(inc.get("newSubstr")))
            elif t["name"] == "runCode":
                what = "what the runner printed"
            else:
                what = "%s's result" % t["name"]
            out.append((tid, t["summary"], what))
    return out


def stops(doc: dict, where: dict) -> list:
    rows_ = publishers(doc, "error")
    if not rows_:
        return ["The document publishes no `error` job variable."]
    out = ["| Step | Task | What `error` says |", "|---|---|---|"]
    for tid, summary, what in sorted(rows_, key=lambda r: (where[r[0]], r[0])):
        out.append("| %d | `%s` %s | %s |" % (where[tid], tid, summary, what.replace("|", "\\|")))
    return out


def outcomes(doc: dict, where: dict) -> list:
    rows_ = publishers(doc, "outcome")
    if not rows_:
        return []
    out = ["| Step | Task | What `outcome` says |", "|---|---|---|"]
    for tid, summary, what in sorted(rows_, key=lambda r: (where[r[0]], r[0])):
        out.append("| %d | `%s` %s | %s |" % (where[tid], tid, summary, what.replace("|", "\\|")))
    return out


def schema_table(schema: dict, with_required: bool) -> list:
    props = (schema or {}).get("properties") or {}
    if not props:
        return []
    required = set((schema or {}).get("required") or [])
    out = ["| Name | Type | Required | What |", "|---|---|---|---|"] if with_required else ["| Name | Type |", "|---|---|"]
    for name, p in props.items():
        typ = p.get("type")
        typ = " or ".join(typ) if isinstance(typ, list) else str(typ)
        if with_required:
            req = "yes" if name in required or p.get("required") else "no"
            out.append("| `%s` | %s | %s | %s |" % (name, typ, req, (p.get("description") or "").replace("|", "\\|")))
        else:
            out.append("| `%s` | %s |" % (name, typ))
    return out


def mermaid(doc: dict, steps_: list, where: dict) -> list:
    out = ["```mermaid", "flowchart TD", "  S([start])", "  E([done])"]
    for n, (title, _) in enumerate(steps_, 1):
        # "1. the plan" would be a Markdown list to Mermaid ("Unsupported markdown: list"), so the label reads step N
        out.append('  s%d["step %d: %s"]' % (n, n, short(title).replace('"', "#quot;")))
    links: dict = OrderedDict()
    for a, b, st in edges(doc):
        src = "S" if a == START else "s%d" % where[a]
        dst = "E" if b == END else "s%d" % where[b]
        if src == dst:
            continue
        if a == START:
            lab = ""
        else:
            t = doc["tasks"][a]
            lab = "%s %s" % (t["summary"], label(t, st)) if t["name"] == "evaluation" else \
                ("" if st == "success" else label(t, st))
        links.setdefault((src, dst), [])
        if lab and lab not in links[(src, dst)]:
            links[(src, dst)].append(lab)
    for (src, dst), labs in links.items():
        if not labs:
            out.append("  %s --> %s" % (src, dst))
        else:
            text = "<br/>".join(labs[:3] + (["…"] if len(labs) > 3 else [])).replace('"', "#quot;")
            out.append('  %s -- "%s" --> %s' % (src, text, dst))
    out.append("```")
    return out


# --- the page --------------------------------------------------------------------------------------------------------
def guide(doc: dict, runs: list, source: str = "") -> str:
    """The page. `source` says where the document came from; empty means the committed file in this repository."""
    steps_ = steps(doc)
    where = {tid: n for n, (_, ids) in enumerate(steps_, 1) for tid in ids}
    n_tasks = len(doc["tasks"]) - 2
    source = source or ("Generated by `itential/workflows/describe.py` from `itential/workflows/%s`; a test keeps "
                        "them equal, so a change goes into `build.py`, not this page." % file_name(doc["name"]))
    lines = [
        "# " + doc["name"],
        "",
        doc.get("description", "").rstrip(".") + ".",
        "",
        "*%s %d task%s in %d step%s.*" % (source, n_tasks, "" if n_tasks == 1 else "s", len(steps_),
                                            "" if len(steps_) == 1 else "s"),
        "",
        "## How it runs",
        "",
        *["- " + r for r in runs],
        "",
        "## Inputs",
        "",
        *(schema_table(doc.get("inputSchema"), True) or ["None: the workflow takes no input."]),
        "",
        "## What it publishes",
        "",
        "The job variables a caller, a card or the next workflow reads when the job is done.",
        "",
        *(schema_table(doc.get("outputSchema"), False) or ["None."]),
        "",
        "## The steps at a glance",
        "",
        "Each box is a step of the walk-through below; an arrow without words is the ordinary way on.",
        "",
        *mermaid(doc, steps_, where),
        "",
        "## The walk-through",
        "",
        "One line per task, in canvas order; the task's id first, then its label as the canvas shows it. A line says "
        "where a branch goes only when it is not simply the next line.",
        "",
        *walk_through(doc, steps_, where),
        "## Where it waits for a person",
        "",
        *waits(doc, where),
        "",
        "## What it touches",
        "",
        *touches(doc),
        "",
        "## When it stops: what the job says",
        "",
        *stops(doc, where),
    ]
    if outcomes(doc, where):
        lines += ["", "## When it finishes: what the job says", "", *outcomes(doc, where)]
    return "\n".join(lines).rstrip() + "\n"


# --- HTML for a card -------------------------------------------------------------------------------------------------
def _inline(text: str) -> str:
    """The guide's inline marks: `code`, *emphasis*; everything else escaped."""
    import html as _html
    import re as _re

    out = _html.escape(text, quote=False)
    out = _re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = _re.sub(r"(?<!\w)\*([^*]+)\*(?!\w)", r"<em>\1</em>", out)
    return out


def html_guide(markdown: str) -> str:
    """The guide as HTML, from the Markdown this file writes (headings, bullets, tables, a Mermaid block). The Mermaid
    source goes into a <pre> (a card cannot load scripts); the step list below it says the same in words."""
    lines = markdown.splitlines()
    out, i, in_list = [], 0, False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    while i < len(lines):
        ln = lines[i]
        if ln.startswith("```mermaid"):
            close_list()
            j = lines.index("```", i + 1)
            out.append('<pre class="diagram">%s</pre>' % _inline("\n".join(lines[i + 1:j])))
            i = j + 1
            continue
        if ln.startswith("|"):
            close_list()
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip("|").split("|")])
                i += 1
            head, body = rows[0], [r for r in rows[2:]]  # rows[1] is the |---| line
            out.append("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (
                "".join("<th>%s</th>" % _inline(c) for c in head),
                "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % _inline(c.replace("\\|", "|")) for c in r) for r in body)))
            continue
        if ln.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>%s</li>" % _inline(ln[2:]))
        elif ln.startswith("#"):
            close_list()
            level = len(ln) - len(ln.lstrip("#"))
            out.append("<h%d>%s</h%d>" % (level, _inline(ln[level:].strip()), level))
        elif ln.strip():
            close_list()
            out.append("<p>%s</p>" % _inline(ln))
        else:
            close_list()
        i += 1
    close_list()
    return "\n".join(out)


def describe_document(data: dict) -> dict:
    """The runner contract: {"document": <a workflow document>, "runs": [lines]} in, the guide out. A document the
    Platform exports carries the same tasks, transitions and schemas the committed files do."""
    doc = data.get("document") or {}
    if not isinstance(doc, dict) or not doc.get("name") or not isinstance(doc.get("tasks"), dict):
        return {"ok": False, "message": "no workflow document: expected {document: {name, tasks, transitions, ...}}"}
    runs = [str(r) for r in (data.get("runs") or [])] or ["As the Platform holds it: this description was made from "
                                                          "the stored document, on request."]
    md = guide(doc, runs, source=str(data.get("source") or "Described from the document as the Platform stores it."))
    steps_ = steps(doc)
    n_tasks, manual = len(doc["tasks"]) - 2, sum(1 for t in doc["tasks"].values() if t.get("type") == "manual")
    summary = "%s: %d task%s in %d step%s, %s" % (doc["name"], n_tasks, "" if n_tasks == 1 else "s", len(steps_),
                                                  "" if len(steps_) == 1 else "s",
                                                  "no stop for a person" if not manual else
                                                  "%d stop%s for a person" % (manual, "" if manual == 1 else "s"))
    return {"ok": True, "name": doc["name"], "tasks": n_tasks, "steps": len(steps_), "manual": manual,
            "summary": summary, "step_titles": [t for t, _ in steps_], "markdown": md, "html": html_guide(md)}


def describe_stdin() -> int:
    """stdin JSON in, one JSON line out; a bad document is an answer, not a traceback (the workflow reads `ok`)."""
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError as exc:
        data = {"document": None, "_error": str(exc)}
    try:
        out = describe_document(data if isinstance(data, dict) else {})
    except Exception as exc:  # noqa: BLE001 - the runner's answer must be JSON whatever happened
        out = {"ok": False, "message": "the description failed: %s: %s" % (type(exc).__name__, exc)}
    sys.stdout.write(json.dumps(out) + "\n")
    return 0


def runs_lines(name: str, versions: dict) -> list:
    """How the lab starts the workflow, from versions.yaml: an automation with its schedule or endpoint, or by hand."""
    key = {v: k for k, v in versions["workflows"].items()}[name]
    om = (versions.get("operations_manager") or {}).get(key)
    if not om:
        return ["By hand, from the Platform: the Studio's run button or the API. No automation, form, endpoint or "
                "schedule starts it."]
    out = ['Through the Operations Manager automation "%s": %s.' % (om["automation"], om.get("description", "").rstrip("."))]
    sched = om.get("schedule")
    if sched:
        every = "every %s" % sched.get("repeat_unit") if sched.get("repeat_frequency", 1) == 1 else \
            "every %s %ss" % (sched["repeat_frequency"], sched.get("repeat_unit"))
        at = " at %s UTC" % sched["at_utc"] if sched.get("at_utc") else ""
        out.append('On the schedule "%s": %s%s.' % (sched["name"], every, at))
    ep = om.get("endpoint")
    if ep:
        out.append('From the endpoint trigger "%s" (route `%s`).' % (ep["name"], ep["route"]))
    return out


def page_path(name: str) -> Path:
    return DOCS / file_name(name).replace(".json", ".md")


def pages(versions: dict) -> dict:
    """{path: text} for every guided workflow and the index, from the committed documents."""
    out, index = {}, []
    for name in guided(versions):
        doc = json.loads((HERE / file_name(name)).read_text())
        out[page_path(name)] = guide(doc, runs_lines(name, versions) + NOTES.get(name, []))
        steps_ = steps(doc)
        manual = sum(1 for t in doc["tasks"].values() if t.get("type") == "manual")
        index.append("| [%s](%s) | %d | %d | %d |" % (name, page_path(name).name, len(steps_), len(doc["tasks"]) - 2,
                                                       manual))
    out[DOCS / "README.md"] = "\n".join([
        "# Workflow guides",
        "",
        "What each workflow does from start to end, for someone who is about to run one or is looking at a long job "
        "in the task bar and wants to know what it is doing. Every page is generated from the workflow's own document "
        "by `itential/workflows/describe.py`: the steps are the ones `build.py` names, the lines are the tasks' "
        "canvas labels, the arrows are the transitions. A test keeps the pages equal to the documents.",
        "",
        "| Workflow | Steps | Tasks | Waits for a person |",
        "|---|---|---|---|",
        *index,
        "",
        "A workflow is listed here as soon as versions.yaml names it: its builder in `build.py` names the steps "
        "(`step(...)`), and the renderer needs nothing else.",
        "",
    ])
    return out


def main(argv: list) -> int:
    if not argv and not sys.stdin.isatty():
        return describe_stdin()  # the runner: no arguments, the document on stdin
    if argv == ["--write"]:
        import yaml  # the repo's own dependency; the renderer itself needs none

        versions = yaml.safe_load((HERE.parent / "versions.yaml").read_text())
        DOCS.mkdir(exist_ok=True)
        for path, text in pages(versions).items():
            path.write_text(text)
            print(path.relative_to(ROOT))
        return 0
    if len(argv) == 1 and argv[0].endswith(".json"):
        doc = json.loads(Path(argv[0]).read_text())
        sys.stdout.write(guide(doc, ["(how it runs: not known from the document alone)"] + NOTES.get(doc["name"], [])))
        return 0
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
