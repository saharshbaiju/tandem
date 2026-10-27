# Tandem — Architecture & Pipeline

This document explains the whole system, from the idea down to every stage of
the pipeline, the exact bytes on the wire, and the reliability rules that keep a
connection up. Platform setup (Arch / Ubuntu / Fedora) lives in
[`PLATFORMS.md`](./PLATFORMS.md); the phone app in [`ANDROID.md`](./ANDROID.md).

---

## 1. The core idea

We want a phone to behave like a **real second monitor** for a Linux desktop —
drag a window "onto" the phone, watch it appear there with near-zero lag.

A monitor, to the compositor, is just **an output it can draw into**. It does not
have to be physical. So:

1. **Create a monitor that doesn't physically exist** (a virtual output). The
   compositor renders into it exactly like a real screen.
2. **Capture** whatever is drawn there and **compress** it to H.264 (on the GPU
   where possible).
3. **Ship** it to the phone over USB tethering or Wi-Fi.
4. **Decode and display** with **no buffering**.
5. **Send touches back** and replay them on the virtual monitor through a
   virtual *touchscreen*, so the phone is a touch monitor (§4, Stage 5).

The phone is a *thin client*: it decodes video and reports touches. All the
"monitor-ness" lives on the Linux side, in a **backend** per compositor:

```
 ┌──────────────── Linux ─────────────────────────────┐        ┌── Android ──┐
 │                                                     │        │             │
 │  backend: hyprland            backend: gnome        │        │             │
 │  hyprctl output create        Mutter ScreenCast     │        │             │
 │  headless EXT-1               RecordVirtual         │        │             │
 │        │ wlr-screencopy             │ PipeWire       │        │             │
 │  wf-recorder + VA-API         GStreamer + VA/x264/  │        │             │
 │        │                      openh264              │        │             │
 │        └────────── raw Annex-B H.264 ──┘            │        │             │
 │                       │ pipe                        │        │             │
 │             tandemd.py  (parse, GOP cache,        │  TCP   │ MediaCodec  │
 │             per-client senders, heartbeats) ────────┼───────▶│  → Surface  │
 │             phone_input.py (pairing, touches) ◀─────┼────────┤ touches     │
 │               → uinput touchscreen  (Hyprland)      │        │             │
 │               → Mutter RemoteDesktop (GNOME)        │        │             │
 │                       ▲                             │        │             │
 │             tui.py (config, QR pairing, lifecycle)  │        │             │
 └─────────────────────────────────────────────────────┘        └─────────────┘
```

---

## 2. Why the previous approach was slow (and this isn't)

An earlier version reached only ~1 s of lag. **Capture and encoding were never
the bottleneck**; the latency lived in avoidable places:

| Old design | Problem | This design |
| --- | --- | --- |
| Phone played the stream with mpv / VLC | Players add a 0.5–2 s **jitter buffer** by design | A **dedicated app** feeds frames to the hardware decoder with **no buffer** |
| **mpegts** container | Demuxing 188-byte TS packets before decoding | **Raw Annex-B** H.264 |
| Encoder on the wrong GPU | Every frame copied across GPUs | Encoder on **the GPU the compositor renders on** (§3) — zero copy |
| VKMS kernel virtual monitor | CPU-rendered, crashed the session | Compositor-native virtual outputs |

Guiding principle: **never let a frame sit in a buffer waiting.** Every stage
forwards its output the instant it's ready; a consumer that falls behind is
*resynced at the next keyframe* instead of being fed a growing backlog.

---

## 3. Encode on the GPU the compositor renders on (Hyprland)

```
card0 / renderD129  →  NVIDIA RTX 4060 (dGPU)
card1 / renderD128  →  AMD Radeon 890M (iGPU)
```

wf-recorder can hand frames to the encoder zero-copy (**DMA-BUF**) only when
the encoder sits on the **same GPU** Hyprland renders on. Across GPUs it logs
`compositor running on different device, disabling DMA-BUF`, falls back to CPU
copies in `bgra`, and VA-API then fails with `Unsupported format: bgra / Cannot
create hw frames context` — capture dies before the first frame.

Which GPU renders depends on the user's config: the **first entry of
`AQ_DRM_DEVICES`** (exported by Hyprland to every child process), or the boot VGA
device when it's unset. The backend (`pick_encoder` in `backends/hyprland.py`)
therefore decides at start-up:

