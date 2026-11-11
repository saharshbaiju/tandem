"""Tests for the niri backend's IPC client and event-stream watcher against
a fake niri listening on a real UNIX socket: Ok / Err unwrapping, a niri
without virtual outputs, silent and broken servers, and the debounced
re-apply on ConfigLoaded."""

import support  # noqa: F401  (must come first: sys.path + scratch HOME)

import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

import backends
from backends import niri


class FakeSocketServer:
    """A UNIX socket that answers each connection with `respond(request)`.

    `respond` returns the bytes to send (None: send nothing and keep the
    connection open until the test ends). Requests are recorded.
    """

    def __init__(self, respond):
        self.respond = respond
        self.requests = []
        self.directory = tempfile.mkdtemp(prefix="tandem-niri-sock-")
        self.path = os.path.join(self.directory, "niri.sock")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        self.listener.listen(8)
        self.stopped = threading.Event()
        self.connections = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.listener.settimeout(0.1)
        while not self.stopped.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections.append(connection)
            threading.Thread(target=self._handle, args=(connection,),
                             daemon=True).start()

    def _handle(self, connection):
        buffer = b""
        while b"\n" not in buffer:
            try:
                chunk = connection.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
        request = json.loads(buffer.split(b"\n")[0])
        self.requests.append(request)
        reply = self.respond(request)
        if reply is None:
            return
        try:
            if callable(reply):
                reply(connection)
            else:
                connection.sendall(reply)
                connection.close()
        except OSError:
            pass

    def close(self):
        self.stopped.set()
        self.listener.close()
        for connection in self.connections:
            try:
                connection.close()
            except OSError:
                pass
        shutil.rmtree(self.directory, ignore_errors=True)


def line(value):
    return json.dumps(value).encode() + b"\n"


class IpcTestCase(unittest.TestCase):
    def serve(self, respond):
        server = FakeSocketServer(respond)
        self.addCleanup(server.close)
        patch = mock.patch.dict(os.environ, {"NIRI_SOCKET": server.path})
        patch.start()
        self.addCleanup(patch.stop)
        return server


class RequestTest(IpcTestCase):
    def test_ok_is_unwrapped(self):
        server = self.serve(lambda request: line(
            {"Ok": {"Version": "26.04 (v26.04-139-g7fd585dd)"}}))
        self.assertEqual(niri.request("Version"),
                         {"Version": "26.04 (v26.04-139-g7fd585dd)"})
        self.assertEqual(niri.version(), "26.04 (v26.04-139-g7fd585dd)")
        self.assertEqual(server.requests, ["Version", "Version"])

    def test_err_raises_niri_error(self):
        self.serve(lambda request: line(
            {"Err": "output 'Tandem' not found"}))
        with self.assertRaises(niri.NiriError) as caught:
            niri.request({"RemoveVirtualOutput": {"name": "Tandem"}})
        self.assertEqual(caught.exception.message,
                         "output 'Tandem' not found")
        self.assertFalse(niri.remove_output())   # "not found" is fine

    def test_reply_split_across_reads(self):
        def slow(connection):
            payload = line({"Ok": {"Outputs": {"A": {"name": "A"}}}})
            for index in range(0, len(payload), 7):
                connection.sendall(payload[index:index + 7])
                time.sleep(0.001)
            connection.close()
        self.serve(lambda request: slow)
        self.assertEqual(niri.outputs(), {"A": {"name": "A"}})

    def test_silent_niri_times_out(self):
        self.serve(lambda request: None)
        start = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "did not answer"):
            niri.request("Outputs", timeout=0.3)
        self.assertLess(time.monotonic() - start, 2.0)
        self.assertIsNone(niri.query("Outputs", timeout=0.3))

    def test_connection_closed_without_answer(self):
        self.serve(lambda request: b"")
        with self.assertRaisesRegex(RuntimeError, "without answering"):
            niri.request("Outputs")

    def test_garbage_reply(self):
        self.serve(lambda request: b"not json\n")
        with self.assertRaisesRegex(RuntimeError, "can't read"):
            niri.request("Outputs")
        self.assertIsNone(niri.outputs())

    def test_unexpected_reply_shape(self):
        self.serve(lambda request: line({"Something": 1}))
        with self.assertRaisesRegex(RuntimeError, "unexpected reply"):
            niri.request("Outputs")

    def test_no_socket(self):
        with mock.patch.dict(os.environ, {"NIRI_SOCKET": ""}):
            with self.assertRaisesRegex(RuntimeError, "NIRI_SOCKET"):
                niri.request("Outputs")
        with mock.patch.dict(os.environ,
                             {"NIRI_SOCKET": "/nonexistent/niri.sock"}):
            with self.assertRaisesRegex(RuntimeError, "cannot reach niri"):
                niri.request("Outputs")
            self.assertIsNone(niri.outputs())
            self.assertIsNone(niri.virtual_outputs_supported())

    def test_output_action_must_be_applied(self):
        replies = iter([line({"Ok": {"OutputConfigChanged": "Applied"}}),
                        line({"Ok": {"OutputConfigChanged":
                                     "OutputWasMissing"}})])
        server = self.serve(lambda request: next(replies))
        niri.output_action("Tandem", {"Scale": {"scale": {"Specific": 1.5}}})
        with self.assertRaisesRegex(RuntimeError, "did not apply"):
            niri.output_action("Tandem", {"Position": {
                "position": "Automatic"}})
        self.assertEqual(server.requests[0], {"Output": {
            "output": "Tandem",
            "action": {"Scale": {"scale": {"Specific": 1.5}}}}})


