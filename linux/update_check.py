"""
update_check.py - tell the user when a newer Tandem release is out.

The TUI asks GitHub for the latest release every time it starts, in the
background, and shows a line in the main menu when it is newer than this
copy (about.VERSION). Nothing is downloaded or installed: updating stays
the user's choice, by running the install command again (UPDATE_COMMAND).

What is sent: one HTTPS GET to api.github.com for the repository's latest
release, with a generic User-Agent. No identifiers, no settings. The answer
and its ETag are kept in CACHE_PATH: the next ask sends the ETag
(If-None-Match), and while nothing changed GitHub answers "304 Not
Modified", which doesn't count against its limit of 60 anonymous requests an
hour. Offline, or when GitHub can't answer, the cached answer is used.
Set TANDEM_NO_UPDATE_CHECK=1 to turn it off.

GitHub's "latest release" skips pre-releases, so a beta never shows up here.
The Android app does the same check on its side (UpdateCheck.kt).
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

import about

LATEST_URL = "https://api.github.com/repos/saharshbaiju/tandem/releases/latest"
UPDATE_COMMAND = ("curl -fsSL https://raw.githubusercontent.com/saharshbaiju/"
                  "tandem/main/install.sh | bash")
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


UNCHANGED = "unchanged"         # fetch_latest: GitHub said 304 Not Modified


def _read_cache(path):
    """(version, etag or None) from the cache file, or None."""
    try:
        with open(path) as f:
            data = json.load(f)
        etag = data.get("etag")
        return str(data["version"]), etag if isinstance(etag, str) else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _write_cache(path, version, etag, now):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"version": version, "etag": etag, "checked": now}, f)
        os.replace(tmp, path)
    except OSError:
        pass


def fetch_latest(etag=None, url=LATEST_URL, timeout=TIMEOUT):
    """(answer, etag): answer is the latest release's version ("0.8.0"),
    UNCHANGED if it is still the one `etag` names, or None if GitHub can't
    be reached or answers with something unexpected."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if etag:
        headers["If-None-Match"] = etag
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            tag = json.load(response).get("tag_name", "")
            new_etag = response.headers.get("ETag")
    except urllib.error.HTTPError as e:
        return (UNCHANGED, etag) if e.code == 304 else (None, None)
    except Exception:            # offline, rate limited, bad JSON: say nothing
        return None, None
    version = parse_version(tag)
    return ("{}.{}.{}".format(*version) if version else None), new_etag


def latest_version(now=None, path=CACHE_PATH, fetch=fetch_latest):
    """The latest release's version, asked of GitHub every time (cheaply,
    with the cached ETag); the cached answer when GitHub can't answer. None
    if unknown."""
    now = time.time() if now is None else now
    cached = _read_cache(path)
    answer, etag = fetch(cached[1] if cached else None)
    if answer == UNCHANGED and cached:
        return cached[0]
    if answer and answer != UNCHANGED:
        _write_cache(path, answer, etag, now)
        return answer
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
