"""The lab's CloudNativePG databases are rebuildable and are not backed up (ADR 0064, PID amendment 1.31).

WAL archiving and nightly base backups to Garage filled Garage, the unarchived WAL then filled zabbix-db's volume
and Zabbix was down ~15 h (2026-09-16). These tests keep the backups from coming back in the manifests, and keep
the plays deleting the live objects - because dropping a document from a manifest does not delete what it made.
Runs in CI with no lab access.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
MANIFESTS = {
    "platform-db": ROOT / "k8s" / "platform" / "manifests" / "cnpg-platform-db.yaml",
    "zabbix-db": ROOT / "k8s" / "observability" / "manifests" / "zabbix-db.yaml",
}
PLAYS = {
    "platform-db": ROOT / "ansible" / "playbooks" / "k8s-platform.yml",
    "zabbix-db": ROOT / "ansible" / "playbooks" / "observability.yml",
}
OBS_VERSIONS = yaml.safe_load((ROOT / "k8s" / "observability" / "versions.yaml").read_text())
NAMESPACE = {"platform-db": "cnpg-system", "zabbix-db": OBS_VERSIONS["obs_namespace"]}
# exactly the four live objects the backups left behind
CLEANUP = {
    ("ScheduledBackup", "cnpg-system", "platform-db-nightly"),
    ("ObjectStore", "cnpg-system", "garage"),
    ("ScheduledBackup", "observability", "zabbix-db-nightly"),
    ("ObjectStore", "observability", "garage"),
}
BACKUP_KINDS = {"ObjectStore", "ScheduledBackup", "Backup"}
K8S = ("kubernetes.core.k8s", "k8s")
JSON_PATCH = ("kubernetes.core.k8s_json_patch", "k8s_json_patch")


def _docs(path: Path) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def _walk_keys(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [str(k) for k in node] + [k for v in node.values() for k in _walk_keys(v)]
    if isinstance(node, list):
        return [k for v in node for k in _walk_keys(v)]
    return []


def _walk_strings(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [s for k, v in node.items() for s in [str(k), *_walk_strings(v)]]
    if isinstance(node, list):
        return [s for v in node for s in _walk_strings(v)]
    return [str(node)]


def _tasks(play_file: Path) -> list[dict[str, Any]]:
    """Every task of every play in the file, blocks flattened, in file order."""

    def flatten(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for t in items or []:
            if "block" in t:
                out += flatten(t["block"]) + flatten(t.get("rescue", [])) + flatten(t.get("always", []))
            else:
                out.append(t)
        return out

    tasks: list[dict[str, Any]] = []
    for play in yaml.safe_load(play_file.read_text()):
        for section in ("pre_tasks", "tasks", "post_tasks"):
            tasks += flatten(play.get(section, []))
    return tasks


def _module(task: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any] | None:
    for n in names:
        if n in task:
            return task[n]
    return None


def _resolve(value: str) -> str:
    return value.replace("{{ obs_namespace }}", NAMESPACE["zabbix-db"]).replace(
        "{{ zabbix_db.name }}", OBS_VERSIONS["zabbix_db"]["name"]
    )


def _absent_targets(play_file: Path) -> list[tuple[int, tuple[str, str, str]]]:
    found = []
    for i, t in enumerate(_tasks(play_file)):
        m = _module(t, K8S)
        if m and m.get("state") == "absent" and "kind" in m:
            found.append((i, (m["kind"], _resolve(m.get("namespace", "")), _resolve(m.get("name", "")))))
    return found


# --- the manifests --------------------------------------------------------------------------------------


@pytest.mark.parametrize("cluster", sorted(MANIFESTS))
def test_the_manifest_holds_only_the_cluster(cluster: str) -> None:
    docs = _docs(MANIFESTS[cluster])
    kinds = [d["kind"] for d in docs]
    assert kinds == ["Cluster"], f"{cluster}: expected only the Cluster, found {kinds} (ADR 0064)"
    assert not any("barmancloud" in d["apiVersion"] for d in docs), f"{cluster}: a Barman Cloud object is back"


@pytest.mark.parametrize("cluster", sorted(MANIFESTS))
def test_no_cluster_archives_wal_or_names_a_plugin(cluster: str) -> None:
    for d in _docs(MANIFESTS[cluster]):
        keys = _walk_keys(d)
        assert "plugins" not in keys, f"{cluster}: the Cluster names a plugin (ADR 0064)"
        assert "isWALArchiver" not in keys, f"{cluster}: isWALArchiver is back (ADR 0064)"
        barman = [s for s in _walk_strings(d) if "barman" in s.lower()]
        assert not barman, f"{cluster}: Barman is referenced: {barman}"


def test_zabbix_db_is_12gi_and_agrees_with_versions_yaml() -> None:
    (cl,) = [d for d in _docs(MANIFESTS["zabbix-db"]) if d["kind"] == "Cluster"]
    assert cl["spec"]["storage"]["size"] == "12Gi", "zabbix-db grew to 12Gi so a full volume lets postgres start"
    assert OBS_VERSIONS["zabbix_db"]["storage_gb"] == 12, "k8s/observability/versions.yaml zabbix_db.storage_gb"


# --- the plays delete the live objects ------------------------------------------------------------------


def test_the_plays_delete_exactly_the_four_leftover_backup_objects() -> None:
    backup_absent = {
        target for play in PLAYS.values() for _, target in _absent_targets(play) if target[0] in BACKUP_KINDS
    }
    assert backup_absent == CLEANUP, (
        f"the plays must delete exactly {sorted(CLEANUP)}; they delete {sorted(backup_absent)}"
    )


@pytest.mark.parametrize("cluster", sorted(PLAYS))
def test_the_play_removes_the_plugin_between_the_schedule_and_the_object_store(cluster: str) -> None:
    """Order: no further scheduled backup, then the Cluster stops naming the plugin, then its ObjectStore goes,
    then the Cluster is applied. A merge patch cannot remove `plugins`, so a JSON patch must."""
    tasks = _tasks(PLAYS[cluster])
    ns = NAMESPACE[cluster]
    absent = {target: i for i, target in _absent_targets(PLAYS[cluster])}
    sched = absent.get(("ScheduledBackup", ns, f"{cluster}-nightly"))
    store = absent.get(("ObjectStore", ns, "garage"))
    assert sched is not None and store is not None, f"{cluster}: missing a state: absent cleanup task"
    patches = [
        i
        for i, t in enumerate(tasks)
        if (m := _module(t, JSON_PATCH))
        and m.get("kind") == "Cluster"
        and _resolve(m.get("name", "")) == cluster
        and _resolve(m.get("namespace", "")) == ns
        and {"op": "remove", "path": "/spec/plugins"} in m.get("patch", [])
    ]
    assert len(patches) == 1, f"{cluster}: expected one JSON patch removing /spec/plugins, found {len(patches)}"
    manifest = MANIFESTS[cluster].name
    applies = [
        i
        for i, t in enumerate(tasks)
        if (m := _module(t, K8S)) and m.get("state") == "present" and str(m.get("src", "")).endswith(manifest)
    ]
    assert len(applies) == 1 and sched < patches[0] < store < applies[0], (
        f"{cluster}: order must be ScheduledBackup absent ({sched}) < plugin removal ({patches[0]}) "
        f"< ObjectStore absent ({store}) < the one apply of {manifest} ({applies})"
    )


@pytest.mark.parametrize("cluster", sorted(PLAYS))
def test_the_play_no_longer_copies_the_garage_credentials_and_removes_the_copy(cluster: str) -> None:
    ns = NAMESPACE[cluster]
    for t in _tasks(PLAYS[cluster]):
        m = _module(t, K8S) or {}
        d = m.get("definition")
        if m.get("state") == "present" and isinstance(d, dict) and d.get("kind") == "Secret":
            meta = d.get("metadata", {})
            assert not (meta.get("name") == "garage-s3" and _resolve(meta.get("namespace", "")) == ns), (
                f"{cluster}: the play still copies garage-s3 into {ns}, which only the ObjectStore read"
            )
    assert any(target == ("Secret", ns, "garage-s3") for _, target in _absent_targets(PLAYS[cluster])), (
        f"{cluster}: the play must delete the live garage-s3 copy in {ns}"
    )


def test_no_play_waits_for_wal_archiving() -> None:
    waits = [t.get("name") for play in PLAYS.values() for t in _tasks(play) if "ContinuousArchiving" in yaml.safe_dump(t)]
    assert not waits, f"tasks still wait for WAL archiving: {waits}"


def test_no_play_takes_a_backup() -> None:
    takes = []
    for play in PLAYS.values():
        for t in _tasks(play):
            m = _module(t, K8S) or {}
            d = m.get("definition")
            kinds = {x.get("kind") for x in (d if isinstance(d, list) else [d]) if isinstance(x, dict)}
            if m.get("state") != "absent" and kinds & BACKUP_KINDS:
                takes.append(t.get("name"))
    assert not takes, f"tasks still create backup objects: {takes}"
