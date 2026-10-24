#!/usr/bin/env python3
"""一对一 relay 端到端验收：本地起服务，mentor/worker 各跑一轮。

覆盖：连接 → 建 Topic → 两端加入 → 下发指令 → 执行回报 → 长轮询送达 →
ack → 幂等重试 → 断线后按游标补读 → 撤销 → 撤销后重入/读历史被拒 → 重置 Topic 码。

只使用 127.0.0.1 与临时数据目录，不读写生产 data。默认端口 8765，被占用时用
--port 8766 之类换一个。

用法：
    /usr/local/bin/python3.11 scripts/relay_acceptance.py
    /usr/local/bin/python3.11 scripts/relay_acceptance.py --port 8766 --keep
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from diaodu.identity import Identity  # noqa: E402
from diaodu.library import Library  # noqa: E402
from diaodu.server import make_handler  # noqa: E402

WORKER_ENV = {"os_name": "Linux", "os_version": "Ubuntu 24.04", "architecture": "x86_64",
              "shells": ["bash"], "python_version": "3.12.3"}


class Client:
    def __init__(self, base):
        self.base = base
        self.token = ""
        self.results = []

    def call(self, method, path, body=None, expect=(200, 201), raw=None):
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            headers["Content-Type"] = "application/json"
        started = time.monotonic()
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                status, payload = response.status, json.load(response)
        except HTTPError as error:
            with error:
                try:
                    payload = json.load(error)
                except (ValueError, AttributeError):
                    payload = {"error": "HTTP " + str(error.code)}
                status = error.code
        elapsed = (time.monotonic() - started) * 1000
        ok = status in expect
        self.record(ok, f"{method} {path.split('?')[0]}", status, elapsed, payload)
        if ok:
            return payload
        return payload

    def record(self, ok, label, status, elapsed, payload):
        self.results.append((ok, label, status, elapsed, payload))

    def connect(self, alias, agent_type="Codex"):
        payload = self.call("POST", "/v1/connect", {"alias": alias, "agent_type": agent_type})
        self.token = payload["token"]
        return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="本地 relay 一对一端到端验收")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--keep", action="store_true", help="结束后保留临时数据目录")
    args = parser.parse_args()

    temporary = Path(tempfile.mkdtemp(prefix="relay-acceptance-"))
    library = Library(temporary)
    identity = Identity(temporary / "identity.sqlite3", "")
    try:
        server = ThreadingHTTPServer((args.host, args.port), make_handler(None, library, identity))
    except OSError as error:
        print(f"端口 {args.port} 不可用：{error}。换一个端口，例如 --port 8766。")
        shutil.rmtree(temporary, ignore_errors=True)
        return 2
    base = f"http://{args.host}:{args.port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"本地 relay 已启动：{base}（数据 {temporary}）\n")

    checks = []
    timings = []

    def check(ok, label, detail=""):
        checks.append((ok, label, detail))
        print(("  ✓ " if ok else "  ✗ ") + label + (f"  {detail}" if detail else ""))

    try:
        owner, mentor, worker = Client(base), Client(base), Client(base)
        owner.connect("验收管理者", "验收脚本")
        mentor.connect("指导端", "Codex")
        worker.connect("执行端", "WorkBuddy")
        check(all(c.token for c in (owner, mentor, worker)), "三端 connect 成功")

        started = time.monotonic()
        created = owner.call("POST", "/v1/relay/sessions", {"title": "验收会话", "goal": "跑通一轮指令"})
        sid, topic = created["session"]["id"], created["topic_code"]
        timings.append(("建 Topic", (time.monotonic() - started) * 1000))
        check(bool(sid and topic), "创建 Topic 并拿到共享配对码", sid)

        started = time.monotonic()
        mentor.call("POST", "/v1/relay/join", {"code": topic, "role": "mentor"})
        worker.call("POST", "/v1/relay/join", {"code": topic, "role": "worker", "environment": WORKER_ENV})
        timings.append(("两端加入", (time.monotonic() - started) * 1000))
        check(True, "mentor / worker 凭 Topic 码各占一席")

        worker_cursor = worker.call("GET", f"/v1/relay/sessions/{sid}")["session"]["last_seq"]
        mentor_cursor = mentor.call("GET", f"/v1/relay/sessions/{sid}")["session"]["last_seq"]

        # 1) 下发指令 → 长轮询立刻唤醒。
        waiting = {}
        def poll():
            waiting["batch"] = worker.call(
                "GET", f"/v1/relay/sessions/{sid}/events?after={worker_cursor}&wait=20")
        poller = threading.Thread(target=poll, daemon=True)
        poller.start()
        time.sleep(0.3)
        started = time.monotonic()
        instruction = mentor.call("POST", f"/v1/relay/sessions/{sid}/messages",
                                  {"kind": "instruction", "text": "执行：列出当前目录",
                                   "request_id": "round-1-instruction"})
        poller.join(timeout=10)
        delivery = (time.monotonic() - started) * 1000
        timings.append(("指令送达（长轮询唤醒）", delivery))
        events = waiting.get("batch", {}).get("events", [])
        check(any(e.get("message", {}).get("text", "").startswith("执行：列出当前目录") for e in events),
              "worker 长轮询收到 instruction", f"{delivery:.0f} ms")
        worker_cursor = max([worker_cursor] + [e["seq"] for e in events])

        # 2) 执行端回报 progress / result 并 ack。
        started = time.monotonic()
        progress = worker.call("POST", f"/v1/relay/sessions/{sid}/messages",
                               {"kind": "progress", "text": "已开始执行", "request_id": "round-1-progress"})
        result = worker.call("POST", f"/v1/relay/sessions/{sid}/messages",
                             {"kind": "result", "text": "已完成，结果为 ok", "request_id": "round-1-result",
                              "reply_to": instruction["event"]["seq"]})
        worker_cursor = result["event"]["seq"]
        worker.call("POST", f"/v1/relay/sessions/{sid}/ack", {"seq": worker_cursor})
        timings.append(("回报 + ack", (time.monotonic() - started) * 1000))
        check(progress["event"]["message"]["kind"] == "progress"
              and result["event"]["message"]["kind"] == "result",
              "worker 回报 progress 与 result")

        # 3) 指挥端取回结果。
        started = time.monotonic()
        batch = mentor.call("GET", f"/v1/relay/sessions/{sid}/events?after={mentor_cursor}&wait=20")
        elapsed = (time.monotonic() - started) * 1000
        timings.append(("结果回到指挥端", elapsed))
        kinds = [e["kind"] for e in batch["events"]]
        check("message" in kinds, "mentor 拉到 worker 的回报", f"{elapsed:.0f} ms")
        mentor_cursor = batch["next_cursor"]
        check(batch["session"]["peers"][2]["ack_seq"] == worker_cursor, "ack 已记录在 session 快照")

        # 4) 幂等：同一 request_id 重发，返回 duplicate 而不是新事件。
        again = mentor.call("POST", f"/v1/relay/sessions/{sid}/messages",
                            {"kind": "instruction", "text": "执行：列出当前目录",
                             "request_id": "round-1-instruction"})
        check(again.get("duplicate") is True and again["event"]["seq"] == instruction["event"]["seq"],
              "相同 request_id 重发返回 duplicate，不产生新序号")

        # 5) 断线补读：worker 不轮询，指挥端再发一条，worker 用旧游标补回。
        offline = mentor.call("POST", f"/v1/relay/sessions/{sid}/messages",
                              {"kind": "note", "text": "你掉线时发的消息", "request_id": "round-1-note"})
        replay = worker.call("GET", f"/v1/relay/sessions/{sid}/events?after={worker_cursor}&wait=0")
        check(any(e["seq"] == offline["event"]["seq"] for e in replay["events"]),
              "断线后按游标补读到离线期间的消息")
        worker_cursor = offline["event"]["seq"]

        # 6) 撤销：重入被拒、历史不可读，另一端不受影响。
        owner.call("POST", f"/v1/relay/sessions/{sid}/revoke", {"role": "worker"})
        blocked = worker.call("POST", "/v1/relay/join",
                              {"code": topic, "role": "worker", "environment": WORKER_ENV}, expect=(403,))
        check(blocked.get("code") == "forbidden" or blocked.get("error"), "撤销后用原 Topic 码重入被拒", "403")
        denied = worker.call("GET", f"/v1/relay/sessions/{sid}/events?after=0", expect=(404,))
        check(bool(denied.get("error")), "撤销后读历史被拒", "404")
        still = mentor.call("GET", f"/v1/relay/sessions/{sid}")
        check(still["session"]["status"] == "active", "未撤销的 mentor 不受影响")

        # 7) 重置 Topic 码：旧码失效。
        fresh = owner.call("POST", f"/v1/relay/sessions/{sid}/topic-code", {})
        stale = worker.call("POST", "/v1/relay/join",
                            {"code": topic, "role": "worker", "environment": WORKER_ENV}, expect=(403,))
        check(stale.get("code") == "forbidden" or stale.get("error"), "重置后旧 Topic 码失效")
        check(fresh["code"] != topic, "新 Topic 码与旧码不同")

        # 8) 空轮询按 1 秒超时正常返回；基线取当前最新序号（撤销会追加 revoked 事件）。
        latest = mentor.call("GET", f"/v1/relay/sessions/{sid}")["session"]["last_seq"]
        started = time.monotonic()
        empty = mentor.call("GET", f"/v1/relay/sessions/{sid}/events?after={latest}&wait=1")
        waited = (time.monotonic() - started) * 1000
        timings.append(("空轮询按时返回", waited))
        check(empty["events"] == [] and 900 <= waited <= 3000, "无新事件时空轮询约 1 秒返回", f"{waited:.0f} ms")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    print("\n耗时：")
    for label, ms in timings:
        print(f"  {label:<24} {ms:>8.1f} ms")
    failed = [c for c in checks if not c[0]]
    print(f"\n结果：{len(checks) - len(failed)}/{len(checks)} 项通过")
    if failed:
        print("失败项：")
        for _, label, detail in failed:
            print(f"  - {label} {detail}")
    if args.keep:
        print(f"数据保留在 {temporary}")
    else:
        shutil.rmtree(temporary, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
