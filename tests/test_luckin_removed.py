"""The abandoned Luckin promo is gone from pages, the API, and MCP."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from diaodu.identity import Identity
from diaodu.library import Library
from diaodu.server import make_handler


class LuckinRemoved(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.library = Library(root)
        self.identity = Identity(root / "identity.sqlite3")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, self.library, self.identity))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.tmp.cleanup()

    def raw(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        headers = {} if data is None else {"Content-Type": "application/json"}
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except HTTPError as exc:
            return exc.code, exc.read()

    def test_routes_are_404_and_tools_list_has_no_luckin_tool(self):
        self.assertFalse(hasattr(self.library, "luckin"))
        self.assertEqual(self.raw("GET", "/luckin/")[0], 404)
        self.assertEqual(self.raw("GET", "/v1/luckin/menu")[0], 404)

        status, raw = self.raw("POST", "/v1/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        self.assertEqual(status, 200)
        names = [tool["name"] for tool in json.loads(raw)["result"]["tools"]]
        self.assertIn("diaodu_connect", names)
        self.assertIn("diaodu_list_tasks", names)
        self.assertFalse(any("luckin" in name.lower() for name in names))


if __name__ == "__main__":
    unittest.main()
