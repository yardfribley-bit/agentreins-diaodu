"""Connect once, then answer instructions on a team seat and/or a 1:1 Topic."""

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
            config.fallback_model,
        )
        self.tools = Tools(config, secrets)
        self._team_closed = False
        self._topic_closed = False
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

    def ensure_channels(self) -> None:
        if self.state.session_id or self.config.invite:
            self.ensure_seat()
        if self.state.topic_session_id or self.config.topic_invite:
            self.ensure_topic()
        if not self.state.session_id and not self.state.topic_session_id:
            raise ConfigError("缺少 TEAM_INVITE_CODE 或 TOPIC_INVITE_CODE，无法加入席位")

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
        self._announce("team")
        logger.info("已加入团队席位 %s", self.state.seat)
        return session

    def ensure_topic(self) -> dict:
        if self.state.topic_session_id:
            try:
                view = self.hub.topic_get(self.state.topic_session_id)
            except HubError as exc:
                if exc.status in (401, 404):
                    raise CredentialsError(
                        "一对一会话已失效或凭证无效，需要操作者处理。代理不会自动换身份重新加入。"
                    ) from None
                raise
            session = view["session"]
            if session.get("role") != "worker":
                raise ConfigError("当前一对一席位不是执行端")
            self.state.save()
            return session
        if not self.config.topic_invite:
            raise ConfigError("缺少 TOPIC_INVITE_CODE，无法加入一对一 Topic")
        joined = self.hub.join_topic(self.config.topic_invite, host_environment())
        session = joined["session"]
        if session.get("role") != "worker":
            raise ConfigError("这份 Topic 码加入的不是执行端席位")
        self.state.topic_session_id = str(session["id"])
        self.state.topic_cursor = 0
        self.state.save()
        self.config.topic_invite = ""
        self._announce("topic")
        logger.info("已加入一对一 Topic %s", self.state.topic_session_id)
        return session

    def _announce(self, channel: str) -> None:
        if channel == "team" and self.state.announced:
            return
        if channel == "topic" and self.state.topic_announced:
            return
        text = outbound(f"linux-worker 已上线。model={self.config.model}", self.secrets)
        try:
            self._send(channel, "note", text, "note-online")
        except HubError as exc:
            if exc.status != 409:
                raise
        if channel == "team":
            self.state.announced = True
        else:
            self.state.topic_announced = True
        self.state.save()

    def run_forever(self) -> None:
        self.ensure_identity()
        self.ensure_channels()
        primary = "team" if self.state.session_id else "topic"
        while True:
            try:
                team_alive = bool(self.state.session_id) and not self._team_closed
                topic_alive = bool(self.state.topic_session_id) and not self._topic_closed
                if not team_alive and not topic_alive:
                    logger.info("会话已结束，退出")
                    return
                if primary == "team" and not team_alive:
                    primary = "topic"
                elif primary == "topic" and not topic_alive:
                    primary = "team"
                wait_team = self.config.relay_wait if primary == "team" else 0
                wait_topic = self.config.relay_wait if primary == "topic" else 0
                if team_alive:
                    if self.poll_once(wait_team):
                        self._team_closed = True
                if topic_alive:
                    if self.poll_topic_once(wait_topic):
                        self._topic_closed = True
                if team_alive and topic_alive and not self._team_closed and not self._topic_closed:
                    primary = "topic" if primary == "team" else "team"
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
        self.ensure_channels()
        if self.state.session_id:
            self.poll_once(0)
        if self.state.topic_session_id:
            self.poll_topic_once(0)

    def poll_once(self, wait: int | None = None) -> bool:
        """Read one burst of team events. Returns True when the session is closed."""
        if not self.state.session_id:
            raise ConfigError("尚未加入团队席位")
        return self._poll("team", wait)

    def poll_topic_once(self, wait: int | None = None) -> bool:
        """Read one burst of 1:1 Topic events. Returns True when the session is closed."""
        if not self.state.topic_session_id:
            raise ConfigError("尚未加入一对一 Topic")
        return self._poll("topic", wait)

    def _poll(self, channel: str, wait: int | None) -> bool:
        team = channel == "team"
        sid = self.state.session_id if team else self.state.topic_session_id
        if team:
            self.hub.team_heartbeat(sid, host_environment())
        else:
            self.hub.topic_heartbeat(sid, host_environment())
        cursor = self.state.cursor if team else self.state.topic_cursor
        wait = self.config.relay_wait if wait is None else wait
        while True:
            previous = cursor
            if team:
                batch = self.hub.team_events(sid, previous, wait)
            else:
                batch = self.hub.topic_events(sid, previous, wait)
            wait = 0
            session = batch.get("session") or {}
            status = session.get("status") or "active"
            events = batch.get("events") or []
            next_cursor = batch.get("next_cursor", previous)
            if isinstance(next_cursor, bool) or not isinstance(next_cursor, int) or next_cursor < previous:
                raise HubError(0, "事件游标没有前进")
            for event in events:
                if status == "closed":
                    break
                self._handle(event, status, channel)
            cursor = next_cursor
            if team:
                self.state.cursor = next_cursor
            else:
                self.state.topic_cursor = next_cursor
            self.state.save()
            seqs = [event.get("seq") for event in events if isinstance(event.get("seq"), int)]
            if seqs:
                try:
                    if team:
                        self.hub.team_ack(sid, max(seqs))
                    else:
                        self.hub.topic_ack(sid, max(seqs))
                except HubError as exc:
                    logger.warning("确认游标失败：%s", exc)
            if status == "closed":
                return True
            if not batch.get("has_more") or next_cursor == previous:
                return False

    def _handled(self, channel: str, seq: int) -> str | None:
        if channel == "topic":
            return self.state.handled.get(f"topic:{seq}")
        return self.state.handled.get(str(seq))

    def _mark(self, channel: str, seq: int, value: str) -> None:
        key = f"topic:{seq}" if channel == "topic" else str(seq)
        self.state.handled[key] = value

    def _handle(self, event: dict, status: str, channel: str) -> None:
        if event.get("kind") != "message":
            return
        message = event.get("message") or {}
        if message.get("kind") != "instruction":
            return
        seq = event.get("seq")
        if not isinstance(seq, int):
            return
        previous = self._handled(channel, seq)
        if previous == "done":
            return
        if previous == "started":
            self._result(channel, seq, "上次执行在完成前中断，已停止以免重复执行。请重新下达一条新指令。", [])
            self._mark(channel, seq, "done")
            self.state.save()
            return
        if status != "active":
            if status == "paused":
                self._question(channel, seq, f"会话已暂停，未执行指令 #{seq}。")
            self._mark(channel, seq, "done")
            self.state.save()
            return
        self._mark(channel, seq, "started")
        self.state.save()
        title = ""
        goal = ""
        try:
            sid = self.state.session_id if channel == "team" else self.state.topic_session_id
            view = (self.hub.team_get if channel == "team" else self.hub.topic_get)(sid)["session"]
            title = str(view.get("title") or "")
            goal = str(view.get("goal") or "")
        except HubError:
            pass
        label = "团队" if channel == "team" else "一对一"
        instruction = f"{label}：{title}\n目标：{goal}\n\n指令 #{seq}：\n{message.get('text') or ''}"
        episode = Episode(self.config, self.llm, self.tools, self.secrets)
        seat = self.state.seat if channel == "team" else None

        def progress(text: str, seq=seq, channel=channel) -> None:
            self._progress(channel, seq, text)

        def question(text: str, seq=seq, channel=channel) -> None:
            self._question(channel, seq, text)

        try:
            outcome = episode.run(instruction, seat, progress, question)
            if outcome.send_result:
                self._result(channel, seq, outcome.text, outcome.attachments)
            self._mark(channel, seq, "done")
            self.state.save()
        except Exception:
            logger.exception("指令 %s 执行失败", seq)
            try:
                self._result(channel, seq, "执行中断，已停止以免重复执行。请重新下达一条新指令。", [])
            except HubError:
                logger.warning("未能回传中断说明")
            self._mark(channel, seq, "done")
            self.state.save()

    def _progress(self, channel: str, seq: int, text: str) -> None:
        self.state.progress_n += 1
        request_id = f"p-{seq}-{self.state.progress_n}"
        self.state.save()
        self._send(channel, "progress", outbound(text, self.secrets), request_id, seq)

    def _question(self, channel: str, seq: int, text: str) -> None:
        self.state.progress_n += 1
        request_id = f"q-{seq}-{self.state.progress_n}"
        self.state.save()
        self._send(channel, "question", outbound(text, self.secrets), request_id, seq)

    def _result(self, channel: str, seq: int, text: str, paths: list[str]) -> None:
        notes: list[str] = []
        uploaded: list[str] = []
        sid = self.state.session_id if channel == "team" else self.state.topic_session_id
        upload = self.hub.team_upload if channel == "team" else self.hub.topic_upload
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
            meta = upload(sid, path.name, data)
            file_id = str(meta.get("id") or "")
            if file_id:
                uploaded.append(file_id)
        body = text
        if notes:
            body = text + "\n" + "\n".join(notes)
        self._send(channel, "result", outbound(body, self.secrets), f"result-{seq}", seq, uploaded)

    def _send(
        self,
        channel: str,
        kind: str,
        text: str,
        request_id: str,
        seq: int | None = None,
        attachments: list[str] | None = None,
    ) -> None:
        sid = self.state.session_id if channel == "team" else self.state.topic_session_id
        sender = self.hub.team_send if channel == "team" else self.hub.topic_send
        try:
            sender(sid, kind, text, request_id, reply_to=seq, attachments=attachments or None)
        except HubError as exc:
            if exc.status == 409:
                return
            raise
