from __future__ import annotations

import json
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_trajectory_store.adapters.base import AdapterInput
from agent_trajectory_store.adapters.claude import ClaudeAdapter
from agent_trajectory_store.adapters.codex import CodexAdapter
from agent_trajectory_store.adapters.devin import DevinAdapter
from agent_trajectory_store.config import initialize, load
from agent_trajectory_store.installer import install
from agent_trajectory_store.pipeline import drain, enqueue
from agent_trajectory_store.redaction import redact, remaining_secret_kinds


OPENAI_SAMPLE = "sk-proj-" + "a" * 36
GITHUB_SAMPLE = "ghp_" + "g" * 36


def run(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def claude_fixture(path: Path) -> None:
    records = [
        {
            "type": "system",
            "timestamp": "2026-01-01T00:00:00Z",
            "uuid": "s1",
            "message": {"role": "system", "content": "system text"},
        },
        {
            "type": "user",
            "timestamp": "2026-01-01T00:00:01Z",
            "uuid": "u1",
            "message": {"role": "user", "content": "Inspect the service."},
        },
        {
            "type": "assistant",
            "timestamp": "2026-01-01T00:00:02Z",
            "uuid": "a1",
            "message": {
                "role": "assistant",
                "model": "claude-test",
                "content": [
                    {"type": "text", "text": "I will inspect it."},
                    {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": f"echo {OPENAI_SAMPLE}"}},
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
        {
            "type": "user",
            "timestamp": "2026-01-01T00:00:03Z",
            "uuid": "u2",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": f"token={GITHUB_SAMPLE}"}],
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-01-01T00:00:04Z",
            "uuid": "a2",
            "message": {"role": "assistant", "model": "claude-test", "content": [{"type": "text", "text": "Healthy."}]},
        },
        {
            "type": "assistant",
            "timestamp": "2026-01-01T00:00:05Z",
            "uuid": "side",
            "isSidechain": True,
            "message": {"role": "assistant", "content": "sidechain"},
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")


def codex_fixture(path: Path) -> None:
    records = [
        {"timestamp": "2026-01-02T00:00:00Z", "type": "session_meta", "payload": {"id": "codex-one", "cli_version": "1.0"}},
        {"timestamp": "2026-01-02T00:00:00Z", "type": "turn_context", "payload": {"model": "gpt-test"}},
        {
            "timestamp": "2026-01-02T00:00:01Z",
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Inspect Codex."}]},
        },
        {
            "timestamp": "2026-01-02T00:00:02Z",
            "type": "response_item",
            "payload": {"type": "function_call", "call_id": "call-1", "name": "shell", "arguments": json.dumps({"command": "pwd"})},
        },
        {
            "timestamp": "2026-01-02T00:00:03Z",
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "call-1", "output": "ok"},
        },
        {
            "timestamp": "2026-01-02T00:00:04Z",
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Codex is healthy."}]},
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")


def devin_database(path: Path, cwd: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sessions (
          id TEXT PRIMARY KEY, working_directory TEXT NOT NULL, backend_type TEXT NOT NULL,
          model TEXT NOT NULL, agent_mode TEXT NOT NULL, created_at INTEGER NOT NULL,
          last_activity_at INTEGER NOT NULL, title TEXT, main_chain_id INTEGER,
          workspace_dirs TEXT, metadata TEXT
        );
        CREATE TABLE message_nodes (
          row_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
          node_id INTEGER NOT NULL, parent_node_id INTEGER, chat_message TEXT NOT NULL,
          created_at INTEGER NOT NULL, metadata TEXT, UNIQUE(session_id, node_id)
        );
        """
    )
    messages = [
        {"role": "user", "content": "Inspect Devin.", "message_id": "u", "metadata": {"created_at": "2026-01-03T00:00:00Z"}},
        {
            "role": "assistant",
            "content": "Done.",
            "message_id": "a",
            "tool_calls": [{"id": "d1", "name": "exec", "arguments": {"command": "pwd"}}],
            "metadata": {"created_at": "2026-01-03T00:00:01Z", "generation_model": "devin-test"},
        },
        {"role": "tool", "content": "ok", "message_id": "t", "tool_call_id": "d1", "metadata": {"created_at": "2026-01-03T00:00:02Z"}},
    ]
    for index, message in enumerate(messages):
        connection.execute(
            "INSERT INTO message_nodes(session_id,node_id,parent_node_id,chat_message,created_at) VALUES(?,?,?,?,?)",
            ("devin-one", index, index - 1 if index else None, json.dumps(message), 1767225600 + index),
        )
    connection.execute(
        "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("devin-one", str(cwd), "windsurf", "devin-test", "normal", 1767225600, 1767225602, "Devin Test", 2, "[]", "{}"),
    )
    connection.commit()
    connection.close()


class RedactionTests(unittest.TestCase):
    def test_redacts_known_and_contextual_secrets(self) -> None:
        value, findings = redact({"openai": OPENAI_SAMPLE, "password": "password=abc123", "safe": "API key: placeholder"})
        self.assertNotIn(OPENAI_SAMPLE, json.dumps(value))
        self.assertIn("[REDACTED:", value["openai"])
        self.assertIn("[REDACTED:", value["password"])
        self.assertEqual(value["safe"], "API key: placeholder")
        self.assertFalse(remaining_secret_kinds(value))
        self.assertGreaterEqual(len(findings), 2)


class AdapterTests(unittest.TestCase):
    def test_claude_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claude.jsonl"
            claude_fixture(path)
            result = ClaudeAdapter().convert(AdapterInput("claude-code", "claude-one", path, Path(tmp), None, {}))
            self.assertEqual(result.atif["schema_version"], "ATIF-v1.7")
            self.assertNotIn("sidechain", json.dumps(result.atif))
            tool_step = next(step for step in result.atif["steps"] if step.get("tool_calls"))
            self.assertEqual(tool_step["observation"]["results"][0]["source_call_id"], "tool-1")

    def test_codex_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex.jsonl"
            codex_fixture(path)
            result = CodexAdapter().convert(AdapterInput("codex", "codex-one", path, Path(tmp), None, {}))
            self.assertEqual(result.atif["agent"]["model_name"], "gpt-test")
            tool_step = next(step for step in result.atif["steps"] if step.get("tool_calls"))
            self.assertEqual(tool_step["observation"]["results"][0]["content"], "ok")

    def test_devin_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "sessions.db"
            devin_database(database, root)
            with mock.patch.object(DevinAdapter, "_database_path", return_value=database):
                result = DevinAdapter().convert(AdapterInput("devin", "devin-one", None, root, None, {}))
            self.assertEqual(result.title, "Devin Test")
            agent = next(step for step in result.atif["steps"] if step["source"] == "agent")
            self.assertEqual(agent["observation"]["results"][0]["content"], "ok")


class InstallerTests(unittest.TestCase):
    def test_install_merges_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            claude = home / ".claude" / "settings.json"
            claude.parent.mkdir()
            claude.write_text('{\n  // keep URL strings intact\n  "theme": "dark",\n  "url": "https://example.com/x",\n  "hooks": {"Stop": []}\n}\n')
            changed = install(("devin", "claude-code", "codex"), home=home)
            self.assertEqual(len(changed), 3)
            installed = json.loads(claude.read_text())
            self.assertEqual(installed["theme"], "dark")
            self.assertEqual(installed["url"], "https://example.com/x")
            self.assertEqual(installed["hooks"]["SessionEnd"][0]["matcher"], "")
            backups = list(claude.parent.glob("settings.json.ats-backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
            self.assertEqual(install(("devin", "claude-code", "codex"), home=home), [])


class PipelineTests(unittest.TestCase):
    def test_end_to_end_commit_push_and_idempotence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            remote = base / "remote.git"
            repo.mkdir()
            run("git", "init", "-b", "main", cwd=repo)
            run("git", "config", "user.name", "Test User", cwd=repo)
            run("git", "config", "user.email", "test@example.com", cwd=repo)
            run("git", "init", "--bare", str(remote), cwd=base)
            run("git", "remote", "add", "origin", str(remote), cwd=repo)
            initialize(repo, ("claude-code",), auto_commit=True, auto_push=True, branch="main")
            config_path = repo / ".devin" / "trajectory-store.json"
            config_value = json.loads(config_path.read_text())
            config_value["settleSeconds"] = 0
            config_path.write_text(json.dumps(config_value, indent=2) + "\n")
            (repo / "tracked.txt").write_text("base\n")
            run("git", "add", ".devin/trajectory-store.json", "trajectories", "tracked.txt", cwd=repo)
            run("git", "commit", "-m", "base", cwd=repo)
            run("git", "push", "-u", "origin", "main", cwd=repo)
            transcript = base / "claude.jsonl"
            claude_fixture(transcript)
            manifest = {
                "hook_event_name": "SessionEnd",
                "session_id": "claude-one",
                "transcript_path": str(transcript),
                "cwd": str(repo),
                "model": "claude-test",
            }
            enqueue("claude-code", manifest, repo)
            (repo / "tracked.txt").write_text("unrelated\n")
            results = drain(repo)
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0].changed)
            atif_path = repo / "trajectories" / "claude-code" / "2026-01-01--claude-one.atif.json"
            serialized = atif_path.read_text()
            self.assertNotIn(OPENAI_SAMPLE, serialized)
            self.assertNotIn(GITHUB_SAMPLE, serialized)
            self.assertEqual(run("git", "status", "--short", cwd=repo), "M tracked.txt")
            self.assertEqual(run("git", "rev-parse", "HEAD", cwd=repo), run("git", "rev-parse", "origin/main", cwd=repo))
            enqueue("claude-code", manifest, repo)
            second = drain(repo)
            self.assertEqual(len(second), 1)
            self.assertFalse(second[0].changed)

            with transcript.open("a") as handle:
                handle.write(json.dumps({
                    "type": "assistant",
                    "timestamp": "2026-01-01T00:00:06Z",
                    "uuid": "a3",
                    "message": {"role": "assistant", "model": "claude-test", "content": "One more result."},
                }) + "\n")
            enqueue("claude-code", manifest, repo)
            with mock.patch("agent_trajectory_store.pipeline.commit", side_effect=RuntimeError("commit failed")):
                with self.assertRaisesRegex(RuntimeError, "commit failed"):
                    drain(repo)
            spool = repo / ".git" / "agent-trajectory-store" / "spool"
            self.assertEqual(len(list(spool.glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
