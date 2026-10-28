"""Tools the model may call. Shell output is redacted before it leaves this module."""

from __future__ import annotations

import json
import os
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from linux_worker.redact import redact
from linux_worker.safety import Policy, decide
from linux_worker.search import SearchClient, SearchError


_READ_LIMIT = 256_000
_WRITE_LIMIT = 1_000_000
_FETCH_LIMIT = 64_000
_UPLOAD_LIMIT = 8 * 1024 * 1024


@dataclass
class ToolResult:
    content: str
    finish: bool = False
    finish_text: str = ""
    attachments: list[str] = field(default_factory=list)
    progress: str | None = None
    question: str | None = None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head = limit // 2
    tail = limit - head
    return text[:head] + "\n…（输出已截断）…\n" + text[-tail:], True


def resolve_inside(workspace: Path, raw: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("路径不能为空")
    if "\x00" in raw:
        raise ValueError("路径不合法")
    root = workspace.resolve()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("路径必须留在工作区内")
    return resolved


class Tools:
    def __init__(self, config, secrets: list[str]):
        self.config = config
        self.secrets = secrets
        self.policy = Policy(
            config.allowlist,
            config.workspace,
            secrets,
            config.forbidden_paths(),
        )
        self.search = SearchClient(config, secrets)
        self.opener = build_opener(_NoRedirect())
        self._handlers = {
            "run_shell": self._shell,
            "read_file": self._read,
            "write_file": self._write,
            "http_fetch": self._fetch,
            "web_search": self._search,
            "post_progress": self._progress,
            "ask_question": self._question,
            "finish": self._finish,
        }

    def schemas(self) -> list[dict]:
        return [
            _schema(
                "run_shell",
                "在工作区执行一条 shell 命令。网络目标必须落在操作者配置的允许范围内。命令有超时，输出会被截断。返回的 stdout/stderr 要写进 finish。",
                {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "description": "秒，不超过配置上限"},
                },
                ["command"],
            ),
            _schema(
                "read_file",
                "读取工作区内的文本文件。",
                {"path": {"type": "string"}},
                ["path"],
            ),
            _schema(
                "write_file",
                "把文本写入工作区内的文件，必要时创建父目录。",
                {"path": {"type": "string"}, "content": {"type": "string"}},
                ["path", "content"],
            ),
            _schema(
                "http_fetch",
                "GET 或 HEAD 一个 http(s) 地址。主机必须在允许范围内，或是本团队附件的签名链接。",
                {
                    "url": {"type": "string"},
                    "method": {"type": "string", "enum": ["GET", "HEAD"]},
                },
                ["url"],
            ),
            _schema(
                "web_search",
                "用已配置的搜索后端查询公开网页，返回标题、链接和摘要。finish 时要带上这些字段，不要只说搜过了。",
                {"query": {"type": "string"}},
                ["query"],
            ),
            _schema(
                "post_progress",
                "向导师发送一条进度消息。",
                {"text": {"type": "string"}},
                ["text"],
            ),
            _schema(
                "ask_question",
                "向导师提一个问题并等待下一条指令。",
                {"text": {"type": "string"}},
                ["text"],
            ),
            _schema(
                "finish",
                "结束本条指令。text 必须包含具体结果：命令、退出码、stdout/stderr 的关键原文，或搜索结果的标题、链接和摘要。不要只写「完成」。可选附件是工作区相对路径。",
                {
                    "text": {"type": "string"},
                    "attachments": {"type": "array", "items": {"type": "string"}},
                },
                ["text"],
            ),
        ]

    def call(self, name: str, arguments) -> ToolResult:
        try:
            if isinstance(arguments, str):
                arguments = json.loads(arguments) if arguments.strip() else {}
            if not isinstance(arguments, dict):
                return self._error("参数必须是对象")
            handler = self._handlers.get(name)
            if handler is None:
                return self._error("未知工具")
            return handler(arguments)
        except json.JSONDecodeError:
            return self._error("参数不是合法 JSON")
        except Exception as exc:
            return self._error(redact(str(exc), self.secrets))

    def _error(self, message: str) -> ToolResult:
        return ToolResult(json.dumps({"ok": False, "error": message}, ensure_ascii=False))

    def _shell(self, args: dict) -> ToolResult:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return self._error("command 不能为空")
        timeout = args.get("timeout", self.config.shell_timeout)
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            timeout = self.config.shell_timeout
        timeout = max(1, min(timeout, self.config.shell_timeout))
        decision = decide(command, self.policy)
        if not decision.allowed:
            return ToolResult(
                json.dumps({"ok": False, "denied": True, "error": decision.reason}, ensure_ascii=False)
            )
        return ToolResult(self._execute(command, timeout))

    def _execute(self, command: str, timeout: int) -> str:
        workspace = self.config.workspace
        workspace.mkdir(parents=True, exist_ok=True)
        tmp = workspace / "tmp"
        tmp.mkdir(exist_ok=True)
        bash = "/bin/bash"
        if self.config.shell_user:
            if os.geteuid() != 0:
                return json.dumps(
                    {"ok": False, "error": "SHELL_USER 仅在以 root 启动时使用，当前进程不会降权执行"},
                    ensure_ascii=False,
                )
            runuser = "/usr/sbin/runuser"
            if not os.path.exists(runuser):
                runuser = "/bin/runuser"
            argv = [runuser, "-u", self.config.shell_user, "--", bash, "--noprofile", "--norc", "-c", command]
        else:
            argv = [bash, "--noprofile", "--norc", "-c", command]
        env = clean_shell_env(workspace, self.config.shell_user or "worker")
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(workspace),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
        except OSError as exc:
            return json.dumps({"ok": False, "error": "无法启动 shell：" + redact(str(exc), self.secrets)}, ensure_ascii=False)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate()
            stdout, truncated = _clip(redact(stdout or "", self.secrets), self.config.shell_output_limit)
            stderr, more = _clip(redact(stderr or "", self.secrets), self.config.shell_output_limit)
            return json.dumps(
                {
                    "ok": False,
                    "command": redact(command, self.secrets)[:500],
                    "error": "命令超时",
                    "stdout": stdout,
                    "stderr": stderr,
                    "truncated": truncated or more,
                },
                ensure_ascii=False,
            )
        stdout, cut_out = _clip(redact(stdout or "", self.secrets), self.config.shell_output_limit)
        stderr, cut_err = _clip(redact(stderr or "", self.secrets), self.config.shell_output_limit)
        return json.dumps(
            {
                "ok": proc.returncode == 0,
                "command": redact(command, self.secrets)[:500],
                "exit_code": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "truncated": cut_out or cut_err,
            },
            ensure_ascii=False,
        )

    def _read(self, args: dict) -> ToolResult:
        path = resolve_inside(self.config.workspace, str(args.get("path") or ""))
        if not path.is_file() or path.is_symlink():
            return self._error("不是普通文件")
        data = path.read_bytes()[: _READ_LIMIT + 1]
        truncated = len(data) > _READ_LIMIT
        text = data[:_READ_LIMIT].decode("utf-8", errors="replace")
        text, cut = _clip(redact(text, self.secrets), _READ_LIMIT)
        return ToolResult(
            json.dumps({"ok": True, "path": path.name, "content": text, "truncated": truncated or cut}, ensure_ascii=False)
        )

    def _write(self, args: dict) -> ToolResult:
        path = resolve_inside(self.config.workspace, str(args.get("path") or ""))
        content = args.get("content")
        if not isinstance(content, str):
            return self._error("content 必须是字符串")
        if len(content) > _WRITE_LIMIT:
            return self._error("内容超过 1MB")
        if ".ssh" in path.parts or path.name in {"id_rsa", "id_ed25519", "id_ecdsa"}:
            return self._error("拒绝写入密钥文件")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            return self._error("拒绝跟随符号链接")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
        return ToolResult(json.dumps({"ok": True, "path": str(path.relative_to(self.config.workspace.resolve()))}, ensure_ascii=False))

    def _fetch(self, args: dict) -> ToolResult:
        url = str(args.get("url") or "")
        method = str(args.get("method") or "GET").upper()
        if method not in {"GET", "HEAD"}:
            return self._error("只允许 GET 和 HEAD")
        reason = self._fetch_allowed(url)
        if reason:
            return ToolResult(json.dumps({"ok": False, "denied": True, "error": reason}, ensure_ascii=False))
        request = Request(url, method=method, headers={"User-Agent": "linux-worker/0.1"})
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = b"" if method == "HEAD" else response.read(_FETCH_LIMIT + 1)
                status = response.status
                final = response.geturl()
        except HTTPError as error:
            with error:
                raw = error.read(_FETCH_LIMIT + 1)
            status = error.code
            final = url
        except (URLError, TimeoutError, ConnectionError, ValueError) as exc:
            return self._error("请求失败：" + redact(str(getattr(exc, "reason", exc)), self.secrets))
        text, truncated = _clip(redact(raw[:_FETCH_LIMIT].decode("utf-8", errors="replace"), self.secrets), _FETCH_LIMIT)
        return ToolResult(
            json.dumps(
                {"ok": 200 <= status < 400, "status": status, "url": final, "body": text, "truncated": truncated or len(raw) > _FETCH_LIMIT},
                ensure_ascii=False,
            )
        )

    def _fetch_allowed(self, url: str) -> str | None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"}:
            return "只允许 http 或 https"
        if parsed.username or parsed.password:
            return "URL 不能携带凭据"
        host = parsed.hostname or ""
        if not host:
            return "URL 缺少主机"
        hub = urlsplit(self.config.hub_base)
        relay_file = "/files/" in parsed.path and (
            "/relay/teams/" in parsed.path or "/relay/sessions/" in parsed.path
        )
        if host == hub.hostname and relay_file:
            return None
        if not self.policy.allows(host):
            return f"目标不在允许范围内：{host}"
        return None

    def _search(self, args: dict) -> ToolResult:
        try:
            rows = self.search.search(str(args.get("query") or ""))
        except (SearchError, ValueError) as exc:
            return self._error(redact(str(exc), self.secrets))
        safe = []
        for row in rows:
            safe.append({key: redact(str(value), self.secrets) for key, value in row.items()})
        return ToolResult(json.dumps({"ok": True, "results": safe}, ensure_ascii=False))

    def _progress(self, args: dict) -> ToolResult:
        text = str(args.get("text") or "").strip()
        if not text:
            return self._error("text 不能为空")
        return ToolResult('{"ok":true}', progress=text)

    def _question(self, args: dict) -> ToolResult:
        text = str(args.get("text") or "").strip()
        if not text:
            return self._error("text 不能为空")
        return ToolResult('{"ok":true}', question=text)

    def _finish(self, args: dict) -> ToolResult:
        text = str(args.get("text") or "").strip() or "完成"
        attachments = args.get("attachments") or []
        if not isinstance(attachments, list) or any(not isinstance(item, str) for item in attachments):
            return self._error("attachments 必须是路径字符串数组")
        return ToolResult("", finish=True, finish_text=text, attachments=attachments[:20])


def clean_shell_env(workspace: Path, user: str = "worker") -> dict:
    """The environment passed to run_shell. Exposed for tests."""
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(workspace),
        "TMPDIR": str(workspace / "tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "dumb",
        "USER": user or "worker",
        "PWD": str(workspace),
    }
