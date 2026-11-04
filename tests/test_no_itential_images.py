"""Nothing that could leak Itential's images reaches this public repository's main (the owner's red line, 2026-11-03).

The rules live in verify/imageguard.py; its pre-push mode is what keeps a leak off GitHub, because a pushed branch is
public before CI runs. These tests are CI's half: they run the same rules over every tracked file in the required
pytest check, so nothing of the kind reaches main from a clone without the hook, and they pin CI itself to GitHub's
own runners with no secrets: a self-hosted runner would put the machines that hold the images in Docker one pull
request away, and a secret could be a registry sign-in."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
GUARD_PATH = ROOT / "verify" / "imageguard.py"
_spec = importlib.util.spec_from_file_location("imageguard", GUARD_PATH)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)
# GitHub-hosted runner labels (an exact list: a self-hosted runner can carry any label, "ubuntu-lab" included)
GITHUB_RUNNERS = {"ubuntu-latest", "ubuntu-24.04", "ubuntu-22.04", "ubuntu-24.04-arm", "ubuntu-22.04-arm"}


def tracked() -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True).stdout
    return [p for p in out.decode().split("\0") if p]


def test_no_tracked_file_could_leak_an_itential_image():
    files = tracked()
    assert len(files) > 100, "git ls-files listed almost nothing: is this a git checkout?"
    found = {p: why for p in files if (ROOT / p).is_file() and (why := guard.leaks(p, (ROOT / p).read_bytes()))}
    assert not found, f"must not be in this public repository: {found}"


# A positive control for each rule: the check must fire on what it is there to stop. Fake values, built at run time
# so that no secret scanner flags this file.
TAR_HEADER = b"layer.tar".ljust(257, b"\x00") + b"ustar\x0000" + b"\x00" * 100
LEAKS = [
    ("images/itential/gateway.tar", TAR_HEADER, "binary"),
    ("layer.tar.gz", b"\x1f\x8b\x08\x00" + b"\x00" * 16, "binary"),
    ("usr/local/bin/iagctl", b"\x7fELF\x02\x01\x01" + b"\x00" * 32, "binary"),
    ("layer.zst", b"\x28\xb5\x2f\xfd" + b"\x00" * 16, "binary"),
    ("notes.md", b"a" * (guard.MAX_BYTES + 1), "over 2 MB"),
    ("images/gateway.tar", b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"a" * 64 + b"\n", "Git LFS"),
    (".gitattributes", b"*.tar filter=lfs diff=lfs merge=lfs -text\n", "Git LFS"),
    ("oci/oci-layout", b'{"imageLayoutVersion": "1.0.0"}', "image layout"),
    ("oci/blobs/sha256/" + "a" * 64, b'{"architecture": "amd64"}', "image layout"),
    ("manifest.json", b'[{"Config": "c.json", "RepoTags": ["gw:5"], "Layers": ["l/layer.tar"]}]', "image layout"),
    ("env.sh", b"AWS_ACCESS_KEY_ID=" + b"ASIA" + b"Q" * 16, "AWS access key"),
    ("login.txt", b"eyJwYXlsb2Fk" + b"IjoiX" + b"A" * 60, "ECR login token"),
    ("config.json", b'{"auths": {"registry.example": {"auth": "' + b"QQ" * 12 + b'"}}}', "registry sign-in"),
    ("push.sh", b"docker push ghcr.io/someone/gateway5-runner:5.5.3\n", "pushes an image"),
    ("push.sh", b"docker --config /tmp/d image push ghcr.io/someone/runner\n", "pushes an image"),
    ("push.sh", b"nerdctl push ghcr.io/someone/runner\n", "pushes an image"),
    ("push.sh", b"ctr -n k8s.io images push ghcr.io/someone/runner\n", "pushes an image"),
    (
        "build.sh",
        b"docker buildx build \\\n  --platform linux/amd64 \\\n  --push -t ghcr.io/someone/runner .\n",
        "pushes",
    ),
    ("build.sh", b"docker build --push -t ghcr.io/someone/runner .\n", "pushes an image"),
    ("play.yml", b"- community.docker.docker_image:\n    name: gateway5-runner\n    push: True\n", "pushes an image"),
    ("play.yml", b"- community.docker.docker_image_push:\n    name: ghcr.io/someone/runner\n", "pushes an image"),
    ("ci.yml", b"- uses: docker/build-push-action@v6\n  with:\n    push: 'true'\n", "pushes an image"),
    ("server/app.js", b"/* Copyright (c) 2026 Itential, LLC. All rights reserved. */\n", "copyright"),
    ("ui/bundle.js", b"var a=1;" * 1000, "minified"),
]


@pytest.mark.parametrize(("path", "data", "reason"), LEAKS)
def test_the_check_catches_each_kind_of_leak(path, data, reason):
    why = guard.leaks(path, data)
    assert any(reason in w for w in why), f"{path}: {why}"


def test_the_guards_own_files_are_named_right_and_still_checked_as_files():
    assert {
        GUARD_PATH.relative_to(ROOT).as_posix(),
        Path(__file__).resolve().relative_to(ROOT).as_posix(),
    } == guard.SELF
    assert guard.leaks("verify/imageguard.py", b"docker push x\n") == []
    assert "binary" in guard.leaks("verify/imageguard.py", b"\x7fELF\x02\x01\x01\x00")[0]


def test_the_check_passes_ordinary_files():
    ordinary = [
        ("docs/runbooks/05-itential-dev-stack.md", "Platform runbook: `docker pull` by digest ✓\n".encode()),
        ("itential/docker-compose.yml", b'LDAP_ORGANISATION: "Itential, LLC."\n'),
        ("notes.md", b"git push --push-option=ci.skip origin main; the images are never pushed\n"),
        ("ci.yml", b"on:\n  push:\n    branches: [main]\n"),
    ]
    assert {p: guard.leaks(p, d) for p, d in ordinary} == {p: [] for p, _ in ordinary}


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    return repo


def _commit(repo: Path, msg: str) -> str:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--no-verify", "-m", msg], check=True)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def _push_line(sha: str) -> list[str]:
    return [f"refs/heads/main {sha} refs/heads/main {guard.ZERO}"]


def test_pre_push_checks_every_pushed_commit_not_only_the_tip(tmp_path):
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("lab\n")
    (repo / "gateway.layer").write_bytes(TAR_HEADER)
    _commit(repo, "add a layer")
    (repo / "gateway.layer").unlink()
    tip = _commit(repo, "remove it again")  # the tip is clean; the pushed history is not
    found = guard.pushed(str(repo), _push_line(tip))
    assert [w for w in found if w.startswith("gateway.layer ")] and all("binary" in why[0] for why in found.values())


def test_pre_push_passes_a_clean_push_and_a_branch_delete(tmp_path):
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("lab\n")
    tip = _commit(repo, "clean")
    assert guard.pushed(str(repo), _push_line(tip)) == {}
    assert guard.pushed(str(repo), [f"(delete) {guard.ZERO} refs/heads/old {tip}"]) == {}


def test_pre_push_refuses_with_exit_1_and_names_the_file(tmp_path):
    repo = _repo(tmp_path)
    (repo / "iagctl").write_bytes(b"\x7fELF\x02\x01\x01" + b"\x00" * 32)
    tip = _commit(repo, "copy a binary")
    r = subprocess.run(
        ["python3", "-I", str(GUARD_PATH), "pre-push"],
        cwd=repo,
        input=_push_line(tip)[0] + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 1 and "iagctl" in r.stderr and "push refused" in r.stderr, r.stderr


@pytest.mark.parametrize(
    "path",
    [
        ".aws/credentials",
        ".docker/config.json",
        ".dockerconfigjson",
        "images/itential/gateway.tar",
        "layer.tar.gz",
        "layer.tar.bz2",
        "layer.zst",
        "x.oci",
        "images/gw/oci-layout",
        "images/gw/blobs/sha256/" + "a" * 64,
    ],
)
def test_git_ignores_registry_sign_ins_and_image_files(path):
    ignored = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", "--no-index", path], check=False)
    assert ignored.returncode == 0, f"{path} is not in .gitignore"


def ci_risks(text: str) -> list[str]:
    """Why a workflow file would open a path from this public repository to the images: a runner that is not one of
    GitHub's (the machines that hold the images in Docker) or a secret (a registry sign-in) a pull request could reach.
    """
    why = []
    jobs = (yaml.safe_load(text) or {}).get("jobs", {})
    for name, job in jobs.items():
        runs_on = job.get("runs-on", "")
        labels = runs_on if isinstance(runs_on, list) else [runs_on]
        if not all(isinstance(label, str) and label in GITHUB_RUNNERS for label in labels):
            why.append(f"job {name} runs on {runs_on!r}, not one of GitHub's runners")
    if "secrets." in text:
        why.append("uses a secret")
    if "pull_request_target" in text:
        why.append("runs pull requests with the repository's own rights (pull_request_target)")
    return why


def test_ci_runs_only_on_githubs_runners_without_secrets():
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
    assert workflows, "no CI workflow found"
    found = {w.name: why for w in workflows if (why := ci_risks(w.read_text()))}
    assert not found, found


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("jobs:\n  build:\n    runs-on: self-hosted\n", "runs on"),
        ("jobs:\n  build:\n    runs-on: [self-hosted, mac-mini]\n", "runs on"),
        ("jobs:\n  build:\n    runs-on: ubuntu-lab\n", "runs on"),
        ("jobs:\n  build:\n    runs-on: {group: lab}\n", "runs on"),
        ("jobs:\n  pull:\n    runs-on: ubuntu-latest\n    env: {T: '${{ secrets.ECR }}'}\n", "secret"),
        ("on: pull_request_target\njobs:\n  t:\n    runs-on: ubuntu-latest\n", "pull_request_target"),
    ],
)
def test_the_ci_check_catches_each_risk(text, reason):
    assert any(reason in w for w in ci_risks(text)), ci_risks(text)
