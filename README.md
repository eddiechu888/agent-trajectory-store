# Agent Trajectory Store

Archive local coding-agent sessions as sanitized [ATIF v1.7](https://github.com/harbor-framework/harbor/blob/main/rfcs/0001-trajectory-format.md) and readable Markdown inside the repository they belong to.

Agent Trajectory Store supports:

- [Devin CLI](https://docs.devin.ai/cli)
- [Claude Code](https://code.claude.com/docs/en/hooks)
- [Codex](https://developers.openai.com/codex/hooks)

It installs one global lifecycle hook per agent. Repositories opt in independently through `.devin/trajectory-store.json`; agents never need access to another project that happens to use the store.

> [!WARNING]
> Agent transcripts can contain credentials, private source code, personal information, and complete tool output. The store redacts common credential formats and fails closed when those patterns remain, but no automated scanner can guarantee that every sensitive value will be detected. Review trajectories before publishing a repository.

## Requirements

- macOS or Linux
- Python 3.9+
- Git
- At least one supported coding agent

The runtime has no third-party Python dependencies.

## Install

Until a package release is published, install directly from GitHub with `pipx`:

```bash
pipx install git+https://github.com/eddiechu888/agent-trajectory-store.git
```

Or use a virtual environment:

```bash
python3 -m venv ~/.config/agent-trajectory-store/venv
~/.config/agent-trajectory-store/venv/bin/pip install git+https://github.com/eddiechu888/agent-trajectory-store.git
```

Install global hooks for all supported agents:

```bash
ats install-hooks
```

Codex requires reviewing a new or changed hook once with `/hooks` before it runs.

## Enable a repository

Run from the target repository:

```bash
ats init --auto-push
```

This creates:

```text
.devin/trajectory-store.json
trajectories/index.json
trajectories/README.md
```

Commit those files normally. The recorded `expectedOrigin` prevents the worker from publishing to a different remote if `origin` is later changed.

Choose specific agents if needed:

```bash
ats init --agents claude-code codex --auto-push
```

Inspect the installation:

```bash
ats doctor
```

## Lifecycle

1. A supported agent emits `SessionEnd` with a session ID, working directory, and transcript path when available.
2. The global hook writes a small pointer manifest under the repository's Git metadata and exits quickly. Raw transcripts are never copied into the working tree.
3. A detached worker waits for the transcript to settle, converts the source format to ATIF, redacts secrets recursively, renders Markdown, and performs a second scan.
4. The worker updates `trajectories/index.json` atomically while holding a repository-wide pipeline lock.
5. If enabled, it commits only generated trajectory paths.
6. It pushes only when the active branch and origin match the repository configuration, upstream is an ancestor, and every unpushed commit is trajectory-only.
7. `SessionStart` drains pending work and retries safe pushes after crashes, network failures, or transcript lag.

Codex gives `SessionEnd` hooks at most three seconds, so conversion and Git operations always happen in the detached worker. Claude Code writes transcripts asynchronously; the worker waits for file size and modification time to stabilize before reading them.

## Output

```text
trajectories/
├── index.json
├── devin/
│   ├── 2026-07-30--session-id.atif.json
│   └── 2026-07-30--session-id.md
├── claude-code/
│   └── ...
└── codex/
    └── ...
```

Index identity is `(source, sessionId)`, so sessions from different agents cannot collide. Reprocessing a resumed session replaces its existing files and index entry rather than creating a duplicate.

## Secret handling

Built-in patterns cover private keys and common OpenAI, Anthropic, GitHub, GitLab, Slack, Hugging Face, Tailscale, npm, PyPI, AWS, Google, Stripe, bearer-token, database-credential, and contextual secret formats. Replacements are deterministic markers such as:

```text
[REDACTED:github-token:7b16d19c]
```

The original value is never written to the repository index or an error log. Pending manifests and worker logs live under `.git/agent-trajectory-store/` and are never staged.

## Commands

```text
ats init             Opt a Git repository into trajectory storage
ats install-hooks    Merge global hooks without replacing existing hooks
ats doctor           Inspect repo configuration and hook locations
ats drain            Process pending manifests synchronously
ats hook              Internal lifecycle-hook entry point
```

## Limitations

- Claude Code and Codex explicitly document their local transcript formats as unstable. Their adapters fail visibly when a schema change removes required message records; pending manifests remain available for a corrected adapter.
- Subagent trajectories are currently retained only when the parent transcript embeds them. Separate subagent exports are planned.
- Windows is not supported in v0.1 because the pipeline lock uses `fcntl`.
- Secret scanning is defense in depth, not a substitute for credential rotation or review.

## Development

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

## License

MIT
