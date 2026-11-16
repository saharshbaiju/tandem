"""Tests for the "a newer Tandem is out" check (update_check.py) and the
line it adds to the TUI's main menu."""

import support  # noqa: F401  (must come first: sys.path + scratch HOME)

import json
import os
import tempfile
import time
import unittest
from unittest import mock

import about
import update_check


class VersionTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(update_check.parse_version("0.8.0"), (0, 8, 0))
        self.assertEqual(update_check.parse_version("v0.10.2"), (0, 10, 2))
        self.assertEqual(update_check.parse_version("0.8.0-debug"), (0, 8, 0))
        self.assertEqual(update_check.parse_version(" v1.2.3 "), (1, 2, 3))

    def test_parse_rejects_junk(self):
        for text in ("", None, "latest", "v1.2", "x0.8.0"):
            self.assertIsNone(update_check.parse_version(text), text)

    def test_newer_compares_numbers_not_text(self):
        self.assertTrue(update_check.is_newer("0.10.0", "0.9.9"))
        self.assertTrue(update_check.is_newer("0.8.0", "0.7.2"))
        self.assertTrue(update_check.is_newer("1.0.0", "0.99.99"))
        self.assertFalse(update_check.is_newer("0.7.2", "0.7.2"))
        self.assertFalse(update_check.is_newer("0.7.1", "0.7.2"))
        self.assertFalse(update_check.is_newer("garbage", "0.7.2"))
        self.assertFalse(update_check.is_newer("0.8.0", "garbage"))


class LatestVersionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "tandem", "latest.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_asks_github_then_caches_for_a_day(self):
        calls = []
        fetch = lambda: calls.append(1) or "0.8.0"
        self.assertEqual(update_check.latest_version(1000.0, self.path, fetch), "0.8.0")
        with open(self.path) as f:
            self.assertEqual(json.load(f), {"version": "0.8.0", "checked": 1000.0})
        # Within the day: answered from the cache, GitHub not asked again.
        self.assertEqual(update_check.latest_version(1000.0 + 3600, self.path, fetch), "0.8.0")
        self.assertEqual(len(calls), 1)
        # A day later: asked again.
        later = 1000.0 + update_check.CHECK_EVERY + 1
        update_check.latest_version(later, self.path, lambda: calls.append(1) or "0.8.1")
        self.assertEqual(len(calls), 2)

    def test_offline_keeps_the_old_answer(self):
        update_check.latest_version(1000.0, self.path, lambda: "0.8.0")
        later = 1000.0 + update_check.CHECK_EVERY + 1
        self.assertEqual(update_check.latest_version(later, self.path, lambda: None), "0.8.0")

    def test_offline_with_no_cache_is_unknown(self):
        self.assertIsNone(update_check.latest_version(1000.0, self.path, lambda: None))

    def test_a_clock_moved_back_asks_again(self):
        update_check.latest_version(5000.0, self.path, lambda: "0.8.0")
        calls = []
        update_check.latest_version(100.0, self.path, lambda: calls.append(1) or "0.8.0")
        self.assertEqual(calls, [1])

    def test_a_broken_cache_file_is_ignored(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertEqual(update_check.latest_version(1000.0, self.path, lambda: "0.8.0"), "0.8.0")


class FetchTest(unittest.TestCase):
    def _response(self, body):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = body
        return response

    def test_reads_the_tag(self):
        body = json.dumps({"tag_name": "v0.8.0", "html_url": "x"}).encode()
        with mock.patch("urllib.request.urlopen", return_value=self._response(body)) as urlopen:
            self.assertEqual(update_check.fetch_latest(), "0.8.0")
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, update_check.LATEST_URL)
        # A generic agent: nothing about the user or the machine.
        self.assertEqual(request.get_header("User-agent"), update_check.USER_AGENT)

    def test_errors_and_odd_answers_are_none(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            self.assertIsNone(update_check.fetch_latest())
        for body in (b"not json", b'{"message": "API rate limit exceeded"}',
                     b'{"tag_name": "nightly"}'):
            with mock.patch("urllib.request.urlopen", return_value=self._response(body)):
                self.assertIsNone(update_check.fetch_latest(), body)


class UpdateCheckTest(unittest.TestCase):
    def wait(self, check):
        deadline = time.monotonic() + 2
        while check.available is None and time.monotonic() < deadline:
            time.sleep(0.01)

    def test_newer_release_is_available(self):
        with mock.patch.dict(os.environ, {"TANDEM_NO_UPDATE_CHECK": ""}):
            check = update_check.UpdateCheck("0.7.2", check=lambda: "0.8.0")
        self.wait(check)
        self.assertEqual(check.available, "0.8.0")

    def test_same_or_older_is_not(self):
        for latest in ("0.7.2", "0.7.0", None):
            with mock.patch.dict(os.environ, {"TANDEM_NO_UPDATE_CHECK": ""}):
                check = update_check.UpdateCheck("0.7.2", check=lambda: latest)
            time.sleep(0.05)
            self.assertIsNone(check.available, latest)

    def test_can_be_turned_off(self):
        calls = []
        with mock.patch.dict(os.environ, {"TANDEM_NO_UPDATE_CHECK": "1"}):
            check = update_check.UpdateCheck("0.7.2", check=lambda: calls.append(1) or "9.9.9")
        time.sleep(0.05)
        self.assertEqual(calls, [])
        self.assertIsNone(check.available)

    def test_defaults_to_this_copys_version(self):
        with mock.patch.dict(os.environ, {"TANDEM_NO_UPDATE_CHECK": "1"}):
            self.assertEqual(update_check.UpdateCheck().current, about.VERSION)


if __name__ == "__main__":
    unittest.main()
