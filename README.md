# Tandem

Turn an Android phone into a **real, low-latency second monitor** for Linux —
like spacedesk on Windows, but native and GPU-accelerated end to end.

| Desktop | Distros | How the virtual monitor is made |
| --- | --- | --- |
| **Hyprland** | Arch (and anything running Hyprland) | `hyprctl output create headless` + wf-recorder / VA-API (NVIDIA: xdg-desktop-portal-hyprland + GStreamer NVENC, zero-copy) |
| **GNOME on Wayland** | **Ubuntu 22.04+**, **Fedora 36+**, any GNOME 40+ | Mutter's virtual-monitor API + PipeWire + GStreamer |

**Touch works too:** tap, drag, scroll and pinch on the phone to control what's
on it — through a virtual touchscreen, so your mouse pointer stays where it is.
Only a phone that scanned the computer's QR code may control it. Pair by
**scanning a QR code** in the terminal; connect over a **USB cable** (adb, automatic when USB debugging is on),
**USB tethering** (no USB debugging needed) or **Wi-Fi**.

## Documentation

- [`docs/PLATFORMS.md`](docs/PLATFORMS.md) — install & setup on **Arch / Ubuntu /
  Fedora**, how the GNOME backend works, troubleshooting.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the whole pipeline, the wire
  protocol, the reliability rules, the latency budget, and the bugs fixed.
- [`docs/ANDROID.md`](docs/ANDROID.md) — the phone app: UI, components, decode
  path, developer workflow.
- [`docs/SECURITY.md`](docs/SECURITY.md) — who can connect, encryption (TLS with
  a pinned certificate), the pairing-key handshake, limits, and how it was tested.
- [`docs/RELEASING.md`](docs/RELEASING.md) — how releases are built, signed and
  published (APK + Linux tarball on GitHub Releases).
- [`website/index.html`](website/index.html) — the landing page: one static
  file, no build step. Black and white with ASCII art; dark by default with a
  light theme (follows the system, switch in the nav). The splash is the app's
  connect ripple (`RippleRevealLayout`'s wave maths) rendered in ASCII.
  The scroll demo draws front views of a gunmetal laptop, phone, tablet and
  foldable in CSS, showing a Hyprland desktop (Waybar, Firefox on the GitHub
  repo, kitty) over a generated wallpaper; no image files except
  `website/assets/saharsh.jpg` (the maker photo). The star count comes from the
  GitHub API; the word "star" in the open-source section gets the red wipe and
  the Star button's star pops red. *Buy me a chai* (first in the nav, and with
  the Star and Share buttons) opens the UPI app on phones (a `upi://` link) and
  shows the UPI QR (`assets/upi-qr.svg`, made with `linux/qr.py`) on computers;
  *Share Tandem* uses the system share sheet, else copies the link. Published to
  <https://saharshbaiju.github.io/tandem/> by `.github/workflows/pages.yml` on
  every push to `main` that touches `website/`. Search and sharing: the head has
  the description, canonical URL, Open Graph / Twitter card (`assets/og.png`)
  and schema.org JSON-LD; `llms.txt` is a plain summary for AI assistants and
  `sitemap.xml` is for Search Console. Preview with `python3 -m http.server -d website 8765`; add `?nosplash` (or
  any `#section` link) to skip the splash.

## Install

### On the computer (Ubuntu, Fedora, Arch)

```bash
curl -fsSL https://raw.githubusercontent.com/saharshbaiju/tandem/main/install.sh | bash
```

Then open a new terminal and run **`tandem`**.

