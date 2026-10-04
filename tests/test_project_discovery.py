import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_trajectory_store.bindings import bind, read_registry
from agent_trajectory_store.config import load
from agent_trajectory_store.pipeline import archive, hook
from agent_trajectory_store.projects import bind_project, discover_projects
from agent_trajectory_store.redaction import redact
from agent_trajectory_store.watcher import watch_once
from test_project_bindings import setup_repo, record, message
from test_store import run


class ProjectDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.env = mock.patch.dict(os.environ, {
            "ATS_BINDINGS_FILE": str(self.base / "bindings.json"),
            "CODEX_HOME": str(self.base / "codex"), "ATS_FOREGROUND": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.project = setup_repo(self.base / "project").repo_root
        self.config = setup_repo(self.base / "archive")
        path = self.config.repo_root / ".devin/trajectory-store.json"
        value = json.loads(path.read_text())
        value["captureProjectSessions"] = True
        path.write_text(json.dumps(value))
        self.config = load(self.config.repo_root)
        bind_project(self.project, self.config.repo_root)

    def session(self, sid, cwd=None, source="vscode", fork=None, completed=True, text="Product design"):
        path = self.base / "codex/sessions/2026/01/01" / f"rollout-{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        values = [record("session_meta", {"id": sid, "cwd": str(cwd or self.project),
                                         "source": source, "forked_from_id": fork}),
                  record("event_msg", {"type": "task_started", "turn_id": "turn-1"}),
                  message("user", text), message("assistant", "Recorded")]
        if completed:
            values.append(record("event_msg", {"type": "task_complete", "turn_id": "turn-1"}))
        path.write_text("".join(json.dumps(r) + "\n" for r in values))
        return path

    def test_only_exact_root_interactive_source_is_discovered(self):
        self.session("desktop")
        self.session("cli", source="cli")
        self.session("other", cwd=self.base, text=f"Please use {self.project}")
        self.session("nested-runtime", cwd=self.project / "logs/runtime")
        self.session("subagent", source={"subagent": {"thread_spawn": {}}})
        self.session("exec", source="exec")
        self.session("fork", fork="unrelated-parent")
        entries, errors = discover_projects()
        self.assertEqual({b["sessionId"] for b in entries}, {"desktop", "cli"})
        self.assertEqual(errors, [])

    def test_backfill_then_discover_new_chat_without_allowlist_edit(self):
        first = self.session("first")
        report = watch_once()
        self.assertEqual(report[0]["status"], "healthy")
        self.session("later", completed=False)
        report = {v["sessionId"]: v for v in watch_once()}
        self.assertEqual(report["later"]["status"], "waiting-for-completed-turn")
        later = self.session("later")
        self.assertTrue(all(v["status"] == "healthy" for v in watch_once()))
        index = json.loads((self.config.repo_root / "trajectories/index.json").read_text())
        self.assertEqual({c["sessionId"] for c in index["conversations"]}, {"first", "later"})
        self.assertEqual(load(self.config.repo_root).sessions, {"thread-one": "Named product design"})
        with first.open("a") as handle:
            handle.write(json.dumps(message("user", "Next correction")) + "\n")
            handle.write(json.dumps(record("event_msg", {"type": "turn_aborted"})) + "\n")
        self.assertTrue(all(v["status"] == "healthy" for v in watch_once()))
        self.assertIn("Next correction", next(self.config.repo_root.glob("trajectories/codex/*first.md")).read_text())

    def test_hook_routes_using_metadata_not_hook_cwd(self):
        path = self.session("good")
        self.assertTrue(hook("codex", {"session_id": "good", "cwd": str(self.base),
                                      "transcript_path": str(path), "hook_event_name": "Stop"}))
        path = self.session("bad", cwd=self.base)
        self.assertFalse(hook("codex", {"session_id": "bad", "cwd": str(self.config.repo_root),
                                       "transcript_path": str(path), "hook_event_name": "Stop"}))
        with self.assertRaisesRegex(ValueError, "allowlist"):
            archive(self.config, {"source": "codex", "sessionId": "bad", "transcriptPath": str(path),
                                  "cwd": str(self.project)})

    def test_missing_opt_in_and_changed_origin_fail_closed(self):
        other = setup_repo(self.base / "other")
        with self.assertRaisesRegex(ValueError, "opted"):
            bind_project(self.project, other.repo_root)
        self.session("good")
        run("git", "remote", "set-url", "origin", "https://example.com/different.git", cwd=self.project)
        entries, errors = discover_projects()
        self.assertEqual(entries, [])
        self.assertEqual(errors[0]["status"], "error")
        self.assertIn("origin changed", errors[0]["error"])

    def test_explicit_bindings_and_project_secret_filters_survive_updates(self):
        secret = self.base / "secret"
        secret.write_text("synthetic-private-value-for-tests")
        bind_project(self.project, self.config.repo_root, [secret])
        bind("codex", "thread-one", self.config.repo_root)
        bind_project(self.project, self.config.repo_root)
        registry = read_registry()
        self.assertEqual(len(registry["bindings"]), 1)
        self.assertEqual(registry["projects"][0]["secretFiles"], [str(secret.resolve())])

    def test_original_title_and_secrets_are_sanitized_everywhere(self):
        secret = self.base / "secret"
        secret.write_text("synthetic-private-value-for-tests")
        bind_project(self.project, self.config.repo_root, [secret])
        self.session("secret-chat", text="Key synthetic-private-value-for-tests")
        (self.base / "codex/session_index.jsonl").write_text(json.dumps({
            "id": "secret-chat", "thread_name": "Design synthetic-private-value-for-tests"}) + "\n")
        self.assertEqual(watch_once()[0]["status"], "healthy")
        for path in (self.config.repo_root / "trajectories").rglob("*"):
            if path.is_file():
                self.assertNotIn(secret.read_text(), path.read_text())
        index = json.loads((self.config.repo_root / "trajectories/index.json").read_text())
        self.assertTrue(index["conversations"][0]["title"].startswith("Design [REDACTED:"))

    def test_partial_metadata_is_retried_and_duplicate_identity_rejected(self):
        path = self.session("partial")
        original = path.read_bytes()
        path.write_bytes(b'{"type":')
        self.assertEqual(discover_projects()[0], [])
        path.write_bytes(original)
        self.assertEqual(len(discover_projects()[0]), 1)
        (path.parent / "duplicate.jsonl").write_bytes(original)
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            discover_projects()

    def test_bitwarden_send_capability_is_redacted(self):
        for url in ["https://send.bitwarden.com/#example/private", "https://send.bitwarden.eu/#example/private",
                    "https://vault.bitwarden.com/#/send/example/private"]:
            text, findings = redact(f"Share {url}")
            self.assertNotIn(url, text)
            self.assertEqual(findings[0]["kind"], "private-capability-url")

    def test_discovery_errors_are_persisted_and_opt_out_rejects_pending_capture(self):
        path = self.session("pending")
        with mock.patch("agent_trajectory_store.watcher.discover_projects", side_effect=RuntimeError("ambiguous")):
            self.assertEqual(watch_once()[0]["status"], "error")
        state = json.loads((self.base / "watch-state.json").read_text())
        self.assertIn("ambiguous", state["report"][0]["error"])
        location = self.config.repo_root / ".devin/trajectory-store.json"
        value = json.loads(location.read_text())
        value["captureProjectSessions"] = False
        location.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "allowlist"):
            archive(load(self.config.repo_root), {"source": "codex", "sessionId": "pending",
                                                "transcriptPath": str(path)})

    def test_publication_commit_contains_sanitized_title(self):
        self.session("title")
        secret = self.base / "secret"
        secret.write_text("synthetic-private-title-value")
        bind_project(self.project, self.config.repo_root, [secret])
        (self.base / "codex/session_index.jsonl").write_text(json.dumps({
            "id": "title", "thread_name": secret.read_text()}) + "\n")
        location = self.config.repo_root / ".devin/trajectory-store.json"
        value = json.loads(location.read_text())
        value["autoCommit"] = True
        location.write_text(json.dumps(value))
        run("git", "add", ".", cwd=self.config.repo_root)
        run("git", "commit", "-m", "Configure capture", cwd=self.config.repo_root)
        self.assertEqual(watch_once()[0]["status"], "healthy")
        subject = run("git", "log", "-1", "--format=%s", cwd=self.config.repo_root)
        self.assertNotIn(secret.read_text(), subject)
        self.assertIn("[REDACTED:", subject)
        self.assertEqual(run("git", "status", "--porcelain", cwd=self.config.repo_root), "")