class SupportDetectionTest(IpcTestCase):
    def test_stable_niri(self):
        def stable(request):
            if request == "Version":
                return line({"Ok": {"Version": "26.04 (8ed0da4)"}})
            return line({"Err": "error parsing request"})
        server = self.serve(stable)
        self.assertIs(niri.virtual_outputs_supported(), False)
        self.assertEqual(server.requests,
                         [{"RemoveVirtualOutput": {"name": ""}}])
        message = niri.unsupported_message()
        self.assertIn("this niri (26.04 (8ed0da4)) cannot create virtual "
                      "monitors", message)
        problem = niri.session_problem()
        self.assertTrue(problem[0].startswith("This niri (26.04"))

    def test_niri_with_virtual_outputs(self):
        self.serve(lambda request: line({"Err": "output '' not found"}))
        self.assertIs(niri.virtual_outputs_supported(), True)
        self.assertIsNone(niri.session_problem())

    def test_create_on_stable_niri_explains(self):
        def stable(request):
            if request == "Outputs":
                return line({"Ok": {"Outputs": {}}})
            if request == "Version":
                return line({"Ok": {"Version": "26.04 (8ed0da4)"}})
            return line({"Err": "error parsing request"})
        self.serve(stable)
        backend = niri.NiriBackend(mock.Mock(width=2400, height=1080, fps=60,
                                             scale=1.5, side="right"))
        backend._watcher = mock.Mock()
        with mock.patch.object(niri, "log"):
            with self.assertRaisesRegex(RuntimeError,
                                        r"this niri \(26\.04 \(8ed0da4\)\) "
                                        "cannot create virtual monitors"):
                backend.setup()
        backend._watcher.start.assert_not_called()


