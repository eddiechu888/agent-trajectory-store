"""Opt-in discovery from source session metadata, never from dialogue path mentions."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .bindings import read_registry, write_registry
from .config import git, load, repo_root

_metadata_cache = {}


def codex_home():
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def session_metadata(path):
    path = Path(path)
    stat = path.stat()
    identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    cached = _metadata_cache.get(path)
    if cached and cached[0] == identity:
        return cached[1]
    with path.open("rb") as handle:
        raw = handle.readline(2 * 1024 * 1024)
    if not raw.endswith(b"\n"):
        return {}  # New/incomplete or oversized metadata is not eligible.
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(record, dict):
        return {}
    value = record.get("payload") if record.get("type") == "session_meta" else None
    if not isinstance(value, dict):
        return {}
    # Cache only routing fields, never injected instructions or other context.
    result = {key: value.get(key) for key in ("id", "cwd", "source", "forked_from_id")}
    _metadata_cache[path] = (identity, result)
    return result


def bind_project(project, repository, secret_files=()):
    config = load(repository)
    if not config.capture_project_sessions:
        raise ValueError("target repository has not opted into captureProjectSessions")
    root = repo_root(Path(project))
    if root == config.repo_root:
        raise ValueError("project capture requires a separate dedicated archive checkout")
    if git(root, "remote", "get-url", "origin").stdout.strip() != config.expected_origin:
        raise ValueError("project origin does not match the archive configuration")
    for filename in secret_files:
        if not Path(filename).is_file():
            raise ValueError("configured secret file is not readable")
    registry = read_registry()
    entries = registry.get("projects", [])
    previous = next((p for p in entries if p["projectRoot"] == str(root)), {})
    entry = dict(previous, source="codex", projectRoot=str(root), repository=str(config.repo_root),
                 expectedOrigin=config.expected_origin)
    if secret_files:
        entry["secretFiles"] = [str(Path(p).resolve()) for p in secret_files]
    registry["projects"] = [p for p in entries if p["projectRoot"] != str(root)] + [entry]
    write_registry(registry)
    return entry


def validate_project(project):
    config = load(Path(project["repository"]))
    if (not config.capture_project_sessions or project.get("source") != "codex"
            or project["expectedOrigin"] != config.expected_origin):
        raise RuntimeError("project capture policy/origin changed; refusing discovery")
    root = Path(project["projectRoot"])
    if repo_root(root) != root.resolve():
        raise RuntimeError("registered project is no longer a repository root")
    if git(root, "remote", "get-url", "origin").stdout.strip() != config.expected_origin:
        raise RuntimeError("registered project origin changed; refusing discovery")
    if git(config.repo_root, "remote", "get-url", "origin").stdout.strip() != config.expected_origin:
        raise RuntimeError("archive origin changed; refusing discovery")
    return config


def matches(project, metadata, session_id):
    # Exact initial cwd only: nested runtime dirs, other repos, exec sessions,
    # subagents and forks carrying potentially unrelated history are not included.
    return (metadata.get("id") == session_id
            and isinstance(metadata.get("source"), str)
            and metadata["source"] in {"vscode", "cli"}
            and not metadata.get("forked_from_id")
            and isinstance(metadata.get("cwd"), str)
            and Path(metadata["cwd"]).is_absolute()
            and Path(metadata["cwd"]).resolve() == Path(project["projectRoot"]).resolve())


def project_binding_for(source, session_id, transcript):
    if source != "codex" or transcript is None:
        return None
    metadata = session_metadata(transcript)
    found = []
    for project in read_registry().get("projects", []):
        if matches(project, metadata, session_id):
            validate_project(project)
            found.append(dict(project, sessionId=session_id, transcriptPath=str(transcript)))
    if len(found) > 1:
        raise RuntimeError("ambiguous project capture binding")
    return found[0] if found else None


def session_titles():
    titles = {}
    path = codex_home() / "session_index.jsonl"
    if path.exists():
        with path.open() as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if value.get("id") and isinstance(value.get("thread_name"), str):
                    titles[value["id"]] = value["thread_name"]
    return titles


def discover_projects():
    projects = read_registry().get("projects", [])
    errors = []
    valid = []
    for project in projects:
        try:
            validate_project(project)
            valid.append(project)
        except Exception as exc:
            errors.append({"projectRoot": project.get("projectRoot"), "status": "error",
                           "error": f"{type(exc).__name__}: {exc}"})
    if not valid:
        return [], errors
    titles = session_titles()
    entries = {}
    for path in sorted((codex_home() / "sessions").glob("**/*.jsonl")):
        metadata = session_metadata(path)
        sid = metadata.get("id")
        for project in valid:
            if not sid or not matches(project, metadata, sid):
                continue
            if sid in entries:
                raise RuntimeError("ambiguous project session source or binding")
            entries[sid] = dict(project, sessionId=sid, transcriptPath=str(path), title=titles.get(sid))
    return list(entries.values()), errors
