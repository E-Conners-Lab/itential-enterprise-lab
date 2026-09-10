"""The runbook series (ADR 0056, PID amendment 1.21).

Nine chapters, one per track, each with the same five sections. These tests hold the chapters to the repo
they describe - every `make` target and play a chapter names must exist, every verify it cites must exist -
and to the parameterisation rule: no value specific to one environment may be transcribed into a chapter,
so the series stays safe to publish by construction rather than by anyone remembering.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
BOOKS = ROOT / "docs" / "runbooks"
CHAPTERS = {
    "00": "prerequisites",
    "01": "oob-network-and-addressing",
    "02": "k3s-platform-services",
    "03": "netbox-source-of-truth",
    "04": "eve-ng-topology",
    "05": "itential-dev-stack",
    "06": "platform-applications",
    "07": "observability",
    "08": "production-ha2-and-migration",
}
SECTIONS = ("Before you start", "The commands, in order", 'What "done" looks like', "Verification", "Troubleshooting")
# Values that belong to one environment and must never be transcribed (ADR 0056 decision 4). The lab's own
# 10.100.0.0/24 addresses are deliberately not here: they are private, invented in this repo, and the
# chapters are far harder to follow without them.
FORBIDDEN = (
    (r"192\.168\.\d+\.\d+", "a home-LAN address"),
    (r"\b\d{12}\b", "an AWS account id"),
    (r"\bdev\d{5,6}\b", "a ServiceNow PDI instance"),
    (r"(?i)\b(nbt|ghp|sk-ant)[-_a-z0-9]{10,}", "a token"),
)


def chapter(num: str) -> Path:
    return BOOKS / f"{num}-{CHAPTERS[num]}.md"


def text(num: str) -> str:
    return chapter(num).read_text()


@pytest.mark.parametrize("num", sorted(CHAPTERS))
def test_every_chapter_exists(num: str) -> None:
    assert chapter(num).exists(), f"docs/runbooks/{num}-{CHAPTERS[num]}.md missing"


@pytest.mark.parametrize("num", sorted(CHAPTERS))
def test_every_chapter_has_the_five_sections(num: str) -> None:
    """One shape, learned once (ADR 0056 decision 2)."""
    t = text(num)
    for section in SECTIONS:
        assert re.search(rf"^#+ .*{re.escape(section)}", t, re.M | re.I), f"{num}: no '{section}' section"


@pytest.mark.parametrize("num", sorted(CHAPTERS))
def test_no_chapter_transcribes_an_environment_specific_value(num: str) -> None:
    """ADR 0056 decision 4: publishing is safe by construction, not by memory."""
    t = text(num)
    for pattern, what in FORBIDDEN:
        hits = [m.group(0) for m in re.finditer(pattern, t)]
        # chapter 00 names the placeholders themselves, so an example inside a fill-in row is allowed
        hits = [h for h in hits if f"${{{h}}}" not in t]
        assert not hits, f"{num} transcribes {what}: {hits[:3]} - use a ${{PLACEHOLDER}} from chapter 00"


def test_chapter_00_carries_the_fill_in_table() -> None:
    t = text("00")
    assert re.search(r"^\|.*\bValue\b.*\|", t, re.M), "chapter 00 needs the fill-in table"
    for key in ("HOME_LAN", "ECR_ACCOUNT", "SNOW_INSTANCE"):
        assert key in t, f"chapter 00's table must name {key}"


@pytest.mark.parametrize("num", sorted(CHAPTERS))
def test_every_make_target_a_chapter_names_exists(num: str) -> None:
    mk = (ROOT / "Makefile").read_text()
    targets = {m.group(1) for m in re.finditer(r"^([a-z0-9][a-z0-9-]*):", mk, re.M)}
    for named in re.findall(r"`make ([a-z0-9-]+)`", text(num)):
        assert named in targets, f"{num} names `make {named}`, which the Makefile does not define"


@pytest.mark.parametrize("num", sorted(CHAPTERS))
def test_every_play_and_verify_a_chapter_names_exists(num: str) -> None:
    for named in re.findall(r"`(ansible/playbooks/[\w./-]+\.yml)`", text(num)):
        assert (ROOT / named).exists(), f"{num} names {named}, which does not exist"
    for named in re.findall(r"`(verify/[\w./-]+\.sh)`", text(num)):
        assert (ROOT / named).exists(), f"{num} names {named}, which does not exist"


@pytest.mark.parametrize("num", sorted(CHAPTERS))
def test_every_chapter_pins_what_was_tested(num: str) -> None:
    """Adaptable, but never vague about what was actually run (ADR 0056 decision 5)."""
    t = text(num)
    assert re.search(r"(?i)^#+ .*tested", t, re.M) or re.search(r"(?i)\|\s*version\s*\|", t), \
        f"{num} needs a tested-versions table"


def test_the_index_lists_every_chapter() -> None:
    index = BOOKS / "README.md"
    assert index.exists(), "docs/runbooks/README.md missing"
    t = index.read_text()
    for num, slug in sorted(CHAPTERS.items()):
        assert f"{num}-{slug}.md" in t, f"the index does not link {num}-{slug}.md"


def test_the_troubleshooting_entries_carry_the_symptom() -> None:
    """A trap is only useful if a reader can find it by what they are seeing (ADR 0056 decision 3)."""
    t = text("08")
    for symptom in (
        "There is already an active connection for gateway cluster",
        "netsdk-musl-linux-amd64.pex",
        "sentinel-user",
        "curl",
        "describe_inventory",
    ):
        assert symptom in t, f"chapter 08's troubleshooting is missing the {symptom!r} trap"
