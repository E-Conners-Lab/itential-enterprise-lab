"""PID S13 (amendment 1.36, ADR 0068): Itential runs the AWS site-to-site VPN. These tests hold the PID, the ADR and
the README to each other and to the two owner rules of 2026-09-26: Terraform (not the native OpenTofu service), and
every AWS secret only in Vault. The service, workflows and verify scripts join them as each piece is built."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PID = ROOT / "docs" / "PID.md"
ADR = ROOT / "docs" / "adr" / "0068-itential-runs-the-aws-vpn-through-terraform-on-the-gateway.md"

# The company ECR pull credentials predate S13 and are not part of it (ADR 0035); nothing else may put AWS keys in .env.
ECR_KEYS = {"ECR_AWS_PROFILE", "ECR_AWS_ACCESS_KEY_ID", "ECR_AWS_SECRET_ACCESS_KEY", "ECR_AWS_SESSION_TOKEN"}


def test_pid_carries_amendment_1_36_and_s13() -> None:
    pid = PID.read_text()
    assert "| **Version** | 1.36 |" in pid
    assert "| 1.36 | 2026-09-26 | AWS site-to-site VPN run by Itential (ADR 0068" in pid
    assert "### S13 — AWS site-to-site VPN run by Itential (amendment 1.36, ADR 0068)" in pid
    assert "| `aws-vpn` *(1.36)* | `phase-aws-vpn/*`" in pid
    assert "| aws-vpn | `phase-aws-vpn/*` |" in (ROOT / "README.md").read_text()


def test_s13_has_five_acceptance_criteria() -> None:
    s13 = PID.read_text().split("### S13 —", 1)[1].split("\n## ", 1)[0]
    assert re.findall(r"^  (\d)\. ", s13, re.M) == ["1", "2", "3", "4", "5"]


def test_adr_keeps_the_owner_rules() -> None:
    adr = ADR.read_text()
    assert "**Status:** proposed" in adr
    assert '"can we not just use terraform?"' in adr and "Terraform 1.5.7" in adr
    assert '"i want the aws secrets\n   to be a part of my vault. no plaintext passwords anywhere"' in adr
    # the PSK leaves the Terraform state and the approved plan file is the one applied
    assert "Vault is the source of the PSK; Terraform never sees it" in adr
    assert "`terraform apply <plan file>`" in adr


def test_no_aws_secret_is_added_to_env_example() -> None:
    names = {m.group(1) for m in re.finditer(r"^([A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.M)}
    aws = {n for n in names if "AWS" in n or n.startswith(("TF_VAR_", "VPN_PSK"))}
    assert aws <= ECR_KEYS, f"AWS secrets belong in Vault only (ADR 0068 decision 3): {sorted(aws - ECR_KEYS)}"