| Compositor GPU (kernel driver) | Encoder | Notes |
| --- | --- | --- |
| `nvidia` | **xdph + GStreamer `nvh264enc`** (zero-copy, `backends/xdph.py`), then wf-recorder `h264_nvenc` — `preset=p1 tune=ull zerolatency=1 rc=cbr delay=0 forced-idr=1` | NVIDIA's VA driver (`nvidia-vaapi-driver`) only *decodes*; wf-recorder can't hand GPU frames to NVENC, see below |
| `amdgpu` / `i915` / `xe` | `h264_vaapi` on that GPU's render node | `LIBVA_DRIVER_NAME` forced to `radeonsi` / `iHD` (`i965` when iHD isn't installed) |
| anything, if the above fails to start | `libx264` on the CPU (`preset=ultrafast tune=zerolatency`, `-x yuv420p`) | NVIDIA without NVENC, nouveau, VMs, VA drivers without H.264 encode |

Each candidate gets 4 s to produce its first byte (a working encoder needs
~0.1 s). An encoder that can't initialise either makes wf-recorder exit, or
leaves it alive but silent (seen with VA-API on Intel); either way the backend
kills it and moves on instead of leaving the phone on a black screen
(`encoder X failed to start; trying the next one`). The last candidate (x264)
is never killed.

Forcing `LIBVA_DRIVER_NAME` matters: NVIDIA Hyprland setups typically export
`LIBVA_DRIVER_NAME=nvidia` globally, which makes `vainfo` (and FFmpeg) load the
decode-only NVDEC driver even on the AMD/Intel node. Override the choice with
`tandemd.py --encoder h264_vaapi|h264_nvenc` (`--device` then picks the VA node).

The dev machine runs Hyprland on the NVIDIA GPU (`AQ_DRM_DEVICES=card0:card1`),
so it streams via NVENC. NVENC in CBR mode emits a filler NAL after almost every
frame; the daemon drops filler, SEI and AUD NALs (`NAL_DROP`) before they reach
the network.

**NVIDIA rendering: why wf-recorder isn't used there any more.** wf-recorder only
does DMA-BUF capture for VA-API, and only on the compositor's own GPU (with NVENC
and `-d` it fails with `Unsupported format: bgra`; with VA-API on the iGPU it
logs `compositor running on different device, disabling DMA-BUF`). So with
Hyprland on NVIDIA every frame goes through `wl_shm` buffers
(`/tmp/wf-recorder-shared-*`). Hyprland reads each frame back from the GPU on its
single render thread. Measured at 2560x1440@120, that thread ran at **87 %** with
capture running and **13 %** with wf-recorder paused. That leaves the main screen
no headroom, so it drops frames, worse as the laptop heats up. wf-recorder also
keeps a pool of ~11 shared-memory frames (~0.5 GB RSS at 2400x1080), mapped into
Hyprland as well.

**The fix: capture through xdg-desktop-portal-hyprland (xdph), all on the GPU.**
xdph asks Hyprland for **DMA-BUF** frames, which costs Hyprland a GPU blit
instead of a readback, and GStreamer encodes them without a CPU copy:

```
xdph ScreenCast session (EXT-1)  ->  PipeWire node, DMA-BUF in NVIDIA's tiled layout
  -> pipewiresrc -> glupload -> glcolorconvert (NV12 on the GPU)
  -> nvh264enc (CUDA/GL interop; preset p1, tune ultra-low-latency, CBR, no B-frames)
  -> h264parse (byte-stream, one access unit per buffer) -> pipe -> tandemd
```

