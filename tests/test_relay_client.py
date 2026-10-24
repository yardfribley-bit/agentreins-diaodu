import argparse
import io
import json
import os
import sys
from contextlib import redirect_stderr
from pathlib import Path
import tempfile
import unittest
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from diaodu import relay_client as rc


class Response:
    def __init__(self, payload):
        self.payload = payload

    def read(self, *args):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def http_error(code, payload=None, headers=None):
    body = json.dumps(payload or {"error": "失败"}).encode()
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return HTTPError("http://127.0.0.1/v1/x", code, "boom", message, io.BytesIO(body))


class ClientTests(unittest.TestCase):
    def client(self, steps):
        client = rc.RelayClient("http://127.0.0.1:8765/v1", token="t")
        client.opener = FakeOpener(steps)
        client.sleeps = []
        client._sleep = client.sleeps.append
        return client

    def test_retries_429_with_exponential_backoff(self):
        client = self.client([http_error(429), http_error(429), Response({"ok": True})])
        self.assertEqual(client.request("/relay/sessions"), {"ok": True})
        self.assertEqual(client.sleeps, [0.5, 1.0])
        self.assertEqual(len(client.opener.requests), 3)

    def test_retries_network_error_then_succeeds(self):
        client = self.client([URLError(OSError("boom")), Response({"ok": True})])
        self.assertEqual(client.request("/relay/sessions"), {"ok": True})
        self.assertEqual(client.sleeps, [0.5])

    def test_retry_after_header_is_honored(self):
        client = self.client([http_error(429, headers={"Retry-After": "2"}), Response({"ok": True})])
        client.request("/relay/sessions")
        self.assertEqual(client.sleeps, [2.0])

    def test_gives_up_after_max_retries(self):
        client = self.client([http_error(503) for _ in range(4)])
        with self.assertRaises(rc.RelayError) as caught:
            client.request("/relay/sessions")
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(len(client.sleeps), 3)

    def test_non_idempotent_write_is_not_retried(self):
        client = self.client([http_error(500)])
        with self.assertRaises(rc.RelayError) as caught:
            client.create("标题", "目标")
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual(client.sleeps, [])
        self.assertEqual(len(client.opener.requests), 1)

    def test_send_reuses_one_request_id_across_transport_retries(self):
        client = self.client([http_error(429), Response({"event": {"seq": 1}})])
        client.send("sid", "result", "完成", request_id="fixed-1")
        bodies = [json.loads(request.data) for request in client.opener.requests]
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(bodies[0]["request_id"], "fixed-1")

    def test_send_generates_request_id_when_missing(self):
        calls = []
        client = rc.RelayClient("http://127.0.0.1:8765/v1", token="t")
        client.request = lambda path, body=None, **kwargs: calls.append(body) or {"ok": True}
        client.send("sid", "note", "备注")
        client.send("sid", "note", "备注")
        self.assertTrue(calls[0]["request_id"])
        self.assertNotEqual(calls[0]["request_id"], calls[1]["request_id"])


class CursorTests(unittest.TestCase):
    def test_legacy_state_only_has_acked_cursor(self):
        config = {"cursors": {"sid": 5}}
        self.assertEqual(rc.acked_cursor(config, "sid"), 5)
        self.assertIsNone(rc.read_cursor(config, "sid"))

    def test_read_and_acked_cursors_are_independent(self):
        config = {"cursors": {"sid": 5}}
        rc.remember_read_cursor(config, "sid", 7)
        self.assertEqual(rc.read_cursor(config, "sid"), 7)
        self.assertEqual(rc.acked_cursor(config, "sid"), 5)

    def test_missing_or_invalid_values_fall_back(self):
        self.assertEqual(rc.acked_cursor({}, "sid"), 0)
        self.assertIsNone(rc.read_cursor({}, "sid"))
        self.assertEqual(rc.acked_cursor({"cursors": {"sid": "x"}}, "sid"), 0)
        self.assertIsNone(rc.read_cursor({"read_cursors": {"sid": -1}}, "sid"))
        rc.remember_read_cursor({}, "sid", -1)


class FakeClient:
    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = []

    def events(self, sid, after, wait):
        self.calls.append((sid, after, wait))
        return self.batches.pop(0)


