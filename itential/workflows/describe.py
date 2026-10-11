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


# --- the step chart: the same graph as the Mermaid block, drawn as SVG for a card that cannot run scripts -----------
# Colours for the two places the chart goes: the dark branded card (ADR 0077) and, when asked, a light page.
CHART_DARK = {"box": "#1b2733", "edge": "#2f3d4b", "text": "#e6e4de", "muted": "#9aa3ac", "main": "#5fb6d2",
              "branch": "#c9a35a", "error": "#e0906f", "person": "#f2b134"}
CHART_LIGHT = {"box": "#eef2f6", "edge": "#c7d0da", "text": "#1d2126", "muted": "#5d6670", "main": "#0f6e8c",
               "branch": "#8a6a1e", "error": "#a1543a", "person": "#8a5a1e"}
BOX_W, BOX_H, PITCH, LANE = 320, 46, 84, 16


def step_links(doc: dict, where: dict) -> list:
    """[(src, dst, labels)] between steps, as the Mermaid block draws them: 0 is start, N + 1 is done; labels are the
    deciding evaluations ("roll back? yes"), "failed" or "could not run"; the plain way on carries none."""
    n = max(where.values()) if where else 0
    links: dict = OrderedDict()
    for a, b, st in edges(doc):
        src = 0 if a == START else where[a]
        dst = n + 1 if b == END else where[b]
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
    return [(src, dst, labs) for (src, dst), labs in links.items()]


def _lanes(spans: list) -> list:
    """A lane per edge so that edges whose vertical spans overlap never share one (interval colouring)."""
    lanes: list = []  # each lane: the end row of its last span
    out = []
    for lo, hi in spans:
        for k, end in enumerate(lanes):
            if end < lo:
                lanes[k] = hi
                out.append(k)
                break
        else:
            lanes.append(hi)
            out.append(len(lanes) - 1)
    return out