- **No picker.** The daemon calls xdph's *backend* interface
  (`org.freedesktop.impl.portal.ScreenCast` on
  `org.freedesktop.impl.portal.desktop.hyprland`) directly, passing restore data
  (xdph's format, version 3) that names the output. xdph treats it as a saved
  choice ("restore data valid, not prompting"), so no dialog appears. The
  PipeWire node comes back from `Start`; `pipewiresrc path=<node>` reads it.
- **Damage-driven.** Hyprland only produces frames when EXT changes;
  `keepalive-time=1000` re-sends the last frame every second so a phone that
  joins a still desktop isn't left black.
- **Session lifetime.** The session is closed in `stop_capture`, before any
  output removal. Its D-Bus path is kept in `$XDG_RUNTIME_DIR/tandem-xdph-session`
  so `remove_output` can close one a killed daemon left open.
- **Fallback.** If xdph isn't running, a GStreamer element is missing, or the
  pipeline produces nothing for 4 s, the backend moves on to wf-recorder NVENC,
  then x264, as before. The TUI's Dependencies screen lists the pieces as
  optional on NVIDIA machines.

Measured on the dev machine (RTX 4060, Hyprland 0.56, 2400x1080@60 with a
terminal redrawing every frame): both paths deliver ~59 fps, but only
wf-recorder maps `wf-recorder-shared` buffers into Hyprland; the GStreamer
process uses ~8 % CPU against wf-recorder's 11–12 %. End to end through the
daemon to a paired test client: 503 decodable frames in 8 s.

---

## 4. The pipeline, stage by stage

### Stage 0a — Virtual monitor, Hyprland backend (`backends/hyprland.py`)

```bash
hyprctl output create headless EXT-1
hyprctl eval 'hl.monitor({ output = "EXT-1", mode = "2400x1080@60", position = "1920x0", scale = 1.5 })'
```

`EXT-1` is indistinguishable from a plugged-in monitor. Details that matter:

- **Hyprland ≥ 0.55 uses a Lua config parser**, so `hyprctl keyword monitor` and
  `hyprctl dispatch …` *silently fail*. Everything goes through
  `hyprctl eval '<lua>'` (`hl.monitor`, `hl.workspace_rule`,
  `hl.dispatch(hl.dsp.workspace.move{…})`), falling back to the legacy keyword
  form for older Hyprland.
- **Side placement** uses explicit coordinates, not `auto-right`: the primary is
  first *pinned* at its current position (with `auto` placement Hyprland
  re-places it every time EXT moves, so the two leapfrog), then EXT is placed
  flush using its *live, scale-snapped* logical size (Hyprland snaps 1.5 → 1.6
  when that keeps logical pixels integral). The primary goes back to `auto` on
  teardown.
- **Workspace split:** workspaces 1–10 are bound to the laptop, 11–20 to the
  phone — set *before* the output is created so EXT opens on 11.
- **Disconnect (`remove_output`) runs in a fixed order.** Each step closes a way
  the compositor could freeze:
  0. close the xdph screencast session if one is still open (a killed daemon
     can leave one);
  1. stop and *wait for* every wf-recorder still capturing EXT
     (`kill_stale_capture`: SIGTERM, then SIGKILL after 2 s). A screencopy frame
     still pending on a destroyed output can hang Hyprland;
  2. focus the primary, because removing the focused monitor is a crash path;
  3. move EXT's workspaces back to the primary, so no window vanishes;
  4. `hyprctl output remove`, then hand the primary back to `auto`.
- wf-recorder and the GStreamer pipeline are spawned with `PR_SET_PDEATHSIG`, so they die with the daemon
  even when the daemon is SIGKILLed. Without that, an orphaned wf-recorder kept
  capturing a monitor that the TUI was about to remove.

### Stage 0b — Virtual monitor, GNOME backend (`backends/gnome.py`)

Mutter has no "create output" command, but its ScreenCast D-Bus API can create a
**virtual monitor** — the mechanism GNOME Remote Desktop uses for "extend":

```
org.gnome.Mutter.ScreenCast.CreateSession({})              -> session
session.RecordVirtual({cursor-mode: 1 (embedded), is-platform: true}) -> stream
session.Start()   ->  stream emits PipeWireStreamAdded(node_id)
```

The monitor appears once a PipeWire consumer negotiates a format, and **its
resolution is whatever the consumer asks for** — so the `width,height` caps in
our GStreamer pipeline literally set the monitor's mode. The daemon watches
`org.gnome.Mutter.DisplayConfig` for the new connector (`Meta-0`…), then places
it with `ApplyMonitorsConfig` (method *temporary*: nothing written to
`monitors.xml`, no "keep changes?" dialog). The layout is rebuilt atomically —
other monitors keep their position, EXT is butted flush against the primary on
the chosen side, everything is shifted so the top-left is `0,0` (Mutter rejects
offset or non-adjacent layouts), and the scale is snapped to one Mutter supports
for that mode.

The virtual monitor lives exactly as long as the daemon's D-Bus connection: when
the daemon exits (even if killed), Mutter removes it and rehomes its windows.

### Stage 1 — Capture + encode

**Hyprland:** `wf-recorder` (wlr-screencopy, DMA-BUF) → FFmpeg `h264_vaapi` or
`h264_nvenc`, whichever matches the compositor's GPU (§3):

```bash
LIBVA_DRIVER_NAME=radeonsi wf-recorder -o EXT-1 -c h264_vaapi -d /dev/dri/renderD128 \
    -p g=30 -p bf=0 -p b=12M -p rc_mode=CBR -p async_depth=1 \
    -r 60 --muxer=h264 -f /dev/stdout
# compositor on NVIDIA (zero-copy; <node> comes from the xdph session):
gst-launch-1.0 pipewiresrc path=<node> keepalive-time=1000 ! 'video/x-raw(memory:DMABuf)' \
    ! glupload ! glcolorconvert ! 'video/x-raw(memory:GLMemory),format=NV12' \
    ! nvh264enc preset=p1 tune=ultra-low-latency rc-mode=cbr bitrate=12000 gop-size=30 bframes=0 \
    ! h264parse config-interval=-1 ! 'video/x-h264,stream-format=byte-stream,alignment=au' ! fdsink
# compositor on NVIDIA, fallback (costs Hyprland a readback per frame):
wf-recorder -o EXT-1 -c h264_nvenc -p g=30 -p bf=0 -p b=12M -p preset=p1 -p tune=ull \
    -p zerolatency=1 -p rc=cbr -p delay=0 -p forced-idr=1 -r 60 --muxer=h264 -f /dev/stdout
```

The **scale** is snapped first (`valid_scale`): Hyprland only accepts scales
that give whole logical pixels in both dimensions (in 1/120 steps). 2772×1280 at
1.5 would be 853.3 px tall, so Hyprland shows *"invalid scale"*, substitutes its
own guess, and the side position computed from 1.5 then overlaps the laptop
screen (*"Monitor EXT-1 overlaps with other monitor(s)"*). The daemon uses the
nearest valid scale instead (4/3 here) and logs the substitution.

**GNOME:** `gst-launch-1.0` reading the PipeWire node:

```
pipewiresrc path=<node> do-timestamp=true keepalive-time=1000
  ! video/x-raw,width=W,height=H            ← sizes the virtual monitor
  ! queue max-size-buffers=1 leaky=downstream
  ! <encoder chain>
  ! h264parse config-interval=-1             ← SPS/PPS before every IDR
  ! video/x-h264,stream-format=byte-stream,alignment=au
  ! fdsink fd=3                              ← a pipe to the daemon
```

The encoder is picked by probing which GStreamer elements exist, hardware first:
`vah264enc` → `vah264lpenc` → `vaapih264enc` → `x264enc` → `openh264enc` (the VA
plugins only register an element when the driver can really encode H.264, so
"element exists" is a reliable probe). Override with `--encoder`.

Every encoder is configured the same way, for the same reasons:

| Setting | Why |
| --- | --- |
| **no B-frames** | B-frames need *future* frames → reorder delay |
| keyframe every `gop` frames (30) | fast joins and fast recovery from loss |
| CBR at the quality preset's bitrate | no bitrate spikes that stall the link |
| **one slice per frame** (`sliced-threads=false` for x264) | the phone's hardware decoder wants one complete access unit per input buffer |
| raw Annex-B, no container | the decoder eats it directly |
| `keepalive-time=1000` (GNOME) | Mutter only sends frames on damage; resend the last one every second |

### Stage 2 — NAL parsing + broadcast (`tandemd.py`)

The daemon splits the Annex-B byte stream into **NAL units**:

```
…00 00 00 01 [67 …SPS…] 00 00 00 01 [68 …PPS…] 00 00 00 01 [65 …IDR…] 00 00 00 01 [41 …P…]…
               type 7                  type 8                 type 5                 type 1
```

| Type | Name | Role |
| --- | --- | --- |
| 7 | **SPS** | resolution, profile — the decoder can't start without it |
| 8 | **PPS** | slice decoding parameters |
| 5 | **IDR** | a keyframe: decodable on its own |
| 1 | non-IDR | a P-frame: only the *difference* from earlier frames |

The parser uses `bytes.find` (C speed — the old per-byte Python loop burned ~1
CPU-second per second at 20 Mbit/s). A NAL is complete when the next start code
appears; but the *last* NAL of a burst has no successor until the next frame, so
when the pipe stays quiet for 5 ms the buffered tail is flushed as complete
(encoders write an access unit as back-to-back `write()`s microseconds apart, and
a frame interval is ≥ 8 ms even at 120 fps). Without this, the final frame of
every burst — e.g. the last state after you stop moving a window — only reached
the phone when *something else* changed.

`Broadcaster.feed()` then applies these rules:

```
for each NAL:
    cache SPS / PPS
    if IDR (first slice): GOP cache = [SPS, PPS, IDR]
    else:                 GOP cache += NAL     (dropped if it grows past 16 MB)
    for each client:
        started  → enqueue NAL
        not yet  → wait for an IDR, then enqueue SPS, PPS, IDR → started
on connect:
    enqueue banner (settings JSON), then replay the whole GOP cache → started
```

**GOP replay** is what makes joining instant even on a static desktop: the new
phone receives the last keyframe and every P-frame since, decodes them in a
burst, and is showing the *current* picture within milliseconds (measured: 2 ms
from accept to first picture data). Before this, a phone joining an idle desktop
waited for damage that might never come — a black screen.

### Stage 3 — Transport (framed TCP inside TLS)

The TCP connection is wrapped in **TLS**, and a phone gets nothing until it has
proved it scanned this computer's QR code (pairing key + certificate
fingerprint): see [`SECURITY.md`](./SECURITY.md). Inside the TLS connection,
every message, in both directions, is **`4-byte type` + `4-byte big-endian
length` + body**:

