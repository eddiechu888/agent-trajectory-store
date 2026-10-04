from __future__ import annotations

import contextlib
import fcntl
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .adapters import ADAPTERS, AdapterInput
from .atif import atomic_write, digest, markdown, safe_id, timestamp, update_index
from .config import StoreConfig, git, load, repo_root
from .gitops import commit, error_log, push, sync_checkout
from .redaction import redact, redact_literals, remaining_secret_kinds, summarize


@dataclass(frozen=True)
class ArchiveResult:
    changed: bool
    title: str
    paths: tuple
    redactions: tuple


def state_dir(root: Path) -> Path:
    value = git(root, "rev-parse", "--git-path", "agent-trajectory-store", check=False).stdout.strip()
    if not value:
        value = str(root / ".git" / "agent-trajectory-store")
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


@contextlib.contextmanager
def pipeline_lock(root: Path):
    path = state_dir(root) / "pipeline.lock"
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def enqueue(source: str, payload: Dict[str, Any], root: Path) -> Path:
    if source not in ADAPTERS:
        raise ValueError(f"unsupported source: {source}")
    session_id = payload.get("session_id") or payload.get("trajectory_id")
    if not session_id:
        raise ValueError("hook payload does not contain a session id")
    spool = state_dir(root) / "spool"
    spool.mkdir(exist_ok=True)
    value = {
        "schemaVersion": 1,
        "source": source,
        "sessionId": str(session_id),
        "transcriptPath": payload.get("transcript_path"),
        "cwd": str(payload.get("cwd") or root),
        "model": payload.get("model"),
        "enqueuedAt": timestamp(),
        "payload": {
            key: item
            for key, item in payload.items()
            if key in {"hook_event_name", "reason", "source", "session_id", "trajectory_id", "transcript_path", "cwd", "model"}
        },
    }
    path = spool / f"{source}--{safe_id(str(session_id))}.json"
    atomic_write(path, (json.dumps(value, indent=2) + "\n").encode())
    return path


def spawn_drain(root: Path) -> None:
    directory = state_dir(root)
    log = (directory / "worker.log").open("ab")
    subprocess.Popen(
        (sys.executable, "-m", "agent_trajectory_store", "drain", "--repo", str(root)),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
        close_fds=True,
    )
    log.close()


def hook(source: str, payload: Dict[str, Any]) -> bool:
    from .bindings import binding_for, read_registry
    from .projects import project_binding_for
    session_id = payload.get("session_id") or payload.get("trajectory_id")
    binding = binding_for(source, session_id)
    if binding is None and source == "codex" and read_registry().get("projects"):
        from .adapters.base import AdapterError
        try:
            transcript = ADAPTERS[source]._resolve_transcript(AdapterInput(
                source, session_id, Path(payload["transcript_path"]) if payload.get("transcript_path") else None,
                Path(payload.get("cwd") or Path.cwd()), None, {}))
            binding = project_binding_for(source, session_id, transcript)
        except (FileNotFoundError, AdapterError):
            return False
    cwd = Path(binding["repository"] if binding else payload.get("cwd") or Path.cwd())
    try:
        root = repo_root(cwd)
        config = load(root)
    except (FileNotFoundError, RuntimeError):
        return False
    if source not in config.agents:
        return False
    if ((config.sessions or config.capture_project_sessions) and session_id not in (config.sessions or {})
            and not (config.capture_project_sessions and binding and binding.get("projectRoot"))):
        return False
    if binding and binding["expectedOrigin"] != config.expected_origin:
        raise RuntimeError("binding origin no longer matches archive configuration")
    event = payload.get("hook_event_name") or payload.get("event_type")
    if event in {"SessionEnd", "session_end", "Stop", "stop", "Interrupt", "interrupt"}:
        enqueue(source, payload, root)
    elif event not in {"SessionStart", "session_start"}:
        return False
    if os.environ.get("ATS_FOREGROUND") == "1":
        drain(root)
    else:
        spawn_drain(root)
    return True


def _settle(path: Path | None, seconds: float) -> None:
    if path is None or not path.exists() or seconds <= 0:
        return
    deadline = time.monotonic() + max(seconds, 0.2) * 3
    previous = None
    stable_since = None
    while time.monotonic() < deadline:
        current = (path.stat().st_size, path.stat().st_mtime_ns)
        if current == previous:
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= seconds:
                return
        else:
            stable_since = None
        previous = current
        time.sleep(min(0.25, max(seconds / 4, 0.05)))


