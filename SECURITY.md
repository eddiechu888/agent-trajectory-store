# Security

Agent transcripts can contain credentials, private source code, personal data, and tool output. Agent Trajectory Store applies deterministic redaction and refuses to publish files that still match its secret patterns, but no automated scanner can guarantee that every sensitive value will be detected.

Review generated trajectories before making a repository public. Rotate any credential that was exposed to an agent, even when the archived copy was redacted.

Report vulnerabilities privately through GitHub's security-advisory interface. Do not include live credentials or private transcripts in an issue.
