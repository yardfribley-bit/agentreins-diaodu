"""Shipped pages must only reference stylesheets and scripts the server actually routes."""

import threading
import unittest
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

from diaodu.server import make_handler


class _Refs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = []

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "link" and attr.get("rel") == "stylesheet" and attr.get("href"):
            self.assets.append(attr["href"])
        elif tag == "script" and attr.get("src"):
            self.assets.append(attr["src"])


class StaticAssets(unittest.TestCase):
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

    def fetch(self, url):
        try:
            with urlopen(url, timeout=10) as response:
                return response.status, response.headers.get("Content-Type", ""), response.read()
        except HTTPError as error:
            with error:
                return error.code, error.headers.get("Content-Type", ""), error.read()

    def test_every_css_and_js_reference_returns_200(self):
        pages = ("/", "/relay/", "/relay/watch/sample-token",
                 "/conversations/", "/conversations/C-p00000000/")
        for path in pages:
            page_url = self.base + path
            status, content_type, body = self.fetch(page_url)
            self.assertEqual(status, 200, path)
            self.assertIn("text/html", content_type, path)
            parser = _Refs()
            parser.feed(body.decode())
            self.assertTrue(parser.assets, path + " should reference a stylesheet or script")
            kinds = set()
            for href in parser.assets:
                resolved = urljoin(page_url, href)
                asset_status, asset_type, asset_body = self.fetch(resolved)
                self.assertEqual(asset_status, 200, resolved + " referenced by " + path)
                self.assertGreater(len(asset_body), 0, resolved)
                suffix = urlparse(resolved).path.rsplit(".", 1)[-1]
                kinds.add(suffix)
                if suffix == "css":
                    self.assertIn("text/css", asset_type, resolved)
                elif suffix == "js":
                    self.assertIn("javascript", asset_type, resolved)
            self.assertIn("css", kinds, path)
            self.assertIn("js", kinds, path)
            self.assertTrue(any("tokens.css" in href for href in parser.assets), path)

    def test_legacy_watch_stylesheet_path_is_routed(self):
        # /relay/watch/<token> used to resolve ../../relay.css onto /relay.css.
        status, content_type, body = self.fetch(self.base + "/relay.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", content_type)
        self.assertIn(b"--primary", body)

    def test_shipped_pages_use_the_hub_name(self):
        for path in ("/", "/relay/", "/relay/watch/sample-token",
                     "/conversations/", "/conversations/C-p00000000/"):
            status, _, body = self.fetch(self.base + path)
            self.assertEqual(status, 200, path)
            page = body.decode()
            self.assertNotIn("出海剑", page, path)
            self.assertIn("AgentReins Hub", page, path)
            self.assertIn("对话", page, path)
        _, _, script = self.fetch(self.base + "/relay/relay.js")
        source = script.decode()
        self.assertIn("instruction:'指令'", source)
        self.assertNotIn("人工指令", source)
        self.assertNotIn("出海剑", source)

    def test_workbench_can_start_and_join_both_conversation_modes(self):
        status, _, body = self.fetch(self.base + "/relay/")
        self.assertEqual(status, 200)
        page = body.decode()
        for snippet in (
            "加入已有对话",
            'id="join-dialog"',
            'id="directory-link"',
            'id="message-to"',
            'id="message-file"',
            "不要在消息中粘贴",
            "一对一 Topic",
            "团队席位",
        ):
            self.assertIn(snippet, page)
        _, _, script = self.fetch(self.base + "/relay/relay.js")
        source = script.decode()
        for snippet in (
            "publicConversationHref",
            "searchParams.delete('code')",
            "searchParams.delete('topic_code')",
            "searchParams.delete('invite')",
            "relay/join",
            "relay/teams/join",
            "全体执行端",
            "progress",
            "result",
        ):
            self.assertIn(snippet, source)
        # Preview data includes these fields from the public lookup. The workbench
        # must not copy them into the page; the code stays in the password-like input only.
        self.assertNotIn("invite_code", source)
        self.assertNotIn("join_url", source)
        _, _, watch = self.fetch(self.base + "/relay-watch.js")
        watch_source = watch.decode()
        for secret in ("topic_code", "invite_code", "join_url", "environment", "actor_key"):
            self.assertNotIn(secret, watch_source, secret)


if __name__ == "__main__":
    unittest.main()
