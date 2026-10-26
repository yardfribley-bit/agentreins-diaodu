import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diaodu.control import ControlError, actions
from diaodu.runners import CallResult, CallSpec, LiveRunners, RunnerError
from diaodu.service import Service, load_config
from diaodu.store import Store


class FakeRunners:
    def __init__(self):
        self.calls = []

    def run(self, job_id, spec, on_pid):
        self.calls.append((job_id, spec.agent, spec.publish))
        on_pid(1)
        return CallResult(True, f"{spec.agent} 说完了")

    def stop(self, job_id):
        return None


class BlockingRunners:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.stopped = False

    def run(self, job_id, spec, on_pid):
        on_pid(42)
        self.started.set()
        self.release.wait(3)
        return CallResult(False, "已停止", stopped=True)

    def stop(self, job_id):
        self.stopped = True
        self.release.set()


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runners = FakeRunners()
        self.service = Service(Store(Path(self.tmp.name)), self.runners, load_config(Path("/no/config.toml")))

    def tearDown(self):
        self.tmp.cleanup()

    def test_platform_orders_the_four(self):
        job = self.service.create("拆几条抖音，写成公众号")
        with self.assertRaises(ControlError):
            self.service.call(job["id"], "workbuddy")

        job = self.service.start(job["id"])
        self.assertEqual(job["phase"], "analyze")
        allowed = actions(job)
        self.assertIn("codex", allowed["call"])
        self.assertNotIn("workbuddy", allowed["call"])
        self.assertTrue((Path(self.tmp.name) / job["id"] / "work" / "research.json").is_file())

        with self.assertRaises(ControlError):
            self.service.confirm_topic(job["id"])

        self.service.call(job["id"], "doubao")
        job = self.service.call(job["id"], "grok")
        self.assertTrue(actions(job)["confirm_topic"])
        job = self.service.confirm_topic(job["id"])
        self.assertEqual(job["phase"], "draft")
        self.assertIn("workbuddy", actions(job)["call"])

        job = self.service.call(job["id"], "workbuddy")
        self.assertTrue(actions(job)["confirm_publish"])
        publish_before = [item for item in self.runners.calls if item[2]]
        self.assertEqual(publish_before, [])

        job = self.service.confirm_publish(job["id"])
        self.assertEqual(job["phase"], "done")
        self.assertEqual(self.runners.calls[-1][1:], ("doubao", True))
        self.assertFalse(actions(job)["call"])
        self.assertTrue((Path(self.tmp.name) / job["id"] / "work" / "douyin" / "publish-pack" / "status.md").is_file())

    def test_say_calls_the_person_named_in_the_chat(self):
        job = self.service.create("聊天")
        job = self.service.say(job["id"], "hello", "workbuddy")
        self.assertEqual(self.runners.calls[-1][1:], ("workbuddy", False))
        self.assertEqual(job["messages"][0]["role"], "user")
        self.assertEqual(job["messages"][-1]["text"], "workbuddy 说完了")
        self.assertEqual(job["phase"], "collect")

    def test_stop_discards_the_turn(self):
        runners = BlockingRunners()
        service = Service(Store(Path(self.tmp.name)), runners, load_config(Path("/no/config.toml")))
        job = service.create("停下来")
        done = threading.Event()
        holder = {}

        def run():
            holder["job"] = service.start(job["id"])
            done.set()

        threading.Thread(target=run).start()
        self.assertTrue(runners.started.wait(2))
        service.stop(job["id"])
        self.assertTrue(done.wait(3))
        self.assertTrue(runners.stopped)
        self.assertIsNone(holder["job"]["running"])
        self.assertEqual(holder["job"]["phase"], "collect")
        self.assertTrue(any(message["text"].startswith("已停止") for message in holder["job"]["messages"]))

    def test_runner_error_stays_on_the_same_step(self):
        class Boom(FakeRunners):
            def run(self, job_id, spec, on_pid):
                raise RunnerError("豆包还不能被点名")

        service = Service(Store(Path(self.tmp.name)), Boom(), load_config(Path("/no/config.toml")))
        job = service.create("看错误")
        job = service.start(job["id"])
        self.assertEqual(job["phase"], "collect")
        self.assertEqual(job["messages"][-1]["kind"], "error")
        self.assertIsNone(job["running"])


class StoreAtomicTest(unittest.TestCase):
    def test_concurrent_readers_never_see_a_partial_job_file(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = Store(Path(temporary.name))
        job = store.create("同时读")
        stop = threading.Event()
        errors = []

        def read() -> None:
            while not stop.is_set():
                try:
                    loaded = store.load(job["id"])
                except Exception as exc:
                    errors.append(exc)
                    stop.set()
                    return
                if loaded["id"] != job["id"] or not isinstance(loaded["messages"], list):
                    errors.append(AssertionError(loaded))
                    stop.set()
                    return

        threads = [threading.Thread(target=read) for _ in range(4)]
        for thread in threads:
            thread.start()
        try:
            for index in range(200):
                job["tick"] = index
                job["messages"] = [{"text": "x" * 80, "n": index} for _ in range(6)]
                store.save(job)
        finally:
            stop.set()
            for thread in threads:
                thread.join(2)
        self.assertEqual(errors, [])
        self.assertEqual(store.load(job["id"])["tick"], 199)
        leftovers = list(Path(temporary.name, job["id"]).glob(".job-*.tmp"))
        self.assertEqual(leftovers, [])


class PathTest(unittest.TestCase):
    def test_codex_uses_the_proxy_and_ignores_the_broken_model_config(self):
        runners = LiveRunners(load_config(Path("/no/config.toml")))
        spec = CallSpec("codex", "hello", Path("/tmp/diaodu-ping"), False, 30, chat=True)
        command = runners._codex_cmd(spec)
        self.assertIn("--ignore-user-config", command)
        self.assertEqual(command[-1], "hello")
        self.assertIn("read-only", command)
        env = runners._child_env()
        self.assertEqual(env["https_proxy"], "http://127.0.0.1:1087")
        self.assertNotIn("ALL_PROXY", env)
        self.assertNotIn("all_proxy", env)


if __name__ == "__main__":
    unittest.main()
