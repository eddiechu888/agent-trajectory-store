from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


def timestamp(value: Any = None) -> str:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def markdown(title: str, atif: Dict[str, Any], redactions: Iterable[dict]) -> str:
    counts = Counter(item["kind"] for item in redactions)
    extra = atif.get("extra") or {}
    lines = [
        f"# {title}",
        "",
        f"- Source: `{extra.get('source', '')}`",
        f"- Session ID: `{atif.get('session_id', '')}`",
        f"- Model: `{(atif.get('agent') or {}).get('model_name', '')}`",
        f"- Created: {extra.get('created_at', '')}",
        f"- Last modified: {extra.get('last_activity_at', '')}",
        f"- ATIF steps: {len(atif.get('steps', []))}",
        f"- Redactions: {sum(counts.values())}",
        "",
        ("Development dialogue is preserved with original timestamps and source-line references. Injected context, reasoning and raw tool payloads are explicitly omitted; the ATIF file records references and omission counts."
         if extra.get("capture_profile") == "development-dialogue" else
         "The ATIF file preserves structured system messages, tool calls, observations, metrics, and available reasoning after secret redaction."),
        "",
    ]
    visible = 0
    for step in atif.get("steps", []):
        source = step.get("source")
        if source not in {"user", "agent"}:
            continue
        if extra.get("capture_profile") == "development-dialogue" and not step.get("message"):
            continue
        visible += 1
        label = "User" if source == "user" else "Assistant"
        lines.extend([f"## {label} {visible}", "", f"_{step.get('timestamp', '')} · source line {(step.get('extra') or {}).get('sourceLine', 'unknown')}_", "", content_text(step.get("message")) or "_No visible text._", ""])
        calls = step.get("tool_calls") or []
        if calls:
            names = ", ".join(str(call.get("function_name", "unknown")) for call in calls)
            lines.extend([f"_Tools: {names}_", ""])
    return "\n".join(lines).rstrip() + "\n"


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".tmp-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def safe_id(value: str) -> str:
    import re

    result = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    if not result:
        raise ValueError("session id does not contain safe filename characters")
    return result


def update_index(root: Path, entry: Dict[str, Any]) -> Tuple[Path, bytes, bool]:
    path = root / "trajectories" / "index.json"
    index = json.loads(path.read_text()) if path.exists() else {"conversations": []}
    conversations = [
        item
        for item in index.get("conversations", [])
        if not (item.get("source") == entry["source"] and item.get("sessionId") == entry["sessionId"])
    ]
    conversations.append(entry)
    conversations.sort(key=lambda item: (item.get("createdTime", ""), item.get("source", ""), item.get("title", "")))
    source_counts = dict(sorted(Counter(item.get("source", "unknown") for item in conversations).items()))
    value = {
        "schemaVersion": 2,
        "exportedAt": timestamp(),
        "workspace": root.as_uri(),
        "conversationCount": len(conversations),
        "sourceCounts": source_counts,
        "conversations": conversations,
    }
    candidate = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()
    comparable = dict(value)
    comparable.pop("exportedAt", None)
    current = dict(index)
    current.pop("exportedAt", None)
    changed = comparable != current
    return path, candidate, changed
