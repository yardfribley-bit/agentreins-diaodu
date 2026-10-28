"""The relay workbench is a projection of Hub data, not the Stitch mock."""

import re
import threading
import unittest
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from diaodu.server import make_handler

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "relay.html").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "relay.css").read_text(encoding="utf-8")
JS = (ROOT / "static" / "relay.js").read_text(encoding="utf-8")

BANNED = (
    "hub.agentreins.io",
    "agentreins.io",
    "Mesh-US-East",
    "99.98",
    "v2.4.12",
    "MCP 2024-11-05",
    "TASK-202505",
    "animate-pulse",
    "animate-ping",
    "CVE-2024-21626",
)


class _Headings(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.tags.append(tag)


class RelayWorkbenchSource(unittest.TestCase):
    def test_nav_and_topic_title_do_not_wrap_or_invent_a_host(self):
        nav = HTML.split('id="site-nav"', 1)[1].split("</nav>", 1)[0]
        for label in ("任务", "对话", "通信工作台", "接入"):
            self.assertIn(label, nav)
            self.assertIn('title="' + label + '"', nav)
        self.assertNotIn("<br", nav.lower())
        self.assertIn('id="nav-toggle"', HTML)
        self.assertIn(">菜单<", HTML)
        self.assertIn("white-space:nowrap", CSS.replace(" ", ""))
        self.assertIn(".nav-toggle", CSS)
        self.assertIn("nav-check:checked", CSS.replace(" ", ""))
        self.assertIn("text-overflow:ellipsis", CSS.replace(" ", ""))
        self.assertIn(".topic-title", CSS)
        self.assertIn('class="topic-title"', HTML)
        for banned in BANNED:
            self.assertNotIn(banned, HTML, banned)
            self.assertNotIn(banned, CSS, banned)
            self.assertNotIn(banned, JS, banned)

    def test_heading_order_and_type_ramp(self):
        parser = _Headings()
        parser.feed(HTML)
        self.assertEqual(parser.tags[0], "h1")
        self.assertNotIn("h3", parser.tags)
        self.assertNotIn("h4", parser.tags)
        self.assertIn("h2", parser.tags)
        sizes = [float(item) for item in re.findall(r"font-size:\s*(\d+(?:\.\d+)?)px", CSS)]
        self.assertTrue(sizes)
        self.assertGreaterEqual(min(sizes), 12)
        for step in (32, 25, 16):
            self.assertIn(step, sizes)
        self.assertNotIn("@keyframes", CSS)
        self.assertNotIn("animation:", CSS)

    def test_workbench_keeps_existing_flows_and_real_data_only(self):
        for marker in (
            "我的对话",
            "创建沟通 Topic",
            "加入已有对话",
            'id="join-dialog"',
            'id="directory-link"',
            'id="message-to"',
            'id="message-file"',
            'id="session-search"',
            'data-filter="paused"',
            "发送类型",
            "发送对象",
            "0 / 8000",
            "Ctrl / ⌘ + Enter",
            "不要在消息中粘贴",
            "一对一 Topic",
            "团队席位",
            "连接状态与服务端",
            "参会席位",
            "各席回报",
            "任务板",
            "邀请与接入",
            ">生成邀请<",
            "暂停指令",
            "结束 Topic",
            "撤销两端访问",
            "公开围观",
            ">导出<",
        ):
            self.assertIn(marker, HTML, marker)
        for marker in (
            "publicConversationHref",
            "searchParams.delete('code')",
            "searchParams.delete('topic_code')",
            "searchParams.delete('invite')",
            "relay/join",
            "relay/teams/join",
            "全体执行端",
            "instruction:'指令'",
            "progress",
            "result",
            "isProtocolNoise",
            "waiter_busy",
            "实时同步在另一个标签页中进行，此页按 3 秒轮询",
            "presentCount",
            "visibleWorkEvent",
            "待答复",
            "reply-quote",
            "ack_seq",
            "exportTranscript",
            "showWorkbenchPanel",
        ):
            self.assertIn(marker, JS, marker)
        self.assertNotIn("innerHTML", JS)
        self.assertNotIn("'号席'", JS)
        self.assertNotIn('"号席"', JS)
        self.assertNotIn("invite_code", JS)
        self.assertNotIn("join_url", JS)
        self.assertNotIn("人工指令", JS)
        # Pairing material stays in the dialogs. The rail must not render a live code.
        rail = HTML.split('id="col-rail"', 1)[1].split("</aside>", 1)[0]
        self.assertNotIn("<code", rail.lower())
        self.assertNotIn("value=\"http", rail.lower())


class RelayWorkbenchPage(unittest.TestCase):
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

    def test_workbench_shell(self):
        with urlopen(self.base + "/relay/", timeout=10) as response:
            page = response.read().decode()
            self.assertEqual(response.status, 200)
        self.assertIn("AgentReins Hub", page)
        self.assertIn("通信工作台", page)
        self.assertIn("我的对话", page)
        self.assertNotIn("hub.agentreins.io", page)
        self.assertNotIn("出海剑", page)
        self.assertNotIn("animate-pulse", page)


if __name__ == "__main__":
    unittest.main()
