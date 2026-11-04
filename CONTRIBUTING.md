# Contributing to Tandem

Thanks for taking a look. Bug reports, testing on hardware I don't have, and
pull requests all help.

## Reporting a bug

Open an issue with the bug report template. Tandem runs on a lot of
combinations of distro, desktop, GPU and phone, so the details it asks for
usually decide whether I can reproduce the problem. Please include:

- the output of `tandem --version`
- your distro, desktop (Hyprland or GNOME) and GPU
- your phone model and Android version
- how the phone is connected: USB debugging, USB tethering or Wi-Fi
- the daemon log from the TUI's live view, and if the phone side is involved,
  `adb logcat -s StreamSession`

Check the troubleshooting table in [`docs/PLATFORMS.md`](docs/PLATFORMS.md)
first. Connection problems (firewall, tethering, client isolation on public
Wi-Fi) are covered there.

Security problems go through [`SECURITY.md`](.github/SECURITY.md), not public
issues.

## Suggesting a feature

Open an issue with the feature request template and describe what you're
trying to do. Check the roadmap in the [README](README.md#roadmap) to see if
it's already planned.

## Working on the code

The repository has two halves that talk over one protocol:

- `linux/` is the daemon, the TUI and the desktop backends, in Python. There is
  no build step.
- `android/` is the Kotlin app.

[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) explains how they fit together,
and [`docs/ANDROID.md`](docs/ANDROID.md) covers the app.

You don't need a spare monitor or even Hyprland or GNOME to work on the app.
`testsrc.py` streams a test pattern through the real daemon code:

```bash
python3 linux/testsrc.py --width 2400 --height 1080
python3 linux/testsrc.py --log-touch      # also prints the touches it receives
```

To build and install the app (JDK 17 to 21, or the JBR that ships with Android
Studio):

```bash
cd android
JAVA_HOME=/opt/android-studio/jbr ./gradlew :app:assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

The emulator reaches the PC at `10.0.2.2`. More debugging tips are in
[`ANDROID.md` §7](docs/ANDROID.md#7-developer-install-and-debugging).

## Pull requests

- Keep each PR to one change. Small PRs get reviewed faster.
- If you change the protocol (anything in `FrameReader.kt`, `FrameWriter.kt`,
  `SecureChannel.kt`, `tandemd.py` or `secure_link.py`), change both sides in
  the same PR and say what happens when an older app talks to a newer daemon,
  and the other way round.
- Say what you tested on: distro, desktop, GPU and phone. If you could only test
  with `testsrc.py`, that's fine, just say so.
- Update the docs when behavior changes. The README and `docs/` describe what
  the code does, so they go stale quickly otherwise.
- Keep to the style of the file you're editing.

## License

Tandem is GPL-3.0 with the additional terms in [`NOTICE`](NOTICE). By sending a
pull request you agree that your contribution is released under the same
license.

Everyone taking part is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).
