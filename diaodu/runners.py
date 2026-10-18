"""点名一次，调用一个代理。四个代理之间没有直连。"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


class RunnerError(Exception):
    pass


@dataclass
class CallSpec:
    agent: str
    prompt: str
    work_dir: Path
    publish: bool
    timeout_seconds: int
    chat: bool = False
    remember: bool = False
    remember_title: str = ""


@dataclass
class CallResult:
    ok: bool
    text: str
    stopped: bool = False


class LiveRunners:
    def __init__(self, config: dict):
        self.config = config
        self._procs: dict[str, subprocess.Popen] = {}

    def run(self, job_id: str, spec: CallSpec, on_pid) -> CallResult:
        agent = spec.agent
        if agent == "codex":
            return self._subprocess(job_id, spec, self._codex_cmd(spec), on_pid, self._codex_text)
        if agent == "workbuddy":
            return self._subprocess(job_id, spec, self._workbuddy_cmd(spec), on_pid, lambda _spec, proc: proc.stdout or "")
        if agent == "doubao":
            command = self._doubao_cmd(spec)
            return self._subprocess(job_id, spec, command, on_pid, lambda _spec, proc: proc.stdout or "")
        if agent == "grok":
            return self._grok(spec)
        raise RunnerError(f"未知代理：{agent}")

    def stop(self, job_id: str) -> None:
        proc = self._procs.get(job_id)
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        except ProcessLookupError:
            return

    def _child_env(self) -> dict:
        env = os.environ.copy()
        for key in ("https_proxy", "http_proxy", "all_proxy", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
            env.pop(key, None)
        network = self.config.get("network", {})
        https = network.get("https_proxy") or "http://127.0.0.1:1087"
        http = network.get("http_proxy") or https
        env["https_proxy"] = https
        env["HTTPS_PROXY"] = https
        env["http_proxy"] = http
        env["HTTP_PROXY"] = http
        return env

    def _codex_cmd(self, spec: CallSpec) -> list[str]:
        command = self.config["agents"]["codex"]["command"]
        last = spec.work_dir / ".last-message.txt"
        command_line = [command, "exec"]
        if self.config["agents"]["codex"].get("ignore_user_config", True):
            command_line.append("--ignore-user-config")
        command_line.extend(
            [
                "--ephemeral",
                "--skip-git-repo-check",
                "-s",
                "read-only",
                "-C",
                str(spec.work_dir),
                "-o",
                str(last),
                spec.prompt,
            ]
        )
        return command_line

    def _codex_text(self, spec: CallSpec, proc: subprocess.CompletedProcess) -> str:
        last = spec.work_dir / ".last-message.txt"
        if last.is_file() and last.stat().st_size:
            return last.read_text(encoding="utf-8").strip()
        return (proc.stdout or "").strip()

    def _workbuddy_cmd(self, spec: CallSpec) -> list[str]:
        command = self.config["agents"]["workbuddy"]["command"]
        return [command, "-p", spec.prompt, "--output-format", "text", "--allowedTools", "Read"]

    def _doubao_cmd(self, spec: CallSpec) -> list[str]:
        command = (self.config["agents"]["doubao"].get("command") or "").strip()
        if not command:
            raise RunnerError(
                "豆包还不能被点名。桌面版没有无头入口。"
                "在 config.toml 的 agents.doubao.command 填入一条已登录、已连接抖音的命令。"
            )
        return [*shlex.split(command), spec.prompt]

    def _subprocess(self, job_id, spec, command, on_pid, read_text) -> CallResult:
        try:
            proc = subprocess.Popen(
                command,
                cwd=spec.work_dir,
                env=self._child_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise RunnerError(f"找不到命令：{command[0]}") from exc
        self._procs[job_id] = proc
        on_pid(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=spec.timeout_seconds)
        except subprocess.TimeoutExpired:
            self.stop(job_id)
            raise RunnerError(f"{spec.agent} 超时（{spec.timeout_seconds} 秒）")
        finally:
            self._procs.pop(job_id, None)
        stopped = proc.returncode is not None and proc.returncode < 0
        holder = subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
        text = read_text(spec, holder).strip()
        if stopped:
            return CallResult(False, text or "已停止", stopped=True)
        if proc.returncode != 0 or not text:
            detail = _useful_error(stderr, stdout) or f"{spec.agent} 没有返回内容"
            raise RunnerError(detail)
        return CallResult(True, text)

    def _grok(self, spec: CallSpec) -> CallResult:
        api_key = os.environ.get("XAI_API_KEY", "").strip()
        if not api_key:
            raise RunnerError("未设置 XAI_API_KEY，Grok 无法调用")
        grok = self.config["agents"]["grok"]
        payload = {
            "model": grok["model"],
            "messages": [
                {"role": "system", "content": "你是 Grok。只根据给出的材料和公开信息核对，不发布任何内容。用中文回答。"},
                {"role": "user", "content": spec.prompt},
            ],
        }
        request = urllib.request.Request(
            grok["base_url"].rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=spec.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RunnerError(f"Grok 接口返回 {exc.code}：{detail}") from exc
        except urllib.error.URLError as exc:
            raise RunnerError(f"Grok 接口连不上：{exc.reason}") from exc
        try:
            text = body["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RunnerError("Grok 接口没有返回正文") from exc
        if not text:
            raise RunnerError("Grok 没有返回内容")
        return CallResult(True, text)


def _useful_error(stderr: str, stdout: str) -> str:
    lines = [
        line
        for line in (stderr or "").splitlines()
        if line.startswith("ERROR:") or line.startswith("error:")
    ]
    if lines:
        return "\n".join(lines[-5:])
    blob = (stderr or stdout or "").strip()
    return blob[-800:]
