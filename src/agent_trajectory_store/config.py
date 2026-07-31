from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Tuple


SUPPORTED_AGENTS = ("devin", "claude-code", "codex")


@dataclass(frozen=True)
class StoreConfig:
    repo_root: Path
    expected_origin: str
    branch: str
    auto_commit: bool
    auto_push: bool
    agents: Tuple[str, ...]
    settle_seconds: float


def git(repo: Path, *args: str, check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ("git", *args),
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def repo_root(path: Path) -> Path:
    result = git(path.resolve(), "rev-parse", "--show-toplevel", check=False)
    if result.returncode != 0:
        raise RuntimeError(f"not inside a Git repository: {path}")
    return Path(result.stdout.strip()).resolve()


def config_path(root: Path) -> Path:
    return root / ".devin" / "trajectory-store.json"


def validate_agents(agents: Iterable[str]) -> Tuple[str, ...]:
    values = tuple(dict.fromkeys(agents))
    unknown = sorted(set(values) - set(SUPPORTED_AGENTS))
    if unknown:
        raise ValueError(f"unsupported agents: {', '.join(unknown)}")
    if not values:
        raise ValueError("at least one agent is required")
    return values


def load(path: Path) -> StoreConfig:
    root = repo_root(path)
    location = config_path(root)
    if not location.exists():
        raise FileNotFoundError(f"trajectory store is not initialized: {location}")
    value = json.loads(location.read_text())
    if value.get("schemaVersion") != 1:
        raise RuntimeError(f"unsupported trajectory-store schema in {location}")
    if not value.get("enabled", False):
        raise RuntimeError(f"trajectory store is disabled in {location}")
    agents = validate_agents(value.get("agents", []))
    auto_commit = bool(value.get("autoCommit", True))
    auto_push = bool(value.get("autoPush", False))
    if auto_push and not auto_commit:
        raise ValueError("autoPush requires autoCommit")
    return StoreConfig(
        repo_root=root,
        expected_origin=str(value["expectedOrigin"]),
        branch=str(value.get("branch", "main")),
        auto_commit=auto_commit,
        auto_push=auto_push,
        agents=agents,
        settle_seconds=float(value.get("settleSeconds", 2.0)),
    )


def initialize(
    path: Path,
    agents: Iterable[str],
    auto_commit: bool,
    auto_push: bool,
    branch: str,
) -> StoreConfig:
    root = repo_root(path)
    selected = validate_agents(agents)
    if auto_push and not auto_commit:
        raise ValueError("autoPush requires autoCommit")
    origin = git(root, "remote", "get-url", "origin", check=False).stdout.strip()
    if not origin:
        raise RuntimeError("repository has no origin remote")
    location = config_path(root)
    location.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "schemaVersion": 1,
        "enabled": True,
        "expectedOrigin": origin,
        "branch": branch,
        "autoCommit": auto_commit,
        "autoPush": auto_push,
        "agents": list(selected),
        "settleSeconds": 2.0,
    }
    location.write_text(json.dumps(value, indent=2) + "\n")
    trajectories = root / "trajectories"
    trajectories.mkdir(exist_ok=True)
    index = trajectories / "index.json"
    if not index.exists():
        index.write_text(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "workspace": root.as_uri(),
                    "conversationCount": 0,
                    "sourceCounts": {},
                    "conversations": [],
                },
                indent=2,
            )
            + "\n"
        )
    readme = trajectories / "README.md"
    if not readme.exists():
        readme.write_text(
            "# Agent trajectories\n\n"
            "This directory is managed by [Agent Trajectory Store](https://github.com/eddiechu888/agent-trajectory-store). "
            "It contains sanitized ATIF v1.7 trajectories, readable Markdown transcripts, and an integrity index.\n"
        )
    return load(root)
