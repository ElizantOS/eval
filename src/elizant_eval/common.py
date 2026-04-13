from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote


class EvalConfigError(RuntimeError):
    """Raised when eval configuration or runtime wiring is invalid."""


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def workspace_dir() -> Path:
    raw = str(os.environ.get("SMARTBOT_EVAL_WORKSPACE_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    cwd = Path.cwd().resolve()
    if (cwd / "config").exists() and (cwd / "cases").exists():
        return cwd
    if (cwd / "evals" / "config").exists() and (cwd / "evals" / "cases").exists():
        return (cwd / "evals").resolve()
    raise EvalConfigError("SMARTBOT_EVAL_WORKSPACE_DIR missing and workspace could not be inferred")


def smartbot_dir() -> Path:
    raw = str(os.environ.get("SMARTBOT_EVAL_SMARTBOT_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return workspace_dir().parent


def default_live_case_file() -> Path:
    return workspace_dir() / ".promptfoo" / "live-case.json"


def serialize_x_user_info(value: dict[str, Any] | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        raise EvalConfigError("identity must be an object or string")
    return quote(json.dumps(value, separators=(",", ":"), ensure_ascii=False), safe="")