| Type | Direction | Body | When |
| --- | --- | --- | --- |
| `NALU` | PC → phone | one Annex-B NAL, start code included → straight to the decoder | video |
| `ASX1` | PC → phone | JSON settings: `side width height fps scale quality bitrate codec gop host backend` | on connect, and on any live config change |
| `PING` | PC → phone | empty | after 1 s with nothing else to send |
| `HELO` `CHAL` `AUTH` `OKAY` `DENY` | both | the pairing handshake ([`SECURITY.md`](./SECURITY.md) §3) | once, before anything else |
| `INPT` | PC → phone | JSON `{"touch": true\|false, "reason": "<why not>"}` | right after the phone is admitted |
| `TUCH` | phone → PC | 6 bytes: `action u8, finger u8, x u16, y u16` (big-endian) | every touch change (Stage 5) |

After the handshake, unknown types are ignored on both ends, so new types stay
compatible. The daemon refuses phone → PC frames larger than 4 KB (1 KB during
the handshake).

**One I/O thread per phone.** Each phone has a queue of *whole frames* and a
single thread that both sends them and reads what the phone sends back,
`select()`ing on the non-blocking TLS socket and a wake-up pipe (a TLS
connection must not be used by two threads at once; see `SECURITY.md` §4.8). A
send that makes no progress for 5 s means a dead phone. The parser only ever
enqueues, so one slow phone can't stall the encoder pipe or other phones. If a
queue exceeds 4 MB (weak Wi-Fi), the *queued* frames are dropped — never the one
mid-send — and that phone restarts at the next keyframe. Previously the daemon
truncated a half-sent frame when resyncing; the phone then read garbage as a
frame length and dropped the connection, which on a busy link repeated forever.

