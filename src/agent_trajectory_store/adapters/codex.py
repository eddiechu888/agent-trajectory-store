from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from ..atif import content_text, timestamp
from .base import AdapterError, AdapterInput, AdapterOutput


class CodexAdapter:
    def convert(self, value: AdapterInput) -> AdapterOutput:
        transcript = self._resolve_transcript(value)
        records = self._records(transcript)
        steps: List[Dict[str, Any]] = []
        call_steps: Dict[str, Dict[str, Any]] = {}
        model = value.model or "unknown"
        created = None
        modified = None
        first_user = None
        session_extra: Dict[str, Any] = {}
        has_response_items = any(record.get("type") == "response_item" for record in records)
        for record in records:
            record_type = record.get("type")
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            record_time = record.get("timestamp") or payload.get("timestamp")
            created = created or record_time
            modified = record_time or modified
            if record_type == "session_meta":
                session_extra = payload
                continue
            if record_type == "turn_context":
                model = payload.get("model") or model
                continue
            if record_type == "response_item":
                item_type = payload.get("type")
                if item_type == "message":
                    role = payload.get("role", "system")
                    source = {"assistant": "agent", "user": "user", "developer": "system", "system": "system"}.get(role, "system")
                    text = self._message_text(payload.get("content"))
                    step: Dict[str, Any] = {
                        "step_id": len(steps) + 1,
                        "timestamp": timestamp(record_time),
                        "source": source,
                        "message": text,
                    }
                    if source == "agent":
                        step["model_name"] = model
                    if source == "user" and text and first_user is None:
                        first_user = text
                    steps.append(step)
                elif item_type in {"function_call", "custom_tool_call", "local_shell_call", "web_search_call"}:
                    call_id = str(payload.get("call_id") or payload.get("id") or f"call-{len(steps) + 1}")
                    arguments = payload.get("arguments") or payload.get("input") or payload.get("action") or {}
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            arguments = {"raw": arguments}
                    step = {
                        "step_id": len(steps) + 1,
                        "timestamp": timestamp(record_time),
                        "source": "agent",
                        "message": "",
                        "model_name": model,
                        "tool_calls": [
                            {
                                "tool_call_id": call_id,
                                "function_name": str(payload.get("name") or item_type),
                                "arguments": arguments,
                            }
                        ],
                    }
                    steps.append(step)
                    call_steps[call_id] = step
                elif item_type in {"function_call_output", "custom_tool_call_output", "local_shell_call_output"}:
                    call_id = str(payload.get("call_id") or payload.get("id") or "")
                    result = {"source_call_id": call_id, "content": content_text(payload.get("output"))}
                    target = call_steps.get(call_id)
                    if target is not None:
                        target.setdefault("observation", {}).setdefault("results", []).append(result)
                    else:
                        steps.append(
                            {
                                "step_id": len(steps) + 1,
                                "timestamp": timestamp(record_time),
                                "source": "system",
                                "message": f"[Orphan tool result: {call_id}]\n{result['content']}",
                            }
                        )
                elif item_type == "reasoning":
                    reasoning = self._message_text(payload.get("summary") or payload.get("content"))
                    target = next((step for step in reversed(steps) if step.get("source") == "agent"), None)
                    if target is not None and reasoning:
                        target["reasoning_content"] = reasoning
                continue
            if not has_response_items and record_type == "event_msg":
                event_type = payload.get("type")
                if event_type in {"user_message", "agent_message"}:
                    source = "user" if event_type == "user_message" else "agent"
                    text = content_text(payload.get("message"))
                    step = {
                        "step_id": len(steps) + 1,
                        "timestamp": timestamp(record_time),
                        "source": source,
                        "message": text,
                    }
                    if source == "agent":
                        step["model_name"] = model
                    if source == "user" and text and first_user is None:
                        first_user = text
                    steps.append(step)
        if not steps:
            raise AdapterError(f"no Codex messages found in {transcript}")
        title = self._title(first_user, value.session_id)
        atif = {
            "schema_version": "ATIF-v1.7",
            "session_id": value.session_id,
            "agent": {
                "name": "Codex",
                "version": str(session_extra.get("cli_version", "unknown")),
                "model_name": model,
            },
            "steps": steps,
            "final_metrics": {"total_steps": len(steps)},
            "extra": {
                "source": "codex",
                "title": title,
                "working_directory": str(value.cwd),
                "created_at": timestamp(created),
                "last_activity_at": timestamp(modified),
                "transcript_format": "codex-rollout-jsonl-unstable",
                "session_meta": session_extra,
            },
        }
        return AdapterOutput(title=title, atif=atif)

    def _resolve_transcript(self, value: AdapterInput) -> Path:
        if value.transcript_path is not None and value.transcript_path.is_file():
            return value.transcript_path
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        candidates = list((codex_home / "sessions").glob(f"**/*{value.session_id}*.jsonl"))
        if not candidates:
            raise AdapterError("Codex hook did not provide a readable transcript and no session fallback was found")
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
                raise AdapterError(f"invalid Codex JSONL at line {index + 1}: {exc}") from exc
            if isinstance(record, dict):
                records.append(record)
        return records

    def _message_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return content_text(content)
        parts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)

    def _title(self, first_user: str | None, session_id: str) -> str:
        if not first_user:
            return session_id
        compact = " ".join(first_user.split())
        return compact[:80] + ("…" if len(compact) > 80 else "")
