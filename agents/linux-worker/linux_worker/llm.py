"""OpenAI-compatible chat completions with tool calls. The API key is not logged."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from linux_worker.redact import redact


class LLMError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"模型接口错误 HTTP {status}: {message}")
        self.status = status


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, max_output: int, secrets: list[str]):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.max_output = max_output
        self.secrets = [api_key, *secrets]

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0,
            "max_tokens": self.max_output,
        }
        data = json.dumps(payload, ensure_ascii=False).encode()
        request = Request(
            self.url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": "Bearer " + self.api_key,
                "User-Agent": "linux-worker/0.1",
            },
        )
        try:
            with urlopen(request, timeout=120) as response:
                body = json.loads(response.read().decode())
        except HTTPError as error:
            with error:
                raw = error.read().decode(errors="replace")
            raise LLMError(error.code, redact(raw[:500], self.secrets)) from None
        except (URLError, TimeoutError, ConnectionError) as error:
            reason = getattr(error, "reason", error)
            raise LLMError(0, "网络连接失败：" + redact(str(reason), self.secrets)) from None
        if not isinstance(body, dict) or not body.get("choices"):
            raise LLMError(0, "模型响应缺少 choices")
        return body
