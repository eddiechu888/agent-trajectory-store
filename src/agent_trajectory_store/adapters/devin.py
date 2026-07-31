from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from ..atif import content_text, timestamp
from .base import AdapterError, AdapterInput, AdapterOutput


class DevinAdapter:
    def convert(self, value: AdapterInput) -> AdapterOutput:
        native = self._native(value.transcript_path)
        if native is not None:
            native.setdefault("session_id", value.session_id)
            native.setdefault("extra", {})["source"] = "devin"
            title = native["extra"].get("title") or value.session_id
            return AdapterOutput(title=title, atif=native)
        database = self._database_path()
        connection = self._connect(database)
        try:
            connection.execute("BEGIN")
            session = connection.execute("SELECT * FROM sessions WHERE id = ?", (value.session_id,)).fetchone()
            if session is None:
                raise AdapterError(f"Devin session not found: {value.session_id}")
            if session["main_chain_id"] is None:
                raise AdapterError(f"Devin session has no main chain: {value.session_id}")
            rows = connection.execute(
                """
                WITH RECURSIVE chain(node_id,parent_node_id,chat_message,created_at,depth) AS (
                  SELECT node_id,parent_node_id,chat_message,created_at,0
                  FROM message_nodes WHERE session_id = ? AND node_id = ?
                  UNION ALL
                  SELECT m.node_id,m.parent_node_id,m.chat_message,m.created_at,c.depth + 1
                  FROM message_nodes m JOIN chain c ON m.node_id = c.parent_node_id
                  WHERE m.session_id = ?
                )
                SELECT node_id,parent_node_id,chat_message,created_at,depth
                FROM chain ORDER BY depth DESC
                """,
                (value.session_id, session["main_chain_id"], value.session_id),
            ).fetchall()
            atif = self._atif(session, rows)
            return AdapterOutput(title=session["title"] or value.session_id, atif=atif)
        finally:
            connection.close()

    def _native(self, path: Path | None) -> Dict[str, Any] | None:
        if path is None or not path.is_file():
            return None
        try:
            value = json.loads(path.read_text())
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if isinstance(value, dict) and str(value.get("schema_version", "")).startswith("ATIF-v"):
            return value
        return None

    def _database_path(self) -> Path:
        data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        return data_home / "devin" / "cli" / "sessions.db"

    def _connect(self, path: Path) -> sqlite3.Connection:
        if not path.exists():
            raise AdapterError(f"Devin session database not found: {path}")
        required = {"sessions", "message_nodes"}
        last_error = None
        for query in ("mode=ro", "mode=ro&immutable=1"):
            connection = sqlite3.connect(f"file:{path.resolve()}?{query}", uri=True)
            connection.row_factory = sqlite3.Row
            try:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            except sqlite3.OperationalError as exc:
                connection.close()
                last_error = exc
                continue
            if not required.issubset(tables):
                connection.close()
                raise AdapterError(f"unsupported Devin session schema: {path}")
            return connection
        raise AdapterError(f"unable to read Devin session database {path}: {last_error}")

    def _calls(self, value: Any) -> List[Dict[str, Any]]:
        calls = []
        if not isinstance(value, list):
            return calls
        for call in value:
            if not isinstance(call, dict):
                continue
            arguments = call.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {"raw": arguments}
            calls.append(
                {
                    "tool_call_id": str(call.get("id", "")),
                    "function_name": str(call.get("name", "unknown")),
                    "arguments": arguments,
                }
            )
        return calls

    def _metrics(self, metadata: Dict[str, Any]) -> Dict[str, Any] | None:
        metrics = metadata.get("metrics") or {}
        mapping = {
            "input_tokens": "prompt_tokens",
            "output_tokens": "completion_tokens",
            "cache_read_tokens": "cached_tokens",
            "cache_creation_tokens": "cache_creation_input_tokens",
        }
        result = {target: metrics[source] for source, target in mapping.items() if metrics.get(source) is not None}
        return result or None

    def _atif(self, session: sqlite3.Row, rows: List[sqlite3.Row]) -> Dict[str, Any]:
        steps = []
        call_steps: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            message = json.loads(row["chat_message"])
            role = message.get("role", "system")
            metadata = message.get("metadata") or {}
            created = timestamp(metadata.get("created_at") or row["created_at"])
            if role == "tool":
                call_id = str(message.get("tool_call_id", ""))
                result = {"source_call_id": call_id, "content": content_text(message.get("content"))}
                target = call_steps.get(call_id)
                if target is not None:
                    target.setdefault("observation", {}).setdefault("results", []).append(result)
                else:
                    steps.append(
                        {
                            "step_id": len(steps) + 1,
                            "timestamp": created,
                            "source": "system",
                            "message": f"[Orphan tool result: {call_id}]\n{result['content']}",
                        }
                    )
                continue
            source = {"assistant": "agent", "user": "user", "system": "system"}.get(role, "system")
            step: Dict[str, Any] = {
                "step_id": len(steps) + 1,
                "timestamp": created,
                "source": source,
                "message": message.get("content", ""),
                "extra": {"node_id": row["node_id"], "message_id": message.get("message_id"), "metadata": metadata},
            }
            if source == "agent":
                step["model_name"] = metadata.get("generation_model") or session["model"]
                thinking = message.get("thinking")
                if isinstance(thinking, dict) and thinking.get("thinking"):
                    step["reasoning_content"] = thinking["thinking"]
                calls = self._calls(message.get("tool_calls"))
                if calls:
                    step["tool_calls"] = calls
                    for call in calls:
                        if call["tool_call_id"]:
                            call_steps[call["tool_call_id"]] = step
                metrics = self._metrics(metadata)
                if metrics:
                    step["metrics"] = metrics
            steps.append(step)
        session_metadata = json.loads(session["metadata"] or "{}")
        return {
            "schema_version": "ATIF-v1.7",
            "session_id": session["id"],
            "agent": {
                "name": "Devin CLI",
                "version": os.environ.get("DEVIN_VERSION", "unknown"),
                "model_name": session["model"],
                "extra": {"backend_type": session["backend_type"], "agent_mode": session["agent_mode"]},
            },
            "steps": steps,
            "final_metrics": {
                "total_steps": len(steps),
                "extra": {
                    "committed_credit_cost": session_metadata.get("total_credit_cost", 0),
                    "committed_acu_cost": session_metadata.get("total_acu_cost", 0),
                },
            },
            "extra": {
                "source": "devin",
                "title": session["title"] or session["id"],
                "working_directory": session["working_directory"],
                "created_at": timestamp(session["created_at"]),
                "last_activity_at": timestamp(session["last_activity_at"]),
                "main_chain_id": session["main_chain_id"],
            },
        }