**Liveness.** `PING` after 1 s of silence + a 4 s read timeout on the phone, plus
TCP keepalive (3 s idle, 1 s × 3 probes) on the daemon side: a dead link is
detected on both ends within seconds instead of hanging half-open.

**Links.** The daemon binds `0.0.0.0:5000`:

- **USB tethering** (no USB debugging): the phone becomes a USB network gadget
  and hands the PC an address on `192.168.42.0/24`.
- **Wi-Fi**: the phone connects to the PC's LAN address.
- `adb reverse tcp:5000 tcp:5000` also works (phone connects to `127.0.0.1`).

**Pairing QR.** The TUI encodes
`tandem://c?h=<ip1>,<ip2>&p=5000&n=<hostname>&k=<pairing key>&f=<cert fingerprint>&v=2` with every
plausible address (USB tether first or LAN first, per *Preferred
link*; Docker/VPN/VM bridges excluded) and draws it with Unicode half-blocks
(`qr.py`, dependency-free, verified with `zbarimg`). The app connects to *all*
listed hosts in parallel and keeps the first that answers — so the same QR works
over USB and Wi-Fi. `k` is the pairing key the phone must prove it knows to
connect at all, and `f` pins the computer's TLS certificate
([`SECURITY.md`](./SECURITY.md)). Under the QR the TUI also shows the key as a
typable *pairing code* (`xxxxx-xxxxx-xxxxx-xxxxx`) for entering an address by
hand.

