"""把点名变成一次调用，并把回复写回同一条群聊。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from diaodu.control import (
    LABELS,
    ControlError,
    actions,
    advance_after_reply,
    artifact_for,
    assert_can_call,
    assert_can_confirm_publish,
    assert_can_confirm_topic,
    is_publish_turn,
)
from diaodu.runners import CallSpec, LiveRunners, RunnerError
from diaodu.store import Store, now
from diaodu.vault import Vault


def load_config(path: Path) -> dict:
    defaults = {
        "server": {"host": "127.0.0.1", "port": 8765},
        "agents": {
            "codex": {
                "command": "/usr/local/bin/codex",
                "timeout_seconds": 120,
                "ignore_user_config": True,
            },
            "workbuddy": {
                "command": "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy",
                "timeout_seconds": 600,
            },
            "grok": {
                "base_url": "https://api.x.ai/v1",
                "model": "grok-4.7",
                "timeout_seconds": 180,
            },
            "doubao": {"command": "", "timeout_seconds": 600},
        },
        "network": {
            "https_proxy": "http://127.0.0.1:1087",
            "http_proxy": "http://127.0.0.1:1087",
        },
        "vault": {"path": ""},
    }
    if not path.is_file():
        return defaults
    import tomllib

    loaded = tomllib.loads(path.read_text(encoding="utf-8"))
    for key, value in loaded.get("server", {}).items():
        defaults["server"][key] = value
    for key, value in loaded.get("network", {}).items():
        defaults["network"][key] = value
    for key, value in loaded.get("vault", {}).items():
        defaults["vault"][key] = value
    for agent, settings in loaded.get("agents", {}).items():
        defaults["agents"].setdefault(agent, {}).update(settings)
    return defaults


class Service:
    def __init__(self, store: Store, runners: LiveRunners, config: dict, vault: Vault | None = None):
        self.store = store
        self.runners = runners
        self.config = config
        self.vault = vault
        self._lock = threading.Lock()

    def create(self, brief: str) -> dict:
        return self.store.create(brief)

    def get(self, job_id: str) -> dict:
        return self.store.load(job_id)

    def list_jobs(self) -> list[dict]:
        return self.store.list_jobs()

    def say(self, job_id: str, text: str, agent: str = "codex", *, background: bool = False) -> dict:
        text = text.strip()
        if not text:
            raise ValueError("话不能为空")
        if agent not in LABELS:
            raise ControlError(f"未知代理：{agent}")
        remember = any(word in text for word in ("记住", "记下来", "记入知识库", "写入知识库"))
        with self._lock:
            job = self.store.load(job_id)
            if job.get("running"):
                raise ControlError("现在正在听别人说")
            job["messages"].append({"at": now(), "role": "user", "kind": "reply", "text": text})
            self.store.save(job)
        return self.call(
            job_id,
            agent,
            background=background,
            addressed=True,
            remember=remember,
            remember_title=text[:40],
        )

    def start(self, job_id: str, *, background: bool = False) -> dict:
        with self._lock:
            job = self.store.load(job_id)
            if not actions(job)["start"]:
                raise ControlError("这一步不能开始")
        return self.call(job_id, "codex", background=background)

    def confirm_topic(self, job_id: str) -> dict:
        with self._lock:
            job = self.store.load(job_id)
            assert_can_confirm_topic(job)
            job["phase"] = "draft"
            job["messages"].append(self._status("已确认选题。接下来可以写公众号，并准备抖音发布包。"))
            self.store.save(job)
        return self.store.load(job_id)

    def confirm_publish(self, job_id: str, *, background: bool = False) -> dict:
        with self._lock:
            job = self.store.load(job_id)
            assert_can_confirm_publish(job)
            job["phase"] = "publish"
            job["messages"].append(
                self._status("已确认发布。叫豆包提交抖音。公众号稿留在任务目录里，由你在后台发布。")
            )
            self.store.save(job)
        return self.call(job_id, "doubao", background=background)

    def stop(self, job_id: str) -> dict:
        with self._lock:
            job = self.store.load(job_id)
            if job.get("running") is None:
                raise ControlError("现在没有人在说话")
            job["stop_requested"] = True
            self.store.save(job)
        self.runners.stop(job_id)
        return self.store.load(job_id)

    def call(
        self,
        job_id: str,
        agent: str,
        *,
        background: bool = False,
        addressed: bool = False,
        remember: bool = False,
        remember_title: str = "",
    ) -> dict:
        with self._lock:
            job = self.store.load(job_id)
            job["stop_requested"] = False
            if not addressed:
                assert_can_call(job, agent)
            publish = False if addressed else is_publish_turn(job, agent)
            job["running"] = {"agent": agent, "pid": None}
            job["messages"].append(self._status(f"叫了{LABELS[agent]}。"))
            self.store.save(job)
            prompt = self._chat_prompt(job) if addressed else self._prompt(job, agent, publish)
            timeout = int(self.config["agents"][agent].get("timeout_seconds", 120))
            spec = CallSpec(
                agent,
                prompt,
                self.store.work_dir(job_id),
                publish,
                timeout,
                chat=addressed,
                remember=remember,
                remember_title=remember_title,
            )
            snapshot = self.store.load(job_id)
        if background:
            threading.Thread(target=self._execute, args=(job_id, spec), daemon=True).start()
            return snapshot
        return self._execute(job_id, spec)

    def _execute(self, job_id: str, spec: CallSpec) -> dict:
        agent = spec.agent
        publish = spec.publish

        def on_pid(pid: int) -> None:
            with self._lock:
                current = self.store.load(job_id)
                if current.get("running"):
                    current["running"]["pid"] = pid
                    self.store.save(current)

        try:
            result = self.runners.run(job_id, spec, on_pid)
        except RunnerError as exc:
            with self._lock:
                current = self.store.load(job_id)
                current["running"] = None
                current["messages"].append(
                    {"at": now(), "role": agent, "kind": "error", "text": str(exc), "publish": publish}
                )
                self.store.save(current)
            return current

        with self._lock:
            current = self.store.load(job_id)
            stopped = result.stopped or current.get("stop_requested")
            current["running"] = None
            current["stop_requested"] = False
            if stopped:
                current["messages"].append(self._status(f"已停止{LABELS[agent]}。"))
            else:
                current["messages"].append(
                    {
                        "at": now(),
                        "role": agent,
                        "kind": "reply",
                        "text": result.text,
                        "publish": publish,
                    }
                )
                if not spec.chat:
                    self._keep_artifact(job_id, agent, publish, result.text)
                    advance_after_reply(current, agent, publish)
            if spec.chat and spec.remember and self.vault is not None and not stopped:
                self.vault.add(spec.remember_title, result.text)
            self.store.save(current)
            return current

    def _keep_artifact(self, job_id: str, agent: str, publish: bool, text: str) -> None:
        relative = artifact_for(agent, publish)
        path = self.store.work_dir(job_id) / relative
        if path.is_file() and path.stat().st_size > 0:
            return
        if agent == "codex":
            payload = text.strip()
            try:
                json.loads(payload)
            except json.JSONDecodeError:
                payload = json.dumps({"summary": text}, ensure_ascii=False, indent=2) + "\n"
            self.store.write_file(job_id, relative, payload if payload.endswith("\n") else payload + "\n")
            return
        self.store.write_file(job_id, relative, text.rstrip() + "\n")

    def _prompt(self, job: dict, agent: str, publish: bool) -> str:
        files = "\n".join(f"- {name}" for name in self.store.list_files(job["id"])) or "（还没有文件）"
        lines = []
        for message in job["messages"][-30:]:
            if message["kind"] == "status":
                speaker = "调度室"
            else:
                speaker = LABELS.get(message["role"], message["role"])
            lines.append(f"{speaker}：{message['text'][:4000]}")
        transcript = "\n\n".join(lines) or "（还没有人说过话）"
        artifact = artifact_for(agent, publish)
        if publish:
            duty = "用户已经确认发布。提交 douyin/publish-pack 里的内容到抖音。不要动公众号。"
        elif agent == "codex":
            duty = "只整理数据，把结论写入 research.json。不连接抖音，不连接公众号。"
        elif agent == "doubao":
            duty = "只分析已连接账号里的抖音作品，并准备发布包。这一轮不能提交发布。"
        elif agent == "grok":
            duty = "只核对主张是否成立。分开写：成立、对不上、缺证据。不发布。"
        else:
            duty = "只根据已有文件写公众号正文，写入 wechat/article.md。不要从草稿箱发布。"
        return (
            f"你是{LABELS[agent]}。这轮由调度室点名，你不能自己去叫别人。\n\n"
            f"本轮只做：{duty}\n"
            f"需要落盘时只写：{artifact}\n\n"
            f"目标：\n{job['brief']}\n\n"
            f"任务目录里已有：\n{files}\n\n"
            f"群聊：\n{transcript}\n"
        )

    def _chat_prompt(self, job: dict) -> str:
        lines = []
        for message in job["messages"][-20:]:
            if message["kind"] == "status":
                continue
            speaker = "用户" if message["role"] == "user" else LABELS.get(message["role"], message["role"])
            lines.append(f"{speaker}：{message['text'][:2000]}")
        transcript = "\n".join(lines) or "（还没有人说过话）"
        notes = "（空）"
        if self.vault is not None:
            listed = self.vault.list_notes()
            notes = "\n".join(f"- {name}" for name in listed[:30]) or "（空）"
        return (
            "你在一场聊天里。用一两句中文直接回复用户刚说的话，像面对面说话。不要使用工具。\n\n"
            f"对话：\n{transcript}\n\n"
            f"知识库笔记：\n{notes}\n"
        )

    @staticmethod
    def _status(text: str) -> dict:
        return {"at": now(), "role": "platform", "kind": "status", "text": text}
