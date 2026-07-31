from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .atif import atomic_write
from .config import SUPPORTED_AGENTS, validate_agents


def _strip_json_comments(text: str) -> str:
    output = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == "/" and next_char == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(text) and text[index:index + 2] != "*/":
                index += 1
            index = min(index + 2, len(text))
            continue
        output.append(char)
        index += 1
    return "".join(output)


def _read(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(_strip_json_comments(path.read_text()))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"cannot merge malformed JSON config: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"expected a JSON object in {path}")
    return value


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S%f")
    backup = path.with_name(path.name + f".ats-backup-{stamp}")
    atomic_write(backup, path.read_bytes())
    return backup


def _command(agent: str) -> str:
    return f'"{sys.executable}" -m agent_trajectory_store hook --agent {agent}'


def _entry(agent: str, event: str) -> Dict[str, Any]:
    timeout = 3 if agent == "codex" and event == "SessionEnd" else 30
    return {
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": _command(agent),
                "timeout": timeout,
            }
        ]
    }


def _merge_hook(config: Dict[str, Any], agent: str) -> Dict[str, Any]:
    hooks = config.setdefault("hooks", {})
    for event in ("SessionStart", "SessionEnd"):
        groups = hooks.setdefault(event, [])
        command = _command(agent)
        exists = any(
            handler.get("command") == command
            for group in groups
            for handler in group.get("hooks", [])
            if isinstance(handler, dict)
        )
        if not exists:
            groups.append(_entry(agent, event))
    return config


def locations(home: Path | None = None) -> Dict[str, Path]:
    home = home or Path.home()
    devin_config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "devin" / "config.json"
    return {
        "devin": devin_config,
        "claude-code": home / ".claude" / "settings.json",
        "codex": home / ".codex" / "hooks.json",
    }


def install(agents: Iterable[str], home: Path | None = None, dry_run: bool = False) -> List[Path]:
    selected = validate_agents(agents)
    paths = locations(home)
    changed = []
    for agent in selected:
        path = paths[agent]
        config = _read(path)
        if agent == "codex":
            config.setdefault("description", "Global hooks installed by Agent Trajectory Store.")
        before = json.dumps(config, sort_keys=True)
        merged = _merge_hook(config, agent)
        if json.dumps(merged, sort_keys=True) == before:
            continue
        changed.append(path)
        if dry_run:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        _backup(path)
        atomic_write(path, (json.dumps(merged, indent=2) + "\n").encode())
    return changed
