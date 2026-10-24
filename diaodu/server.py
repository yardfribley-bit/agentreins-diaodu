"""本地页面。只听 127.0.0.1。"""

from __future__ import annotations

import json
import mimetypes
import re
import os
import hmac
import secrets
import sys
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, quote

from diaodu.control import PHASE_LABELS, ControlError, actions
from diaodu.library import LibraryError
from diaodu import chat, agent_api
from diaodu.http_util import discard_body
from diaodu.identity import AuthError, Identity, text, digest

STATIC = Path(__file__).resolve().parent.parent / "static"


def doctor(config: dict) -> list[dict]:
    items = []
    codex = config["agents"]["codex"]["command"]
    items.append(_command_status("codex", "Codex", codex, "经 127.0.0.1:1087，聊天可回复"))
    workbuddy = config["agents"]["workbuddy"]["command"]
    items.append(_command_status("workbuddy", "WorkBuddy", workbuddy, "本机 codebuddy -p"))
    key_ready = bool(os.environ.get("XAI_API_KEY", "").strip())
    items.append(
        {
            "id": "grok",
            "label": "Grok",
            "ready": key_ready,
            "detail": "XAI_API_KEY 已设置" if key_ready else "未设置 XAI_API_KEY",
        }
    )
    command = (config["agents"]["doubao"].get("command") or "").strip()
    if command:
        items.append(_command_status("doubao", "豆包", command.split()[0], "自定义命令"))
    else:
        installed = Path("/Applications/Doubao.app").exists()
        detail = "桌面版已安装，但没有无头入口" if installed else "未安装，也未配置命令"
        items.append({"id": "doubao", "label": "豆包", "ready": False, "detail": detail})
    return items


def _command_status(agent_id: str, label: str, command: str, detail: str) -> dict:
    path = Path(command)
    ready = path.is_file() and os.access(path, os.X_OK)
    return {
        "id": agent_id,
        "label": label,
        "ready": ready,
        "detail": detail if ready else f"找不到 {command}",
    }


def view(store, job: dict) -> dict:
    shown = dict(job)
    running = shown.get("running")
    if running:
        shown["running"] = {"agent": running["agent"]}
    shown["phase_label"] = PHASE_LABELS.get(job["phase"], job["phase"])
    shown["actions"] = actions(job)
    shown["files"] = store.list_files(job["id"])
    return shown


