#!/usr/bin/env python3
"""Demonstrate a team session: one mentor + three workers.

One broadcast instruction, one instruction targeted at seat 2, then verify that
workers 1 and 3 cannot see the targeted instruction while worker 2 can, collect
worker 2's result, and aggregate by reply_to so missing seats are obvious.

    /usr/local/bin/python3.11 scripts/team_demo.py
    /usr/local/bin/python3.11 scripts/team_demo.py --port 8768 --keep
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from diaodu.identity import Identity  # noqa: E402
from diaodu.library import Library  # noqa: E402
from diaodu.server import make_handler  # noqa: E402

ENVIRONMENT = {"os_name": "macOS", "os_version": "demo", "architecture": "arm64",
               "shells": ["bash"], "python_version": "3.11"}


def main() -> int:
    parser = argparse.ArgumentParser(description="团队会话演示：一个 mentor + 三个 worker")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--keep", action="store_true", help="结束后保留临时数据目录")
    args = parser.parse_args()

    temporary = Path(tempfile.mkdtemp(prefix="team-demo-"))
    library = Library(temporary, "")
    identity = Identity(temporary / "identity.sqlite3", "")
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port),
                                     make_handler(None, library, identity))
    except OSError as error:
        print(f"端口 {args.port} 不可用：{error}，换一个 --port。")
        shutil.rmtree(temporary, ignore_errors=True)
        return 2
    base = f"http://127.0.0.1:{args.port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"本地团队演示已启动：{base}（数据 {temporary}）\n")

    def call(method, path, body=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = json.dumps(body).encode() if body is not None else None
        try:
            with urlopen(Request(base + path, data=data, headers=headers, method=method), timeout=30) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def connect(alias):
        status, payload = call("POST", "/v1/connect", {"alias": alias, "agent_type": "Dev"})
        assert status == 201, payload
        return payload

    def invite(role, seat=None):
        body = {"role": role}
        if seat is not None:
            body["seat"] = seat
        status, payload = call("POST", f"/v1/relay/teams/{sid}/invites", body, owner["token"])
        assert status == 201, payload
        return payload["invite"]["code"]

    def join(token, code, role=None):
        body = {"code": code}
        if role:
            body["role"] = role
            if role == "mentor":
                status, payload = call("POST", "/v1/relay/teams/join", body, token)
                assert status == 200, payload
                return payload["session"]
        body["environment"] = dict(ENVIRONMENT)
        status, payload = call("POST", "/v1/relay/teams/join", body, token)
        assert status == 200, payload
        return payload["session"]

    def send(token, kind, text, request_id, **extra):
        return call("POST", f"/v1/relay/teams/{sid}/messages",
                    {"kind": kind, "text": text, "request_id": request_id, **extra}, token)[1]["event"]

    def events(token, after=0):
        status, payload = call("GET", f"/v1/relay/teams/{sid}/events?after={after}&wait=0", token=token)
        assert status == 200, payload
        return payload["events"]

    checks = []

    def check(ok, label, detail=""):
        checks.append(ok)
        print(("  ✓ " if ok else "  ✗ ") + label + (f"  {detail}" if detail else ""))

    owner = connect("演示管理者")
    mentor = connect("演示指挥端")
    workers = {name: connect(name) for name in ("worker-1", "worker-2", "worker-3")}
    status, created = call("POST", "/v1/relay/teams", {"title": "团队演示", "goal": "一个指挥三个执行"},
                           owner["token"])
    assert status == 201, created
    sid = created["session"]["id"]
    mentor_session = join(mentor["token"], invite("mentor"), "mentor")
    seats = {}
    for name, token in workers.items():
        seats[name] = join(token["token"], invite("worker"))["seat"]
    check(mentor_session["seat"] == 0, "mentor 占指导席")
    check(sorted(seats.values()) == [1, 2, 3], "三个 worker 自动分配 1/2/3 号席", seats)

    broadcast = send(mentor["token"], "instruction", "全体：报告各自进度", "demo-broadcast")
    targeted = send(mentor["token"], "instruction", "只给 2 号：生成一份报告",
                    "demo-target", to="worker:2")
    print(f"\n广播指令 #{broadcast['seq']}，定向指令 #{targeted['seq']} → worker:2\n")

    seen = {}
    for name, token in workers.items():
        sequences = [event["seq"] for event in events(token["token"])]
        seen[name] = sequences
    check(targeted["seq"] in seen["worker-2"], "worker-2 能看到定向指令")
    check(targeted["seq"] not in seen["worker-1"], "worker-1 看不到定向指令")
    check(targeted["seq"] not in seen["worker-3"], "worker-3 看不到定向指令")
    check(all(broadcast["seq"] in seq for seq in seen.values()), "广播指令三个 worker 都能看到")

    result = send(workers["worker-2"]["token"], "result", "2 号报告已生成",
                  "demo-result", reply_to=targeted["seq"], attachments=[])
    worker_1_sees = [event["seq"] for event in events(workers["worker-1"]["token"])]
    check(result["seq"] not in worker_1_sees, "worker-1 读不到 worker-2 的结果")

    mentor_events = events(mentor["token"])
    results_by_reply = {}
    for event in mentor_events:
        message = event.get("message") or {}
        if message.get("kind") == "result":
            results_by_reply.setdefault(message.get("reply_to"), []).append(event["sender"])
    print("\n按 reply_to 汇总（指挥端视角）")
    print("  指令        目标        已回报座位")
    for instruction, label in ((broadcast, "全体"), (targeted, "worker:2")):
        returned = results_by_reply.get(instruction["seq"], [])
        seats_returned = ", ".join(str(sender.get("seat")) for sender in returned) or "（无）"
        print(f"  #{instruction['seq']:<10} {label:<10} {seats_returned}")
    check(any(sender.get("seat") == 2 for sender in results_by_reply.get(targeted["seq"], [])),
          "指挥端按 reply_to 看到 2 号席的结果")
    missing = [seat for seat in (1, 3)
               if not any(sender.get("seat") == seat for sender in results_by_reply.get(broadcast["seq"], []))]
    check(missing == [1, 3], "广播指令的未回报席位可被识别", missing)

    server.shutdown()
    server.server_close()
    print(f"\n结果：{sum(checks)}/{len(checks)} 项通过")
    if args.keep:
        print(f"数据保留在 {temporary}")
    else:
        shutil.rmtree(temporary, ignore_errors=True)
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
