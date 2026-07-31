from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, List, Tuple


@dataclass(frozen=True)
class SecretPattern:
    kind: str
    pattern: re.Pattern
    secret_group: int = 0


PATTERNS = (
    SecretPattern(
        "private-key",
        re.compile(
            r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----.*?-----END (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    SecretPattern("openai-api-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    SecretPattern("anthropic-api-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    SecretPattern("stripe-secret-key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}\b")),
    SecretPattern("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    SecretPattern("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    SecretPattern("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    SecretPattern("huggingface-token", re.compile(r"\bhf_[A-Za-z0-9]{20,}\b")),
    SecretPattern("tailscale-key", re.compile(r"\btskey-(?:auth|client|api)-[A-Za-z0-9_-]{20,}\b")),
    SecretPattern("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b")),
    SecretPattern("pypi-token", re.compile(r"\bpypi-[A-Za-z0-9_-]{20,}\b")),
    SecretPattern("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    SecretPattern("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    SecretPattern(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
    SecretPattern(
        "bearer-token",
        re.compile(r"(?i)(Authorization\s*:\s*Bearer\s+)([A-Za-z0-9._~+/=-]{12,})"),
        2,
    ),
    SecretPattern(
        "database-credential",
        re.compile(r"(?i)\b((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s:/@]+:)([^\s/@]+)(@)"),
        2,
    ),
    SecretPattern(
        "contextual-secret",
        re.compile(
            r"(?i)(\b(?:[A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)|api[_ -]?key|access[_ -]?token|auth[_ -]?token|refresh[_ -]?token|client[_ -]?secret|password|passwd|secret)\b\s*[:=]\s*[\"'`]?)(?!(?:placeholder|example|redacted|none|null|changeme)\b)([^\s\"'`,;)}\]]{4,})"
        ),
        2,
    ),
)


def marker(kind: str, secret: str) -> str:
    digest = hashlib.sha256(secret.encode()).hexdigest()[:8]
    return f"[REDACTED:{kind}:{digest}]"


def redact_text(text: str) -> Tuple[str, List[dict]]:
    nested = re.compile(r"\[REDACTED:([a-z0-9-]+):\[REDACTED:[a-z0-9-]+:([0-9a-f]{8})\]\]")
    value = text
    while nested.search(value):
        value = nested.sub(r"[REDACTED:\1:\2]", value)
    markers = re.compile(r"\[REDACTED:[^\]]+\]")
    findings: List[dict] = []
    for secret_pattern in PATTERNS:
        protected = {}

        def protect(match: re.Match) -> str:
            placeholder = f",ATS_REDACTION_{len(protected)},"
            protected[placeholder] = match.group(0)
            return placeholder

        value = markers.sub(protect, value)

        def replace(match: re.Match) -> str:
            secret = match.group(secret_pattern.secret_group)
            replacement = marker(secret_pattern.kind, secret)
            findings.append({"kind": secret_pattern.kind, "marker": replacement})
            if secret_pattern.secret_group == 0:
                return replacement
            start, end = match.span(secret_pattern.secret_group)
            relative_start = start - match.start()
            relative_end = end - match.start()
            matched = match.group(0)
            return matched[:relative_start] + replacement + matched[relative_end:]

        value = secret_pattern.pattern.sub(replace, value)
        for placeholder, replacement in protected.items():
            value = value.replace(placeholder, replacement)
    return value, findings


def redact(value: Any) -> Tuple[Any, List[dict]]:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        output = []
        findings: List[dict] = []
        for item in value:
            redacted, item_findings = redact(item)
            output.append(redacted)
            findings.extend(item_findings)
        return output, findings
    if isinstance(value, dict):
        output = {}
        findings = []
        for key, item in value.items():
            redacted, item_findings = redact(item)
            output[key] = redacted
            findings.extend(item_findings)
        return output, findings
    return value, []


def remaining_secret_kinds(value: Any) -> List[str]:
    serialized = json.dumps(value, ensure_ascii=False)
    serialized = re.sub(r"\[REDACTED:[^\]]+\]", "", serialized)
    return sorted({pattern.kind for pattern in PATTERNS if pattern.pattern.search(serialized)})


def summarize(findings: Iterable[dict]) -> List[dict]:
    counts = Counter(finding["kind"] for finding in findings)
    return [{"kind": kind, "count": counts[kind]} for kind in sorted(counts)]
