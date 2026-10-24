"""Persist the Hub bearer token and team cursor. Mode 0600, no symlinks."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class StateError(Exception):
    pass


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.token = ""
        self.agent_id = ""
        self.session_id = ""
        self.seat = None
        self.cursor = 0
        self.handled: dict[str, str] = {}
        self.announced = False
        self.progress_n = 0

    def load(self) -> "State":
        path = self.path
        if not path.exists():
            return self
        if path.is_symlink():
            raise StateError("状态文件不能是符号链接")
        mode = path.stat().st_mode
        if os.name != "nt" and mode & 0o077:
            raise StateError("状态文件权限过宽，请设置为 0600")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise StateError("状态文件格式不正确")
        self.token = str(data.get("token") or "")
        self.agent_id = str(data.get("agent_id") or "")
        self.session_id = str(data.get("session_id") or "")
        seat = data.get("seat")
        self.seat = seat if isinstance(seat, int) and not isinstance(seat, bool) else None
        cursor = data.get("cursor", 0)
        self.cursor = cursor if isinstance(cursor, int) and not isinstance(cursor, bool) and cursor >= 0 else 0
        handled = data.get("handled") or {}
        self.handled = {str(key): str(value) for key, value in handled.items()} if isinstance(handled, dict) else {}
        self.announced = bool(data.get("announced"))
        progress_n = data.get("progress_n", 0)
        self.progress_n = progress_n if isinstance(progress_n, int) and progress_n >= 0 else 0
        return self

    def save(self) -> "State":
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.is_symlink():
            raise StateError("状态文件不能是符号链接")
        try:
            if path.parent.stat().st_uid == os.getuid():
                os.chmod(path.parent, 0o700)
        except OSError:
            pass
        payload = {
            "token": self.token,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "seat": self.seat,
            "cursor": self.cursor,
            "handled": self.handled,
            "announced": self.announced,
            "progress_n": self.progress_n,
        }
        fd, name = tempfile.mkstemp(prefix=".state-", dir=str(path.parent))
        try:
            os.chmod(name, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(payload, output, ensure_ascii=False)
            os.chmod(name, 0o600)
            os.replace(name, path)
            os.chmod(path, 0o600)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return self
