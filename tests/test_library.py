import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diaodu.library import Library, LibraryError
from diaodu.server import make_handler


class BoardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.library = Library(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_open_handoff_and_retry(self):
        task = self.library.publish("共同完成调研")
        task = self.library.report(task["id"], "Codex", "整理来源", status="in_progress", remaining="分析", next_step="核对来源", request_id="first")
        first = task["entries"][-1]
        retry = self.library.report(task["id"], "Codex", "整理来源", status="in_progress", remaining="分析", next_step="核对来源", request_id="first")
        self.assertEqual(len(retry["entries"]), 1)
        with self.assertRaises(LibraryError):
            self.library.report(task["id"], "Codex", "不同内容", request_id="first")
        continued = self.library.report(task["id"], "Claude", "核对完成", references=[first["id"]])
        self.assertEqual(continued["status"], "open")
        self.assertEqual(continued["entries"][-1]["status"], "completed")
        self.assertEqual(continued["entries"][-1]["references"], [first["id"]])
        self.library.report(task["id"], "豆包", "仍可继续补充")

    def test_deployment_attachment_urls_include_prefix(self):
        local = self.library
        task = local.publish("部署附件")
        item = local.upload(task["id"], "report.txt", b"shared")
        local.report(task["id"], "Claude", "成果", attachments=[item["id"]])
        deployed = Library(Path(self.tmp.name), "/task")
        result = deployed.read_task(task["id"])
        expected = f"/task/v1/tasks/{task['id']}/files/{item['id']}"
        self.assertEqual(result["files"][0]["url"], expected)
        self.assertEqual(result["entries"][0]["attachments"][0]["url"], expected)
        self.assertEqual(deployed.read_file(task["id"], item["id"])[1], b"shared")
        uploaded = deployed.upload(task["id"], "next.txt", b"next")
        self.assertTrue(uploaded["url"].startswith("/task/v1/"))

    def test_legacy_tasks_can_continue(self):
        task = self.library.publish("历史任务")
        data = json.loads(self.library._path.read_text())
        data["tasks"][0]["status"] = "closed"
        self.library._path.write_text(json.dumps(data))
        self.assertEqual(self.library.report(task["id"], "Claude", "继续工作")["status"], "open")

    def test_task_numbers_persist_and_resolve(self):
        first=self.library.publish('第一件')
        second=self.library.publish('第二件')
        self.assertEqual(first['number'],'T000001')
        self.assertEqual(second['number'],'T000002')
        self.assertEqual(self.library.read_task('t000001')['id'],first['id'])
        restarted=Library(Path(self.tmp.name))
        self.assertEqual(restarted.read_task('T000002')['id'],second['id'])
        restarted.report('T000001','Codex','完成')
        self.assertEqual(len(restarted.read_task(first['id'])['entries']),1)

    def test_existing_tasks_receive_stable_numbers(self):
        self.library.publish('历史任务')
        data=json.loads(self.library._path.read_text())
        del data['tasks'][0]['number']
        self.library._path.write_text(json.dumps(data))
        migrated=self.library.list_tasks()[0]
        self.assertEqual(migrated['number'],'T000001')
        self.assertEqual(json.loads(self.library._path.read_text())['tasks'][0]['number'],'T000001')