def step_chart(doc: dict, steps_: list, where: dict, palette: dict | None = None, states: dict | None = None,
               taken: list | None = None) -> str:
    """The steps as boxes, top to bottom in the walk-through's order, start above and done below; the ordinary way on
    is a straight arrow, a branch is a bracket on the right (forward) or the left (back, or straight to done) with its
    label; a step that waits for a person says so. Pure and deterministic: the same document draws the same picture.
    With `states` ({step: ran|here|waiting|failed|canceled|skipped}) and `taken` ([(src, dst)] of step links, 0 start
    and N + 1 done) a job's run is laid over the picture: the steps and arrows it took stand out, the rest fade."""
    import html as _html

    c = palette or CHART_DARK
    states = states or {}
    taken_set = {(a, b) for a, b in (taken or [])}
    n = len(steps_)
    manual = {where[tid] for tid, t in doc["tasks"].items() if t.get("type") == "manual"}
    links = step_links(doc, where)
    right = [(a, b, labs) for a, b, labs in links if b > a and b != a + 1 and b != n + 1]
    left = [(a, b, labs) for a, b, labs in links if b < a or (b == n + 1 and a != n)]
    straight = [(a, b, labs) for a, b, labs in links if b == a + 1 or (a == n and b == n + 1 and (a, b, labs) not in left)]
    r_lanes = _lanes([(min(a, b), max(a, b)) for a, b, _ in sorted(right)])
    right = sorted(right)
    l_sorted = sorted(left, key=lambda e: (min(e[0], e[1]), max(e[0], e[1])))
    l_lanes = _lanes([(min(a, b), max(a, b)) for a, b, _ in l_sorted])
    label_room = 230
    x0 = 24 + (max(l_lanes) + 1 if l_lanes else 0) * LANE + label_room
    box_r = x0 + BOX_W
    width = box_r + 24 + (max(r_lanes) + 1 if r_lanes else 0) * LANE + label_room
    height = (n + 2) * PITCH + 24

    def y(i: int) -> float:  # the centre of row i (0 start ... n + 1 done)
        return 32 + i * PITCH + BOX_H / 2

    def text(s_: str, limit: int = 44) -> str:
        return _html.escape(s_ if len(s_) <= limit else s_[: limit - 3] + "...")

    def label_text(labs: list) -> str:
        return text(" / ".join(labs[:2]) + (" ..." if len(labs) > 2 else ""), 40)

    def spread(items: list) -> list:
        """Labels in one column, each at its edge's row, pushed down just enough never to overlap the one above."""
        out_, last = [], -1e9
        for y_, rest in sorted(items, key=lambda it: it[0]):
            y_ = max(y_, last + 13)
            out_.append((y_, rest))
            last = y_
        return out_

    cx = x0 + BOX_W / 2
    # an explicit size: as an <img> the browser needs the intrinsic size to scale the picture to the card's width
    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" role="img" '
           'aria-label="the steps of %s" font-family="IBM Plex Sans, Helvetica Neue, Arial, sans-serif" font-size="13">'
           % (width, height, width, height, _html.escape(doc["name"])),
           '<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
           '<path d="M0 0L10 5L0 10z" fill="context-stroke"/></marker></defs>']
    # start and done
    for i, word in ((0, "start"), (n + 1, "done")):
        out.append('<rect x="%.1f" y="%.1f" width="88" height="30" rx="15" fill="%s" stroke="%s"/>'
                   '<text x="%.1f" y="%.1f" text-anchor="middle" fill="%s">%s</text>'
                   % (cx - 44, y(i) - 15, c["box"], c["edge"], cx, y(i) + 5, c["muted"], word))
    # the steps; with a run laid over, each box says what happened to it
    state_colour = {"ran": c["main"], "here": c["person"], "waiting": c["person"], "failed": c["error"],
                    "canceled": c["error"], "skipped": c["edge"]}
    state_word = {"ran": "ran", "here": "here now", "waiting": "waiting for a person now", "failed": "ended in error",
                  "canceled": "canceled", "skipped": "not reached"}
    for i, (title, _) in enumerate(steps_, 1):
        st = states.get(i, "")
        faded = ' opacity="0.45"' if st == "skipped" else ""
        out.append('<rect x="%d" y="%.1f" width="%d" height="%d" rx="8" fill="%s" stroke="%s"%s%s/>'
                   % (x0, y(i) - BOX_H / 2, BOX_W, BOX_H, c["box"], state_colour.get(st, c["edge"]),
                      ' stroke-width="2.4"' if st in ("here", "waiting") else "", faded))
        sub = i in manual or st in ("here", "waiting", "failed", "canceled")
        out.append('<text x="%d" y="%.1f" fill="%s"%s><tspan fill="%s">step %d:</tspan> %s</text>'
                   % (x0 + 12, y(i) + 5 - (7 if sub else 0), c["text"], faded, c["muted"], i, text(short(title), 36)))
        if st in ("here", "waiting", "failed", "canceled"):
            out.append('<text x="%d" y="%.1f" fill="%s" font-size="11">%s</text>'
                       % (x0 + 12, y(i) + 17, state_colour[st], state_word[st]))
        elif i in manual:
            out.append('<text x="%d" y="%.1f" fill="%s" font-size="11">waits for a person</text>'
                       % (x0 + 12, y(i) + 17, c["person"]))
        if st in ("ran", "skipped"):
            out.append('<text x="%d" y="%.1f" fill="%s" font-size="11" text-anchor="end"%s>%s</text>'
                       % (x0 + BOX_W - 10, y(i) + 5, state_colour[st], faded, state_word[st]))

    def stroke(labs: list, a: int, b: int, width: float) -> str:
        if any(l_ == "could not run" for l_ in labs):
            base = 'stroke="%s" stroke-dasharray="5 4"' % c["error"]
        else:
            base = 'stroke="%s"' % (c["branch"] if labs else c["main"])
        if not states:
            return base + ' stroke-width="%s"' % width
        return base + (' stroke-width="2.6"' if (a, b) in taken_set else ' stroke-width="%s" opacity="0.35"' % width)

    # straight arrows between neighbours
    for a, b, labs in straight:
        top = y(a) + (15 if a == 0 else BOX_H / 2)
        bottom = y(b) - (15 if b == n + 1 else BOX_H / 2)
        out.append('<path d="M%.1f %.1fV%.1f" fill="none" %s marker-end="url(#arr)"/>'
                   % (cx, top, bottom, stroke(labs, a, b, 1.6)))
        if labs:
            out.append('<text x="%.1f" y="%.1f" fill="%s" font-size="11">%s</text>'
                       % (cx + 8, (top + bottom) / 2 + 4, c["branch"], label_text(labs)))
    # right brackets: forward skips; their labels in one column past every lane, so no lane crosses a word
    r_col = box_r + 20 + (max(r_lanes) + 1 if r_lanes else 0) * LANE + 6
    r_labels = []
    for (a, b, labs), k in zip(right, r_lanes):
        lane = box_r + 20 + k * LANE
        ys, yt = y(a), y(b)
        x_src = cx + 44 if a == 0 else box_r
        out.append('<path d="M%.1f %.1fH%.1fV%.1fH%.1f" fill="none" %s marker-end="url(#arr)"/>'
                   % (x_src, ys, lane, yt, box_r + 2, stroke(labs, a, b, 1.4)))
        if labs:
            r_labels.append((ys + 4, (c["error"] if "could not run" in labs else c["branch"], label_text(labs))))
    for y_, (colour, words) in spread(r_labels):
        out.append('<text x="%.1f" y="%.1f" fill="%s" font-size="11">%s</text>' % (r_col, y_, colour, words))
    # left brackets: back edges and the early ways to done; labels in a column left of every lane
    l_col = x0 - 20 - (max(l_lanes) + 1 if l_lanes else 0) * LANE - 6
    l_labels = []
    for (a, b, labs), k in zip(l_sorted, l_lanes):
        lane = x0 - 20 - k * LANE
        ys, yt = y(a), y(b)
        x_dst = cx - 44 if b == n + 1 else x0
        out.append('<path d="M%.1f %.1fH%.1fV%.1fH%.1f" fill="none" %s marker-end="url(#arr)"/>'
                   % (x0, ys, lane, yt, x_dst - 2, stroke(labs, a, b, 1.4)))
        if labs:
            l_labels.append((ys + 4, (c["error"] if "could not run" in labs else c["branch"], label_text(labs))))
    for y_, (colour, words) in spread(l_labels):
        out.append('<text x="%.1f" y="%.1f" fill="%s" font-size="11" text-anchor="end">%s</text>' % (l_col, y_, colour, words))
    out.append("</svg>")
    return "\n".join(out)


