import io
import json
import sqlite3
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import test_identity as fixtures


class ResilienceAPI(unittest.TestCase):
    setUp = fixtures.IdentityAPI.setUp
    tearDown = fixtures.IdentityAPI.tearDown
    req = fixtures.IdentityAPI.req

    def assert_generic_server_error(self, status, body):
        self.assertEqual(status, 500)
        self.assertEqual(body.get("code"), "server_error")
        self.assertTrue(body.get("retryable"))
        serialized = json.dumps(body, ensure_ascii=False)
        for leak in ("OperationalError", "database is locked", "board.json", "Traceback", 'File "'):
            self.assertNotIn(leak, serialized)

    def test_get_internal_error_returns_json_500(self):
        log = io.StringIO()
        with patch.object(self.library, "read_task",
                          side_effect=sqlite3.OperationalError(
                              "database is locked at /opt/diaodu/data/library/board.json")), \
                redirect_stderr(log):
            status, body = self.req("GET", "/v1/tasks/" + self.task["number"])
        self.assert_generic_server_error(status, body)
        self.assertIn("OperationalError", log.getvalue())

    def test_post_internal_error_returns_json_500(self):
        log = io.StringIO()
        with patch.object(self.library, "publish",
                          side_effect=sqlite3.OperationalError("disk I/O error")), redirect_stderr(log):
            status, body = self.req("POST", "/v1/tasks", {"requirement": "x"}, agent=0)
        self.assert_generic_server_error(status, body)
        self.assertIn("OperationalError", log.getvalue())

    def test_attention_stats_failure_keeps_reads_working(self):
        with patch.object(self.library.attention, "stats",
                          side_effect=sqlite3.OperationalError("stats unavailable")):
            status, body = self.req("GET", "/v1/tasks/" + self.task["number"])
        self.assertEqual(status, 200)
        self.assertEqual(body["attention"]["page_views"], 0)
        self.assertEqual(body["attention"]["api_reads"], 0)
        self.assertIn("started_at", body["attention"])

    def test_attention_record_failure_keeps_reads_working(self):
        with patch.object(self.library.attention, "record",
                          side_effect=sqlite3.OperationalError("record unavailable")):
            status, body = self.req("GET", "/v1/tasks/" + self.task["number"] + "/context")
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["id"], self.task["id"])

    def test_known_errors_stay_4xx(self):
        path = "/v1/tasks/" + self.task["number"]
        self.assertEqual(self.req("GET", "/v1/tasks/T999999")[0], 404)
        self.assertEqual(self.req("GET", path + "/chat")[0], 401)
        self.assertEqual(self.req("GET", path + "/chat", user=1)[0], 403)
        self.assertEqual(self.req("POST", path + "/chat", {"text": "x"}, user=1)[0], 403)
        self.assertEqual(self.req("GET", path + "/updates?chat_after=-1", user=0)[0], 400)
        self.assertEqual(self.req("GET", path + "/entries?after=999999")[0], 400)


if __name__ == "__main__":
    unittest.main()
