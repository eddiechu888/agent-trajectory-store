from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class AdapterInput:
    source: str
    session_id: str
    transcript_path: Optional[Path]
    cwd: Path
    model: Optional[str]
    payload: Dict[str, Any]


@dataclass(frozen=True)
class AdapterOutput:
    title: str
    atif: Dict[str, Any]


class AdapterError(RuntimeError):
    pass
