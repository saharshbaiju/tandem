"""
update_check.py - tell the user when a newer Tandem release is out.

The TUI asks GitHub for the latest release at most once a day, in the
background, and shows a line in the main menu when it is newer than this
copy (about.VERSION). Nothing is downloaded or installed: updating stays
the user's choice, by running the install command again (UPDATE_COMMAND).

What is sent: one HTTPS GET to api.github.com for the repository's latest
release, with a generic User-Agent. No identifiers, no settings. The answer
is cached in CACHE_PATH, so the other starts that day stay offline.
Set TANDEM_NO_UPDATE_CHECK=1 to turn it off.

GitHub's "latest release" skips pre-releases, so a beta never shows up here.
The Android app does the same check on its side (UpdateCheck.kt).
"""

import json
import os
import re
import threading
import time
import urllib.request

import about

LATEST_URL = "https://api.github.com/repos/saharshbaiju/tandem/releases/latest"
UPDATE_COMMAND = ("curl -fsSL https://raw.githubusercontent.com/saharshbaiju/"
                  "tandem/main/install.sh | bash")
CHECK_EVERY = 24 * 3600          # seconds between asks
TIMEOUT = 5                      # seconds for the request
USER_AGENT = "Tandem-update-check"

CACHE_PATH = os.path.join(os.environ.get("XDG_CACHE_HOME")
                          or os.path.expanduser("~/.cache"),
                          "tandem", "latest-release.json")


def parse_version(text):
    """(major, minor, patch) from "0.8.0", "v0.8.0" or "0.8.0-debug";
    None for anything else. A suffix is dropped: betas never reach the
    latest-release answer, and "-debug" marks a build, not a version."""
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", (text or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


def is_newer(latest, current):
    """True if version string `latest` is newer than `current`."""
    a, b = parse_version(latest), parse_version(current)
    return a is not None and b is not None and a > b


def disabled():
    return os.environ.get("TANDEM_NO_UPDATE_CHECK", "") not in ("", "0")


def _read_cache(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return str(data["version"]), float(data["checked"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_cache(path, version, now):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"version": version, "checked": now}, f)
        os.replace(tmp, path)
    except OSError:
        pass


def fetch_latest(url=LATEST_URL, timeout=TIMEOUT):
    """The latest release's version ("0.8.0"), or None if GitHub can't be
    reached or answers with something unexpected."""
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            tag = json.load(response).get("tag_name", "")
    except Exception:            # offline, rate limited, bad JSON: say nothing
        return None
    version = parse_version(tag)
    return "{}.{}.{}".format(*version) if version else None


def latest_version(now=None, path=CACHE_PATH, fetch=fetch_latest):
    """The latest release's version, from the cache if it was checked in the
    last CHECK_EVERY seconds, otherwise from GitHub. None if unknown."""
    now = time.time() if now is None else now
    cached = _read_cache(path)
    if cached and 0 <= now - cached[1] < CHECK_EVERY:
        return cached[0]
    version = fetch()
    if version:
        _write_cache(path, version, now)
        return version
    return cached[0] if cached else None


class UpdateCheck:
    """Runs latest_version() on a background thread. `available` is the
    newer version once known, else None; the TUI reads it when it redraws."""

    def __init__(self, current=about.VERSION, check=latest_version):
        self.current = current
        self.available = None
        if disabled():
            return
        threading.Thread(target=self._run, args=(check,), name="update-check",
                         daemon=True).start()

    def _run(self, check):
        latest = check()
        if latest and is_newer(latest, self.current):
            self.available = latest
