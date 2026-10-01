"""Explicit source session -> archive checkout bindings (local host paths stay local)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .atif import atomic_write
from .config import load, validate_agents


def registry_path():
    return Path(os.environ.get("ATS_BINDINGS_FILE", Path.home() / ".config/agent-trajectory-store/bindings.json"))


def read_bindings():
    path = registry_path()
    if not path.exists():
        return []
    value = json.loads(path.read_text())
    if value.get("schemaVersion") != 1:
        raise RuntimeError("unsupported project bindings schema")
    return value.get("bindings", [])


def binding_for(source, session_id):
    matches = [b for b in read_bindings() if b["source"] == source and b["sessionId"] == session_id]
    if len(matches) > 1:
        raise RuntimeError("ambiguous explicit session binding")
    return matches[0] if matches else None


def bind(source, session_id, repository):
    validate_agents([source])
    if not session_id or "/" in session_id or "\\" in session_id:
        raise ValueError("invalid source session id")
    config = load(repository)
    if source not in config.agents or (config.sessions and session_id not in config.sessions):
        raise ValueError("session/source not allowed by target repository configuration")
    entries = read_bindings()
    entry = {"source": source, "sessionId": session_id, "repository": str(config.repo_root),
             "expectedOrigin": config.expected_origin}
    entries = [b for b in entries if (b["source"], b["sessionId"]) != (source, session_id)] + [entry]
    entries.sort(key=lambda b: (b["source"], b["sessionId"]))
    atomic_write(registry_path(), (json.dumps({"schemaVersion": 1, "bindings": entries}, indent=2) + "\n").encode())
    registry_path().chmod(0o600)
    return entry
