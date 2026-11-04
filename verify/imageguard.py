#!/usr/bin/env python3
"""Nothing that could leak Itential's images goes into this public repository (the owner's red line, 2026-11-03).

The Platform and Gateway images are Itential's property, pulled from Itential's private ECR under the owner's sign-in.
A blob is refused when it is binary (an image layer, an archive, or the iagctl binary the Gateway runner image copies),
over 2 MB, a Git LFS pointer (LFS would upload the real file beside the repository), part of a docker-save or OCI image
layout, a registry sign-in, a step that pushes an image anywhere, or code copied out of an image.

Two places run these rules. A pushed branch is public before CI runs, so the pre-push hook is what keeps a leak off
GitHub: `imageguard.py pre-push` reads git's pre-push lines on stdin and checks every blob of every commit being
pushed, history included, not only the tip. tests/test_no_itential_images.py runs the same rules over the tracked
files in CI's required pytest check, so nothing of the kind reaches main from a clone without the hook.
Prints paths and reasons only, never a file's content. Standard library only, Python 3.9+ (the hook's python3)."""

from __future__ import annotations

import re
import subprocess
import sys

# the same limit as the pre-commit hook check-added-large-files --maxkb=2048
MAX_BYTES = 2 * 1024 * 1024
ZERO = "0" * 40
PATTERNS = {
    "an AWS access key id": re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "an ECR login token": re.compile(rb"eyJwYXlsb2FkIjoi[A-Za-z0-9+/=]{40,}"),
    "a Docker registry sign-in": re.compile(rb'"auths"\s*:\s*\{[^}]{0,400}"auth"\s*:\s*"[A-Za-z0-9+/=]{12,}'),
    "a step that pushes an image": re.compile(
        rb"\b(?:docker|podman|nerdctl)\b[^\n|;&]*?\bpush\b"  # docker push, docker image push, docker --config d push
        rb"|\bctr\b[^\n|;&]*?\bimages? push\b|\bskopeo copy\b|\bcrane (?:push|copy)\b"
        rb"|--push\b(?!-)"  # buildx/build --push, also on a continued line (git push --push-option is not one)
        rb"|\bdocker_image_push\b|(?i:\bpush:\s*[\"']?(?:true|yes)\b)"  # Ansible docker_image(_push), Actions push: true
    ),
    "an Itential copyright header (code copied out of an image)": re.compile(rb"(?i)copyright[^\n]{0,40}itential"),
}
LFS = (b"version https://git-lfs.github.com/spec/", b"filter=lfs")
# the guard's own files hold the patterns and their positive controls: they must still be small UTF-8 text, but
# the pattern rules would match themselves there
SELF = {"verify/imageguard.py", "tests/test_no_itential_images.py"}


def leaks(path: str, data: bytes) -> list[str]:
    """Why this blob must not be in the public repository; empty when it may."""
    why = []
    if len(data) > MAX_BYTES:
        why.append(f"over 2 MB ({len(data)} bytes)")
    try:
        data.decode("utf-8")
        is_text = b"\x00" not in data
    except UnicodeDecodeError:
        is_text = False
    if not is_text:
        why.append("binary, not UTF-8 text")
    if data.startswith(LFS[0]) or (path.rsplit("/", 1)[-1] == ".gitattributes" and LFS[1] in data):
        why.append("Git LFS (the real file would be uploaded beside the repository)")
    name = path.rsplit("/", 1)[-1]
    if name == "oci-layout" or "blobs/sha256/" in path or (name == "manifest.json" and b'"Layers"' in data):
        why.append("part of a docker-save or OCI image layout")
    if path not in SELF:
        why += [what for what, rx in PATTERNS.items() if rx.search(data)]
    if path.endswith((".js", ".mjs", ".cjs", ".css")) and any(len(line) > 5000 for line in data.splitlines()):
        why.append("a minified bundle (code copied out of an image)")
    return why


def git(repo: str, *args: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(["git", "-C", repo, *args], input=stdin, capture_output=True, check=True).stdout


def pushed(repo: str, lines: list[str]) -> dict[str, list[str]]:
    """Every blob in the commits a push would add (not yet on any remote-tracking ref) -> why it must not go."""
    tips = [parts[1] for parts in (ln.split() for ln in lines) if len(parts) == 4 and parts[1] != ZERO]
    if not tips:
        return {}
    paths: dict[str, str] = {}
    for line in git(repo, "rev-list", "--objects", *tips, "--not", "--remotes").decode().splitlines():
        oid, _, path = line.partition(" ")
        paths.setdefault(oid, path)
    found: dict[str, list[str]] = {}
    sizes = git(repo, "cat-file", "--batch-check", stdin="\n".join(paths).encode() + b"\n").decode().splitlines()
    for oid, kind, size in (ln.split() for ln in sizes):
        if kind != "blob":
            continue
        where = f"{paths[oid] or oid} (blob {oid[:12]})"
        if int(size) > MAX_BYTES:  # never read a layer-sized blob into memory
            found[where] = [f"over 2 MB ({size} bytes)"]
        elif why := leaks(paths[oid], git(repo, "cat-file", "blob", oid)):
            found[where] = why
    return found


def main(argv: list[str]) -> int:
    if argv[1:] != ["pre-push"]:
        print("usage: imageguard.py pre-push  (git's pre-push lines on stdin)", file=sys.stderr)
        return 2
    repo = git(".", "rev-parse", "--show-toplevel").decode().strip()
    found = pushed(repo, sys.stdin.read().splitlines())
    for where, why in sorted(found.items()):
        print(f"imageguard: {where}: {', '.join(why)}", file=sys.stderr)
    if found:
        print(
            "imageguard: push refused - this public repository must hold nothing that could leak Itential's images",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
