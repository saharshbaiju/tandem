# Tandem on each desktop and distribution

The daemon picks a **backend** based on the running session:

| Session | Detected by | Backend |
| --- | --- | --- |
| Hyprland | `$HYPRLAND_INSTANCE_SIGNATURE` | `hyprland` |
| KDE Plasma on Wayland (Arch, CachyOS, Fedora KDE, Kubuntu, KDE neon, Debian and others) | `$XDG_CURRENT_DESKTOP` contains `KDE`, and `$WAYLAND_DISPLAY` is set | `kde` (beta, see [below](#kde-plasma-beta)) |
| GNOME on Wayland (Ubuntu, Fedora Workstation and similar) | `$XDG_CURRENT_DESKTOP` contains `GNOME`, and `$WAYLAND_DISPLAY` is set | `gnome` |
| KDE Plasma or GNOME on X11 | `$XDG_CURRENT_DESKTOP` alone | Not supported, because virtual monitors need Wayland. The TUI says so and explains how to switch |
| Sway and others | None | Not yet. See the roadmap in the README |

Hyprland's own IPC variable (`HYPRLAND_INSTANCE_SIGNATURE`) counts for more
than the desktop name. When only `$XDG_CURRENT_DESKTOP` names Hyprland (from
ssh, say), the Hyprland backend is still picked.

To override the detected backend, use *Configure → Desktop backend* in the TUI,
or the `--backend` option on the daemon. The TUI's **Dependencies** screen shows
exactly what is missing for your distribution and backend. Press `i` to install
it with pacman, apt or dnf. CachyOS, EndeavourOS, Manjaro and other Arch
derivatives use the Arch package names, Kubuntu, KDE neon and Linux Mint the
Debian ones. On other distributions (openSUSE, for example) the screen lists
the packages and you install them yourself.

---

## Arch Linux with Hyprland

```bash
sudo pacman -S --needed hyprland wf-recorder python
# Hardware encoding, chosen by the GPU Hyprland renders on (x264 on the CPU otherwise):
sudo pacman -S --needed intel-media-driver   # Intel (Broadwell and newer); libva-intel-driver for older
sudo pacman -S --needed libva-mesa-driver    # AMD. NVIDIA: nvidia-utils provides NVENC
sudo pacman -S --needed libva-utils android-tools   # optional: VA-API probe, phone size via adb
```

- Hyprland 0.55 and later (Lua config parser) and older versions are both
  supported. The daemon detects which one is running (`hyprctl eval`, or the
  legacy `hyprctl keyword` and `hyprctl dispatch`) and logs `Hyprland config
  dialect: …`.
- **Tandem never modifies your existing monitors.** It only writes rules for its
  own output, `EXT-1`. The phone is placed with `auto-right` (or `auto-left`,
  `auto-up`, `auto-down`), which Hyprland lays out after your monitors, so your
  laptop screen and any external monitors keep their mode, scale and position.
  When that leaves the phone touching no monitor, `auto-center-<side>` is tried.
  On Hyprland older than 0.46, a phone below the main screen would overlap it,
  so it goes to the right instead, with a warning in the log.
- Saving your Hyprland config, or plugging in a monitor, is handled while
  streaming. The daemon follows Hyprland's event socket and re-applies the
  phone's mode, scale, side, workspace and touch mapping (`re-applied … after a
  Hyprland config reload or monitor change` in the log).
- Started from ssh or a systemd user service without
  `HYPRLAND_INSTANCE_SIGNATURE`, Tandem uses the one running Hyprland session.
  With several sessions it warns and asks you to run it inside the session.
- The encoder runs on the GPU that Hyprland renders on, which is the first entry
  of `AQ_DRM_DEVICES`. On NVIDIA it uses NVENC. This needs the proprietary driver,
  because NVIDIA's VA-API driver cannot encode. Capture then goes zero-copy through
  xdg-desktop-portal-hyprland and GStreamer. Install these packages:
  `xdg-desktop-portal-hyprland gst-plugin-pipewire gst-plugins-base gst-plugins-bad python-gobject`.
  The installer adds them. Without them, Tandem falls back to wf-recorder, which
  makes the main screen stutter. On other GPUs, it uses VA-API on that GPU's render
  node, with the matching `LIBVA_DRIVER_NAME`. To force a choice, use
  `--encoder h264_nvenc`, `--encoder h264_vaapi`, `--encoder libx264` or
  `--encoder libopenh264`. If the hardware encoder fails to start, meaning it
  exits or produces nothing for 4 s, the daemon falls back to x264 on the CPU,
  then OpenH264 (only those `ffmpeg -encoders` lists). On Intel with
  `intel-media-driver`, the older `i965` driver (`libva-intel-driver`) is tried
  first if it is installed, for GPUs older than Broadwell. The TUI's
  Dependencies screen lists the VA-API driver your GPU needs
  (`intel-media-driver` or `libva-mesa-driver`) as optional.
- If the encoder exits mid-session (a driver hiccup, a GPU reset), the daemon
  restarts it and the phone stays connected. The log shows `the encoder exited;
  restarting capture`, then `capture restarted`.
- Scales are snapped to the nearest value Hyprland accepts for the resolution, in
  whole logical pixels. For example, 1.5 becomes 1.333 at 2772×1280.
- The phone gets one workspace of its own, and your workspaces and workspace
  rules are left alone. Its number is chosen at start, above every workspace and
  numeric workspace rule you have, and 11 or higher. The log shows it
  (`workspace 11 -> EXT-1`). To move a window to the phone, send it to that
  workspace, or drag it across the edge. Any workspace you move onto the phone
  works too. On disconnect, the phone's workspaces move back to your main
  monitor.
- **Touch input** needs a one-time setup. The phone's touches are replayed through
  a virtual touchscreen in `/dev/uinput`, which is root-only on a standard Arch
  install. The installer offers to add a udev rule that gives access to the user
  logged in at the seat. Alternatively, start Tandem from `tandem` and press **t**
  when the live view says *Touch: needs one-time setup*. This is what it runs,
  with sudo:

  ```bash
  echo 'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"' \
      > /etc/udev/rules.d/70-tandem-uinput.rules
  echo uinput > /etc/modules-load.d/tandem-uinput.conf
  modprobe uinput && udevadm control --reload \
      && udevadm trigger --name-match=/dev/uinput && udevadm settle
  ```

  If you are already in the `input` group and `/dev/uinput` is writable by that
  group (ydotool's package sets this up), nothing is needed. The device is called
  `tandem-touch`. `hyprctl devices` lists it under touch while streaming. It is
  mapped onto `EXT-1` with `hl.device({ name = "tandem-touch", output = "EXT-1" })`.

## Ubuntu (22.04 and newer, GNOME)

```bash
sudo apt install python3-gi gstreamer1.0-tools gstreamer1.0-pipewire \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly
# Optional hardware encoding (Intel or AMD):
sudo apt install gstreamer1.0-vaapi        # or, on 24.04 and newer, the va plugin in plugins-bad
sudo apt install adb                       # optional: "Match connected phone"
```

- Log in with the **"Ubuntu"** session, which runs Wayland. Do not use "Ubuntu on
  Xorg". The option is the gear icon on the login screen. NVIDIA machines
  sometimes default to Xorg, and the TUI tells you when that happens.
- `gstreamer1.0-plugins-ugly` provides `x264enc`. When a working VA-API driver is
  present, `vah264enc` is used instead.

## Fedora Workstation (36 and newer, GNOME)

```bash
sudo dnf install python3-gobject gstreamer1 pipewire-gstreamer \
    gstreamer1-plugins-base gstreamer1-plugins-bad-free gstreamer1-plugin-openh264
sudo dnf install android-tools             # optional
```

- `gstreamer1-plugin-openh264` comes from the **fedora-cisco-openh264** repository,
  which is enabled by default on Workstation. Cisco hosts those RPMs itself, and
  its mirror is sometimes slow or unreachable. If the download fails, retry with
  `sudo dnf install --setopt=retries=20 …`.
- Fedora's Mesa ships with H.264 *encoding* disabled, for patent reasons. The
  VA-API path is therefore not available out of the box. For hardware encoding, or
  for x264, enable **RPM Fusion** and install `mesa-va-drivers-freeworld` (AMD or
  Intel) or `gstreamer1-plugins-ugly` (x264). The backend picks the best encoder
  that is available.

## KDE Plasma (beta)

KDE Plasma support is new in **0.7.0-beta.1** and is a beta: it passes the
automated tests against real KWin 6.7.5 and 6.8.2 (see *How the KDE backend
works*).
The TUI, the installer and the daemon's log say so. Please report what works
and what doesn't in [GitHub issues](https://github.com/saharshbaiju/tandem/issues),
with your distribution and the output of `kwin_wayland --version`.

The beta is a GitHub pre-release, which the default install command skips, so
name it when installing (the same line on every distribution below):

```bash
curl -fsSL https://raw.githubusercontent.com/saharshbaiju/tandem/v0.7.0-beta.1/install.sh | TANDEM_VERSION=v0.7.0-beta.1 bash
```

The sections below list the packages it installs, for installing by hand.

## KDE Plasma on Arch, CachyOS, EndeavourOS and Manjaro

```bash
sudo pacman -S --needed libkscreen gcc pkgconf wayland gstreamer gst-plugin-pipewire \
    gst-plugins-base gst-plugins-bad gst-plugins-ugly openssl
sudo pacman -S --needed android-tools    # optional: "Match connected phone"
```

- Log in to the **Plasma (Wayland)** session, the default on Plasma 6.
- `libkscreen` provides `kscreen-doctor`, which places and scales the phone's
  monitor. It is already installed with Plasma.
- `gcc`, `pkgconf` and `wayland` build Tandem's small KWin helper (see *How the
  KDE backend works*). The installer and the first start build it, which takes
  a second or two, and it is rebuilt only when Tandem's copy of its sources
  changes. Once it is built, the compiler is no longer needed.
- `gst-plugins-ugly` provides `x264enc`. With a working VA-API driver
  (`intel-media-driver` or `libva-mesa-driver`), `vah264enc` from
  `gst-plugins-bad` is used instead.
- CachyOS, EndeavourOS and Manjaro need nothing special: they are detected as
  Arch.

## Fedora KDE (Plasma 6)

```bash
sudo dnf install libkscreen gcc pkgconf-pkg-config wayland-devel gstreamer1 \
    pipewire-gstreamer gstreamer1-plugins-base gstreamer1-plugins-bad-free \
    gstreamer1-plugin-openh264 openssl
sudo dnf install android-tools             # optional
```

- The H.264 encoder is OpenH264, as on Fedora Workstation: the plugin needs
  `libopenh264` from the **fedora-cisco-openh264** repository, which is
  enabled by default. For x264 or VA-API encoding, see the RPM Fusion note
  under *Fedora Workstation*.
- Fedora 43 ships Plasma 6.7 (tested with KWin 6.7.5), which needs the helper's
  desktop entry and has KDE bug 527747. Both are handled by Tandem; see
  *How the KDE backend works*.

## Kubuntu, KDE neon and Debian with KDE Plasma

```bash
sudo apt install libkscreen-bin gcc pkgconf libwayland-dev libwayland-bin \
    gstreamer1.0-tools gstreamer1.0-pipewire gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly openssl
sudo apt install adb                       # optional
```

- `kscreen-doctor` is in `libkscreen-bin` on Plasma 6 (Debian 13, Kubuntu 25.04
  and newer, KDE neon) and in `libkf5screen-bin` on Plasma 5.27 (Kubuntu 24.04).
  The Dependencies screen picks the right one by which libkscreen is installed.
- Tandem is tested on Plasma 6 (KWin 6.7 and 6.8). Plasma 5.27
  should work, and the helper builds there, but it has not been tested.
- Choose the **Plasma (Wayland)** session on the login screen. Kubuntu 24.04
  still offers *Plasma (X11)*, which does not work: the TUI says so.

## openSUSE and other distributions

The installer and the Dependencies screen know the package names for Arch,
Fedora and Debian/Ubuntu (and their derivatives). Elsewhere they list what is
missing by its Arch name, and you install the equivalent yourself. For KDE
Plasma that is `kscreen-doctor`, a C compiler, `pkg-config`, `wayland-scanner`
and the libwayland-client headers, `gst-launch-1.0` with the PipeWire, base,
bad and an H.264 encoder plugin (x264 or OpenH264), and `openssl`. On openSUSE
Tumbleweed with Plasma 6 those packages are:

```bash
sudo zypper install libkscreen6-plugin gcc pkgconf-pkg-config wayland-devel \
    gstreamer-utils gstreamer-plugin-pipewire gstreamer-plugins-base \
    gstreamer-plugins-bad gstreamer-plugin-openh264 openssl
```

openSUSE's own repositories have no x264; `gstreamer-plugin-openh264` (or x264
from Packman) provides the encoder.

---

## How the GNOME backend works

1. It opens a session on Mutter's private ScreenCast D-Bus API and calls
   `RecordVirtual` with `cursor-mode = embedded`, which draws the pointer into the
   frames, and `is-platform = true`, which makes it a real extended monitor rather
   than a mirror.
2. It calls `Start` on the session. Mutter then announces a PipeWire node.
3. `gst-launch-1.0 pipewiresrc path=<node> ! video/x-raw,width=W,height=H ! …`
   connects. The **caps set the monitor's resolution**. Mutter creates a W×H
   monitor, with a connector such as `Meta-0`, as soon as the format is negotiated.
4. The daemon notices the new connector in `DisplayConfig.GetCurrentState` and
   calls `ApplyMonitorsConfig` with the *temporary* method, to place it on the
   configured side of the primary monitor and set its scale. Every other monitor
   is sent back exactly as it is: mode, scale, rotation, primary flag, position,
   underscanning, HDR (`color-mode`) and RGB range. If the spot beside the
   primary is taken, the phone goes to the outer edge of the layout on that side.
   Nothing is sent when the layout is already right. The daemon keeps checking
   for a few seconds, because Mutter sometimes re-creates the monitor under a new
   connector name. Live side or scale changes from the TUI repeat this step
   without restarting the stream.
5. H.264 from the encoder goes through `h264parse`, which puts SPS and PPS before
   every keyframe, into a pipe. From there it is the same daemon as on Hyprland.

**Touch input** needs no setup on GNOME. Before step 1, the daemon creates a
session on Mutter's RemoteDesktop D-Bus API and links the ScreenCast session to
it with `remote-desktop-session-id`. In step 2, the pair is started through the
RemoteDesktop session. Touches are injected with `NotifyTouchDown`, `NotifyTouchMotion`
and `NotifyTouchUp` on the virtual monitor's stream, in its pixel coordinates.
Mutter creates its virtual touchscreen on the first touch, so the seat advertises
touch only from then on. The first tap itself is still delivered. If the
RemoteDesktop session cannot be created, video still works, and the phone shows
why touch is off.

**Tested.** On Ubuntu 24.04 (Mutter 46, headless, in Docker), the virtual monitor
appears as `Meta-1`. It is placed on the configured side, and H.264 from x264enc
reaches the client and decodes. Fedora 42 (Mutter 48) passes the same run with
openh264. On 2026-10-16, touch was tested on both: taps and drags from a paired
client reach a Wayland window on the virtual monitor (`wev` shows `wl_touch`
down, motion and up at the expected positions). Two fingers get separate IDs. A
finger left down when the client disconnects is lifted. A client with the wrong key
injects nothing.

When the daemon exits for any reason, its D-Bus connection closes. Mutter then
removes the monitor and moves its windows back. Nothing needs cleaning up.

`pipewiresrc keepalive-time=1000` resends the last frame every second when the
desktop is idle, because Mutter only sends frames when something changes. It is
only set where the installed PipeWire plugin has the property.

Each encoder gets a rate-control buffer of two frames where its GStreamer element
has one, so keyframes do not arrive as one large burst. The encoder runs in its
own process session, so Ctrl+C in the terminal cannot stop capture behind the
daemon's back. Touches go to Mutter on their own thread and never hold up the
video. The
daemon's GOP replay gives a phone that connects to an idle desktop a picture at
once.

**Display settings.** While Tandem runs, the virtual monitor appears in *Settings
→ Displays* like any other monitor. You can drag it there too. Placement made by
Tandem is temporary. It never changes `~/.config/monitors.xml`.

---

## How the KDE backend works

KWin can create a *virtual output* and stream it to PipeWire through its
`zkde_screencast_unstable_v1` Wayland protocol, and it injects touches through
`org_kde_kwin_fake_input`. Both are Wayland requests, which Python cannot make
without extra libraries, so Tandem ships a small C client for them,
`tandem-kwin-helper` (`linux/kwin-helper/`). The daemon drives it over its
standard input and output.

1. **Build.** On first start (or from the installer), `backends/kwin_helper.py`
   runs `wayland-scanner` on the protocol XML files Tandem ships and compiles the
   helper with `cc` into `~/.local/share/tandem/kwin-helper/`. It is rebuilt only
   when a hash of its sources changes. That path stays the same whether Tandem
   runs from a checkout or an install.
2. **Permission.** KWin 6.0 to 6.7 offer those two protocols only to a program
   named in a desktop entry with `X-KDE-Wayland-Interfaces`. Tandem writes
   `~/.local/share/applications/tandem-kwin-helper.desktop` (hidden from menus,
   `Exec=` the helper's absolute path). KWin notices it without a restart.
   KWin 6.8 no longer asks, and ignores the entry.
3. **Create.** `tandem-kwin-helper --name Tandem --width W --height H` asks KWin
   for the output at scale 1 with the pointer drawn into the frames. KWin calls
   it `Virtual-Tandem` and reports its PipeWire node (`created <node>`). The
   scale is set afterwards, because KWin's virtual backend multiplies the
   requested size by the scale, and KWin re-applies a scale it saved for that
   name anyway.
4. **Place.** The daemon reads `kscreen-doctor -j` and sets the phone's scale
   and position beside the primary monitor in one `kscreen-doctor` call
   (`output.Virtual-Tandem.scale.1.5 output.Virtual-Tandem.position.1920,0`).
   The scale is snapped so the logical size is whole pixels. The phone is never
   made primary. Nothing is sent when the layout is already right, and live side
   or scale changes repeat this step.
5. **Capture.** `gst-launch-1.0 pipewiresrc path=<node> ! video/x-raw,width=W,height=H ! …`,
   the same pipeline as GNOME. The encoder input is pinned to I420/NV12, since
   x264 would otherwise pick a 4:4:4 profile that phone decoders refuse.
6. **Touch.** Touches go to the helper as lines (`down 0 0.25 0.5`), and it sends
   them to KWin with fake input at that point of the phone's monitor. No
   setup is needed.

**Left and up.** KWin refuses negative positions. To put the phone left of or
above the main monitor, Tandem moves your monitors right or down by the phone's
size, in the same `kscreen-doctor` call. Their modes, scales, rotations and
arrangement are kept, and the log says `note: moving your other monitors by …`.
The layout's top-left corner stays at 0,0, so moving the phone back to the right
or below, or stopping Tandem, undoes the shift. This is the one case where
Tandem changes your monitors.

**Stopping.** The output exists exactly as long as the helper's Wayland
connection. When the daemon stops, or is killed (the helper gets SIGTERM from the
kernel, and end of input), KWin removes the output and moves its windows back.
Nothing needs cleaning up, so `--keep-output` does not apply.

**KWin 6.7 and KDE bug 527747.** After the phone's monitor is disabled (Meta+P
*Built-in only*, or the display settings), KWin 6.7 closes it, and every later
output with that name fails with `Could not find output` (in your session's
language: KWin translates it). The daemon stops when its output is closed. On
the next start it tries `Tandem-2`, then `Tandem-3`, up to `Tandem-5`, and logs
why, whatever language KWin answers in.

**Idle desktop.** KWin sends frames only when something on the output changes.
KWin 6.8 with PipeWire 1.6 re-sends the last frame through `pipewiresrc
keepalive-time`. On KWin 6.7 with PipeWire 1.4 an idle phone monitor gets almost
no frames. A phone that connects still gets the current picture at once from the
daemon's GOP replay.

**Tested.** In Docker, with the real daemon at 2400×1080 and scale 1.5: KWin 6.8.2
(Arch, x264, 60 fps) and KWin 6.7.5 (Fedora 43, OpenH264, without bypassing
KWin's permission check). Placement on the right, a live move to the left and
back, taps landing at the exact point, encoder restarts, SIGTERM and SIGKILL cleanup, and the
bug 527747 recovery all passed.

---

## USB cable with USB debugging (all distributions)

If the phone has **USB debugging** on and `adb` is installed, plugging in the cable
is enough. The TUI and `start-tandem.sh` run `adb reverse tcp:5000 tcp:5000` for
each connected phone, and put `127.0.0.1` first in the QR code. The phone then
reaches the PC over the cable, even on mobile data and off your Wi-Fi.

## USB tethering (all distributions)

The wired path uses **Android USB tethering**, not `adb`. That means no developer
mode or USB debugging is needed. Turn it on under *Settings → Connections → Mobile
Hotspot and Tethering → USB tethering*. The phone becomes a USB network adapter
and gives the PC an address on `192.168.42.0/24`. The exact range depends on the
phone's vendor. The pairing QR code then includes that address.

> While tethering is on, NetworkManager may route internet traffic through the
> phone. If you don't want that, lower the priority of that connection, or turn off
> mobile data on the phone. The stream needs only the local link.

---

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| The phone shows two apps, *Screen Extender* and *Tandem* | Screen Extender (`com.archextender`, amVD 0.4 or older) is a separate app from Tandem (`io.github.saharshbaiju.tandem`, 0.5 and later), so one cannot replace the other. Uninstall Screen Extender. See [RELEASING.md](RELEASING.md#names) |
| Phone: *"reachable, but Tandem isn't running"* | Start it in the TUI (`tandem`, then Start Tandem). Check the daemon log in the live view |
| Live view: *"port 5000 is used by …; Tandem now uses port 5001"* | Another program, often a Flask or development server whose default is port 5000, already listens on Tandem's port. Tandem never stops other programs. It moves to the next free port, saves the choice in `~/.config/tandem/config.json`, and puts the new port in the QR code. Scan the QR code once more. The port then stays the same. A leftover Tandem daemon on the port is stopped instead, so the port does not change. To pick a port yourself, use Configure → Port |
| Phone: *"Couldn't reach …"* | The live view's **connection checks** explain why. *Firewall … press f* opens TCP 5000. Detection works without root for ufw, firewalld, nftables and iptables. On Arch, the stock `/etc/nftables.conf` drops everything except SSH once `nftables.service` is enabled. Pressing `f` adds a runtime rule. To keep it, add `tcp dport 5000 accept` to the file. For *USB cable*, use either **USB tethering**, which needs no developer options, or **USB debugging**, which needs adb installed and "Allow USB debugging" accepted. With tethering, the PC must get an IP address from the phone. NetworkManager does this. Minimal Hyprland setups that use only iwd or systemd-networkd may not, so the check offers **u**. That runs `nmcli device connect`, writes a persistent systemd-networkd `.network` file that matches the tether drivers, or uses `dhcpcd`. Tethering goes through the firewall like Wi-Fi does. adb does not. Rescan the QR code after fixing the problem. If you are on the same Wi-Fi and still cannot connect, guest, campus and hotel networks often isolate clients. Use the cable, or the phone's hotspot |
| Phone: *"Connected, but no video arrived"* | The encoder failed. The daemon log in the live view shows `gst:` or wf-recorder errors. Running `python3 tandemd.py … </dev/null` prints everything |
| TUI: *"python gi [missing]"* even after installing `python3-gobject` or `python-gobject` | Another `python3`, from conda, miniconda, pyenv, a venv or Homebrew, comes first on `PATH`. Distribution packages go only into `/usr/bin/python3`. The launchers `tandem`, `start-tandem.sh` and `install.sh` always use `/usr/bin/python3`. To override this, set `TANDEM_PYTHON=…`. The Dependencies screen shows which interpreter it is running under |
| `tandem`: `_curses.error: setupterm: could not find terminfo database` | The same cause. Conda's Python bundles its own ncurses, which does not look in `/usr/share/terminfo`. The launchers use the system Python, which avoids this. When you start the TUI directly with `python3 tui.py`, it also retries with the system terminfo directories, and then with `TERM=xterm-256color` |
| TUI: *"GNOME is running on X11"* or *"Plasma is running on X11"* | Log in to the Wayland session (*Ubuntu* / *GNOME*, or *Plasma (Wayland)*) |
| KDE: `KWin refused the screen-cast protocol to …` | KWin 6.7 or older has not read the helper's desktop entry (`tandem-kwin-helper.desktop` in the `applications` folder of KWin's own data directory, usually `~/.local/share`; the message names the path). If the daemon wrote the entry on this start, it already waited 2 s and tried once more. Log out and back in so KWin reads it |
| KDE: *"KWin refused touch input (org_kde_kwin_fake_input)"* | The same cause, for touch. Log out and back in |
| KDE: `could not build tandem-kwin-helper: …` | A build tool is missing. The Dependencies screen lists the C compiler, pkg-config, wayland-scanner and the wayland-client headers with your distribution's package names. By hand: `make -C ~/.local/share/tandem/linux/kwin-helper` shows the full compiler output |
| KDE: the daemon stops with `KWin closed Virtual-Tandem` | The phone's monitor was disabled (Meta+P, or the display settings). Start Tandem again. On KWin 6.7 the next one is called `Virtual-Tandem-2`, because of KDE bug 527747 |
| KDE: `KWin refused the virtual monitor under every name Tandem tried (…)` | KWin 6.7 refused all five names Tandem tries; when KWin's reason is `Could not find output` (or its translation), that is bug 527747. Remove the `Virtual-Tandem` entries from `~/.config/kwinoutputconfig.json` and log out and back in, or update Plasma |
| KDE: my monitors moved right or down | The phone is on the left or above, and KWin does not allow negative positions, so the other monitors moved by the phone's size (the log has `note: moving your other monitors …`). They move back when the phone goes right or below, or Tandem stops |
| KDE: monitor on the wrong side, `warning: could not place virtual monitor` | `kscreen-doctor` failed (it is in `libkscreen` / `libkscreen-bin`). The stream keeps working; drag the monitor in *System Settings → Display & Monitor* |
| GNOME: `could not create the virtual monitor` | GNOME is older than 40, or Mutter's ScreenCast interface is unavailable. Check with `gdbus introspect --session -d org.gnome.Mutter.ScreenCast -o /org/gnome/Mutter/ScreenCast` |
| GNOME: monitor appears on the wrong side | The log shows `could not place virtual monitor (…)`. Drag it in *Settings → Displays*. The stream keeps working |
| Hyprland: resolution or scale ignored | Hyprland 0.55 and later need `hyprctl eval`. The daemon uses it automatically. Check that `hyprctl eval 'return 1'` prints `ok` |
| Hyprland: `Unsupported format: bgra` or `Cannot create hw frames context` | The encoder is on a different GPU from Hyprland. With `--encoder auto`, Tandem follows the compositor, so check the daemon's `encoder:` log line |
| Hyprland: *"invalid scale"* or *"EXT-1 overlaps"* popup | Tandem snaps the scale to a value Hyprland accepts. If you still see the popup, two daemons are running. The new one waits for the old one to exit |
| Hyprland on Intel: connects, but the screen stays black | Install `intel-media-driver`. `vainfo` should list `VAProfileH264… VAEntrypointEncSlice*`. Without it, the daemon falls back to x264 after 4 s. The log shows an `encoder: …` line for each attempt |
| Stutter on Wi-Fi | Connect the computer to the phone's own mobile hotspot. It is a direct link with no router, no other devices competing for airtime and no client isolation. The computer then uses the phone's mobile data for the internet. Otherwise use 5 GHz, lower *Quality*, or use USB tethering |
| Phone sheet: *Touch Off, scan the QR code…* | The phone connected with a typed address, or with a QR code from before touch existed. Scan the current QR code |
| Phone: *"Wrong pairing code, scan the QR code on the computer again"* | *Paired phones → forget all* was used, or the key file was deleted. Alternatively, the typed code is wrong. Scan the current QR code, or check the code under it |
| Phone: *"This isn't the computer you paired with…"* | The computer's certificate differs from the one in the scanned QR code. Either another machine answered on that address, or `~/.config/tandem/tls/` was recreated. Scan the computer's current QR code |
| Phone: *"Couldn't set up an encrypted connection. Update Tandem…"* or *"This QR code is from an older version…"* | The computer runs a version before 0.5, which has no encryption. Re-run the install command, then scan the new QR code |
| Daemon log: `client … rejected: … (locked out for a minute)`. Phone: *"too many failed pairing attempts from this phone; try again in a minute"* | Five failed pairings from that address within a minute. It is refused for 60 s, and the phone retries on its own ([SECURITY.md](./SECURITY.md) §4.5). Dropped connections and timeouts do not count, only wrong keys |
| Phone: *"already 4 phones connected to this computer"* | Four phones are streaming. The phone retries on its own and connects once one leaves |
| Daemon log: `the encoder exited; restarting capture` | The encoder stopped mid-session. The daemon restarts it and the phones stay connected. Look at the `gst:` or wf-recorder lines just before it for the cause |
| Daemon log: `error: the encoder keeps failing; stopping` | The encoder exited six times in a row within seconds of starting. The lines above it show why. Try another `--encoder`, or check the GPU driver |
| Daemon log: `warning: several Hyprland sessions are running …` | `HYPRLAND_INSTANCE_SIGNATURE` is unset (ssh, a systemd service) and more than one Hyprland is running. Start `tandem` from a terminal inside the session |
| Daemon log: `warning: --codec hevc: the Tandem app … only support h264` | `--codec hevc` was given. The app only decodes H.264, so phones show nothing. Use `--codec h264` |
| TUI: *"Couldn't set up the encrypted connection"* | `openssl` is missing (the Dependencies screen installs it), or `~/.config/tandem/` is not writable |
| Hyprland: *Touch: needs one-time setup (… /dev/uinput …)* | `/dev/uinput` is not writable. Press **t** in the live view (see *Arch Linux with Hyprland*). After a kernel update, if the modules-load file is missing, run `sudo modprobe uinput` |
| Hyprland: touches land on the laptop screen | The device mapping was dropped. The daemon re-applies it after a config reload or monitor change on its own. If it persists, change side or scale, or restart the stream. Check that `hyprctl devices` lists `tandem-touch` |
| Touch does nothing in one app | That app does not handle Wayland touch events. Some terminals and older toolkits do not. GTK, Qt, Chromium, Electron and Firefox do |
| Fedora: the `openh264` download fails | A Cisco mirror problem. Retry, or use RPM Fusion's x264 (`gstreamer1-plugins-ugly`) |
