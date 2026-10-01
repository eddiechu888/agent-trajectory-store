"""Streaming, completed-turn Codex development dialogue projection.

Raw tool payloads and injected operating context deliberately stay at the source.
Every retained item identifies its original line; exclusions are counted explicitly.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from .adapters.base import AdapterError, AdapterInput, AdapterOutput
from .atif import content_text, timestamp

PROFILE = "development-dialogue"
TERMINALS = {"task_complete", "turn_aborted"}
WRAPPERS = (
    "in-app-browser-context", "external_codex_apps_open_page", "environment_context",
    "recommended_plugins", "permissions", "skills_instructions", "app-context",
)


def user_dialogue(text):
    """Remove known machine envelopes, never infer relevance from human prose."""
    stripped = text.lstrip()
    if stripped.startswith(("# AGENTS.md instructions for ", "<heartbeat>", "<user_instructions>",
                            "<system_reminder>", "<skills_instructions>")):
        return "", "injected-user-context"
    original = text
    for tag in WRAPPERS:
        text = re.sub(r"<" + tag + r"\b[^>]*>.*?</" + tag + r">", "", text, flags=re.S)
    if "<heartbeat>" in text:
        return "", "scheduled-heartbeat"
    # The runtime adds this label between an ambient envelope and actual user text.
    if original != text and text.lstrip().startswith("## My request:"):
        text = text.lstrip()[len("## My request:"):].lstrip("\r\n")
    return text, "injected-envelope" if text != original else None


def convert(value: AdapterInput, transcript: Path) -> AdapterOutput:
    steps = []
    omissions = Counter()
    calls = {}
    first_user = None
    created = None
    model = value.model or "unknown"
    version = "unknown"
    current_turn = None
    terminals = []
    has_started = False
    suppressed_turn = False
    digest = hashlib.sha256()
    cutoff = None
    seen_session = None
    bytes_read = 0
    # One line at a time: phone development logs can exceed hundreds of MB.
    with transcript.open("rb") as handle:
        for line_number, raw in enumerate(handle, 1):
            if not raw.endswith(b"\n"):
                break  # an in-flight final JSONL record is not complete
            digest.update(raw)
            bytes_read += len(raw)
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeDecodeError) as exc:
                raise AdapterError(f"invalid Codex JSONL at line {line_number}") from exc
            payload = record.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            kind = record.get("type")
            when = record.get("timestamp") or payload.get("timestamp")
            created = created or when
            if kind == "session_meta":
                seen_session = payload.get("id")
                version = str(payload.get("cli_version", "unknown"))
                continue
            if kind == "turn_context":
                model = payload.get("model") or model
                current_turn = payload.get("turn_id") or current_turn
                continue
            if kind == "event_msg":
                event = payload.get("type")
                if event == "task_started":
                    has_started = True
                    current_turn = payload.get("turn_id") or current_turn
                    suppressed_turn = False
                if event in TERMINALS:
                    terminals.append({"turnId": payload.get("turn_id") or current_turn,
                                      "status": event, "sourceLine": line_number, "timestamp": when})
                    cutoff = {"count": len(steps), "line": line_number, "bytes": bytes_read,
                              "sha256": digest.hexdigest(), "timestamp": when,
                              "omissions": dict(omissions), "model": model}
                continue
            if kind != "response_item":
                continue
            item = payload.get("type")
            ref = {"sourceLine": line_number, "turnId": current_turn}
            if item == "message":
                role = payload.get("role")
                if role not in {"user", "assistant"}:
                    omissions["injected-system-message"] += 1
                    continue
                text = content_text(payload.get("content"))
                if role == "user":
                    if "<heartbeat>" in text:
                        suppressed_turn = True
                    text, reason = user_dialogue(text)
                    if reason:
                        omissions[reason] += 1
                    if not text.strip():
                        continue
                    suppressed_turn = False
                elif payload.get("phase") == "analysis" or payload.get("channel") == "analysis":
                    omissions["reasoning"] += 1
                    continue
                if suppressed_turn:
                    omissions["scheduled-turn-message"] += 1
                    continue
                # Images/attachments are not secretly flattened into prose.
                attachment_count = sum(1 for b in payload.get("content", [])
                                       if isinstance(b, dict) and b.get("type") not in {"input_text", "output_text", "text"}) if isinstance(payload.get("content"), list) else 0
                if attachment_count:
                    omissions["attachment-payload"] += attachment_count
                    text += f"\n[OMITTED: {attachment_count} attachment payload(s); see original source line.]"
                if role == "user" and first_user is None:
                    first_user = text
                step = {"step_id": len(steps) + 1, "timestamp": timestamp(when),
                        "source": "user" if role == "user" else "agent", "message": text,
                        "extra": dict(ref, phase=payload.get("phase"), messageId=payload.get("id"))}
                if role == "assistant":
                    step["model_name"] = model
                steps.append(step)
            elif suppressed_turn:
                omissions["scheduled-turn-item"] += 1
                continue
            elif item in {"function_call", "custom_tool_call", "local_shell_call", "web_search_call"}:
                call_id = str(payload.get("call_id") or payload.get("id") or f"line-{line_number}")
                name = str(payload.get("name") or item)
                arguments = payload.get("arguments") or payload.get("input") or payload.get("action") or {}
                sha = hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()
                step = {"step_id": len(steps) + 1, "timestamp": timestamp(when), "source": "agent", "message": "",
                        "model_name": model, "extra": ref,
                        "tool_calls": [{"tool_call_id": call_id, "function_name": name,
                                        "arguments": {"omitted": "Raw tool arguments remain private at source", "sha256": sha}}]}
                steps.append(step)
                calls[call_id] = step
                omissions["tool-arguments"] += 1
            elif item in {"function_call_output", "custom_tool_call_output", "local_shell_call_output"}:
                call_id = str(payload.get("call_id") or payload.get("id") or "")
                output = payload.get("output")
                result = {"source_call_id": call_id, "content": "[OMITTED: raw tool observation remains private at source]",
                          "extra": dict(ref, sha256=hashlib.sha256(json.dumps(output, sort_keys=True).encode()).hexdigest())}
                target = calls.get(call_id)
                if target is not None:
                    target.setdefault("observation", {}).setdefault("results", []).append(result)
                else:
                    steps.append({"step_id": len(steps) + 1, "timestamp": timestamp(when), "source": "system",
                                  "message": f"[OMITTED: orphan tool observation {call_id}]", "extra": ref})
                omissions["tool-observation"] += 1
            elif item == "reasoning":
                omissions["reasoning"] += 1
    if seen_session != value.session_id:
        raise AdapterError("Codex source session identity does not match bound session")
    if has_started and cutoff is None:
        raise AdapterError("no completed or interrupted Codex turn to archive yet")
    if cutoff:
        steps = steps[:cutoff["count"]]
        omissions = Counter(cutoff["omissions"])
        model = cutoff["model"]
        # A pathological late result cannot cross the captured terminal boundary.
        for step in steps:
            observation = step.get("observation")
            if observation:
                observation["results"] = [r for r in observation["results"] if r["extra"]["sourceLine"] <= cutoff["line"]]
    else:
        cutoff = {"line": line_number, "bytes": bytes_read, "sha256": digest.hexdigest(), "timestamp": when}
    if not steps:
        raise AdapterError("no development dialogue in completed source prefix")
    title = value.payload.get("title") or (" ".join((first_user or value.session_id).split())[:80])
    atif = {"schema_version": "ATIF-v1.7", "session_id": value.session_id,
            "agent": {"name": "Codex", "version": version, "model_name": model}, "steps": steps,
            "final_metrics": {"total_steps": len(steps)},
            "extra": {"source": "codex", "title": title, "capture_profile": PROFILE,
                      "created_at": timestamp(created), "last_activity_at": timestamp(cutoff["timestamp"]),
                      "source_file": transcript.name, "source_prefix_sha256": cutoff["sha256"],
                      "captured_through_line": cutoff["line"], "captured_bytes": cutoff["bytes"],
                      "completed_turns": terminals, "omissions": dict(sorted(omissions.items())),
                      "transcript_format": "codex-rollout-jsonl-unstable"}}
    return AdapterOutput(title=title, atif=atif)