class SessionHooksTest(unittest.TestCase):
    def live_socket(self):
        directory = tempfile.mkdtemp(prefix="tandem-niri-live-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = os.path.join(directory, "niri.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(path)
        return path

    def test_confidence(self):
        with mock.patch.dict(os.environ, {"NIRI_SOCKET": self.live_socket(),
                                          "XDG_CURRENT_DESKTOP": "GNOME"}):
            self.assertEqual(niri.session_confidence(), 3)
        with mock.patch.dict(os.environ, {"NIRI_SOCKET": "",
                                          "XDG_CURRENT_DESKTOP": "niri"}):
            self.assertEqual(niri.session_confidence(), 1)
        with mock.patch.dict(os.environ, {"NIRI_SOCKET": "",
                                          "XDG_CURRENT_DESKTOP": "KDE"}):
            self.assertEqual(niri.session_confidence(), 0)

    def test_a_stale_socket_variable_does_not_claim_the_session(self):
        # Left behind by a crashed niri, seen in a later GNOME login.
        with mock.patch.dict(os.environ, {
                "NIRI_SOCKET": "/nonexistent/niri.sock",
                "XDG_CURRENT_DESKTOP": "GNOME",
                "WAYLAND_DISPLAY": "wayland-0"}, clear=True):
            self.assertEqual(niri.session_confidence(), 0)
            self.assertEqual(backends.detect(), "gnome")
        not_a_socket = os.path.join(os.environ["HOME"], "plain-file")
        with open(not_a_socket, "w", encoding="utf-8"):
            pass
        self.addCleanup(os.unlink, not_a_socket)
        with mock.patch.dict(os.environ, {"NIRI_SOCKET": not_a_socket,
                                          "XDG_CURRENT_DESKTOP": "niri"}):
            self.assertEqual(niri.session_confidence(), 1)

    def test_no_socket_is_a_session_problem(self):
        with mock.patch.dict(os.environ, {"NIRI_SOCKET": ""}):
            self.assertIn("NIRI_SOCKET is not set",
                          niri.session_problem()[0])

    def test_a_missing_socket_is_named_not_called_unset(self):
        with mock.patch.dict(os.environ,
                             {"NIRI_SOCKET": "/nonexistent/niri.sock"}):
            first = niri.session_problem()[0]
        self.assertNotIn("not set", first)
        self.assertIn("/nonexistent/niri.sock", first)


class EventWatcherTest(IpcTestCase):
    def stream(self, events, keep_open=1.5):
        """A server whose EventStream answers Handled, then `events`
        (list of (delay, event dict)), then stays open."""
        def run(connection):
            connection.sendall(line({"Ok": "Handled"}))
            for delay, event in events:
                time.sleep(delay)
                connection.sendall(line(event))
            time.sleep(keep_open)
            connection.close()
        return self.serve(lambda request: run)

    def watch(self, debounce=0.2):
        calls = []
        watcher = niri.EventWatcher(lambda: calls.append(time.monotonic()),
                                    debounce=debounce)
        watcher.start()
        self.addCleanup(watcher.stop)
        return watcher, calls

    def test_replayed_config_event_is_skipped(self):
        server = self.stream([(0, {"WorkspacesChanged": {"workspaces": []}}),
                              (0, {"ConfigLoaded": {"failed": False}})])
        _watcher, calls = self.watch()
        time.sleep(0.8)
        self.assertEqual(calls, [])
        self.assertEqual(server.requests[0], "EventStream")

    def test_reload_burst_reapplies_once(self):
        replay = (0, {"ConfigLoaded": {"failed": False}})
        reload = (0.1, {"ConfigLoaded": {"failed": False}})
        self.stream([replay, reload, reload, reload])
        _watcher, calls = self.watch()
        time.sleep(1.0)
        self.assertEqual(len(calls), 1)

    def test_a_failed_replay_is_skipped_too(self):
        # Tandem started while the config had a parse error: the replay is
        # failed:true, and the fix that follows is a real reload.
        self.stream([(0, {"ConfigLoaded": {"failed": True}}),
                     (0.1, {"ConfigLoaded": {"failed": False}})])
        _watcher, calls = self.watch()
        time.sleep(0.8)
        self.assertEqual(len(calls), 1)

    def test_failed_reload_is_ignored(self):
        self.stream([(0, {"ConfigLoaded": {"failed": False}}),
                     (0.1, {"ConfigLoaded": {"failed": True}})])
        _watcher, calls = self.watch()
        time.sleep(0.8)
        self.assertEqual(calls, [])

    def test_stop_ends_the_thread(self):
        self.stream([], keep_open=5)
        watcher, _calls = self.watch()
        time.sleep(0.2)
        watcher.stop(timeout=2)
        self.assertFalse(watcher._thread.is_alive())

    def test_callback_errors_are_logged_not_fatal(self):
        replay = (0, {"ConfigLoaded": {"failed": False}})
        reload = (0.3, {"ConfigLoaded": {"failed": False}})
        self.stream([replay, reload, (0.5, reload[1])])
        calls = []

        def failing():
            calls.append(1)
            raise RuntimeError("the Tandem output is gone")

        with mock.patch.object(niri, "log") as log:
            watcher = niri.EventWatcher(failing, debounce=0.05)
            watcher.start()
            self.addCleanup(watcher.stop)
            time.sleep(1.3)
        self.assertEqual(len(calls), 2)
        log.assert_any_call("warning: could not re-apply Tandem "
                            "(the Tandem output is gone)")

    def test_config_loaded(self):
        self.assertIs(niri.EventWatcher.config_loaded(
            b'{"ConfigLoaded":{"failed":false}}'), True)
        self.assertIs(niri.EventWatcher.config_loaded(
            b'{"ConfigLoaded":{"failed":true}}'), False)
        self.assertIsNone(niri.EventWatcher.config_loaded(
            b'{"WindowClosed":{"id":3}}'))
        self.assertIsNone(niri.EventWatcher.config_loaded(b"garbage"))
        self.assertIsNone(niri.EventWatcher.config_loaded(b'"Handled"'))


if __name__ == "__main__":
    unittest.main()
