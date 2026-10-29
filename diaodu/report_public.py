"""Owner-approved partner feed for team final reports.

The public conversation directory already serves every listed topic. This
module is the stricter surface a partner pulls: nothing is listed, and the
partner file URLs 404, until the team creator sets public_approved_at.

Credential-shaped text blocks approval. An email in the body does not,
because a published industry report may name a public contact. The approve
call warns the owner instead. The feed object never adds addresses, tokens,
invite codes, or private ids on its own. The owner still has to read the
Markdown and PDF before approving: whatever they leave in the file is what
the partner receives.
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from datetime import datetime, timezone
from email.utils import formatdate, parsedate_to_datetime

HUB_ORIGIN = "https://www.chuhaijian.com"
HUB_PREFIX = "/task"

_EMAIL = re.compile(
    r"(?i)(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]{1,64}"
    r"@[A-Za-z0-9.\-]{1,255}\.[A-Za-z]{2,24}(?![A-Za-z0-9.\-])")
_TOKEN = re.compile(r"\bda_[A-Za-z0-9_\-]{16,}\b")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}")
_PRIVATE_ID = re.compile(r"\b(?:ag|us|tm|fl)_[0-9a-fA-F]{8,}\b")
_INVITE_URL = re.compile(r"/(?:relay/join|relay/watch)/")
_SECRET_WORD = re.compile(
    r"\b(?:actor_key|lease_until|public_token_hash|DIAODU_AGENT_TOKEN|"
    r"DIAODU_PAIR_CODE|DIAODU_TOKEN)\b")

# Stable order. Labels are safe to show the owner; matched text is not.
_BLOCKING = (
    ("访问令牌", (_TOKEN, _BEARER)),
    ("内部编号", (_PRIVATE_ID,)),
    ("邀请或围观链接", (_INVITE_URL,)),
    ("凭证字段", (_SECRET_WORD,)),
)


def canonical_base(public_origin, public_base_path):
    """Absolute origin + prefix. Production is https://www.chuhaijian.com/task."""
    origin = (public_origin or "").rstrip("/")
    prefix = public_base_path or ""
    if not origin:
        origin = HUB_ORIGIN
        if not prefix:
            prefix = HUB_PREFIX
    return origin + prefix


def review(text):
    """Split a deliverable into blocking categories and email warnings.

    The returned labels never include the matched secret itself.
    """
    blob = text if isinstance(text, str) else ""
    blocking = [label for label, patterns in _BLOCKING if any(pattern.search(blob) for pattern in patterns)]
    warnings = []
    if _EMAIL.search(blob):
        warnings.append("正文、标题或摘要里有邮箱地址。批准后这些文字会随 Markdown 和 PDF 原样公开。")
    return blocking, warnings


def clean_line(value, label, limit):
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError(label + "不合法")
    flattened = value.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    cleaned = "".join(char for char in flattened if unicodedata.category(char)[0] != "C").strip()
    if len(cleaned) > limit:
        raise ValueError(label + "不能超过 %d 字" % limit)
    return cleaned


def clean_blurb(value, label, limit):
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError(label + "不合法")
    cleaned = "".join(char for char in value.replace("\r\n", "\n").replace("\r", "\n")
                      if char in "\n\t" or unicodedata.category(char)[0] != "C").strip()
    if len(cleaned) > limit:
        raise ValueError(label + "不能超过 %d 字" % limit)
    return cleaned


def etag_for(body):
    return '"' + hashlib.sha256(body).hexdigest() + '"'


def http_date(stamp):
    return formatdate(float(stamp), usegmt=True)


def _etag_tokens(header):
    tokens = []
    for part in str(header).split(","):
        token = part.strip()
        if token.startswith("W/"):
            token = token[2:].strip()
        if token:
            tokens.append(token)
    return tokens


def not_modified(if_none_match, if_modified_since, etag, stamp):
    """If-None-Match wins when it is present, including a non-match."""
    if if_none_match:
        tokens = _etag_tokens(if_none_match)
        return "*" in tokens or etag in tokens
    if not if_modified_since or not stamp:
        return False
    try:
        when = parsedate_to_datetime(if_modified_since)
    except (TypeError, ValueError, OverflowError):
        return False
    if when is None:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    modified = datetime.fromtimestamp(math.floor(float(stamp)), timezone.utc)
    return when >= modified