> TCP's weakness: on lossy Wi-Fi a lost packet stalls everything behind it
> (head-of-line blocking). The roadmap moves Wi-Fi to RTP/UDP + FEC.

### Stage 4 — Decode + display (Android)

The phone feeds whole access units to the hardware `MediaCodec` decoder and
renders every frame on arrival. See [`ANDROID.md`](./ANDROID.md).

### Stage 5 — Touch input (phone → PC)

Touching the phone's screen acts on the virtual monitor, like a touchscreen
monitor would — **without moving the computer's mouse pointer**. That is the
key design decision: the phone's touches are injected as a virtual
*touchscreen*, never as a mouse. A touchscreen delivers each touch straight to
the window under the finger (Wayland `wl_touch`), so you can keep using the
laptop's touchpad while someone taps on the phone.

```
phone: MotionEvent ─▶ TouchForwarder ─▶ TUCH (fraction of the video picture)
   ─TLS─▶ client I/O thread ─▶ PhoneInput (touch on?) ─▶ TouchRouter (slots)
   ─▶ backend touch surface ─▶ compositor ─▶ window under the finger
```

**Coordinates.** The phone sends positions as fractions of the *video picture*,
scaled to 0..65535. Its video view is letterboxed to the stream's exact aspect
ratio, so a fraction of the view is the same fraction of the virtual monitor —
neither side needs the other's resolution, and the black bars never produce
touches. Each backend turns fractions into its own coordinate space.

**Fingers.** Android pointer ids travel as the `finger` byte. `TouchRouter`
(`phone_input.py`) gives every *(phone, finger)* its own slot on the shared
device (10 slots), so two phones touching at once never collide, and when a
phone disconnects mid-gesture its fingers are lifted (no stuck touch).
`action`: 0 down, 1 move, 2 up, 3 cancel (lift every finger of that phone).

**Pairing (who may touch).** Only phones that passed the pairing handshake are
connected at all ([`SECURITY.md`](./SECURITY.md)), so every phone the daemon
streams to has scanned this computer's QR code (or typed its pairing code).
Touch then only depends on touch input being on and a virtual touchscreen being
available. The daemon tells each admitted phone with `INPT`. **Configure →
Paired phones → forget all** creates a new key: the daemon disconnects every
phone at once, and each must scan the new QR.

**Hyprland: a uinput touchscreen** (`uinput_touch.py`). The daemon creates a
kernel input device named `tandem-touch` through `/dev/uinput` (pure Python ioctls,
no dependencies) speaking the multi-touch protocol *type B* that real
touchscreens use (`ABS_MT_SLOT`, `ABS_MT_TRACKING_ID`, `ABS_MT_POSITION_X/Y`,
plus `BTN_TOUCH` and `ABS_X/Y`, which libinput requires), flagged
`INPUT_PROP_DIRECT`. Its axes span the virtual monitor's pixels, and Hyprland is
told to map it onto that monitor only:

```
hyprctl eval 'hl.device({ name = "tandem-touch", output = "EXT-1" })'
```

(legacy fallback: `hyprctl keyword device[tandem-touch]:output EXT-1`; re-applied on
every live side/scale change because a config reload drops it). The device
lives as long as the daemon: windows only see touch support while it exists.

`/dev/uinput` is root-only on most distros, so this needs a one-time setup — a
udev rule that grants it to the user logged in at the seat (`uaccess`, the same
mechanism that gives you your sound card), plus loading the `uinput` module at
boot:

```
/etc/udev/rules.d/70-tandem-uinput.rules
  KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"
/etc/modules-load.d/tandem-uinput.conf
  uinput
```

