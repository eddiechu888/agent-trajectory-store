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
    if args.command == "hook":
        payload = json.load(sys.stdin)
        hook(args.agent, payload)
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