# --- HTML for a card -------------------------------------------------------------------------------------------------
def _inline(text: str) -> str:
    """The guide's inline marks: `code`, **strong**, *emphasis*; everything else escaped."""
    import html as _html
    import re as _re

    out = _html.escape(text, quote=False)
    out = _re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = _re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = _re.sub(r"(?<!\w)\*([^*]+)\*(?!\w)", r"<em>\1</em>", out)
    return out


def chart_image(svg: str, alt: str) -> str:
    """The chart as an <img> with a data URI. Work Center's sanitizer strips an inline <svg> from a card (measured on
    6.6.0, 2026-10-11: the figure stayed, the drawing went) but keeps an image, which is how the cards' own mark goes
    in (build.py outage_card_image)."""
    import base64
    import html as _html

    return '<img class="chart" alt="%s" style="width:100%%;height:auto" src="data:image/svg+xml;base64,%s">' % (
        _html.escape(alt, quote=True), base64.b64encode(svg.encode()).decode())


def html_guide(markdown: str, svg: str = "", name: str = "") -> str:
    """The guide as HTML, from the Markdown this file writes (headings, bullets, tables, a Mermaid block). The Mermaid
    block becomes the step chart (`svg`, as an image: a card cannot load scripts and drops inline SVG) when one is
    given, else its source in a <pre>."""
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
            out.append('<figure class="chart">%s</figure>' % chart_image(svg, "the steps of " + name) if svg
                       else '<pre class="diagram">%s</pre>' % _inline("\n".join(lines[i + 1:j])))
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
    where = {tid: n for n, (_, ids) in enumerate(steps_, 1) for tid in ids}
    svg = step_chart(doc, steps_, where)
    n_tasks, manual = len(doc["tasks"]) - 2, sum(1 for t in doc["tasks"].values() if t.get("type") == "manual")
    summary = "%s: %d task%s in %d step%s, %s" % (doc["name"], n_tasks, "" if n_tasks == 1 else "s", len(steps_),
                                                  "" if len(steps_) == 1 else "s",
                                                  "no stop for a person" if not manual else
                                                  "%d stop%s for a person" % (manual, "" if manual == 1 else "s"))
    return {"ok": True, "name": doc["name"], "tasks": n_tasks, "steps": len(steps_), "manual": manual,
            "summary": summary, "step_titles": [t for t, _ in steps_], "markdown": md, "svg": svg,
            "html": html_guide(md, svg, doc["name"])}


