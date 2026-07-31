from .base import AdapterError, AdapterInput, AdapterOutput
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .devin import DevinAdapter


ADAPTERS = {
    "claude-code": ClaudeAdapter(),
    "codex": CodexAdapter(),
    "devin": DevinAdapter(),
}


__all__ = ["ADAPTERS", "AdapterError", "AdapterInput", "AdapterOutput"]
