"""Optional local completed-turn archiver; does not control the agent or inject context."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .adapters.base import AdapterInput
from .adapters.codex import CodexAdapter
from .atif import atomic_write, timestamp
from .bindings import read_bindings, registry_path
from .development import TERMINALS
from .config import load
from .pipeline import drain, enqueue


def scan_terminals(path, previous):
    stat = path.stat()
    identity = [stat.st_dev, stat.st_ino]
    previous = previous if previous.get("identity") == identity and previous.get("offset", 0) <= stat.st_size else {}
    offset = previous.get("offset", 0)
    boundary = previous.get("boundary", 0)
    with path.open("rb") as handle:
        handle.seek(offset)
        while True:
            raw = handle.readline()
            if not raw or not raw.endswith(b"\n"):
                break
            record = json.loads(raw)
            payload = record.get("payload") or {}
            if record.get("type") == "event_msg" and payload.get("type") in TERMINALS:
                boundary = handle.tell()
            offset = handle.tell()
    return dict(previous, identity=identity, offset=offset, boundary=boundary)


def watch_once():
    location = registry_path().with_name("watch-state.json")
    state = json.loads(location.read_text()) if location.exists() else {"sessions": {}}
    report = []
    for binding in read_bindings():
        source, session_id = binding["source"], binding["sessionId"]
        if source != "codex":
            continue
        key = source + ":" + session_id
        previous = state["sessions"].get(key, {})
        try:
            config = load(Path(binding["repository"]))
            if binding["expectedOrigin"] != config.expected_origin:
                raise RuntimeError("binding origin changed; refusing capture")
            value = AdapterInput(source, session_id, None, Path(binding["repository"]), None, {})
            path = CodexAdapter()._resolve_transcript(value)
            current = scan_terminals(path, previous)
            if current["boundary"] and (current["boundary"] != previous.get("archivedBoundary") or current["identity"] != previous.get("archivedIdentity")):
                payload = {"session_id": session_id, "transcript_path": str(path), "cwd": binding["repository"],
                           "hook_event_name": "Stop"}
                enqueue(source, payload, Path(binding["repository"]))
                results = drain(Path(binding["repository"]))
                if not results:
                    raise RuntimeError("capture did not finish; inspect archive worker error log")
                current["archivedBoundary"] = current["boundary"]
                current["archivedIdentity"] = current["identity"]
                current["lastArchivedAt"] = timestamp()
            # Retry retained spool / offline archive commits without rewriting dialogue.
            elif previous.get("error"):
                drain(Path(binding["repository"]))
            current.pop("error", None)
            state["sessions"][key] = current
            report.append({"sessionId": session_id, "status": "healthy", "capturedBoundary": current.get("archivedBoundary", 0)})
        except Exception as exc:
            previous["error"] = f"{type(exc).__name__}: {exc}"
            state["sessions"][key] = previous
            report.append({"sessionId": session_id, "status": "error", "error": previous["error"]})
    state["lastScanAt"] = timestamp()
    state["report"] = report
    atomic_write(location, (json.dumps(state, indent=2) + "\n").encode())
    location.chmod(0o600)
    return report


def watch(interval=30):
    if interval < 5:
        raise ValueError("watch interval must be at least five seconds")
    while True:
        print(json.dumps(watch_once()), flush=True)
        time.sleep(interval)