# --- a job: the run laid over the steps (ADR 0080 decision 6) ------------------------------------------------------
# A job record is the workflow document as it was when the job started, plus what happened: each task's status
# (complete, running, incomplete, canceled, error), its finish_state (success, failure, error), when it started (ISO
# 8601) and ended (epoch milliseconds), and the job's variables. A job's copy of a task carries none of the task's
# incoming values (service names, queries), so the words come from the stored document when it still matches.
JOB_LIVE = ("running", "paused")


def _ms(value) -> float | None:
    """A time as the job records it, epoch milliseconds or ISO 8601, as epoch milliseconds; None when absent."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return None


def _clock(ms: float | None) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ms else "an unknown time"


def _span(ms: float | None) -> str:
    """A duration in words: 0.4 s, 12 s, 3 min 25 s, 2 h 5 min."""
    if ms is None or ms < 0:
        return "an unknown time"
    s = ms / 1000
    if s < 10:
        return "%.1f s" % s
    if s < 60:
        return "%d s" % round(s)
    m, sec = divmod(int(round(s)), 60)
    if m < 60:
        return "%d min %d s" % (m, sec) if sec else "%d min" % m
    h, m = divmod(m, 60)
    return "%d h %d min" % (h, m) if m else "%d h" % h


def job_document(job: dict, doc: dict | None) -> tuple[dict, str]:
    """The document to describe the job with, and a note on which. The stored one when its tasks and arrows are the
    job's (it has the words and the steps); otherwise the job's own copy, which may name no step."""
    job_edges = {(a, b, e.get("state")) for a, tos in (job.get("transitions") or {}).items() for b, e in tos.items()}
    if (isinstance(doc, dict) and isinstance(doc.get("tasks"), dict) and set(doc["tasks"]) == set(job.get("tasks") or {})
            and set(edges(doc)) == job_edges):
        return doc, "The steps and the words are the stored workflow's, which still matches this job's tasks and arrows."
    own = {k: job.get(k) for k in ("name", "description", "tasks", "transitions", "inputSchema", "outputSchema")}
    own = {k: v for k, v in own.items() if v is not None}
    for t in own.get("tasks", {}).values():  # a job's copy keeps no incoming values; the walker expects the keys
        t.setdefault("variables", {}).setdefault("incoming", {})
        t["variables"].setdefault("outgoing", {})
    return own, ("The stored workflow has changed since this job started (or could not be read), so this is described "
                 "from the job's own copy of the document, which may name no steps.")


def task_state(t: dict) -> str:
    """ran | here | waiting | failed | canceled | skipped, for one task of a job."""
    status = t.get("status") or "incomplete"
    if status in JOB_LIVE:
        return "waiting" if t.get("type") == "manual" else "here"
    if status == "complete":
        return "failed" if t.get("finish_state") == "error" else "ran"
    if status == "canceled":
        return "canceled" if t.get("start_time") else "skipped"  # the engine marks every unreached task canceled too
    if status == "error":
        return "failed"
    return "skipped"


def step_states(job: dict, steps_: list) -> dict:
    """{step: state}: a step is where the job is if any of its tasks is, failed or canceled if one is, ran if one ran."""
    out = {}
    for n, (_, ids) in enumerate(steps_, 1):
        kinds = {task_state(job["tasks"][tid]) for tid in ids if tid in job["tasks"]}
        for k in ("waiting", "here", "canceled", "failed", "ran"):
            if k in kinds:
                out[n] = k
                break
        else:
            out[n] = "skipped"
    return out


def job_path(doc: dict, job: dict, where: dict) -> list:
    """[(src step, dst step, labels)] in the order the run took them: a task that finished took the arrow of its finish
    state to a task that then ran; the same labels as the chart's. Start is 0, done is N + 1."""
    n = max(where.values()) if where else 0
    tasks = job["tasks"]
    ran_or_live = {k for k, t in tasks.items() if task_state(t) in ("ran", "here", "waiting", "failed", "canceled")}
    out: dict = OrderedDict()
    order = sorted((k for k in ran_or_live if k != END),
                   key=lambda k: (_ms(tasks[k].get("start_time")) or 0, k))
    for a in order:
        t = tasks[a]
        fs = t.get("finish_state") or ("success" if a == START else "")
        for b, e in (doc["transitions"].get(a) or {}).items():
            if b not in ran_or_live and b != END:
                continue
            if b == END and tasks.get(END, {}).get("status") != "complete":
                continue
            if e.get("state") not in (fs, "loop") and not (e.get("state") == "error" and task_state(t) == "failed"):
                continue
            src = 0 if a == START else where.get(a)
            dst = n + 1 if b == END else where.get(b)
            if src is None or dst is None or src == dst:
                continue
            dt = doc["tasks"][a]
            lab = "" if a == START else ("%s %s" % (dt["summary"], label(dt, e["state"])) if dt["name"] == "evaluation"
                                        else ("" if e["state"] == "success" else label(dt, e["state"])))
            out.setdefault((src, dst), [])
            if lab and lab not in out[(src, dst)]:
                out[(src, dst)].append(lab)
    return [(a, b, labs) for (a, b), labs in out.items()]


