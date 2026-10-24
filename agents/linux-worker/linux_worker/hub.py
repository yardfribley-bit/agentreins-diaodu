"""Small Hub client for connect plus the team relay. No task-board calls."""

from __future__ import annotations

import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from linux_worker.redact import redact


class HubError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class CredentialsError(HubError):
    def __init__(self, message: str):
        super().__init__(401, message)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class HubClient:
    def __init__(self, api_root: str, token: str = "", secrets: list[str] | None = None):
        parsed = urlsplit(api_root)
        if parsed.username or parsed.password:
            raise ValueError("API 地址不能包含凭据")
        self.api_root = api_root.rstrip("/")
        self.token = token
        self.secrets = secrets or []
        self.opener = build_opener(_NoRedirect())
        self.max_retries = 2

    def connect(self, alias: str, agent_type: str) -> dict:
        body = self.request(
            "/connect",
            {"alias": alias, "agent_type": agent_type, "member_kind": "agent"},
            idempotent=False,
        )
        token = body.pop("token", "")
        body.pop("csrf", None)
        if not token:
            raise HubError(0, "接入响应没有凭证")
        self.token = token
        return body

    def me(self) -> dict:
        return self.request("/me")

    def join_team(self, code: str, environment: dict) -> dict:
        return self.request(
            "/relay/teams/join",
            {"code": code, "environment": environment},
            idempotent=False,
        )

    def team_get(self, sid: str) -> dict:
        return self.request("/relay/teams/" + quote(sid, safe=""))

    def team_events(self, sid: str, after: int, wait: int) -> dict:
        query = urlencode({"after": after, "wait": wait})
        return self.request(
            "/relay/teams/" + quote(sid, safe="") + "/events?" + query,
            timeout=max(10, wait + 10),
        )

    def team_send(
        self,
        sid: str,
        kind: str,
        text: str,
        request_id: str,
        reply_to: int | None = None,
        attachments: list[str] | None = None,
    ) -> dict:
        body: dict = {"kind": kind, "text": text, "request_id": request_id}
        if reply_to is not None:
            body["reply_to"] = reply_to
        if attachments:
            body["attachments"] = attachments
        return self.request("/relay/teams/" + quote(sid, safe="") + "/messages", body)

    def team_ack(self, sid: str, seq: int) -> dict:
        return self.request("/relay/teams/" + quote(sid, safe="") + "/ack", {"seq": seq})

    def team_heartbeat(self, sid: str, environment: dict) -> dict:
        return self.request(
            "/relay/teams/" + quote(sid, safe="") + "/heartbeat",
            {"environment": environment},
        )

    def team_upload(self, sid: str, name: str, content: bytes) -> dict:
        path = "/relay/teams/" + quote(sid, safe="") + "/files?name=" + quote(name)
        return self.request(path, raw=content, idempotent=False)

    def request(self, path: str, body=None, raw: bytes | None = None, timeout: int = 30, idempotent: bool = True):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("需要 API 相对路径")
        if raw is not None:
            data = raw
            content_type = "application/octet-stream"
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False).encode()
            content_type = "application/json"
        else:
            data = None
            content_type = "application/json"
        attempt = 0
        while True:
            headers = {
                "Accept": "application/json",
                "Content-Type": content_type,
                "User-Agent": "linux-worker/0.1",
            }
            if self.token:
                headers["Authorization"] = "Bearer " + self.token
            request = Request(self.api_root + path, data=data, headers=headers)
            try:
                with self.opener.open(request, timeout=timeout) as response:
                    payload = response.read()
                    if not payload:
                        return {}
                    return json.loads(payload.decode())
            except HTTPError as error:
                with error:
                    try:
                        parsed = json.loads(error.read().decode())
                        message = str(parsed.get("error") or "请求失败")
                    except (ValueError, AttributeError):
                        message = "HTTP " + str(error.code)
                    status = error.code
                message = redact(message, self.secrets + ([self.token] if self.token else []))
                retry = idempotent and status in (429, 500, 502, 503, 504) and attempt < self.max_retries
                if retry:
                    attempt += 1
                    time.sleep(min(0.2 * attempt, 1.0))
                    continue
                raise HubError(status, message) from None
            except (URLError, TimeoutError, ConnectionError) as error:
                if idempotent and attempt < self.max_retries:
                    attempt += 1
                    time.sleep(min(0.2 * attempt, 1.0))
                    continue
                reason = getattr(error, "reason", error)
                raise HubError(0, "网络连接失败：" + redact(str(reason), self.secrets)) from None