The installer puts the program in `~/.local/share/tandem`, adds the `tandem` command
(and adds `~/.local/bin` to your shell's PATH if needed), creates a *Tandem*
app-menu entry, and installs the packages your distro and desktop need
with `apt` / `dnf` / `pacman` (it asks for your password once). Re-run the same
line to update; `./install.sh --uninstall` removes it. From a git checkout,
`./install.sh` installs that checkout instead.

> **Ubuntu / Fedora:** log in to the GNOME **Wayland** session (the default; on
> Ubuntu it's "Ubuntu", not "Ubuntu on Xorg"). **Fedora:** the H.264 encoder
> comes from the `fedora-cisco-openh264` repo, enabled by default on Workstation.

### On the phone (Android 8+)

Download **`Tandem-<version>.apk`** from the
[latest release](https://github.com/saharshbaiju/tandem/releases/latest) on the
phone, open it, and allow "install unknown apps" for your browser when asked.

> **Used it before as *Screen Extender* (amVD 0.4 or older)?** Tandem is a new
> app with its own package id, so it installs next to the old one instead of
> replacing it. Uninstall *Screen Extender*, then scan the new QR code.
> (The `-debug.apk` is a separate app again, *Tandem (debug)*, for testing.)

### Connect

1. Run `tandem` → **Start Tandem**. A QR code appears.
2. Pick how the phone reaches the computer:
   - **USB cable** (lowest latency): enable *USB debugging* on the phone and plug
     it in — `tandem` sets up `adb reverse` by itself. Works even when the phone is
     on mobile data.
   - **USB tethering**: *Settings → Hotspot & tethering → USB tethering* (no
     developer options needed).
   - **Wi-Fi**: phone and computer on the same network. If a firewall is in
     the way, the live view says so — press **f** to open port 5000.

   While it waits for the phone, the live view shows **connection checks**
   (USB debugging authorised? firewall? Wi-Fi) explaining why a phone can't
   connect yet.
3. In the app tap **Scan QR code**, scan the terminal, tap **Connect**. Ripples
   spread from your finger, then the screen dissolves into your extended desktop.
4. Touch the phone's screen to use it. On **Hyprland** touch needs a one-time
   permission for `/dev/uinput` — the installer offers it, or press **t** in the
   live view. On **GNOME** there's nothing to set up.

Pointing the phone's **stock camera** at the QR works too — it's a `tandem://`
link that opens the app and asks you to confirm the computer.

**Only your phone can connect.** The QR carries a random pairing key and the
fingerprint of the computer's encryption certificate. Nothing reaches a phone
until both sides have proved they know that key, and everything (video,
touches) is encrypted with TLS. Others on the same Wi-Fi can neither watch
nor touch. To type an address instead of scanning, also enter the **pairing
code** shown under the QR. Details: [`docs/SECURITY.md`](docs/SECURITY.md).

Without the TUI: `~/.local/share/tandem/linux/start-tandem.sh usb` prints the
same QR and runs the daemon in the foreground.

## How it works

```
virtual monitor (Hyprland headless output / Mutter virtual monitor)
        │  zero-copy capture
   H.264 encode on the compositor's GPU (NVENC / VA-API, x264 fallback;
                GStreamer VA / x264 / openh264 on GNOME)
        │  no B-frames, one slice per frame, CBR
   tandemd.py   splits NAL units, caches the current GOP, one I/O
        │         thread per phone, 1 s heartbeats, resync on keyframes
   TLS over TCP — USB tethering, Wi-Fi, or adb reverse (every QR address tried
        │         in parallel); pairing-key proof before anything is sent
        │                                        ▲
   Android app → MediaCodec → Surface            │ touches (paired phones only)
                hardware decode, rendered on arrival
                                                 │
   phone_input.py → virtual touchscreen: uinput (Hyprland) / Mutter RemoteDesktop (GNOME)
```

Target glass-to-glass latency: **~25–45 ms** over USB with hardware encoding.

### Why it's fast

The old approach (~1 s lag) used a media player with a jitter buffer, an mpegts
container, and encoded on the wrong GPU. This one feeds raw H.264 straight into
the phone's hardware decoder with no buffering, and encodes on the GPU the
compositor already renders on. Details in
[`ARCHITECTURE.md` §2](docs/ARCHITECTURE.md#2-why-the-previous-approach-was-slow-and-this-isnt).

### Why it stays connected

The previous build "connected, then disconnected by itself". The causes — a
session race on the phone, frames truncated mid-send on resync, no heartbeat,
undersized decoder buffers, and broken `hyprctl` commands on Hyprland 0.55+ — are
all fixed; see the table in
[`ARCHITECTURE.md` §7](docs/ARCHITECTURE.md#7-bugs-fixed-in-this-revision-and-why-they-happened).
If the link drops after streaming (cable pulled, Wi-Fi roam, settings change),
the app shows *Reconnecting…* over the last frame and resumes by itself.

## The terminal UI

```bash
tandem
```

- **Start Tandem** — launches the daemon and opens the live view: status, the
  pairing **QR code**, the plain addresses, touch status, and the daemon log. `q` returns to the
  menu *keeping the stream running*; `d` disconnects and removes the monitor.
- **Configure** — desktop backend (auto / hyprland / gnome), screen **side**
  (left / right / up / down), **resolution** (presets or *Match connected phone*
  via adb), **FPS**, desktop **scale**, **quality** (bitrate), keyframe interval,
  **preferred link** (which address goes first in the QR), **port**, **touch
  input** (on / off), and **paired phones → forget all** (new pairing key;
  connected phones are dropped and every phone must rescan the QR). Saving while
  streaming applies side/scale instantly; other changes restart the stream.
  Stored in `~/.config/tandem/config.json`.
- **Dependencies** — per distro (pacman / apt / dnf) and per backend; `i`
  installs what's missing.
- **Detected hardware** — desktop, primary monitor, VA-API node, USB tether
  link, phone panel size (adb), LAN IP.

### Daemon directly (advanced / scripting)

```bash
cd linux
./start-tandem.sh usb|wifi [flags]     # prints the QR, runs in the foreground
python3 tandemd.py --help
```

```
--backend auto|hyprland|gnome            # auto-detected from the session
--width 2400 --height 1080 --fps 60      # virtual monitor mode
--side right|left|up|down --scale 1.5    # placement (applied live on SIGHUP)
--bitrate 12M --gop 30                   # encoder
--encoder auto|vah264enc|x264enc|…       # GNOME: GStreamer element to use
--encoder auto|h264_nvenc|h264_vaapi|libx264  # Hyprland: compositor GPU, x264 fallback
--device /dev/dri/renderD128             # Hyprland: VA-API node (h264_vaapi)
--port 5000
--pairing-key-file ~/.config/tandem/pairing-key   # phones must prove this key (default)
--tls-dir ~/.config/tandem/tls                    # TLS certificate (default; created if missing)
--no-touch                               # view only
```

## The Android app

Users install the APK from GitHub Releases (above). To build it yourself:

```bash
cd android
JAVA_HOME=/opt/android-studio/jbr ./gradlew :app:assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

(Or open `android/` in Android Studio and press Run.) The CLI build needs JDK
17–21; point `JAVA_HOME` at Android Studio's bundled JBR if your system JDK is
newer. Requires Android 8.0+; the QR scanner uses Google Play services (no
camera permission). Material You colors on Android 12+, the shader-based water
transition on Android 13+ (a canvas version on older phones).

## Developing

```bash
python3 linux/testsrc.py --width 2400 --height 1080   # fake desktop, real daemon code
python3 linux/testsrc.py --log-touch                  # …and print the touches it receives
adb shell am start -a android.intent.action.VIEW -d "'tandem://c?h=10.0.2.2&p=5000'"   # emulator
adb logcat -s StreamSession
```

`testsrc.py` streams a test pattern through the real broadcaster without creating
a monitor, so the app can be developed on any machine (the emulator reaches the
PC at `10.0.2.2`). More in [`ANDROID.md` §7](docs/ANDROID.md#7-developer-install--debugging).

## Project layout

```
linux/
  tandem                TUI launcher (installed as the `tandem` command)
  python.sh             picks the system /usr/bin/python3 (not conda/pyenv) for
                        tandem, start-tandem.sh and install.sh
  tui.py                curses UI: deps, detection, config, live view + QR
  core.py               config, distro/dependency detection, addresses, QR URI
  qr.py                 dependency-free QR encoder (half-block terminal output)
  tandemd.py            daemon: backend → NAL parse → GOP cache → TCP broadcast
  secure_link.py        TLS certificate, pairing key, handshake, lock-out
  phone_input.py        what admitted phones send back: touches → slots
  uinput_touch.py       virtual multi-touch touchscreen via /dev/uinput (Hyprland)
  backends/hyprland.py  headless output, hyprctl Lua API, wf-recorder, touch mapping
  backends/xdph.py      NVIDIA: zero-copy capture via the Hyprland portal + GStreamer NVENC
  backends/gnome.py     Mutter ScreenCast virtual monitor, DisplayConfig, GStreamer,
                        RemoteDesktop touch
  start-tandem.sh     no-TUI launcher (prints the QR)
  testsrc.py            test-pattern server for app development
  about.py              the credit line and license notice (printed by --version)
android/app/src/main/java/io/github/saharshbaiju/tandem/
  MainActivity.kt       Material 3 UI, scanner, transitions, side sheet
  StreamSession.kt      parallel connect, framed reader, MediaCodec → Surface
  RippleRevealLayout.kt water ripple + reveal (AGSL)
  ConnectTarget.kt      tandem:// QR / deep link parsing (pairing key, cert fingerprint)
  SecureChannel.kt      TLS + certificate pinning + pairing proof
  FrameReader.kt        NALU / ASX1 / PING / INPT framing
  FrameWriter.kt        HELO / AUTH / TUCH (phone → computer)
  TouchForwarder.kt     MotionEvent → touch frames
  AspectRatioSurfaceView.kt  letterboxing
LICENSE, NOTICE          GPL-3.0 and its additional terms (see License below)
docs/                   PLATFORMS, ARCHITECTURE, ANDROID, SECURITY, RELEASING
install.sh              one-line installer (any distro), adds the `tandem` command
```

## Roadmap

- RTP/UDP + FEC for lossy Wi-Fi (keep TCP for USB)
- HEVC decode in the app
- KDE Plasma backend
- Pen input (pressure, hover) and typing from the phone
- Latency HUD

## License

Tandem is free software under the [GNU GPL version 3](LICENSE), with three
additional terms allowed by its section 7 (written out in [NOTICE](NOTICE)):

- **Keep the credit.** The app's connect screen and `tandem --version` /
  `tandemd.py --version` show *Tandem by Saharsh Baiju ·
  github.com/saharshbaiju/tandem*. Copies and modified versions must keep that
  notice (they can add their own names next to it).
- **Use your own name.** A distributed fork must not call itself Tandem or
  suggest it is the original.
- **Google Play services is allowed.** The Android app's QR scanner comes from
  Google Play services, which isn't free software; linking with it is
  permitted.

Copyright (C) 2026 Saharsh Baiju.