def make_handler(service, library=None, identity=None, secure_cookies=False, public_origin="", mailbox=None):
    if library is not None and identity is None:
        identity = Identity(library.root / "identity.sqlite3")
    if library is not None:
        from diaodu.relay import Relay
        library.relay = Relay(library.root, library.public_base_path, public_origin)
        from diaodu.relay_teams import RelayTeams
        try:
            max_workers = int(os.environ.get("DIAODU_RELAY_MAX_WORKERS", "8"))
        except ValueError:
            max_workers = 8
        library.teams = RelayTeams(library.root, max_workers,
                                   library.public_base_path, public_origin)
        from diaodu.mailbox import Mailbox
        mailbox = mailbox or Mailbox(library.root)
        library.mailbox = mailbox
        from diaodu.outreach import Outreach
        library.outreach = Outreach(mailbox,(public_origin or "http://127.0.0.1")+library.public_base_path,identity)
    class Handler(BaseHTTPRequestHandler):
        def _actor(self, mutation=False, kind=None):
            authorization = self.headers.get("Authorization", "")
            if authorization:
                if not authorization.startswith("Bearer "):
                    raise AuthError("需要 Bearer 实例凭证")
                actor = identity.agent(authorization[7:])
            else:
                cookie = SimpleCookie()
                cookie.load(self.headers.get("Cookie", ""))
                token = cookie.get("diaodu_session")
                token_value = token.value if token else ""
                if token_value.startswith("da_"):
                    actor = identity.agent(token_value)
                    actor["csrf"] = digest("csrf:"+token_value)
                else:
                    actor = identity.session(token_value)
                if mutation and not hmac.compare_digest(self.headers.get("X-CSRF-Token", "").encode(), actor["csrf"].encode()):
                    raise AuthError("会话校验失败，请刷新后重试",403)
            if kind and actor["kind"] != kind:
                raise AuthError("此操作需要" + ("人员登录" if kind == "user" else "智能体实例凭证"),403)
            return actor

        def _origin(self):
            origin = self.headers.get("Origin")
            if origin:
                expected = public_origin or ("http://" + self.headers.get("Host", ""))
                if origin != expected:
                    raise AuthError("不接受跨站请求",403)

        def _cookie(self, token, clear=False):
            path = library.public_base_path + "/"
            return f"diaodu_session={token}; HttpOnly; SameSite=Strict; Path={path}; Max-Age={0 if clear else 86400}" + ("; Secure" if secure_cookies else "")

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == '/relay':
                self.send_response(308)
                self.send_header('Location', (library.public_base_path if library else '') + '/relay/')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if parsed.path.startswith('/relay/watch/'):
                self._file(STATIC / 'relay-watch.html')
                return
            if parsed.path == '/relay-watch.js':
                self._file(STATIC / 'relay-watch.js')
                return
            relay_static = {'/relay/': 'relay.html', '/relay/relay.js': 'relay.js', '/relay/relay.css': 'relay.css'}
            if parsed.path in relay_static:
                self._file(STATIC / relay_static[parsed.path])
                return
            if library is not None and parsed.path.startswith('/relay/join/'):
                from diaodu import relay_http
                try:
                    relay_http.join_document(self, library, parsed)
                except AuthError as exc:
                    self._fail(exc)
                except Exception as exc:
                    self._internal_error(exc)
                return
            if library is not None and (parsed.path == "/v1" or parsed.path.startswith("/v1/")):
                self._v1_get(parsed)
                return
            if parsed.path == "/":
                self._file(STATIC / "index.html")
                return
            if parsed.path == "/share.png":
                self._file(STATIC / "share.png")
                return
            if parsed.path == "/favicon.svg":
                self._file(STATIC / "favicon.svg")
                return
            if parsed.path == "/style.css":
                self._file(STATIC / "style.css")
                return
            if parsed.path == "/mail.js":
                self._file(STATIC / "mail.js")
                return
            if parsed.path == "/app.js":
                self._file(STATIC / "app.js")
                return
            if service is None:
                self._json(404, {"error":"没有这个地址"})
                return
            if parsed.path == "/api/doctor":
                self._json(200, {"agents": doctor(service.config)})
                return
            if parsed.path == "/api/jobs":
                self._json(200, {"jobs": [view(service.store, job) for job in service.list_jobs()]})
                return
            if parsed.path == "/api/vault":
                self._json(200, {"notes": service.vault.list_notes(), "path": str(service.vault.root)})
                return
            if parsed.path == "/api/vault/note":
                relative = parse_qs(parsed.query).get("path", [""])[0]
                try:
                    self._json(200, {"path": relative, "text": service.vault.read(relative)})
                except (FileNotFoundError, ValueError) as exc:
                    self._json(404, {"error": str(exc)})
                return
            if parsed.path.startswith("/api/jobs/"):
                parts = parsed.path.strip("/").split("/")
                if len(parts) == 3:
                    self._read_job(parts[2])
                    return
                if len(parts) == 4 and parts[3] == "file":
                    relative = parse_qs(parsed.query).get("path", [""])[0]
                    self._read_artifact(parts[2], relative)
                    return
            self._json(404, {"error": "没有这个地址"})

        def do_POST(self):
            parsed = urlparse(self.path)
            parts = parsed.path.strip("/").split("/")
            if library is not None and parts[:1] == ["v1"]:
                self._v1_post(parts)
                return
            if service is None:
                self._json(404, {"error":"没有这个地址"})
                return
            try:
                if parts == ["api", "jobs"]:
                    body = self._body()
                    job = service.create(str(body.get("brief", "")))
                    self._json(201, view(service.store, job))
                    return
                if parts == ["api", "vault"]:
                    body = self._body()
                    name = service.vault.add(str(body.get("title") or "笔记"), str(body.get("text") or ""))
                    self._json(201, {"path": name})
                    return
                if len(parts) == 4 and parts[:2] == ["api", "jobs"]:
                    job_id, action = parts[2], parts[3]
                    if action == "start":
                        job = service.start(job_id, background=True)
                    elif action == "call":
                        body = self._body()
                        job = service.call(job_id, str(body.get("agent", "")), background=True)
                    elif action == "say":
                        body = self._body()
                        job = service.say(
                            job_id,
                            str(body.get("text", "")),
                            str(body.get("agent") or "codex"),
                            background=True,
                        )
                    elif action == "stop":
                        job = service.stop(job_id)
                    elif action == "confirm-topic":
                        job = service.confirm_topic(job_id)
                    elif action == "confirm-publish":
                        job = service.confirm_publish(job_id, background=True)
                    else:
                        self._json(404, {"error": "没有这个动作"})
                        return
                    self._json(200, view(service.store, job))
                    return
            except KeyError:
                self._json(404, {"error": "没有这个任务"})
                return
            except json.JSONDecodeError:
                self._json(400, {"error": "请求不是 JSON"})
                return
            except (ControlError, ValueError) as exc:
                self._json(409, {"error": str(exc)})
                return
            except Exception as exc:
                self._internal_error(exc)
                return
            self._json(404, {"error": "没有这个地址"})

        def _v1_get(self, parsed):
            try:
                if parsed.path == '/v1/relay/teams' or parsed.path.startswith('/v1/relay/teams/'):
                    from diaodu import relay_teams_http
                    relay_teams_http.get(self, library, parsed)
                    return
                if parsed.path == '/v1/relay' or parsed.path.startswith('/v1/relay/'):
                    from diaodu import relay_http
                    relay_http.get(self, library, parsed)
                    return
                if parsed.path=="/v1/mcp":
                    self._json(405,{"name":"diaodu","version":"2026.10.19","transport":"Streamable HTTP JSON","method":"POST","guide":library.public_base_path+"/v1/agent-guide.md"},{"Allow":"POST"})
                    return
                base_url=(public_origin or ("http://"+self.headers.get("Host","localhost")))+library.public_base_path+"/v1"
                if parsed.path in ("/v1/discover","/v1/tasks.md","/v1/agent-guide.md"):
                    query=parse_qs(parsed.query)
                    discovery=agent_api.discover(library,base_url,int(query.get("offset",["0"])[0]),int(query.get("limit",["50"])[0]))
                    if parsed.path=="/v1/discover":self._json(200,discovery)
                    else:
                        content=agent_api.discovery_markdown(discovery)
                        if parsed.path=="/v1/agent-guide.md":content+="\n---\n"+(Path(__file__).parent/"agent-guide.md").read_text()
                        data=content.encode()
                        self.send_response(200);self.send_header("Content-Type","text/markdown; charset=utf-8");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(data)));self.end_headers()
                        if not getattr(self,"_head_only",False):self.wfile.write(data)
                    return
                if parsed.path=='/v1/mail/unsubscribe':
                    self._json(200,library.outreach.unsubscribe(parse_qs(parsed.query).get('token',[''])[0]));return
                if parsed.path.startswith('/v1/mail/'):

                    actor=self._actor();query=parse_qs(parsed.query);owner=query.get('owner_id',[None])[0]
                    if parsed.path=='/v1/mail/assignments':result=library.outreach.assignments(actor)
                    elif parsed.path=='/v1/mail/outreach':result=library.outreach.status(actor,owner)
                    elif parsed.path=='/v1/mail/status':result=dict(mailbox.status(actor),recommended_agent_id=os.environ.get('DIAODU_OUTREACH_AGENT_ID',''))
                    elif parsed.path=='/v1/mail/inbox':result=mailbox.inbox(actor,owner,query.get('before',[None])[0])
                    elif parsed.path=='/v1/mail/message':result=mailbox.read(actor,query.get('id',[''])[0],owner)
                    elif parsed.path=='/v1/mail/outbox':result=mailbox.outbox(actor)
                    else:raise ValueError('未知邮件地址')
                    self._json(200,result);return
                if parsed.path=="/v1/me/profile":
                    self._json(200,identity.profile(self._actor()))
                    return
                if parsed.path in ("/v1/client.py","/v1/openapi.json"):
                    name=parsed.path.rsplit("/",1)[-1]
                    self._file(Path(__file__).parent/name)
                    return
                if parsed.path == "/v1":
                    from diaodu.library import guide

                    self._json(200, dict(agent_api.discover(library,base_url),**guide()))
                    return
                if parsed.path in ("/v1/me", "/v1/agents"):
                    actor = self._actor()
                if parsed.path == "/v1/me":
                    self._json(200, actor)
                    return
                if parsed.path == "/v1/agents":
                    if actor["kind"] != "user":
                        raise AuthError("需要人员登录",403)
                    self._json(200, {"agents": identity.agents(actor["user_id"])})
                    return
                if parsed.path == "/v1/tasks":
                    query=parse_qs(parsed.query)
                    tasks=library.list_tasks()
                    if query.get("view",[""])[0]=="summary":
                        offset=int(query.get("offset",["0"])[0]);limit=int(query.get("limit",["20"])[0])
                        if offset<0 or not 1<=limit<=100:raise ValueError("offset>=0，limit为1至100")
                        tasks=list(reversed(tasks));batch=tasks[offset:offset+limit]
                        self._json(200,{"tasks":[agent_api.summary(t) for t in batch],"total_tasks":len(tasks),"next_offset":offset+len(batch),"has_more":offset+len(batch)<len(tasks)})
                    else:self._json(200, {"tasks": tasks})
                    return
                parts = parsed.path.strip("/").split("/")
                if len(parts) >= 3 and parts[:2] == ["v1", "tasks"]:
                    parts[2] = library.read_task(parts[2])["id"]
                    if len(parts)==3 or (len(parts)==4 and parts[3]=='context'):
                        library.record_attention(parts[2],'api',self.headers.get('X-Real-IP',self.client_address[0]) if public_origin else self.client_address[0])
                if len(parts)==4 and parts[:2]==["v1","tasks"] and parts[3] in ("context","updates"):
                    actor=None
                    if self.headers.get("Authorization") or "diaodu_session=" in self.headers.get("Cookie",""):
                        actor=self._actor()
                    query=parse_qs(parsed.query)
                    self._json(200,agent_api.context(library,parts[2],actor,int(query.get("entry_after",["0"])[0]),int(query.get("chat_after",["0"])[0]),parts[3]=="updates"))
                    return
                if len(parts) >= 4 and parts[:2] == ["v1", "tasks"] and parts[3] == "chat":
                    actor = self._actor()
                    if len(parts) == 4:
                        after = int(parse_qs(parsed.query).get("after",["0"])[0])
                        self._json(200,chat.read(library,parts[2],actor,after))
                        return
                    if len(parts) == 6 and parts[4] == "files":
                        item,path = chat.locate(library,parts[2],actor,parts[5])
                        self._stream_file(item,path)
                        return
                    self._json(404,{"error":"没有这个群聊地址"})
                    return
                if len(parts) == 5 and parts[:2] == ["v1", "tasks"] and parts[3] == "files":
                    item,path = library.locate_file(parts[2],parts[4])
                    self._stream_file(item,path)
                    return
                if len(parts) == 4 and parts[:2] == ["v1", "tasks"] and parts[3] == "entries":
                    after = int(parse_qs(parsed.query).get("after", ["0"])[0])
                    task = library.read_task(parts[2])
                    if after < 0 or after > task["cursor"]:
                        raise ValueError("after 不在任务记录范围内")
                    self._json(200, {"entries": [e for e in task["entries"] if e["sequence"] > after], "next_cursor": task["cursor"]})
                    return
                if len(parts) == 3 and parts[:2] == ["v1", "tasks"]:
                    self._json(200, library.read_task(parts[2]))
                    return
                self._json(404, {"error": "没有这个地址"})
            except AuthError as exc:
                self._fail(exc)
            except LookupError:
                self._json(404, {"error": "没有这件任务"})
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:
                self._internal_error(exc)

        def _v1_post(self, parts):
            try:
                self._origin()
                if parts[:3] == ['v1', 'relay', 'teams']:
                    from diaodu import relay_teams_http
                    relay_teams_http.post(self, library, parts)
                    return
                if parts[:2] == ['v1', 'relay']:
                    from diaodu import relay_http
                    relay_http.post(self, library, parts)
                    return
                if parts==['v1','mail','unsubscribe']:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<=length<=1024:self._reject_oversized(length,'退订请求过大')
                    self.rfile.read(length)
                    self._json(200,library.outreach.unsubscribe(parse_qs(urlparse(self.path).query).get('token',[''])[0]));return
                if parts==["v1","mcp"]:
                    from diaodu import mcp
                    length=int(self.headers.get("Content-Length","0"))
                    if length<0 or length>15*1024*1024:self._reject_oversized(length,"MCP请求最大15MB")
                    body=json.loads(self.rfile.read(length))
                    if not isinstance(body,dict):raise ValueError("JSON-RPC请求须为对象")
                    actor=self._actor(mutation=True) if self.headers.get("Authorization") or self.headers.get("Cookie") else None
                    base=(public_origin or ("http://"+self.headers.get("Host","localhost")))+library.public_base_path+"/v1"
                    result,sid=mcp.handle(body,self.headers.get("Mcp-Session-Id"),actor,identity,library,base,lambda:identity.throttle(self.headers.get("X-Real-IP",self.client_address[0]),limit=identity.CONNECT_LIMIT,scope="connect"))
                    if body.get('method') == 'tools/call' and str((body.get('params') or {}).get('name', '')).startswith(('diaodu_relay_', 'relay_')):
                        # A cookie/session or Bearer can expire during a long poll. Requests
                        # that authenticate only through the MCP session skip this recheck;
                        # mcp.handle revalidates that session actor against the identity DB.
                        if self.headers.get("Authorization") or self.headers.get("Cookie"):
                            self._actor(mutation=True)
                    if result and not result.get('error') and not result.get('result',{}).get('isError'):
                        params=body.get('params') or {};task_ref=None
                        if body.get('method')=='tools/call' and params.get('name')=='diaodu_read_task':task_ref=(params.get('arguments') or {}).get('task')
                        if body.get('method')=='resources/read' and str(params.get('uri','')).startswith('diaodu://task/'):task_ref=params['uri'][len('diaodu://task/'):]
                        if task_ref:
                            task=library.read_task(task_ref)
                            library.record_attention(task['id'],'api',self.headers.get('X-Real-IP',self.client_address[0]) if public_origin else self.client_address[0])
                    if result is None:
                        self.send_response(202);self.send_header("Content-Length","0");self.end_headers()
                    else:self._json(200,result,{"Mcp-Session-Id":sid} if sid else None)
                    return
                if parts[:2] == ["v1", "auth"] and len(parts) == 3 and parts[2] in ("register", "login"):
                    identity.throttle(self.headers.get("X-Real-IP",self.client_address[0]) if public_origin else self.client_address[0])
                    body = self._body()
                    if parts[2] == "register":
                        result = identity.register(body.get("username"),body.get("display_name"),body.get("password"),body.get("invite_code"))
                        self._json(201,result)
                    else:
                        token, csrf = identity.login(body.get("username"),body.get("password"))
                        self._json(200, {"csrf":csrf}, {"Set-Cookie":self._cookie(token)})
                    return
                if parts == ["v1", "connect"]:
                    identity.throttle(self.headers.get("X-Real-IP",self.client_address[0]) if public_origin else self.client_address[0],limit=identity.CONNECT_LIMIT,scope="connect")
                    body = self._body()
                    result = identity.connect(body.get("alias"),body.get("agent_type","访客"),body.get("member_kind","agent"),body.get("email"),body.get("contact_consent",False))
                    result["csrf"] = digest("csrf:"+result["token"])
                    self._json(201,result,{"Set-Cookie":self._cookie(result["token"])})
                    return
                if len(parts)==4 and parts[:2]==['v1','tasks'] and parts[3]=='view':
                    task=library.read_task(parts[2])
                    cookie=SimpleCookie();cookie.load(self.headers.get('Cookie',''))
                    value=cookie.get('diaodu_visitor');visitor=value.value if value else ''
                    if not re.fullmatch(r'[a-f0-9]{32}',visitor):visitor=secrets.token_hex(16)
                    ua=self.headers.get('User-Agent','').lower()
                    if not any(bot in ua for bot in ('bot','spider','crawler','curl','python','headless')):
                        library.record_attention(task['id'],'browser',visitor)
                    header=f"diaodu_visitor={visitor}; HttpOnly; SameSite=Lax; Path={library.public_base_path}/; Max-Age=31536000"+('; Secure' if secure_cookies else '')
                    self._json(200,library.attention_stats(task['id']),{'Set-Cookie':header})
                    return
                actor = self._actor(mutation=True)
                if parts[:2]==['v1','mail']:
                    body=self._body(limit=8*1024*1024);mail_headers=None
                    if parts==['v1','mail','connect']:
                        result=mailbox.connect(actor,body)
                        cookie=SimpleCookie(self.headers.get('Cookie',''));saved=cookie.get('diaodu_session')
                        if saved and saved.value.startswith('da_'):mail_headers={'Set-Cookie':self._cookie(saved.value).replace('Max-Age=86400','Max-Age=31536000')}
                    elif parts==['v1','mail','disconnect']:
                        library.outreach.configure(actor,False)
                        result=mailbox.disconnect(actor)
                    elif parts==['v1','mail','grant']:
                        with identity.db() as db:target=db.execute('SELECT id FROM agents WHERE id=? AND revoked=0',(body.get('agent_id'),)).fetchone()
                        if not target:raise ValueError('实例不存在或已撤销')
                        result=mailbox.grant(actor,body.get('agent_id'),body.get('allow'))
                    elif parts==['v1','mail','outreach']:result=library.outreach.configure(actor,body.get('enabled'),body.get('auto_reply',False))
                    elif parts==['v1','mail','reply']:result=library.outreach.reply(actor,body)
                    elif parts==['v1','mail','leads']:result=library.outreach.add(actor,body)
                    elif parts==['v1','mail','suppress']:result=library.outreach.remove(actor,body.get('lead_id'))
                    elif parts==['v1','mail','drafts']:result=mailbox.draft(actor,body)
                    elif len(parts)==5 and parts[2]=='drafts' and parts[4]=='send':result=mailbox.send(actor,parts[3],body.get('confirmed'))
                    else:raise ValueError('未知邮件操作')
                    self._json(200,result,mail_headers);return
                if len(parts) >= 3 and parts[:2] == ["v1", "tasks"]:
                    parts[2] = library.read_task(parts[2])["id"]
                if len(parts)==4 and parts[:2]==['v1','tasks'] and parts[3]=='workflow':
                    from diaodu.workflow import update
                    self._json(200,update(library,parts[2],actor,self._body()))
                    return
                if len(parts)==6 and parts[:2]==["v1","tasks"] and parts[3]=="entries" and parts[5]=="review":
                    self._json(200,chat.review(library,parts[2],parts[4],actor,self._body()))
                    return
                if len(parts) == 4 and parts[:2] == ["v1", "tasks"] and parts[3] == "join":
                    self._json(200,chat.join(library,parts[2],actor))
                    return
                if len(parts) >= 4 and parts[:2] == ["v1", "tasks"] and parts[3] == "chat":
                    if len(parts) == 4:
                        self._json(201,chat.post(library,parts[2],actor,self._body()))
                        return
                    if len(parts)==6 and parts[5] in ("pin","promote"):
                        self._json(200,chat.action(library,parts[2],parts[4],actor,parts[5]))
                        return
                    if len(parts) == 5 and parts[4] == "files":
                        chat.read(library,parts[2],actor)
                        length = int(self.headers.get("Content-Length","0"))
                        if length < 0 or length > 100*1024*1024:
                            self._reject_oversized(length, "文件不能超过100MB")
                        filename = parse_qs(urlparse(self.path).query).get("name",[""])[0]
                        content = self.rfile.read(length)
                        if len(content) != length:raise ValueError("上传未完成")
                        self._json(201,chat.upload(library,parts[2],actor,filename,content))
                        return
                    self._json(404,{"error":"没有这个群聊地址"})
                    return
                if len(parts) == 4 and parts[:2] == ["v1", "tasks"] and parts[3] == "delete":
                    self._json(200,library.delete(parts[2],actor))
                    return
                if parts == ["v1","me","profile"]:
                    updated=identity.update_profile(actor,self._body())
                    library.refresh_identity(updated)
                    self._json(200,identity.profile(updated))
                    return
                if parts == ["v1", "me", "alias"]:
                    body = self._body()
                    self._json(200,identity.rename(actor,body.get("alias")))
                    return
                if parts == ["v1", "auth", "logout"]:
                    with mailbox.db() as db:has_mail=db.execute('SELECT 1 FROM accounts WHERE owner=?',((actor.get('agent_id') or actor['user_id']),)).fetchone()
                    if has_mail:
                        library.outreach.configure(actor,False)
                        mailbox.disconnect(actor)
                    cookie = SimpleCookie(self.headers.get("Cookie", ""))
                    if actor["kind"] == "user":
                        identity.logout(cookie["diaodu_session"].value)
                    else:
                        identity.revoke(actor["user_id"],actor["agent_id"])
                    self._json(200, {"logged_out":True}, {"Set-Cookie":self._cookie("",True)})
                    return
                if parts == ["v1", "agents"]:
                    if actor["kind"] != "user":
                        raise AuthError("需要人员登录",403)
                    body = self._body()
                    self._json(201,identity.create_agent(actor["user_id"],body.get("agent_name"),body.get("agent_type")))
                    return
                if len(parts) == 4 and parts[:2] == ["v1", "agents"] and parts[3] == "revoke":
                    if actor["kind"] != "user":
                        raise AuthError("需要人员登录",403)
                    self._json(200,identity.revoke(actor["user_id"],parts[2]))
                    return
                if len(parts) == 4 and parts[:2] == ["v1", "tasks"] and parts[3] == "runs":
                    if actor["kind"] != "agent":
                        raise AuthError("创建执行需要智能体凭证",403)
                    library.read_task(parts[2])
                    chat.join(library,parts[2],actor)
                    self._json(201,identity.start_run(actor,parts[2]))
                    return
                if len(parts) == 4 and parts[:2] == ["v1", "tasks"] and parts[3] == "materials":
                    task = library.read_task(parts[2])
                    publisher = task.get("publisher") or {}
                    if actor.get("user_id") != publisher.get("user_id") or (publisher.get("agent_id") and actor.get("agent_id") != publisher["agent_id"]):
                        raise AuthError("只有任务发布者可以添加任务资料",403)
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 0 or length > 100 * 1024 * 1024:
                        self._reject_oversized(length, "单个文件不能超过 100 MB")
                    filename = parse_qs(urlparse(self.path).query).get("name",[""])[0]
                    content = self.rfile.read(length)
                    if len(content) != length:
                        raise ValueError("上传未完成")
                    self._json(201,library.upload(parts[2],filename,content,actor={k:v for k,v in actor.items() if k != "csrf"},purpose="material"))
                    return
                if len(parts) == 4 and parts[:2] == ["v1", "tasks"] and parts[3] == "files":
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 0 or length > 100 * 1024 * 1024:
                        self._reject_oversized(length, "单个文件不能超过 100 MB")
                    if actor["kind"] != "agent":
                        raise AuthError("上传需要智能体实例凭证",403)
                    query = parse_qs(urlparse(self.path).query)
                    actor = identity.run({k:v for k,v in actor.items() if k != "csrf"},parts[2],query.get("run_id",[None])[0])
                    filename = query.get("name", [""])[0]
                    content = self.rfile.read(length)
                    if len(content) != length:
                        raise ValueError("上传未完成")
                    self._json(201, library.upload(parts[2], filename, content, actor=actor))
                    return
                if parts == ["v1", "tasks"]:
                    body = self._body()
                    self._json(201, library.publish(text(body.get("requirement"),"需求",8000), actor={k:v for k,v in actor.items() if k != "csrf"},reward_amount=body.get("reward_amount")))
                    return
                if len(parts) == 4 and parts[:2] == ["v1", "tasks"] and parts[3] == "say":
                    body = self._body()
                    actor = self._submission_actor(actor,parts[2],body)
                    self._json(200, library.speak(parts[2], actor["agent_name"], text(body.get("text"),"内容",20000), actor=actor))
                    return
                if len(parts) == 4 and parts[:2] == ["v1", "tasks"] and parts[3] == "report":
                    body = self._body()
                    actor = self._submission_actor(actor,parts[2],body)
                    content = body.get("text", "")
                    if not isinstance(content,str) or len(content)>20000:
                        raise ValueError("text 必须是最多 20000 字符的文字")
                    self._json(200, library.report(parts[2], actor["agent_name"], content, body.get("attachments"),
                        status=body.get("status", "completed"), remaining=body.get("remaining", ""),
                        next_step=body.get("next_step", ""), references=body.get("references"), request_id=body.get("request_id"),actor=actor))
                    return
                self._json(404, {"error": "没有这个地址"})
            except AuthError as exc:
                self._fail(exc)
            except LibraryError as exc:
                self._json(409, {"error": str(exc)})
            except LookupError as exc:
                self._json(404, {"error": str(exc)})
            except json.JSONDecodeError:
                self._json(400, {"error": "请求不是 JSON"})
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:
                self._internal_error(exc)

        def _stream_file(self,item,path,content_type=None,inline=True):
            size=path.stat().st_size
            start,end=0,size-1
            ranged=self.headers.get("Range")
            if ranged:
                match=re.fullmatch(r"bytes=(\d*)-(\d*)",ranged)
                valid=bool(match and any(match.groups()) and size)
                if valid:
                    left,right=match.groups()
                    if left:
                        start=int(left);end=min(int(right) if right else size-1,size-1)
                    else:
                        count=int(right);start=max(0,size-count);end=size-1;valid=count>0
                    valid=valid and 0<=start<=end<size
                if not valid:
                    self.send_response(416)
                    self.send_header("Content-Range",f"bytes */{size}")
                    self.send_header("Content-Length","0")
                    self.send_header("Cache-Control","private, no-store")
                    self.end_headers();return
            self.send_response(206 if ranged else 200)
            self.send_header("Accept-Ranges","bytes")
            self.send_header("Content-Type",content_type or (item["type"] if item["type"].startswith(("image/","video/")) and item["type"]!="image/svg+xml" else "application/octet-stream"))
            self.send_header("Content-Length",str(max(0,end-start+1)))
            self.send_header("Content-Disposition",("inline" if inline else "attachment")+"; filename*=UTF-8''"+quote(item["name"]))
            self.send_header("Cache-Control","private, no-store")
            self.send_header("X-Content-Type-Options","nosniff")
            if ranged:self.send_header("Content-Range",f"bytes {start}-{end}/{size}")
            self.end_headers()
            if getattr(self,"_head_only",False):return
            try:
                with path.open("rb") as file:
                    file.seek(start);remaining=max(0,end-start+1)
                    while remaining:
                        chunk=file.read(min(65536,remaining))
                        if not chunk:break
                        self.wfile.write(chunk);remaining-=len(chunk)
            except (BrokenPipeError,ConnectionResetError):pass

        def do_HEAD(self):
            self._head_only=True
            self.do_GET()

        def _submission_actor(self, actor, task_id, body):
            if actor["kind"] != "agent":
                raise AuthError("提交需要智能体实例凭证",403)
            forbidden = {"name","identity","user_id","user_name","agent_id","agent_name","agent_type"}
            if forbidden.intersection(body):
                raise ValueError("身份由凭证确定，不能在正文指定人员或智能体")
            return identity.run({k:v for k,v in actor.items() if k != "csrf"},task_id,body.get("run_id"))

        def log_message(self, fmt, *args):
            return

        def _read_job(self, job_id: str):
            try:
                self._json(200, view(service.store, service.get(job_id)))
            except KeyError:
                self._json(404, {"error": "没有这个任务"})

        def _read_artifact(self, job_id: str, relative: str):
            try:
                self._json(200, {"path": relative, "text": service.store.read_file(job_id, relative)})
            except (KeyError, FileNotFoundError, ValueError) as exc:
                self._json(404, {"error": str(exc)})

        def _reject_oversized(self, length, message):
            """Consume what we can of a rejected body, then close this connection."""
            discard_body(self.rfile, length)
            self.close_connection = True
            raise ValueError(message)

        def _body(self, limit=1_200_000) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > limit:
                self._reject_oversized(length, "请求过大")
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("JSON 正文必须是对象")
            return body

        def _json(self, status: int, payload: dict, headers=None):
            if status >= 400:
                payload=dict(payload,code={400:"invalid_request",401:"unauthenticated",403:"forbidden",404:"not_found",409:"conflict",429:"rate_limited"}.get(status,"server_error"),retryable=status==429 or status>=500)
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            headers = dict(headers or {})
            if status >= 400:
                # An error response may leave an unread request body behind; do not
                # invite the peer to reuse this connection.
                headers.setdefault("Connection", "close")
            for key, value in headers.items():
                self.send_header(key,value)
            self.end_headers()
            if not getattr(self,"_head_only",False):self.wfile.write(data)

        def _fail(self, exc):
            """AuthError to JSON; 429 keeps the wait the caller needs to back off."""
            payload={"error":str(exc)}
            if getattr(exc,"retry_after",None) is not None:
                payload["retry_after"]=exc.retry_after
            self._json(exc.status,payload)

        def _internal_error(self, exc):
            """Log the detail server-side, return only a generic JSON error to the client."""
            traceback.print_exc(file=sys.stderr)
            try:
                self._json(500, {"error": "服务暂时不可用，请稍后重试"})
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _file(self, path: Path):
            if not path.is_file():
                self._json(404, {"error": "页面不存在"})
                return
            data = path.read_bytes()
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", f"{mime}; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if not getattr(self,"_head_only",False):self.wfile.write(data)

    Handler.public_origin = public_origin
    return Handler


def serve(service, host: str, port: int, library=None, identity=None, secure_cookies=False, public_origin=""):
    server = ThreadingHTTPServer((host, port), make_handler(service, library, identity, secure_cookies, public_origin))
    if library is not None:library.outreach.start()
    print(f"任务 http://{host}:{port}", flush=True)
    server.serve_forever()
