import json
from dataclasses import replace
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_trajectory_store.adapters.base import AdapterInput, AdapterError
from agent_trajectory_store.adapters.codex import CodexAdapter
from agent_trajectory_store.bindings import bind
from agent_trajectory_store.config import initialize, load
from agent_trajectory_store.pipeline import archive, hook
from agent_trajectory_store.gitops import sync_checkout
from agent_trajectory_store.redaction import redact, redact_literals
from agent_trajectory_store.watcher import scan_terminals, watch_once
from test_store import run


def record(kind, payload, when="2026-01-01T00:00:01Z"):
    return {"type": kind, "timestamp": when, "payload": payload}


def message(role, text):
    return record("response_item", {"type": "message", "role": role,
                                    "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]})


def fixture(path, sid="thread-one"):
    items = [record("session_meta", {"id": sid, "cli_version": "test", "base_instructions": "PRIVATE OPERATING RECORD"}),
             record("event_msg", {"type": "task_started", "turn_id": "turn-1"}),
             message("developer", "PRIVATE OPERATING RECORD"),
             message("user", "# AGENTS.md instructions for /private/repo\nPRIVATE OPERATING RECORD"),
             message("user", '<heartbeat>PRIVATE OPERATING RECORD</heartbeat>'),
             message("user", '<in-app-browser-context>PRIVATE CAPABILITY</in-app-browser-context>\n\n## My request:\nKeep the original design.'),
             message("assistant", "I will preserve it."),
             record("response_item", {"type": "function_call", "call_id": "call1", "name": "read", "arguments": '{"private":"RAW DEVICE HISTORY"}'}),
             record("response_item", {"type": "function_call_output", "call_id": "call1", "output": "RAW DEVICE HISTORY"}),
             message("user", "Correction: archive these two chats."),
             message("assistant", "Recorded; implementation remains pending."),
             record("event_msg", {"type": "task_complete", "turn_id": "turn-1"}),
             record("event_msg", {"type": "task_started", "turn_id": "turn-2"}),
             message("user", "ACTIVE TURN NOT COMPLETE"), message("assistant", "ACTIVE WORK")]
    path.write_text("".join(json.dumps(item) + "\n" for item in items))
    return items


def setup_repo(root):
    root.mkdir()
    run("git", "init", "-b", "main", cwd=root)
    run("git", "config", "user.name", "Test", cwd=root)
    run("git", "config", "user.email", "test@example.com", cwd=root)
    run("git", "remote", "add", "origin", "https://example.com/private/project.git", cwd=root)
    initialize(root, ["codex"], auto_commit=False, auto_push=False, branch="main")
    path = root / ".devin/trajectory-store.json"
    config = json.loads(path.read_text())
    config.update(captureProfile="development-dialogue", sessions={"thread-one": "Named product design"}, settleSeconds=0)
    path.write_text(json.dumps(config))
    return load(root)


class DevelopmentTests(unittest.TestCase):
    def test_original_dialogue_and_source_refs_survive_omissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"source.jsonl"; fixture(path)
            output = CodexAdapter().convert(AdapterInput("codex", "thread-one", path, Path(tmp), None,
                                                         {"capture_profile": "development-dialogue", "title": "Original title"}))
            serialized = json.dumps(output.atif)
            for excluded in ["PRIVATE OPERATING RECORD", "RAW DEVICE HISTORY", "PRIVATE CAPABILITY", "ACTIVE TURN NOT COMPLETE", "ACTIVE WORK"]:
                self.assertNotIn(excluded, serialized)
            users = [s["message"] for s in output.atif["steps"] if s["source"] == "user"]
            self.assertEqual(users, ["Keep the original design.", "Correction: archive these two chats."])
            self.assertEqual(output.title, "Original title")
            self.assertEqual(output.atif["extra"]["captured_through_line"], 12)
            self.assertEqual(output.atif["extra"]["completed_turns"][0]["turnId"], "turn-1")
            self.assertEqual(output.atif["extra"]["omissions"]["tool-observation"], 1)
            self.assertEqual(next(s for s in output.atif["steps"] if s.get("tool_calls"))["observation"]["results"][0]["source_call_id"], "call1")

    def test_interruption_keeps_owner_instruction_without_claiming_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"source.jsonl"; fixture(p)
            with p.open("a") as f: f.write(json.dumps(record("event_msg", {"type":"turn_aborted", "turn_id":"turn-2"}))+"\n")
            output=CodexAdapter().convert(AdapterInput("codex","thread-one",p,Path(tmp),None,{"capture_profile":"development-dialogue"}))
            self.assertIn("ACTIVE TURN NOT COMPLETE", json.dumps(output.atif))
            self.assertEqual(output.atif["extra"]["completed_turns"][-1]["status"], "turn_aborted")

    def test_wrong_identity_and_no_completed_turn_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"source.jsonl"; fixture(p)
            with self.assertRaises(AdapterError):
                CodexAdapter().convert(AdapterInput("codex","wrong",p,Path(tmp),None,{"capture_profile":"development-dialogue"}))
            p.write_text(json.dumps(record("session_meta", {"id":"thread-one"}))+"\n"+json.dumps(record("event_msg", {"type":"task_started"}))+"\n")
            with self.assertRaisesRegex(AdapterError,"no completed"):
                CodexAdapter().convert(AdapterInput("codex","thread-one",p,Path(tmp),None,{"capture_profile":"development-dialogue"}))

    def test_unbound_chat_rejected_even_with_target_cwd(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ATS_BINDINGS_FILE": tmp+"/bindings.json", "ATS_FOREGROUND":"1"}):
            config=setup_repo(Path(tmp)/"repo")
            self.assertFalse(hook("codex", {"session_id":"unrelated", "cwd":str(config.repo_root), "hook_event_name":"SessionEnd"}))
            with self.assertRaisesRegex(ValueError,"allowlist"):
                archive(config,{"source":"codex","sessionId":"unrelated"})

    def test_binding_routes_from_non_opted_source_repo_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ATS_BINDINGS_FILE": tmp+"/bindings.json", "ATS_FOREGROUND":"1"}):
            config=setup_repo(Path(tmp)/"product")
            source=Path(tmp)/"ooda";source.mkdir()
            transcript=source/"source.jsonl";fixture(transcript)
            bind("codex","thread-one",config.repo_root)
            payload={"session_id":"thread-one","cwd":str(source),"transcript_path":str(transcript),"hook_event_name":"Stop"}
            self.assertTrue(hook("codex",payload))
            index=config.repo_root/"trajectories/index.json"
            value=json.loads(index.read_text())
            self.assertEqual(value["conversationCount"],1)
            self.assertEqual(value["conversations"][0]["title"],"Named product design")
            atif=config.repo_root/value["conversations"][0]["atifPath"]
            before=(index.read_bytes(),atif.read_bytes())
            self.assertTrue(hook("codex",payload))
            self.assertEqual(before,(index.read_bytes(),atif.read_bytes()))
            with self.assertRaises(ValueError):bind("codex","other",config.repo_root)

    def test_watcher_only_advances_on_complete_records_and_terminals(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"source.jsonl";fixture(p)
            first=scan_terminals(p,{})
            with p.open("a") as f:f.write('{"type":')
            self.assertEqual(scan_terminals(p,first),first)
            p.write_text(json.dumps(record("event_msg",{"type":"task_complete"}))+"\n")
            reset=scan_terminals(p,first)
            self.assertLess(reset["offset"],first["offset"])
            self.assertEqual(reset["boundary"],p.stat().st_size)

    def test_scheduled_turn_is_omitted_and_direct_steering_resumes_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"source.jsonl"
            items=[record("session_meta",{"id":"thread-one"}),record("event_msg",{"type":"task_started","turn_id":"auto"}),
                   message("user","<heartbeat>Automated operating context</heartbeat>"),
                   message("assistant","PRIVATE SCHEDULED UPDATE"),
                   record("event_msg",{"type":"task_complete","turn_id":"auto"}),
                   record("event_msg",{"type":"task_started","turn_id":"human"}),
                   message("user","Keep this direct product decision."),message("assistant","Acknowledged."),
                   record("event_msg",{"type":"task_complete","turn_id":"human"})]
            p.write_text("".join(json.dumps(item)+"\n" for item in items))
            output=CodexAdapter().convert(AdapterInput("codex","thread-one",p,Path(tmp),None,{"capture_profile":"development-dialogue"}))
            self.assertNotIn("PRIVATE SCHEDULED UPDATE",json.dumps(output.atif))
            self.assertIn("Keep this direct product decision.",json.dumps(output.atif))

    def test_private_url_and_configured_literal_key_redacted(self):
        url="https://private-node.ts.net:8443/"+"X"*43+"/"
        result,findings=redact("Download "+url)
        self.assertNotIn(url,result);self.assertTrue(findings)
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"key";p.write_text("unrecognized-provider-key-value")
            value,findings=redact_literals({"dialogue":"Key unrecognized-provider-key-value"},[p])
            self.assertNotIn(p.read_text(),json.dumps(value));self.assertEqual(findings[0]["kind"],"configured-secret")

    def test_watcher_backfill_and_future_completed_turn_use_same_conversation(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ATS_BINDINGS_FILE":tmp+"/bindings.json"}):
            config=setup_repo(Path(tmp)/"repo")
            p=Path(tmp)/"source.jsonl";fixture(p)
            bind("codex","thread-one",config.repo_root)
            with mock.patch.object(CodexAdapter,"_resolve_transcript",return_value=p):
                self.assertEqual(watch_once()[0]["status"],"healthy")
                first=json.loads((config.repo_root/"trajectories/index.json").read_text())["conversations"][0]
                with p.open("a") as f:f.write(json.dumps(record("event_msg",{"type":"task_complete","turn_id":"turn-2"}))+"\n")
                self.assertEqual(watch_once()[0]["status"],"healthy")
                index=json.loads((config.repo_root/"trajectories/index.json").read_text())
                self.assertEqual(index["conversationCount"],1)
                self.assertGreater(index["conversations"][0]["stepCount"],first["stepCount"])
                self.assertEqual(index["conversations"][0]["atifPath"],first["atifPath"])

    def test_sync_preserves_unrelated_dirty_work_and_blocks_remote_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            config=setup_repo(Path(tmp)/"repo")
            (config.repo_root/"unrelated.txt").write_text("keep")
            with self.assertRaisesRegex(RuntimeError,"dirty"):
                sync_checkout(config)
            self.assertEqual((config.repo_root/"unrelated.txt").read_text(),"keep")
            run("git","remote","set-url","origin","https://example.com/other.git",cwd=config.repo_root)
            with self.assertRaisesRegex(RuntimeError,"mismatch"):
                sync_checkout(config)


class CheckoutSyncTests(unittest.TestCase):
    def test_upstream_code_and_offline_archive_commits_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp); config=setup_repo(base/"repo"); root=config.repo_root
            remote=base/"remote.git"
            run("git","init","--bare",str(remote),cwd=base)
            run("git","remote","set-url","origin",str(remote),cwd=root)
            config=replace(config, expected_origin=str(remote))
            run("git","add",".",cwd=root);run("git","commit","-m","base",cwd=root);run("git","push","-u","origin","main",cwd=root)
            other=base/"other";run("git","clone","--branch","main",str(remote),str(other),cwd=base)
            run("git","config","user.name","Test",cwd=other);run("git","config","user.email","test@example.com",cwd=other)
            (other/"app.txt").write_text("upstream first")
            run("git","add","app.txt",cwd=other);run("git","commit","-m","product code",cwd=other);run("git","push","origin","main",cwd=other)
            sync_checkout(config)
            self.assertEqual((root/"app.txt").read_text(),"upstream first")
            (root/"trajectories/offline.md").write_text("retained archive")
            run("git","add","trajectories/offline.md",cwd=root);run("git","commit","-m","offline archive",cwd=root)
            (other/"app.txt").write_text("upstream second")
            run("git","add","app.txt",cwd=other);run("git","commit","-m","more product code",cwd=other);run("git","push","origin","main",cwd=other)
            sync_checkout(config)
            self.assertEqual((root/"app.txt").read_text(),"upstream second")
            self.assertEqual((root/"trajectories/offline.md").read_text(),"retained archive")
            (root/"unauthorized-code.txt").write_text("keep local")
            run("git","add","unauthorized-code.txt",cwd=root);run("git","commit","-m","unrelated",cwd=root)
            with self.assertRaisesRegex(RuntimeError,"non-trajectory"):
                sync_checkout(config)
            self.assertEqual((root/"unauthorized-code.txt").read_text(),"keep local")

    def test_publish_failure_is_not_reported_as_success(self):
        from agent_trajectory_store.pipeline import enqueue, drain
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"ATS_BINDINGS_FILE":tmp+"/bindings.json"}):
            config=setup_repo(Path(tmp)/"repo");root=config.repo_root
            config_path=root/".devin/trajectory-store.json"
            data=json.loads(config_path.read_text());data["autoCommit"]=True;data["autoPush"]=True
            config_path.write_text(json.dumps(data))
            run("git","add",".",cwd=root);run("git","commit","-m","base",cwd=root)
            p=Path(tmp)/"source.jsonl";fixture(p)
            enqueue("codex",{"session_id":"thread-one","transcript_path":str(p)},root)
            with mock.patch("agent_trajectory_store.pipeline.push",return_value=False):
                with self.assertRaisesRegex(RuntimeError,"archived locally"):
                    drain(root)
            self.assertEqual(json.loads((root/"trajectories/index.json").read_text())["conversationCount"],1)
            self.assertFalse(run("git","status","--porcelain",cwd=root))