def archive(config: StoreConfig, manifest: Dict[str, Any]) -> ArchiveResult:
    source = manifest["source"]
    session_id = manifest["sessionId"]
    if source not in config.agents:
        raise ValueError("unbound source session rejected by repository allowlist")
    transcript_value = manifest.get("transcriptPath")
    transcript = Path(transcript_value) if transcript_value else None
    from .bindings import binding_for
    from .projects import project_binding_for, session_titles
    binding = binding_for(source, session_id) or {}
    if config.capture_project_sessions:
        transcript = ADAPTERS[source]._resolve_transcript(AdapterInput(source, session_id, transcript,
                                                       config.repo_root, None, {}))
        project = project_binding_for(source, session_id, transcript)
        if project and Path(project["repository"]).resolve() == config.repo_root:
            secret_files = list(dict.fromkeys(project.get("secretFiles", []) + binding.get("secretFiles", [])))
            binding = dict(project, **binding)
            binding["secretFiles"] = secret_files
    if ((config.sessions or config.capture_project_sessions) and session_id not in (config.sessions or {})
            and not (config.capture_project_sessions and binding.get("projectRoot")
                     and Path(binding["repository"]).resolve() == config.repo_root)):
        raise ValueError("unbound source session rejected by repository allowlist")
    _settle(transcript, config.settle_seconds)
    adapter_input = AdapterInput(
        source=source,
        session_id=session_id,
        transcript_path=transcript,
        cwd=Path(manifest.get("cwd") or config.repo_root),
        model=manifest.get("model"),
        payload=dict(manifest.get("payload") or {}, capture_profile=config.capture_profile,
                     title=(config.sessions or {}).get(session_id) or
                     (session_titles().get(session_id) if binding.get("projectRoot") else None)),
    )
    converted = ADAPTERS[source].convert(adapter_input)
    projected, literal_findings = redact_literals(converted.atif, binding.get("secretFiles", []))
    redacted_atif, findings = redact(projected)
    findings = literal_findings + findings
    remaining = remaining_secret_kinds(redacted_atif)
    if remaining:
        raise RuntimeError(f"secret scan failed after redaction: {', '.join(remaining)}")
    safe_title, _ = redact_literals(converted.title, binding.get("secretFiles", []))
    safe_title, _ = redact(safe_title)
    readable = markdown(safe_title, redacted_atif, findings)
    redacted_markdown, markdown_findings = redact(readable)
    findings.extend(markdown_findings)
    remaining = remaining_secret_kinds(redacted_markdown)
    if remaining:
        raise RuntimeError(f"Markdown secret scan failed after redaction: {', '.join(remaining)}")
    extra = redacted_atif.get("extra") or {}
    created = extra.get("created_at") or manifest.get("enqueuedAt") or timestamp()
    date = str(created)[:10]
    basename = f"{date}--{safe_id(session_id)}"
    output = config.repo_root / "trajectories" / source
    atif_path = output / f"{basename}.atif.json"
    markdown_path = output / f"{basename}.md"
    atif_bytes = (json.dumps(redacted_atif, indent=2, ensure_ascii=False) + "\n").encode()
    markdown_bytes = redacted_markdown.encode()
    redaction_summary = summarize(findings)
    entry = {
        "source": source,
        "sessionId": session_id,
        "title": safe_title,
        "format": redacted_atif.get("schema_version", "ATIF-v1.7"),
        "createdTime": created,
        "lastModifiedTime": extra.get("last_activity_at") or manifest.get("enqueuedAt") or timestamp(),
        "stepCount": len(redacted_atif.get("steps", [])),
        "model": (redacted_atif.get("agent") or {}).get("model_name"),
        "atifPath": str(atif_path.relative_to(config.repo_root)),
        "atifBytes": len(atif_bytes),
        "atifSha256": digest(atif_bytes),
        "transcriptPath": str(markdown_path.relative_to(config.repo_root)),
        "transcriptBytes": len(markdown_bytes),
        "transcriptSha256": digest(markdown_bytes),
        "redactions": redaction_summary,
        "captureProfile": config.capture_profile,
        "capturedThroughLine": extra.get("captured_through_line"),
        "capturedBytes": extra.get("captured_bytes"),
        "sourcePrefixSha256": extra.get("source_prefix_sha256"),
        "omissions": extra.get("omissions", {}),
    }
    index_path, index_bytes, index_changed = update_index(config.repo_root, entry)
    changed = (
        not atif_path.exists()
        or atif_path.read_bytes() != atif_bytes
        or not markdown_path.exists()
        or markdown_path.read_bytes() != markdown_bytes
        or index_changed
    )
    if changed:
        atomic_write(atif_path, atif_bytes)
        atomic_write(markdown_path, markdown_bytes)
        if index_changed:
            atomic_write(index_path, index_bytes)
    return ArchiveResult(
        changed=changed,
        title=f"{source} trajectory: {safe_title}",
        paths=(atif_path, markdown_path, index_path),
        redactions=tuple(redaction_summary),
    )


def drain(path: Path) -> List[ArchiveResult]:
    config = load(path)
    results: List[ArchiveResult] = []
    with pipeline_lock(config.repo_root):
        if config.sync_before_capture:
            sync_checkout(config)
            config = load(path)
        spool = state_dir(config.repo_root) / "spool"
        manifests = sorted(spool.glob("*.json")) if spool.exists() else []
        generated: List[Path] = []
        titles = []
        processed = []
        for manifest_path in manifests:
            try:
                manifest = json.loads(manifest_path.read_text())
                result = archive(config, manifest)
                results.append(result)
                if result.changed:
                    generated.extend(result.paths)
                    titles.append(result.title)
                processed.append(manifest_path)
            except Exception as exc:
                error_log(config.repo_root, f"{manifest_path.name} failed: {type(exc).__name__}: {exc}")
        if generated and config.auto_commit:
            commit(config, generated, titles[0] if len(titles) == 1 else f"{len(titles)} agent trajectories")
        for manifest_path in processed:
            manifest_path.unlink(missing_ok=True)
        if config.auto_push and not push(config):
            raise RuntimeError("trajectory archived locally; publishing deferred, inspect archive error log")
    return results
