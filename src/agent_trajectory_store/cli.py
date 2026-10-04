from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from .config import SUPPORTED_AGENTS, initialize, load
from .installer import install, locations
from .pipeline import drain, hook


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="agent-trajectory-store")
    commands = value.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Opt a Git repository into trajectory storage")
    init.add_argument("--repo", default=".")
    init.add_argument("--agents", nargs="+", choices=SUPPORTED_AGENTS, default=list(SUPPORTED_AGENTS))
    init.add_argument("--branch", default="main")
    init.add_argument("--no-auto-commit", action="store_true")
    init.add_argument("--auto-push", action="store_true")

    hooks = commands.add_parser("install-hooks", help="Install global lifecycle hooks")
    hooks.add_argument("--agents", nargs="+", choices=SUPPORTED_AGENTS, default=list(SUPPORTED_AGENTS))
    hooks.add_argument("--home")
    hooks.add_argument("--dry-run", action="store_true")

    hook_command = commands.add_parser("hook", help="Receive a lifecycle hook payload on stdin")
    hook_command.add_argument("--agent", choices=SUPPORTED_AGENTS, required=True)

    drain_command = commands.add_parser("drain", help="Process pending trajectory work")
    drain_command.add_argument("--repo", default=".")

    bind = commands.add_parser("bind", help="Associate an explicit session with an opted-in archive checkout")
    bind.add_argument("--agent", choices=SUPPORTED_AGENTS, required=True)
    bind.add_argument("--session", required=True)
    bind.add_argument("--repo", required=True)

    project = commands.add_parser("bind-project", help="Capture current and future interactive Codex chats started in a project root")
    project.add_argument("--project", required=True)
    project.add_argument("--repo", required=True, help="Dedicated archive checkout")
    project.add_argument("--secret-file", action="append", default=[])

    watch = commands.add_parser("watch-bindings", help="Archive explicitly bound Codex chats at completed turns")
    watch.add_argument("--once", action="store_true")
    watch.add_argument("--interval", type=float, default=30)

    doctor = commands.add_parser("doctor", help="Inspect repository and global-hook setup")
    doctor.add_argument("--repo", default=".")
    doctor.add_argument("--home")
    return value


def main() -> int:
    args = parser().parse_args()
    if args.command == "init":
        config = initialize(
            Path(args.repo),
            args.agents,
            auto_commit=not args.no_auto_commit,
            auto_push=args.auto_push,
            branch=args.branch,
        )
        print(json.dumps(dataclasses.asdict(config), default=str, indent=2))
        return 0
    if args.command == "install-hooks":
        changed = install(
            args.agents,
            Path(args.home).resolve() if args.home else None,
            dry_run=args.dry_run,
        )
        print(json.dumps([str(path) for path in changed], indent=2))
        if "codex" in args.agents and not args.dry_run:
            print("Codex requires reviewing the installed hook with /hooks before it runs.", file=sys.stderr)
        return 0
    if args.command == "bind":
        from .bindings import bind
        print(json.dumps(bind(args.agent, args.session, Path(args.repo)), indent=2))
        return 0
    if args.command == "bind-project":
        from .projects import bind_project
        print(json.dumps(bind_project(Path(args.project), Path(args.repo), args.secret_file), indent=2))
        return 0
    if args.command == "watch-bindings":
        from .watcher import watch, watch_once
        if args.once:
            report = watch_once()
            print(json.dumps(report, indent=2))
            return int(any(item["status"] == "error" for item in report))
        watch(args.interval)
        return 0
    if args.command == "hook":
        payload = json.load(sys.stdin)
        hook(args.agent, payload)
        if payload.get("hook_event_name") in {"Stop", "Interrupt"}:
            print("{}")
        return 0
    if args.command == "drain":
        results = drain(Path(args.repo))
        print(json.dumps([dataclasses.asdict(result) for result in results], default=str))
        return 0
    hook_paths = locations(Path(args.home).resolve() if args.home else None)
    try:
        config = load(Path(args.repo))
        report = {
            "initialized": True,
            "repository": str(config.repo_root),
            "config": str(config.repo_root / ".devin" / "trajectory-store.json"),
            "agents": list(config.agents),
            "hooks": {agent: {"path": str(path), "exists": path.exists()} for agent, path in hook_paths.items()},
        }
    except (FileNotFoundError, RuntimeError) as exc:
        report = {
            "initialized": False,
            "repository": str(Path(args.repo).resolve()),
            "error": str(exc),
            "hooks": {agent: {"path": str(path), "exists": path.exists()} for agent, path in hook_paths.items()},
        }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