The installer offers it; the TUI's live view shows *Touch: needs one-time setup —
press t* when it's missing (`core.touch_setup_check`).

**GNOME: Mutter RemoteDesktop** (`backends/gnome.py`). No root and no uinput:
the backend creates an `org.gnome.Mutter.RemoteDesktop` session first, then
links the ScreenCast session to it (`CreateSession({"remote-desktop-session-id":
…})`) and starts the pair through the RemoteDesktop session. Touches go to
`NotifyTouchDown/Motion(stream, slot, x, y)` / `NotifyTouchUp(slot)`, where
`stream` is our virtual-monitor stream and `x, y` are in its pixel space. Mutter
ties the session to the D-Bus connection that created it, so it lives exactly as
long as the daemon. If the RemoteDesktop session can't be created, the backend
falls back to a plain ScreenCast session: video works and phones are told why
touch is unavailable.

**What it can and can't do.** Taps, drags, scrolling, long-press menus and
pinch-zoom work in apps that support touch on Wayland (GTK, Qt, Chromium/
Electron, Firefox) — the apps interpret the gestures, exactly as with a real
touchscreen. Tapping a window focuses it, so keyboard input follows your taps.
There is no hover, and an app that ignores touch events ignores the phone too.

**Verified** (2026-10-16): Hyprland 0.56.2 with the real daemon (taps land at the
right logical position on `EXT-1`, two fingers get separate slots, the
disconnect lifts a finger left down, the cursor doesn't move); GNOME in
containers on Mutter 46.2 (Ubuntu 24.04) and 48.8 (Fedora 42), observed with
`wev`; a wrong pairing key gets `touch: false` and injects nothing. On a real
phone (Android, USB with adb reverse, Hyprland + NVENC at 2560×1440@120):
taps, scrolling and pinch work smoothly; 138 recorded touches, every finger-down
matched by a finger-up.

---

## 5. End-to-end latency budget

Glass-to-glass over USB (Hyprland + VA-API):

| Stage | Typical |
| --- | --- |
| Capture (DMA-BUF, zero-copy) | 1–2 ms |
| VA-API encode (no B-frames) | 3–8 ms |
| NAL parse + enqueue + TCP send | < 1 ms |
| USB tethering transport | 2–5 ms |
| MediaCodec decode (low-latency) | 5–10 ms |
| Surface render (vsync) | 8–16 ms |
| **Total** | **~25–45 ms** |

On GNOME with a software encoder add ~5–15 ms for `videoconvert` + x264
(`threads=2` frame threading costs about one frame). A VA-API GStreamer encoder
brings it back in line with Hyprland.

---

## 6. Process & lifecycle

```
tandem (python.sh: /usr/bin/python3) ──▶ tui.py
tui.py ──spawns──▶ tandemd.py --keep-output --banner-file …   (same interpreter)
                       │
                       ├─ backend.setup()          create the virtual monitor
                       ├─ backend.start_capture()  wf-recorder | gst-launch-1.0  → pipe
                       ├─ start_touch()            virtual touchscreen (uinput / RemoteDesktop)
                       ├─ accept thread            TCP :5000, per phone: a sender + a reader thread
                       ├─ control thread           SIGHUP → re-read banner → apply side/scale live
                       └─ on exit                  stop capture, close clients, remove the touch device,
                                                   remove monitor (unless --keep-output on Hyprland)
```

- **Always the system Python.** The launchers (`tandem`, `start-tandem.sh`,
  `install.sh`) source `python.sh`, which picks `/usr/bin/python3` rather than
  the first `python3` on `PATH`. Distro packages (PyGObject) only exist for the
  system interpreter, and conda's bundled ncurses can't find the system
  terminfo database — a conda `python3` first on `PATH` made the GNOME check
  report `python gi` missing forever and crashed curses at startup. The TUI
  starts the daemon with `sys.executable`, so both share that interpreter.
  `tui.setup_terminal()` still recovers when run directly under such a Python.
- The TUI owns the daemon so it survives leaving the live view. `q` keeps
  streaming; `d` disconnects and removes the monitor. The UI shows
  "Disconnecting…" while it works, so the pause isn't mistaken for a hang.
- **Shutdown never blocks in the signal handler.** SIGTERM only sets the stop
  flag. The parser loop notices within 0.5 s, and the `finally` stops capture,
  closes clients and removes the touch device. The TUI waits up to 8 s before
  SIGKILL (it used to wait 4 s, which the daemon's own 3 s encoder wait could
  overrun). Only after the daemon has exited is the monitor removed.
- **Live edits:** `side`/`scale` are applied without restarting — the TUI rewrites
  `~/.config/tandem/daemon-banner.json` (atomically) and sends
  `SIGHUP`; the daemon's control thread moves/rescales the monitor through the
  backend and pushes a fresh `ASX1` frame to connected phones. Resolution, fps,
  quality, gop, port or backend changes restart the daemon; on Hyprland the
  monitor survives (`--keep-output`), on GNOME it is recreated.
- A stale daemon from a previous run is killed by `/proc` argv inspection (never
  `pkill -f`, which matches the parent shell and kills it too).

---

## 7. Bugs fixed in this revision (and why they happened)

The "connects, then disconnects by itself" report had several independent
causes; each is fixed and documented next to the code:

| Symptom | Root cause | Fix |
| --- | --- | --- |
| App loops connect → disconnect | A finished session's `onStopped` nulled the *current* session; the reconnect then created a second decoder on the same Surface, which failed, and so on | Every callback carries its session; stale ones are ignored. The Surface is released synchronously in `surfaceDestroyed` |
| Drops under load / weak Wi-Fi | Resync truncated a half-sent frame → phone parsed garbage lengths | Queues hold whole frames; only queued (unsent) frames are dropped |
| Black screen after connecting to an idle desktop | No damage → no frames → no keyframe for the new client | GOP cache replayed on connect; GNOME `keepalive-time` |
| Last frame of a motion "missing" until the next change | Tail NAL held until the next start code | Quiet-pipe tail flush |
| Silent hangs | No liveness check on either end | `PING` heartbeat + phone read timeout + TCP keepalive |
| Freezes at native phone resolution | Decoder input buffers sized for a 1080p guess; big IDRs silently dropped | `KEY_MAX_INPUT_SIZE` from the real resolution (banner) |
| "Reconnecting…" forever with a wrong address | Any failure, even the very first, auto-retried | Auto-retry only after a stream has worked; first-time failures show a specific error |
| Hyprland: capture dies at once (`Unsupported format: bgra`) | Encoder on a different GPU than the compositor (Hyprland on NVIDIA, VA-API on the iGPU with the NVDEC VA driver) | Encoder follows the compositor GPU: NVENC on NVIDIA, VA-API (with the right `LIBVA_DRIVER_NAME`) elsewhere |
| Hyprland: *"invalid scale"* / *"EXT-1 overlaps"* popups | Requested scale gave fractional logical pixels; position computed from it. Also a restart overlapping the old daemon's teardown | Nearest valid scale; a new daemon waits for the old one to exit |
| Hyprland: main screen drops frames while streaming, worse over time | Compositor on NVIDIA → wf-recorder captures through CPU `wl_shm` copies on Hyprland's render thread (87 % vs 13 % with capture paused) | Zero-copy capture via xdph + GStreamer NVENC (§3); wf-recorder only as a fallback |
| Pressing `d` sometimes freezes the system | The TUI could SIGKILL the daemon before the daemon stopped wf-recorder. The orphaned capture stayed on EXT-1, and the output was removed under it, sometimes while it still had focus | Non-blocking signal handler, 8 s grace, `PR_SET_PDEATHSIG`, ordered `remove_output` (stop capture → focus primary → move workspaces → remove) |
| Live view costs ~5 % CPU while idle | QR code re-encoded (pure Python) every 150 ms | Re-encoded only when the URI changes |
| Resolution/scale/side ignored on Hyprland ≥ 0.55 | `hyprctl keyword`/`dispatch` broken by the Lua parser (fix existed but was left in a git stash) | `hyprctl eval` Lua API (restored and committed) |

---

## 8. Where to go next

- **RTP/UDP + FEC** for lossy Wi-Fi (keep TCP for USB).
- **HEVC** decode in the app (the Hyprland backend can already encode it).
- **KDE Plasma** backend (KWin virtual outputs via `kwin_wayland` screencast).
- **Pen input** (pressure, hover) and a keyboard from the phone.
- **Latency HUD** overlay for tuning.
