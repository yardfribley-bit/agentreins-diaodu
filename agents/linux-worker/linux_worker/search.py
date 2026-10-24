"""Configurable web search: plain HTTP, or an OpenAI-style POST /search."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from linux_worker.redact import redact


class SearchError(Exception):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _clip(text: str, limit: int = 400) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _results_from_json(payload: dict, limit: int) -> list[dict]:
    rows = payload.get("results")
    if not isinstance(rows, list):
        rows = payload.get("items")
    if isinstance(rows, list):
        found = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            found.append(
                {
                    "title": _clip(str(row.get("title") or row.get("name") or ""), 160),
                    "url": str(row.get("url") or row.get("href") or row.get("link") or ""),
                    "snippet": _clip(str(row.get("snippet") or row.get("content") or row.get("body") or "")),
                }
            )
            if len(found) >= limit:
                break
        if found:
            return found
    # DuckDuckGo instant-answer JSON.
    found = []
    abstract = str(payload.get("AbstractText") or "")
    if abstract:
        found.append(
            {
                "title": _clip(str(payload.get("Heading") or "摘要"), 160),
                "url": str(payload.get("AbstractURL") or ""),
                "snippet": _clip(abstract),
            }
        )

    def walk(nodes):
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if isinstance(node.get("Topics"), list):
                walk(node["Topics"])
                continue
            text = str(node.get("Text") or "")
            if text:
                found.append(
                    {
                        "title": _clip(text.split(" - ", 1)[0], 160),
                        "url": str(node.get("FirstURL") or ""),
                        "snippet": _clip(text),
                    }
                )

    walk(payload.get("RelatedTopics"))
    for row in payload.get("Results") or []:
        if isinstance(row, dict) and row.get("Text"):
            found.append(
                {
                    "title": _clip(str(row.get("Text")), 160),
                    "url": str(row.get("FirstURL") or ""),
                    "snippet": _clip(str(row.get("Text"))),
                }
            )
    if found:
        return found[:limit]
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else ""
        if content:
            return [{"title": "", "url": "", "snippet": _clip(str(content), 1200)}]
    return []


def _html_snippet(body: str) -> list[dict]:
    text = []
    skip = False
    for char in body:
        if char == "<":
            skip = True
            continue
        if char == ">":
            skip = False
            text.append(" ")
            continue
        if not skip:
            text.append(char)
    snippet = _clip("".join(text), 800)
    if not snippet:
        return []
    return [{"title": "", "url": "", "snippet": snippet}]


class SearchClient:
    def __init__(self, config, secrets: list[str]):
        self.config = config
        self.secrets = secrets
        self.opener = build_opener(_NoRedirect())

    def search(self, query: str, limit: int = 5) -> list[dict]:
        query = " ".join(str(query or "").split())
        if not query:
            raise SearchError("查询不能为空")
        if len(query) > 300:
            raise SearchError("查询过长")
        limit = max(1, min(int(limit), 8))
        if self.config.search_backend == "openai":
            return self._openai(query, limit)
        return self._http(query, limit)

    def _http(self, query: str, limit: int) -> list[dict]:
        url = self.config.search_url.replace("{query}", quote(query))
        self._check_url(url)
        headers = {"Accept": "application/json, text/html;q=0.5", "User-Agent": "linux-worker/0.1"}
        if self.config.search_api_key:
            headers["Authorization"] = "Bearer " + self.config.search_api_key
        request = Request(url, headers=headers)
        payload, content_type = self._read(request)
        if "json" in content_type or payload[:1] in (b"{", b"["):
            try:
                parsed = json.loads(payload.decode())
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SearchError("搜索结果不是合法 JSON") from exc
            if isinstance(parsed, dict):
                return _results_from_json(parsed, limit)
            if isinstance(parsed, list):
                return _results_from_json({"results": parsed}, limit)
        return _html_snippet(payload.decode(errors="replace"))[:limit]

    def _openai(self, query: str, limit: int) -> list[dict]:
        base = self.config.search_base.rstrip("/")
        url = base if base.endswith("/search") else base + "/search"
        self._check_url(url)
        key = self.config.search_api_key or self.config.llm_api_key
        body = json.dumps(
            {"model": self.config.search_model, "query": query, "limit": limit},
            ensure_ascii=False,
        ).encode()
        request = Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": "Bearer " + key,
                "User-Agent": "linux-worker/0.1",
            },
        )
        payload, _content_type = self._read(request)
        try:
            parsed = json.loads(payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SearchError("搜索结果不是合法 JSON") from exc
        if not isinstance(parsed, dict):
            raise SearchError("搜索结果格式不正确")
        return _results_from_json(parsed, limit)

    def _check_url(self, url: str) -> None:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        local = host in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme == "https" or (parsed.scheme == "http" and local):
            return
        raise SearchError("搜索地址必须使用 HTTPS；HTTP 仅允许本机")

    def _read(self, request: Request) -> tuple[bytes, str]:
        try:
            with self.opener.open(request, timeout=20) as response:
                content_type = response.headers.get("Content-Type", "")
                data = response.read(256 * 1024)
                return data, content_type
        except HTTPError as error:
            with error:
                detail = error.read(400).decode(errors="replace")
            raise SearchError("搜索失败 HTTP " + str(error.code) + " " + redact(detail, self.secrets)) from None
        except (URLError, TimeoutError, ConnectionError) as error:
            reason = getattr(error, "reason", error)
            raise SearchError("搜索连接失败：" + redact(str(reason), self.secrets)) from None
