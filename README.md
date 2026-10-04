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

## Product conversations anchored in another repository

Use explicit bindings when a development chat runs from an operating repository
but its conversation belongs with the product code. This does not guess project
membership from paths mentioned in a transcript.

An opted-in product can restrict capture to named sessions in
`.devin/trajectory-store.json`:

```json
{
  "schemaVersion": 1,
  "enabled": true,
  "expectedOrigin": "https://github.com/example/private-product.git",
  "branch": "main",
  "autoCommit": true,
  "autoPush": true,
  "agents": ["codex"],
  "captureProfile": "development-dialogue",
  "sessions": {"thread-id": "Product design"},
  "syncBeforeCapture": true,
  "settleSeconds": 0
}
```

Bind each source session to a **dedicated archive checkout**, then backfill:

```sh
ats bind --agent codex --session thread-id --repo /path/to/archive-checkout
ats watch-bindings --once
```

Bindings are local host configuration in
`~/.config/agent-trajectory-store/bindings.json` (override with
`ATS_BINDINGS_FILE`). Only session IDs/titles and capture policy are committed in
the product. Hook routing honors explicit bindings before the source cwd, and the
repository session allowlist also rejects accidental path-based bridge dispatch.

The `development-dialogue` profile streams Codex JSONL and preserves human messages
and assistant commentary/final text, timestamps, original source line numbers,
turn IDs, and a hash of the captured source prefix. It includes proposals,
corrections, and rejected ideas without summarizing them. Known injected system,
project, browser and heartbeat context is omitted. Entire scheduled heartbeat
turns, reasoning, attachment bytes and raw tool arguments/observations are omitted;
tool identities and hashed payload references plus omission counts remain in ATIF.
Credential redaction still runs over retained dialogue, including private Tailscale
capability links. A local binding may optionally specify `secretFiles` containing
single literal values for additional redaction; values never enter the registry or
archive. The archive is evidence of discussions, not proof every proposal shipped.
This profile currently requires Codex source logs with matching session identity.

`watch-bindings --interval 30` runs a local archiver that checks only explicitly
bound Codex files. It tails complete records and publishes only through the last
`task_complete` or `turn_aborted` boundary. Active work appears after completion or
interruption; an aborted turn is labeled as such. Resume extends the same indexed
conversation. An optional launchd/systemd service can keep this command running;
the host must be online for capture and publication. This service reads local logs
and does not control Codex or add model context. Existing lifecycle hooks remain a
backstop; no hook trust bypass is necessary. `Stop`/`Interrupt` payloads are also
accepted if a separately reviewed hook is configured for them.

Only enable `syncBeforeCapture` in an archive checkout you dedicate to the store.
It checks pinned origin, branch and clean state, fast-forwards upstream, and rebases
only previously generated trajectory commits. Dirty state, non-trajectory local
commits or conflicts stop synchronization. Publication still rejects every
unpushed non-trajectory path. Network/commit failures preserve source logs and
pending/local archive state; `watch-state.json` and the Git-local error log report
failures. Initial configuration/policy changes should follow the project's normal
review workflow before automatic trajectory-only updates are enabled.

## Automatically capture a project's new chats (0.3.0)

For projects whose owners approve archiving all ordinary development chats started
in the project folder, add `"captureProjectSessions": true` to the reviewed
repository configuration. Keep `captureProfile: "development-dialogue"` and
`agents: ["codex"]`. Existing `sessions` entries still explicitly authorize
cross-repository chats; project discovery does not edit that allowlist.

Register the source project and a separate, dedicated archive checkout locally:

```sh
ats bind-project --project /path/to/product --repo /path/to/archive-checkout
ats watch-bindings --once
ats watch-bindings --interval 30
```

The existing watcher now discovers both current and future chats from Codex's
local `session_meta` records. Only an exact initial working-directory match and
an interactive `vscode` or `cli` source qualify. Nested runtime directories,
noninteractive `exec` sessions, subagents, forked histories, and chats that merely
mention the project are excluded. A separate worktree needs its own
`bind-project` registration. The source and archive origins must both match the
repository's pinned origin. A missing/disabled policy or changed origin stops
project discovery with an error; it never broadens capture to unrelated sessions.

Titles come from the local `session_index.jsonl` when available, falling back to
the first human message. Discovered sessions are not written to the local explicit
session list or the repository configuration. Only their sanitized generated
records/index are published. The watcher reports `waiting-for-completed-turn` for
a new active chat, then backfills after its first completed/interrupted turn.
Run the watcher under launchd/systemd for persistent capture on each source host;
cloning the repository alone does not install a service or expose another host's
source logs.

Local one-value secret files can be applied to every discovered project chat with
repeatable `--secret-file /private/key-file` arguments to `bind-project`. Only
their paths are saved locally, and re-registering preserves existing filters.
Known credential patterns and Bitwarden Send capability links are redacted in
dialogue, titles, indexes and commit subjects. This remains defense in depth;
review an initial backfill before allowing it to publish. Raw source logs and
private acceptance evidence stay outside Git.
