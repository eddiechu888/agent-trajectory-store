from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path
from typing import Iterable

from .config import StoreConfig, git


def relative_paths(root: Path, paths: Iterable[Path]) -> list:
    values = []
    for path in paths:
        resolved = path.resolve()
        try:
            relative = str(resolved.relative_to(root.resolve()))
        except ValueError as exc:
            raise ValueError(f"generated path is outside repository: {path}") from exc
        values.append(relative)
    return sorted(set(values))


def commit(config: StoreConfig, paths: Iterable[Path], title: str) -> bool:
    relative = relative_paths(config.repo_root, paths)
    if not relative:
        return False
    git(config.repo_root, "add", "--", *relative)
    changed = git(config.repo_root, "diff", "--cached", "--quiet", "--", *relative, check=False)
    if changed.returncode == 0:
        return False
    if changed.returncode != 1:
        raise RuntimeError(changed.stderr.strip() or "failed to inspect generated changes")
    message_path = _git_path(config.repo_root, "agent-trajectory-store-commit.txt")
    message_path.write_text(
        f"Archive {title}\n\n"
        "Preserve a sanitized coding-agent trajectory in structured ATIF and readable Markdown formats.\n"
    )
    try:
        git(config.repo_root, "commit", "--only", "-F", str(message_path), "--", *relative, timeout=120)
    finally:
        message_path.unlink(missing_ok=True)
    committed = git(config.repo_root, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").stdout.splitlines()
    if any(path not in relative for path in committed):
        raise RuntimeError("generated commit contains an unexpected path")
    return True


def error_log(root: Path, message: str) -> None:
    path = _git_path(root, "agent-trajectory-store-errors.log")
    now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    with path.open("a") as handle:
        handle.write(f"{now} {message}\n")


def push(config: StoreConfig) -> bool:
    root = config.repo_root
    remote = git(root, "remote", "get-url", "origin", check=False).stdout.strip()
    if remote != config.expected_origin:
        error_log(root, "push skipped: origin URL does not match repository configuration")
        return False
    branch = git(root, "symbolic-ref", "--short", "HEAD", check=False).stdout.strip()
    if branch != config.branch:
        error_log(root, f"push skipped: active branch is {branch or 'detached'}, expected {config.branch}")
        return False
    fetched = git(root, "fetch", "origin", config.branch, check=False, timeout=60)
    if fetched.returncode != 0:
        error_log(root, "push skipped: fetch failed")
        return False
    upstream = f"origin/{config.branch}"
    if git(root, "merge-base", "--is-ancestor", upstream, "HEAD", check=False).returncode != 0:
        error_log(root, "push skipped: upstream is not an ancestor of HEAD")
        return False
    commits = git(root, "rev-list", f"{upstream}..HEAD").stdout.splitlines()
    for revision in commits:
        paths = git(root, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", revision).stdout.splitlines()
        if any(not path.startswith("trajectories/") for path in paths):
            error_log(root, f"push skipped: unpushed commit {revision[:12]} changes non-trajectory paths")
            return False
    if not commits:
        return True
    result = git(root, "push", "origin", config.branch, check=False, timeout=120)
    if result.returncode != 0:
        error_log(root, "push failed")
        return False
    return True


def _git_path(root: Path, name: str) -> Path:
    value = Path(git(root, "rev-parse", "--git-path", name).stdout.strip())
    return value if value.is_absolute() else root / value


def sync_checkout(config: StoreConfig) -> None:
    """Only explicit dedicated checkouts sync; dirty or divergent state fails closed."""
    root = config.repo_root
    remote = git(root, "remote", "get-url", "origin").stdout.strip()
    branch = git(root, "symbolic-ref", "--short", "HEAD").stdout.strip()
    if remote != config.expected_origin or branch != config.branch:
        raise RuntimeError("archive checkout origin/branch mismatch")
    if git(root, "status", "--porcelain").stdout.strip():
        raise RuntimeError("archive checkout is dirty; refusing automatic sync")
    git(root, "fetch", "origin", config.branch, timeout=60)
    upstream = f"origin/{config.branch}"
    # Offline capture may have left generated-only commits. Rebase only those.
    for revision in git(root, "rev-list", f"{upstream}..HEAD").stdout.splitlines():
        paths = git(root, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", revision).stdout.splitlines()
        if any(not path.startswith("trajectories/") for path in paths):
            raise RuntimeError("unpushed non-trajectory commit; refusing automatic sync")
    if git(root, "merge-base", "--is-ancestor", "HEAD", upstream, check=False).returncode == 0:
        git(root, "merge", "--ff-only", upstream)
    elif git(root, "merge-base", "--is-ancestor", upstream, "HEAD", check=False).returncode != 0:
        result = git(root, "rebase", upstream, check=False, timeout=120)
        if result.returncode:
            git(root, "rebase", "--abort", check=False)
            raise RuntimeError("archive rebase conflict; retained local capture for review")