def _step_span(job: dict, ids: list) -> float | None:
    starts = [_ms(job["tasks"][i].get("start_time")) for i in ids if i in job["tasks"]]
    ends = [_ms(job["tasks"][i].get("end_time")) for i in ids if i in job["tasks"]]
    starts, ends = [x for x in starts if x], [x for x in ends if x]
    return max(ends) - min(starts) if starts and ends and max(ends) >= min(starts) else None


def _said(value) -> str:
    """A job variable's value in a few words, never a dump: a string is quoted and cut, the rest is counted."""
    if value is None:
        return "nothing (not set)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        v = " ".join(value.split())
        return '"%s"' % (v if len(v) <= 160 else v[:157] + "...") if v else "an empty string"
    if isinstance(value, list):
        return "a list of %d" % len(value)
    if isinstance(value, dict):
        keys = sorted(value)
        return "an object with %d key%s%s" % (len(keys), "" if len(keys) == 1 else "s",
                                             " (%s)" % ", ".join("`%s`" % k for k in keys[:6]) + ("..." if len(keys) > 6 else "")
                                             if keys else "")
    return str(value)


def job_guide(doc: dict, job: dict, steps_: list, where: dict, read: str, which: str) -> str:
    """The page for one run: what it did, where it is or stopped, what it said, the tasks that ran."""
    tasks = job["tasks"]
    jid = str(job.get("_id") or "")
    status = str(job.get("status") or "unknown")
    metrics = job.get("metrics") or {}
    start, end = _ms(metrics.get("start_time")), _ms(metrics.get("end_time"))
    states_ = step_states(job, steps_)
    path = job_path(doc, job, where)
    n = len(steps_)
    ran = [k for k, t in tasks.items() if k not in (START, END) and task_state(t) in ("ran", "failed", "canceled")]
    ran_steps = [i for i in range(1, n + 1) if states_[i] != "skipped"]
    live = sorted((k for k, t in tasks.items() if k not in (START, END) and task_state(t) in ("here", "waiting")),
                  key=lambda k: (_ms(tasks[k].get("start_time")) or 0, k))
    now_ms = _ms(job.get("_now")) or _ms(job.get("last_updated")) or start or 0
    if status in JOB_LIVE:
        ran_for = "Running for %s" % _span(now_ms - start) if start else "Running"
    else:
        ran_for = "Ran %s" % _span(end - start if start and end else None)
    head = ["# %s: job %s" % (doc["name"], jid[:8] or "?"), "",
            "**%s.** Started %s%s. %s." % (sentence(status), _clock(start),
                                           (" with the description \"%s\"" % " ".join(str(job["description"]).split()))
                                           if job.get("description") else "", ran_for),
            "", "*%s %s*" % (read, which), ""]
    # what this run did, step by step in the order taken
    first_start = {i: min((_ms(tasks[t].get("start_time")) or float("inf")) for t in ids if t in tasks)
                   for i, (_, ids) in enumerate(steps_, 1)}
    did = ["## What this run did", "", "The steps in the order this job took them; a line says which way it went on when "
           "there was a choice.", ""]
    out_labels = {}
    for a, b, labs in path:
        out_labels.setdefault(a, []).append((b, labs))
    for i in sorted(ran_steps, key=lambda i: (first_start[i], i)):
        title, ids = steps_[i - 1]
        st = states_[i]
        if st == "ran":
            what = "ran in %s" % _span(_step_span(job, ids))
        elif st == "waiting":
            t = next(tasks[k] for k in ids if task_state(tasks[k]) == "waiting")
            since = _ms(t.get("start_time"))
            what = "waiting for a person since %s (%s)" % (_clock(since), _span(now_ms - since)) if since \
                else "waiting for a person"
        elif st == "here":
            what = "running now"
        elif st == "failed":
            what = "ended in error"
        else:
            what = "canceled"
        line = "- step %d: %s — %s" % (i, short(title), what)
        ways = []
        for b, labs in out_labels.get(i, []):
            target = "done" if b == n + 1 else "step %d" % b
            ways.append(("%s → %s" % (" / ".join(labs), target)) if labs else "→ %s" % target)
        if ways:
            line += "; then " + "; ".join(ways)
        did.append(line + ".")
    skipped = [i for i in range(1, n + 1) if states_[i] == "skipped"]
    if skipped:
        did.append("- Not reached: %s." % ", ".join("step %d (%s)" % (i, short(steps_[i - 1][0])) for i in skipped))
    did.append("")
    # where it is, or stopped
    if status in JOB_LIVE:
        now_ = ["## Where it is now", ""]
        for k in live or []:
            t = tasks[k]
            since = _ms(t.get("start_time"))
            now_.append("- `%s` %s (step %d): %s since %s." % (
                k, doc["tasks"][k]["summary"], where[k],
                "waits for a person" if task_state(t) == "waiting" else "running", _clock(since)))
        if not live:
            now_.append("- Between tasks: nothing is running at this moment.")
    else:
        now_ = ["## Where it stopped", ""]
        done_ = [k for k in ran if _ms(tasks[k].get("end_time"))]
        last = max(done_, key=lambda k: (_ms(tasks[k].get("end_time")), k)) if done_ else None
        halted = [k for k, t in tasks.items() if k not in (START, END) and task_state(t) in ("canceled", "failed")]
        if status == "complete" and last:
            now_.append("- Done at %s, after `%s` %s (step %d)." % (_clock(end or _ms(tasks[last].get("end_time"))),
                                                                   last, doc["tasks"][last]["summary"], where[last]))
        elif halted:
            for k in sorted(halted, key=lambda k: (where[k], k))[:6]:
                now_.append("- `%s` %s (step %d): %s." % (k, doc["tasks"][k]["summary"], where[k],
                                                          "canceled" if task_state(tasks[k]) == "canceled" else "ended in error"))
        elif last:
            now_.append("- Ended %s at %s, after `%s` %s (step %d)." % (status, _clock(end), last,
                                                                       doc["tasks"][last]["summary"], where[last]))
        else:
            now_.append("- Ended %s before any task finished." % status)
        for err in (job.get("errors") or [])[:6]:
            now_.append("- The job's error: %s." % _said(err.get("message") if isinstance(err, dict) else err).strip('"'))
    now_.append("")
    # what the job said
    said = ["## What the job said", "", "The job variables the workflow publishes, as this job left them.", ""]
    names = list(((doc.get("outputSchema") or {}).get("properties") or {}).keys())
    if not names:
        names = sorted({v for t in doc["tasks"].values() if t.get("variables")
                        for v in [r.split(".", 2)[2] for r in t["variables"].get("outgoing", {}).values()
                                  if isinstance(r, str) and r.startswith("$var.job.")]})
    variables = job.get("variables") or {}
    names = [n for n in names if n not in ("_id", "initiator")]  # the engine's own, not the workflow's words
    unset = [name for name in names if variables.get(name) is None and name not in ("outcome", "error")]
    for name in names:
        if name not in unset:
            said.append("- `%s`: %s" % (name, _said(variables.get(name))))
    if unset:
        said.append("- Not set by this run: %s." % ", ".join("`%s`" % n for n in unset))
    if not names:
        said.append("- Nothing: the workflow publishes no job variable.")
    said.append("")
    warn = []
    if job.get("warnings"):
        warn = ["## Warnings the engine recorded", ""]
        for w in job["warnings"][:8]:
            if isinstance(w, dict):
                warn.append("- `%s` %s: %s" % (w.get("task"), w.get("name", ""), " ".join(str(w.get("message", "")).split())[:200]))
            else:
                warn.append("- %s" % " ".join(str(w).split())[:200])
        warn.append("")
    # the tasks that ran
    tasks_ = ["## The tasks that ran", "", "By step, in canvas order: each task that ran, with how it finished; the tasks "
              "the run never reached are left out.", ""]
    for i, (title, ids) in enumerate(steps_, 1):
        if states_[i] == "skipped":
            continue
        tasks_ += ["### %d. %s" % (i, title), ""]
        for k in ids:
            t = tasks.get(k, {})
            st = task_state(t)
            if st == "skipped":
                continue
            dt = doc["tasks"][k]
            took = _ms(t.get("end_time")) - _ms(t.get("start_time")) if _ms(t.get("end_time")) and _ms(t.get("start_time")) else None
            if st == "ran":
                fs = t.get("finish_state") or "success"
                how = "ran in %s" % _span(took) if took is not None else "ran"
                if fs != "success":
                    how += ", %s" % ("answered no" if dt["name"] == "evaluation" else "failed")
                elif dt["name"] == "evaluation":
                    how += ", answered yes"
            elif st == "waiting":
                how = "waits for a person since %s" % _clock(_ms(t.get("start_time")))
            elif st == "here":
                how = "running since %s" % _clock(_ms(t.get("start_time")))
            elif st == "failed":
                how = "ended in error"
            else:
                how = "canceled"
            tasks_.append("- `%s` %s — %s." % (k, dt["summary"], how))
        tasks_.append("")
    chart = ["## The steps at a glance", "",
             "Each box is a step; the steps and arrows this job took stand out, the ones it never reached fade.", "",
             *mermaid(doc, steps_, where), ""]
    return "\n".join(head + did + now_ + said + warn + chart + tasks_).rstrip() + "\n"


