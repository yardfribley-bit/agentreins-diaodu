"""Shared attachment helpers for the one-to-one relay and the team relay.

Only algorithms live here: filename validation, content-type sniffing, persistent
signing keys and HMAC download signatures. Each relay keeps its own database,
file directory and key file, so data stays isolated.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

SIGNED_SECONDS = 15 * 60


def safe_name(value):
    if not isinstance(value, str):
        raise ValueError("文件名不合法")
    name = value.strip()
    if not name or len(name) > 240 or name in (".", ".."):
        raise ValueError("文件名不合法")
    if "/" in name or "\\" in name or any(ord(char) < 32 for char in name):
        raise ValueError("文件名不能包含路径分隔符或控制字符")
    return name


def content_type(name, head):
    extension = os.path.splitext(name)[1].lower()
    if extension == ".png" and head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if extension in (".jpg", ".jpeg") and head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if extension == ".gif" and head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if extension == ".webp" and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if extension in (".mp4", ".m4v") and head[4:8] == b"ftyp":
        return "video/mp4"
    if extension == ".webm" and head.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    if extension in (".txt", ".log", ".md", ".csv", ".json", ".yaml", ".yml") and b"\x00" not in head:
        return "text/plain"
    return "application/octet-stream"


def load_key(path: Path):
    """Persist a 32-byte signing key so old links survive restarts."""
    path = Path(path)
    if path.is_file():
        existing = path.read_bytes()
        if len(existing) >= 32:
            return existing
    key = secrets.token_bytes(32)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, key)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    return key


def sign(key, session_id, file_id, expires_at=None, ttl=SIGNED_SECONDS):
    expires = int(time.time() + ttl) if expires_at is None else int(expires_at)
    message = f"{session_id}:{file_id}:{expires}".encode()
    return expires, hmac.new(key, message, hashlib.sha256).hexdigest()


def verify(key, session_id, file_id, expires, signature):
    if isinstance(expires, bool) or not isinstance(expires, int):
        return False
    message = f"{session_id}:{file_id}:{expires}".encode()
    expected = hmac.new(key, message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def base_url(public_origin, public_base_path):
    return (public_origin.rstrip("/") + (public_base_path or "")) if public_origin else (public_base_path or "")


def download_url(base, file_path, expires, signature):
    return f"{base}{file_path}?exp={expires}&sig={signature}"
