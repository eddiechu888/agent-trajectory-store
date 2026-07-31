from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..atif import content_text, timestamp
from .base import AdapterError, AdapterInput, AdapterOutput


class ClaudeAdapter:
    def convert(self, value: AdapterInput) -> AdapterOutput:
        transcript = self._resolve_transcript(value)
        records = self._records(transcript)
        steps: List[Dict[str, Any]] = []
        call_steps: Dict[str, Dict[str, Any]] = {}
        model = value.model or "unknown"
        created = None
        modified = None
        first_user = None
        for record in records:
            if record.get("isSidechain") or record.get("agentId") or record.get("parent_tool_use_id"):
                continue
            record_type = record.get("type")
            if record_type not in {"user", "assistant", "system"}:
                continue
            message = record.get("message") if isinstance(record.get("message"), dict) else record
            role = message.get("role") or record_type
            record_time = record.get("timestamp") or message.get("timestamp")
            created = created or record_time
            modified = record_time or modified
            blocks = message.get("content", "")
            text, calls, results = self._blocks(blocks)
            for result in results:
                target = call_steps.get(result["source_call_id"])
                if target is not None:
                    target.setdefault("observation", {}).setdefault("results", []).append(result)
                else:
                    steps.append(
                        {
                            "step_id": len(steps) + 1,
                            "timestamp": timestamp(record_time),
                            "source": "system",
                            "message": f"[Orphan tool result: {result['source_call_id']}]\n{result['content']}",
                        }
                    )
            source = {"assistant": "agent", "user": "user", "system": "system"}.get(role, "system")
            if source == "user" and not text and results:
                continue
            step: Dict[str, Any] = {
                "step_id": len(steps) + 1,
                "timestamp": timestamp(record_time),
                "source": source,
                "message": text,
                "extra": {"uuid": record.get("uuid"), "record_type": record_type},
            }
            if source == "agent":
                model = message.get("model") or model
                step["model_name"] = model
                if calls:
                    step["tool_calls"] = calls
                    for call in calls:
                        call_steps[call["tool_call_id"]] = step
                usage = message.get("usage")
                if isinstance(usage, dict):
                    step["metrics"] = {
                        key: usage[source_key]
                        for source_key, key in (
                            ("input_tokens", "prompt_tokens"),
                            ("output_tokens", "completion_tokens"),
                            ("cache_read_input_tokens", "cached_tokens"),
                            ("cache_creation_input_tokens", "cache_creation_input_tokens"),
                        )
                        if usage.get(source_key) is not None
                    }
            if source == "user" and text and first_user is None:
                first_user = text
            steps.append(step)
        if not steps:
            raise AdapterError(f"no main-thread Claude Code messages found in {transcript}")
        title = self._title(first_user, value.session_id)
        atif = {
            "schema_version": "ATIF-v1.7",
            "session_id": value.session_id,
            "agent": {"name": "Claude Code", "version": "unknown", "model_name": model},
            "steps": steps,
            "final_metrics": {"total_steps": len(steps)},
            "extra": {
                "source": "claude-code",
                "title": title,
                "working_directory": str(value.cwd),
                "created_at": timestamp(created),
                "last_activity_at": timestamp(modified),
                "transcript_format": "claude-code-jsonl-unstable",
            },
        }
        return AdapterOutput(title=title, atif=atif)

    def _resolve_transcript(self, value: AdapterInput) -> Path:
        if value.transcript_path is not None and value.transcript_path.is_file():
            return value.transcript_path
        config_root = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
        candidates = list((config_root / "projects").glob(f"**/{value.session_id}.jsonl"))
        if not candidates:
            raise AdapterError("Claude Code hook did not provide a readable transcript and no session fallback was found")
        return max(candidates, key=lambda path: path.stat().st_mtime_ns)

    def _records(self, path: Path) -> List[Dict[str, Any]]:
        lines = path.read_text(errors="replace").splitlines()
        records = []
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                if index == len(lines) - 1:
                    continue
                raise AdapterError(f"invalid Claude Code JSONL at line {index + 1}: {exc}") from exc
            if isinstance(record, dict):
                records.append(record)
        return records

    def _blocks(self, content: Any) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
        if isinstance(content, str):
            return content, [], []
        texts = []
        calls = []
        results = []
        if not isinstance(content, list):
            return content_text(content), calls, results
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type in {"text", "input_text", "output_text"} and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif block_type == "tool_use":
                calls.append(
                    {
                        "tool_call_id": str(block.get("id", "")),
                        "function_name": str(block.get("name", "unknown")),
                        "arguments": block.get("input", {}),
                    }
                )
            elif block_type == "tool_result":
                results.append(
                    {
                        "source_call_id": str(block.get("tool_use_id", "")),
                        "content": content_text(block.get("content")),
                        "extra": {"is_error": bool(block.get("is_error", False))},
                    }
                )
        return "\n".join(texts), calls, results

    def _title(self, first_user: str | None, session_id: str) -> str:
        if not first_user:
            return session_id
        compact = " ".join(first_user.split())
        return compact[:80] + ("…" if len(compact) > 80 else "")
