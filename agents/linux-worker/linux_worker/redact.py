"""Strip obvious secrets before text is logged, shown to the model, or posted."""

from __future__ import annotations

import logging
import re

_PATTERNS = (
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        re.S,
    ),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"\bda_[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization)\b\s*[=:]\s*\S+"
    ),
)


def redact(text: str, secrets: list[str] | None = None) -> str:
    value = "" if text is None else str(text)
    for secret in secrets or []:
        if isinstance(secret, str) and len(secret) >= 6 and secret in value:
            value = value.replace(secret, "[REDACTED]")
    for pattern in _PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value


class RedactingFilter(logging.Filter):
    """Apply redact() to formatted log lines. Tracebacks do not include locals."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [item for item in secrets if isinstance(item, str) and len(item) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), self.secrets)
        record.args = ()
        return True
