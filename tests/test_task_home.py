"""The task lobby is a projection of Hub data, not the Stitch mock."""

import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from diaodu.server import make_handler

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


class TaskHomeSource(unittest.TestCase):
    def test_nav_labels_stay_on_one_line(self):
        nav = HTML.split('id="site-nav"', 1)[1].split("</nav>", 1)[0]
        for label in ("任务", "对话", "通信工作台", "接入", "发布任务", "我的接入", "使用指南"):
            self.assertIn(label, nav)
        self.assertNotIn("<br", nav.lower())
        self.assertIn('id="nav-toggle"', HTML)
        self.assertIn("white-space:nowrap", CSS.replace(" ", ""))
        self.assertIn(".nav-toggle", CSS)
        self.assertIn("nav-check:checked", CSS.replace(" ", ""))

    def test_lobby_uses_the_documented_hub_endpoint(self):
        self.assertIn("https://www.chuhaijian.com/task/v1/mcp", APP)
        self.assertIn("https://www.chuhaijian.com/task/v1/mcp/design/", APP)
        self.assertIn("任务协作大厅", APP)
        self.assertIn("有报告", APP)
        self.assertIn("生成接入凭证", APP)
        for client in ("Codex", "Cursor", "Claude", "Grok", "WorkBuddy", "豆包"):
            self.assertIn(client, APP)
        for banned in (
            "hub.agentreins.io",
            "agentreins.io",
            "SOC2",
            "集群心跳",
            "CLUSTER_ID",
            "SHA-256",
            "v2.4.12",
            "Mesh-US-East",
        ):
            self.assertNotIn(banned, APP)
            self.assertNotIn(banned, HTML)

    def test_homepage_keeps_existing_flows(self):
        for marker in ('id="new-task"', 'id="my-agents"', 'id="usage-guide"', 'id="api-guide"',
                       'id="alias-dialog"', 'id="token-dialog"', 'id="credential-note"'):
            self.assertIn(marker, HTML)
        self.assertIn("凭证只显示这一次", HTML)
        self.assertIn("createdToken", APP)
        self.assertIn("issueToken", APP)


class TaskHomePage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:" + str(cls.server.server_address[1])

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.server_close()

    def test_homepage_shell(self):
        with urlopen(self.base + "/", timeout=10) as response:
            page = response.read().decode()
            self.assertEqual(response.status, 200)
        self.assertIn("AgentReins Hub", page)
        self.assertIn("对话", page)
        self.assertIn("通信工作台", page)
        self.assertNotIn("hub.agentreins.io", page)
        self.assertNotIn("出海剑", page)
