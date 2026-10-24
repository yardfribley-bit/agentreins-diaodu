"""Connect once, join one team seat, and answer mentor instructions."""

from __future__ import annotations

import logging
import time

from linux_worker.config import Config, ConfigError, agent_type_for
from linux_worker.environment import host_environment
from linux_worker.hub import CredentialsError, HubClient, HubError
from linux_worker.llm import LLMClient
from linux_worker.loop import Episode
from linux_worker.redact import redact
from linux_worker.state import State
from linux_worker.tools import Tools, resolve_inside

logger = logging.getLogger("linux_worker")

_UPLOAD_LIMIT = 8 * 1024 * 1024


def outbound(text: str, secrets: list[str], limit: int = 7900) -> str:
    cleaned = redact(text or "", secrets).strip()
    if not cleaned:
        cleaned = "完成"
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 20] + "\n…（已截断）"


class Worker:
    def __init__(self, config: Config, sleeper=time.sleep):
        self.config = config
        self.sleeper = sleeper
        self.state = State(config.state_file).load()
        secrets = config.secret_values()
        if self.state.token:
            secrets.append(self.state.token)
        self.secrets = secrets
        self.hub = HubClient(config.api_root, self.state.token, secrets)
        self.llm = LLMClient(
            config.llm_base,
            config.llm_api_key,
            config.model,
            config.llm_max_output,
            secrets,
        )
        self.tools = Tools(config, secrets)
        config.workspace.mkdir(parents=True, exist_ok=True)

    def ensure_identity(self) -> dict:
        if self.state.token:
            self.hub.token = self.state.token
            try:
                me = self.hub.me()
            except HubError as exc:
                if exc.status == 401:
                    raise CredentialsError(
                        "已保存的凭证无效。请由操作者删除状态文件后再接入，代理不会自动新建身份。"
                    ) from None
                raise
            self.state.agent_id = str(me.get("agent_id") or self.state.agent_id)
            self.state.save()
            logger.info("复用身份 %s", self.state.agent_id)
            return me
        created = self.hub.connect(self.config.alias, agent_type_for(self.config.model))
        self.state.token = self.hub.token
        self.state.agent_id = str(created.get("agent_id") or "")
        if self.hub.token and self.hub.token not in self.secrets:
            self.secrets.append(self.hub.token)
            self.hub.secrets = self.secrets
            self.tools.secrets = self.secrets
            self.llm.secrets = [self.config.llm_api_key, *self.secrets]
        self.state.save()
        logger.info("已接入 %s model=%s", self.state.agent_id, self.config.model)
        return created

    def ensure_seat(self) -> dict:
        if self.state.session_id:
            try:
                view = self.hub.team_get(self.state.session_id)
            except HubError as exc:
                if exc.status in (401, 404):
                    raise CredentialsError(
                        "席位已失效或凭证无效，需要操作者处理。代理不会自动换身份重新加入。"
                    ) from None
                raise
            session = view["session"]
            if session.get("role") != "worker":
                raise ConfigError("当前席位不是执行端")
            self.state.seat = session.get("seat")
            self.state.save()
            return session
        if not self.config.invite:
            raise ConfigError("缺少 TEAM_INVITE_CODE，无法加入团队席位")
        info = host_environment()
        joined = self.hub.join_team(self.config.invite, info)
        session = joined["session"]
        if session.get("role") != "worker":
            raise ConfigError("这份邀请不是执行端席位")
        self.state.session_id = str(session["id"])
        self.state.seat = session.get("seat")
        self.state.cursor = 0
        self.state.save()
        self.config.invite = ""
        self._announce()
        logger.info("已加入团队席位 %s", self.state.seat)
        return session

    def _announce(self) -> None:
        if self.state.announced:
            return
        text = outbound(f"linux-worker 已上线。model={self.config.model}", self.secrets)
        try:
            self.hub.team_send(self.state.session_id, "note", text, "note-online")
        except HubError as exc:
            if exc.status != 409:
                raise
        self.state.announced = True
        self.state.save()

    def run_forever(self) -> None:
        self.ensure_identity()
        self.ensure_seat()
        while True:
            try:
                if self.poll_once():
                    logger.info("团队会话已结束，退出")
                    return
            except CredentialsError:
                raise
            except HubError as exc:
                if exc.status == 401:
                    raise CredentialsError(
                        "凭证已失效。请由操作者处理，代理不会自动新建身份。"
                    ) from None
                logger.warning("枢纽请求失败：%s", exc)
                self.sleeper(self.config.error_sleep)
            except Exception:
                logger.exception("执行循环出错")
                self.sleeper(self.config.error_sleep)

    def run_once(self) -> None:
        self.ensure_identity()
        self.ensure_seat()
        self.poll_once()

    def poll_once(self) -> bool:
        """Read one burst of team events. Returns True when the session is closed."""
        if not self.state.session_id:
            raise ConfigError("尚未加入团队席位")
        self.hub.team_heartbeat(self.state.session_id, host_environment())
        wait = self.config.relay_wait
        while True:
            previous = self.state.cursor
            batch = self.hub.team_events(self.state.session_id, previous, wait)
            wait = 0
            session = batch.get("session") or {}
            status = session.get("status") or "active"
            events = batch.get("events") or []
            next_cursor = batch.get("next_cursor", previous)
            if isinstance(next_cursor, bool) or not isinstance(next_cursor, int) or next_cursor < previous:
                raise HubError(0, "团队事件游标没有前进")
            for event in events:
                if status == "closed":
                    break
                self._handle(event, status)
            self.state.cursor = next_cursor
            self.state.save()
            seqs = [event.get("seq") for event in events if isinstance(event.get("seq"), int)]
            if seqs:
                try:
                    self.hub.team_ack(self.state.session_id, max(seqs))
                except HubError as exc:
                    logger.warning("确认游标失败：%s", exc)
            if status == "closed":
                return True
            if not batch.get("has_more") or next_cursor == previous:
                return False

    def _handle(self, event: dict, status: str) -> None:
        if event.get("kind") != "message":
            return
        message = event.get("message") or {}
        if message.get("kind") != "instruction":
            return
        seq = event.get("seq")
        if not isinstance(seq, int):
            return
        key = str(seq)
        previous = self.state.handled.get(key)
        if previous == "done":
            return
        if previous == "started":
            self._result(seq, "上次执行在完成前中断，已停止以免重复执行。请重新下达一条新指令。", [])
            self.state.handled[key] = "done"
            self.state.save()
            return
        if status != "active":
            if status == "paused":
                self._question(seq, f"会话已暂停，未执行指令 #{seq}。")
            self.state.handled[key] = "done"
            self.state.save()
            return
        self.state.handled[key] = "started"
        self.state.save()
        title = ""
        try:
            view = self.hub.team_get(self.state.session_id)["session"]
            title = str(view.get("title") or "")
            goal = str(view.get("goal") or "")
        except HubError:
            goal = ""
        instruction = f"团队：{title}\n目标：{goal}\n\n指令 #{seq}：\n{message.get('text') or ''}"
        episode = Episode(self.config, self.llm, self.tools, self.secrets)

        def progress(text: str, seq=seq) -> None:
            self._progress(seq, text)

        def question(text: str, seq=seq) -> None:
            self._question(seq, text)

        try:
            outcome = episode.run(instruction, self.state.seat, progress, question)
            if outcome.send_result:
                self._result(seq, outcome.text, outcome.attachments)
            self.state.handled[key] = "done"
            self.state.save()
        except Exception:
            logger.exception("指令 %s 执行失败", seq)
            try:
                self._result(seq, "执行中断，已停止以免重复执行。请重新下达一条新指令。", [])
            except HubError:
                logger.warning("未能回传中断说明")
            self.state.handled[key] = "done"
            self.state.save()

    def _progress(self, seq: int, text: str) -> None:
        self.state.progress_n += 1
        request_id = f"p-{seq}-{self.state.progress_n}"
        self.state.save()
        self._send("progress", outbound(text, self.secrets), request_id, seq)

    def _question(self, seq: int, text: str) -> None:
        self.state.progress_n += 1
        request_id = f"q-{seq}-{self.state.progress_n}"
        self.state.save()
        self._send("question", outbound(text, self.secrets), request_id, seq)

    def _result(self, seq: int, text: str, paths: list[str]) -> None:
        notes: list[str] = []
        uploaded: list[str] = []
        for raw in paths[:20]:
            try:
                path = resolve_inside(self.config.workspace, raw)
            except ValueError as exc:
                notes.append(redact(str(exc), self.secrets))
                continue
            if not path.is_file() or path.is_symlink():
                notes.append(path.name + " 不是普通文件，未上传")
                continue
            data = path.read_bytes()
            if not data:
                notes.append(path.name + " 是空文件，未上传")
                continue
            if len(data) > _UPLOAD_LIMIT:
                notes.append(path.name + " 超过 8MB，未上传")
                continue
            meta = self.hub.team_upload(self.state.session_id, path.name, data)
            file_id = str(meta.get("id") or "")
            if file_id:
                uploaded.append(file_id)
        body = text
        if notes:
            body = text + "\n" + "\n".join(notes)
        self._send("result", outbound(body, self.secrets), f"result-{seq}", seq, uploaded)

    def _send(self, kind: str, text: str, request_id: str, seq: int, attachments: list[str] | None = None) -> None:
        try:
            self.hub.team_send(
                self.state.session_id,
                kind,
                text,
                request_id,
                reply_to=seq,
                attachments=attachments or None,
            )
        except HubError as exc:
            if exc.status == 409:
                return
            raise
