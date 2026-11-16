"""Who made Tandem, which version this is, and under what license.

CREDIT is the attribution notice that the GPL section 7 terms in NOTICE
require every copy and modified version to keep; `tandem --version` and
`tandemd.py --version` print it, and the Android app shows it on its connect
screen. Keep it.

VERSION is the release this copy of the PC side belongs to. It must equal
`versionName` in android/app/build.gradle.kts (a unit test checks this), so
both halves of a release report the same number; docs/RELEASING.md bumps
them together.
"""

VERSION = "0.8.0"
CREDIT = "Tandem by Saharsh Baiju · github.com/saharshbaiju/tandem"
LICENSE = "GPL-3.0 with additional terms; see LICENSE and NOTICE"


def version_text():
    """What `tandem --version` and `tandemd.py --version` print."""
    return f"Tandem {VERSION}\n{CREDIT}\n{LICENSE}"