class WatchTests(unittest.TestCase):
    def test_watch_prints_each_event_and_stops_on_interrupt_with_cursor(self):
        batches = [
            {"events": [{"seq": 3, "kind": "message"}], "next_cursor": 3},
            {"events": [{"seq": 4, "kind": "message"}], "next_cursor": 4},
            {"events": [{"seq": 5, "kind": "message"}], "next_cursor": 5},
        ]
        client, printed, saved = FakeClient(batches), [], []

        def emit(line):
            printed.append(json.loads(line))
            if len(printed) == 3:
                raise KeyboardInterrupt

        cursor = rc.watch_loop(client, "sid", 2, 20, emit=emit, on_cursor=saved.append)
        self.assertEqual(cursor, 4)
        self.assertEqual([line["seq"] for line in printed], [3, 4, 5])
        self.assertEqual(saved, [3, 4])
        self.assertEqual(client.calls, [("sid", 2, 20), ("sid", 3, 20), ("sid", 4, 20)])

    def test_watch_rejects_a_stalled_cursor(self):
        with self.assertRaises(rc.RelayError):
            rc.watch_loop(FakeClient([{"events": [], "next_cursor": 1}]), "sid", 2, 20, emit=lambda line: None)

    def test_wait_argument_is_bounded(self):
        for value in (0, 21, -1):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                rc._wait_seconds(str(value))
        self.assertEqual(rc._wait_seconds("20"), 20)


class CLITests(unittest.TestCase):
    URL = "http://127.0.0.1:8765/v1"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name) / "client.json"
        rc.save_state(self.state, {self.URL: {"token": "t", "last_session": "rl_last",
                                             "cursors": {"rl_last": 2}}})

    def tearDown(self):
        self.temporary.cleanup()

    def argv(self, *rest):
        return ["--url", self.URL, "--state-file", str(self.state), *rest]

    def run_cli(self, *rest):
        with patch.object(sys, "argv", ["relay_client.py", *self.argv(*rest)]):
            rc.main()

    def fake_client(self):
        patcher = patch.object(rc, "RelayClient")
        self.addCleanup(patcher.stop)
        client = patcher.start().return_value
        client.token = "t"
        client.events.return_value = {"events": [], "next_cursor": 2, "has_more": False}
        client.get.return_value = {"session": {"id": "rl_last"}}
        client.ack.return_value = {"acknowledged": 3}
        return client

    def test_events_and_ack_default_to_last_session(self):
        client = self.fake_client()
        with redirect_stderr(io.StringIO()):
            self.run_cli("events", "--wait", "0")
            self.run_cli("ack", "3")
        client.events.assert_called_once_with("rl_last", 2, 0)
        client.ack.assert_called_once_with("rl_last", 3)
        self.assertEqual(rc.load_state(self.state)[self.URL]["cursors"]["rl_last"], 3)

    def test_get_without_session_uses_last_session(self):
        client = self.fake_client()
        self.run_cli("get")
        client.get.assert_called_once_with("rl_last")

    def test_ack_keeps_explicit_session_form(self):
        client = self.fake_client()
        self.run_cli("ack", "rl_other", "4")
        client.ack.assert_called_once_with("rl_other", 4)

    def test_join_watch_starts_at_join_point_and_remembers_session(self):
        client = self.fake_client()
        client.join.return_value = {"session": {"id": "rl_new", "last_seq": 9}}
        with patch.dict(os.environ, {"DIAODU_PAIR_CODE": "pair"}), \
                patch.object(rc, "watch_loop", return_value=10) as loop, redirect_stderr(io.StringIO()):
            self.run_cli("join", "--role", "worker", "--watch")
        args = loop.call_args
        self.assertEqual(args.args[0], client)
        self.assertEqual(args.args[1], "rl_new")
        self.assertEqual(args.args[2], 9)
        self.assertIsNotNone(args.kwargs.get("on_cursor"))
        self.assertEqual(rc.load_state(self.state)[self.URL]["last_session"], "rl_new")

    def test_missing_session_is_a_usage_error(self):
        rc.save_state(self.state, {self.URL: {"token": "t"}})
        self.fake_client()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            self.run_cli("get")
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