def describe_job_document(data: dict) -> dict:
    """The runner contract for a job: {"job": <a job record>, "document": <the stored workflow or null>, "read": str}
    in, the description out: ok, name, job, status, the counts, the path, summary, markdown, svg, html."""
    job = data.get("job") or {}
    if not isinstance(job, dict) or not job.get("name") or not isinstance(job.get("tasks"), dict):
        return {"ok": False, "message": "no job record: expected {job: {_id, name, status, tasks, transitions, ...}}"}
    if data.get("now") is not None:
        job = {**job, "_now": data["now"]}  # the service's clock: how long a running job has been running
    doc, which = job_document(job, data.get("document"))
    steps_ = steps(doc)
    where = {tid: k for k, (_, ids) in enumerate(steps_, 1) for tid in ids}
    states_ = step_states(job, steps_)
    path = job_path(doc, job, where)
    read = str(data.get("read") or "Read from the Platform's job record, on request.")
    md = job_guide(doc, job, steps_, where, read, which)
    svg = step_chart(doc, steps_, where, states=states_, taken=[(a, b) for a, b, _ in path])
    n_tasks = len(doc["tasks"]) - 2
    ran = sum(1 for k, t in job["tasks"].items() if k not in (START, END) and task_state(t) in ("ran", "failed", "canceled"))
    ran_steps = [i for i, st in states_.items() if st != "skipped"]
    status = str(job.get("status") or "unknown")
    metrics = job.get("metrics") or {}
    start, end = _ms(metrics.get("start_time")), _ms(metrics.get("end_time"))
    jid = str(job.get("_id") or "")
    at = [i for i, st in states_.items() if st in ("here", "waiting", "failed", "canceled")]
    if status in JOB_LIVE and at:
        where_now = "%s at step %d (%s)" % ("waiting for a person" if states_[at[0]] == "waiting" else "running",
                                            at[0], short(steps_[at[0] - 1][0]))
    elif status == "complete" and ran_steps:
        last = max(ran_steps, key=lambda i: max((_ms(job["tasks"][t].get("end_time")) or 0) for t in steps_[i - 1][1]))
        where_now = "ended after step %d (%s)" % (last, short(steps_[last - 1][0]))
    elif at:
        where_now = "%s at step %d (%s)" % (states_[at[0]].replace("failed", "ended in error"), at[0], short(steps_[at[0] - 1][0]))
    else:
        where_now = status
    now_ms = _ms(job.get("_now")) or _ms(job.get("last_updated")) or start or 0
    span = ("running for %s" % _span(now_ms - start)) if status in JOB_LIVE and start \
        else ("%s in %s" % (status, _span(end - start)) if start and end else status)
    summary = "%s, job %s: %s; %d of %d tasks ran through %d of %d steps; %s" % (
        doc["name"], jid[:8] or "?", span, ran, n_tasks, len(ran_steps), len(steps_), where_now)
    name = "%s, job %s" % (doc["name"], jid[:8] or "?")
    return {"ok": True, "name": name, "workflow": doc["name"], "job": jid, "status": status, "tasks": n_tasks,
            "steps": len(steps_), "ran_tasks": ran, "ran_steps": len(ran_steps), "manual": sum(1 for st in states_.values() if st == "waiting"),
            "path": [[a, b, labs] for a, b, labs in path], "step_states": {str(i): st for i, st in states_.items()},
            "summary": summary, "step_titles": [t for t, _ in steps_], "markdown": md, "svg": svg,
            "html": html_guide(md, svg, name), "brand": "the job, described",
            "decide": "Seen closes this card. Nothing runs from it: the workflow only read this job's record and the "
                      "stored copy of %s and described the run." % doc["name"],
            "foot": "Prepared by Itential from the job record and the stored workflow document, with the same words "
                    "the repository's guide uses."}


def describe_stdin() -> int:
    """stdin JSON in, one JSON line out; a bad document is an answer, not a traceback (the workflow reads `ok`)."""
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError as exc:
        data = {"document": None, "_error": str(exc)}
    try:
        data = data if isinstance(data, dict) else {}
        out = describe_job_document(data) if "job" in data else describe_document(data)
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
