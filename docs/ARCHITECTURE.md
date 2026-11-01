# Tandem architecture and pipeline

This document describes the whole system: how a desktop frame gets from a
virtual monitor to the phone's screen, the exact bytes sent on the wire, and
the rules that keep a connection reliable. Platform setup for Arch, Ubuntu and
Fedora is in [`PLATFORMS.md`](./PLATFORMS.md). The phone app is covered in
[`ANDROID.md`](./ANDROID.md).

---

## 1. The core idea

A phone can act as a second monitor for a Linux desktop. Drag a window onto
the phone and it shows up there with very little lag.

To the compositor (the program that draws the desktop), a monitor is an output
it can draw into. The output does not have to be physical. So:

1. Create a monitor that does not physically exist, a virtual output. The
   compositor renders into it the same way it renders into a real screen.
2. Capture what is drawn there and compress it to H.264, on the GPU where
   possible.
3. Send it to the phone over USB tethering or Wi-Fi.
4. Decode and display it with no buffering.
5. Send touches back and replay them on the virtual monitor through a virtual
   touchscreen, so the phone works as a touch monitor (section 4, stage 5).

The phone is a thin client. It decodes video and reports touches. The
monitor logic lives on the Linux side, in a backend for each compositor:

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

## 2. Why the pipeline avoids buffering

Capture and encoding are rarely what limits latency. Most of the delay comes
from a few choices that can be avoided. The table lists common choices and
what this design does instead.

| Common choice | Problem | This design |
| --- | --- | --- |
| Video player such as mpv or VLC | Players add a 0.5 to 2 s jitter buffer by design | A dedicated app feeds frames to the hardware decoder with no buffer |
| MPEG-TS container | The decoder has to demux 188-byte packets first | Raw Annex-B H.264 |
| Encoder on a different GPU from the compositor | Every frame is copied between GPUs | Encoder on the GPU the compositor renders on (section 3), with zero copy |
| Kernel virtual monitor (VKMS) | CPU-rendered, and it crashed the session during testing | Compositor-native virtual outputs |

The guiding rule: a frame never waits in a buffer. Every stage forwards its
output as soon as it is ready. A consumer that falls behind resyncs from the
last keyframe (the cached GOP) or the next one, instead of receiving a growing
backlog.

---

## 3. Encode on the GPU the compositor renders on (Hyprland)

```
card0 / renderD129  →  NVIDIA RTX 4060 (dGPU)
card1 / renderD128  →  AMD Radeon 890M (iGPU)
```

wf-recorder can pass frames to the encoder without copying them (DMA-BUF) only
when the encoder sits on the same GPU that Hyprland renders on. Across GPUs it
logs `compositor running on different device, disabling DMA-BUF`, falls back to
CPU copies in `bgra`, and VA-API then fails with `Unsupported format: bgra /
Cannot create hw frames context`. Capture stops before the first frame.

Which GPU renders depends on the user's configuration: the first entry of
`AQ_DRM_DEVICES` (Hyprland exports it to every child process), or the boot VGA
device when that variable is unset. At startup, `pick_encoder` in
`backends/hyprland.py` makes the choice:

| Compositor GPU (kernel driver) | Encoder | Notes |
| --- | --- | --- |
| `nvidia` | xdph and GStreamer `nvh264enc` (zero copy, `backends/xdph.py`), then wf-recorder `h264_nvenc` with `preset=p1 tune=ull zerolatency=1 rc=cbr delay=0 forced-idr=1` | NVIDIA's VA driver (`nvidia-vaapi-driver`) only decodes. wf-recorder cannot pass GPU frames to NVENC (see below) |
| `amdgpu`, `i915` or `xe` | `h264_vaapi` on that GPU's render node | `LIBVA_DRIVER_NAME` is forced to `radeonsi` or `iHD` (`i965` when iHD is not installed). With iHD, `i965` is tried next when it is installed, for GPUs older than iHD supports (Haswell, Ivy Bridge) |
| Anything, when the above fails to start | `libx264` on the CPU (`preset=ultrafast tune=zerolatency`, one slice, `-x yuv420p`), then `libopenh264` | NVIDIA without NVENC, nouveau, virtual machines, VA drivers without H.264 encoding. Only the CPU encoders that `ffmpeg -encoders` lists are offered, so Fedora's and openSUSE's `ffmpeg-free` (no x264) gets OpenH264 |

Each candidate gets 4 s to produce its first byte. A working encoder needs
about 0.1 s. An encoder that cannot start either makes wf-recorder exit, or
leaves it running but silent (seen with VA-API on Intel). In both cases the
backend stops it and moves to the next candidate, so the phone does not sit on
a black screen (`encoder X failed to start; trying the next one`). The last
candidate is never stopped for being silent, and neither is an encoder that
already worked earlier in the session: a capture restart on a still desktop is
silent too, and must not drop the session to a CPU encoder. If every candidate
exits, `start_capture` raises `no working H264 encoder (tried: …)`.

`start_capture` can be called again mid-session (the daemon does this when an
encoder exits, section 6). It tries the encoder that worked last time first.
`stop_capture` is idempotent and never touches the virtual output.

A `--codec hevc` request is mapped to the same encoder family (`hevc_vaapi`,
`hevc_nvenc`, `libx265`), and OpenH264 and xdph are left out because they only
encode H.264. The app only decodes H.264, so the daemon logs a warning for any
codec other than `h264`.

Forcing `LIBVA_DRIVER_NAME` matters. NVIDIA setups often export
`LIBVA_DRIVER_NAME=nvidia` globally. That makes `vainfo` and FFmpeg load the
decode-only NVDEC driver, even on the AMD or Intel node. The choice can be
overridden with `tandemd.py --encoder h264_vaapi|h264_nvenc`, where `--device`
picks the VA node.

On a laptop where Hyprland renders on the NVIDIA GPU
(`AQ_DRM_DEVICES=card0:card1`), streaming uses NVENC. In CBR mode NVENC emits a
filler NAL after almost every frame. The daemon drops filler, SEI and AUD NALs
(`NAL_DROP`) before they reach the network.

### Why NVIDIA uses xdg-desktop-portal-hyprland

wf-recorder captures through DMA-BUF only for VA-API, and only on the
compositor's own GPU. With NVENC, or with VA-API on the iGPU, it either fails
or logs the different-device message. So when Hyprland runs on NVIDIA, every
frame travels through `wl_shm` buffers (`/tmp/wf-recorder-shared-*`), and
Hyprland reads each frame back from the GPU on its single render thread. At
2560x1440 and 120 Hz, that thread was busy 87 % of the time with capture
running, and 13 % with wf-recorder paused. The main screen then has no spare
time, so it drops frames, more often as the laptop heats up. wf-recorder also
keeps a pool of about 11 shared-memory frames (around 0.5 GB resident at
2400x1080), which Hyprland maps as well.

So on NVIDIA, the capture goes through xdg-desktop-portal-hyprland (xdph) and
stays on the GPU. xdph asks Hyprland for DMA-BUF frames. That costs Hyprland a
GPU copy instead of a readback, and GStreamer encodes the frames without a CPU
copy:

```
xdph ScreenCast session (EXT-1)  ->  PipeWire node, DMA-BUF in NVIDIA's tiled layout
  -> pipewiresrc -> glupload -> glcolorconvert (NV12 on the GPU)
  -> nvh264enc (CUDA/GL interop; preset p1, tune ultra-low-latency, CBR, no B-frames)
  -> h264parse (byte-stream, one access unit per buffer) -> pipe -> tandemd
```

- The daemon calls xdph's backend interface directly
  (`org.freedesktop.impl.portal.ScreenCast` on
  `org.freedesktop.impl.portal.desktop.hyprland`). It passes restore data
  (xdph format, version 3) that names the output. xdph treats that as a saved
  choice and shows no dialog. `Start` returns the PipeWire node, which
  `pipewiresrc path=<node>` reads.
- Hyprland only produces frames when the desktop changes.
  `keepalive-time=1000` resends the last frame every second, so a phone that
  joins a still desktop is not left with a black screen. The property is probed
  first, and left out on a `pipewiresrc` that does not have it.
- The session is closed in `stop_capture`, before any output is removed. Its
  D-Bus path is kept in `$XDG_RUNTIME_DIR/tandem-xdph-session`, so
  `remove_output` can close a session that a killed daemon left open. Session
  and request paths carry a per-process counter, so a quick capture restart
  never reuses the path of the session it just closed.
- If xdph is not running, a GStreamer element is missing, or the pipeline
  produces nothing for 4 s, the backend falls back to wf-recorder NVENC, then
  x264. The TUI's Dependencies screen lists these pieces as optional on NVIDIA
  machines.

Measured on a laptop with an RTX 4060 running Hyprland 0.56, at 2400x1080 and
60 Hz, with a terminal redrawing every frame: both paths deliver about 59 fps.
The difference is in the cost. wf-recorder maps `wf-recorder-shared` buffers
into Hyprland, while the GStreamer process uses about 8 % CPU against 11 to
12 % for wf-recorder. End to end, through the daemon to a paired test client,
503 decodable frames arrived in 8 s.

---

## 4. The pipeline, stage by stage

### Stage 0a: virtual monitor on Hyprland (`backends/hyprland.py`)

```bash
hyprctl output create headless EXT-1
hyprctl eval 'hl.monitor({ output = "EXT-1", mode = "2400x1080@60", position = "auto-right", scale = 1.5, transform = 0 })'
```

EXT-1 looks exactly like a plugged-in monitor. Details that matter:

- **Tandem never modifies your existing monitors.** Every rule it writes names
  its own output only. It does not pin, move, rescale or "restore" the laptop
  screen or any other monitor, so there is nothing to put back after a crash and
  no state file. (Earlier versions pinned the primary at its position and set it
  back to `auto` on disconnect. On Hyprland 0.55 and later, a partial rule like
  that reset the user's monitor to defaults.)
- **Two config dialects.** Hyprland 0.55 and later use a Lua config parser.
  `hyprctl keyword monitor` and `hyprctl dispatch` silently do nothing there.
  The dialect is detected once with the no-op `hyprctl eval 'local _ = 1'`, and
  cached only when Hyprland gives a definite answer (`dialect()`). Every state
  change then goes through either `hyprctl eval '<lua>'` (`hl.monitor`,
  `hl.workspace_rule`, `hl.dispatch(hl.dsp.focus{…})`,
  `hl.dispatch(hl.dsp.workspace.move{…})`, `hl.device`) or the legacy form
  (`keyword monitor`, `keyword workspace`, `dispatch focusmonitor`,
  `dispatch moveworkspacetomonitor`, `keyword device[…]:output`). The `ok`
  reply is checked in both cases. The daemon logs `Hyprland config dialect: …`.
- **Finding Hyprland.** When `HYPRLAND_INSTANCE_SIGNATURE` is unset (over ssh,
  or from a systemd user service) and exactly one Hyprland instance is running,
  that one is used. An instance counts only if its `.socket.sock` accepts a
  connection, because a crashed session leaves its directory behind. With
  several live instances the daemon warns instead of guessing.
- **Side placement uses `auto-<side>`.** Hyprland lays out explicitly placed
  monitors first, then the `auto` ones in creation order. The headless output is
  created after the user's monitors, so it is placed last and can never push one
  of them. (An explicit EXT position did: the user's auto-placed monitors were
  arranged after it, moved out of its way, and the two leapfrogged.) The rule is
  complete, mode, position, scale and `transform 0`, so nothing is left to
  Hyprland's catch-all rule. The layout is then read back, never assumed
  (`_settled_problem`). The Lua API applies rules on a later frame, so the check
  polls until EXT reports the requested mode and scale and sits on the right
  side, and only calls a placement bad after `SETTLE_TIMEOUT` (1.5 s). A good
  placement returns at once.
- **When `auto-<side>` is not enough.** In some multi-monitor layouts
  `auto-<side>` aligns EXT with the layout's top or left edge and leaves it
  touching no monitor. Then `auto-center-<side>` (Hyprland 0.51 and later) is
  tried. Hyprland older than 0.46 did not count `auto-right` monitors when
  placing `auto-down`, so EXT could land on top of the main screen. It then goes
  to `auto-right` with a warning. Every attempt only moves EXT.
- **Logical size.** `logical_size()` swaps width and height for odd transforms,
  so a rotated monitor is measured correctly.
- **One workspace for the phone.** Tandem binds a single workspace to EXT, as
  its default, and touches no other workspace. The id is chosen at runtime
  (`pick_ext_workspace`): the one a reused EXT already shows, otherwise the
  first free id at or above 11 and above every workspace and every numeric
  workspace rule the user has. A reused output's workspace is kept only if no
  user rule binds that number to another monitor. The binding is made before
  the output is created, so EXT opens on it. On Lua the rule object is kept in
  the global `_G.__tandem_ws_rules[<output>]` and disabled with
  `set_enabled(false)` on disconnect. Legacy Hyprland has no rule removal, so
  the rule stays, naming an output that no longer exists, and is gone at the
  next config reload.
- **Creating the output is checked.** `create_output` waits up to 2 s for the
  output to appear. If Hyprland made a `HEADLESS-*` output under another name
  (very old builds ignore the name), that stray output is removed again and the
  daemon fails with `Hyprland did not create the EXT-1 output (…)`. If creation
  fails, the workspace binding is undone.
- **Config reloads and hotplug.** A config reload rebuilds every rule from the
  user's files (autoreload fires on every save, and display GUIs write the
  config), which would drop EXT back to the catch-all rule and lose the
  workspace rule and touch mapping. `EventWatcher` follows Hyprland's event
  socket (`.socket2.sock`). On `configreloaded`, or another monitor being added
  or removed, it re-applies EXT's mode, scale and side, the workspace rule and
  the touch mapping, debounced by 0.4 s, and logs `re-applied … after a
  Hyprland config reload or monitor change`. Events within 1 s of Tandem's own
  change (`ECHO_WINDOW`) are ignored, so a re-apply can never trigger itself.
  Teardown and re-apply share a lock, so a re-apply in flight cannot re-create
  the workspace rule after disconnect dropped it.
- **The focus target** for disconnecting is `primary_for()`: an enabled,
  non-mirrored, physical monitor, preferring the one at `0,0`, then the lowest
  id. It never follows focus, so it is the same every time.
- Disconnecting (`remove_output`) runs in a fixed order. Each step closes a way
  the compositor could freeze:
  0. Close the xdph screencast session if one is still open. A killed daemon
     can leave one behind.
  1. Stop, and wait for, every wf-recorder still capturing EXT
     (`kill_stale_capture`: SIGTERM, then SIGKILL after 2 s). A screencopy frame
     still pending on a destroyed output can hang Hyprland.
  2. If EXT has focus, focus the main monitor. Removing the focused monitor is a
     crash path. Focus is left alone otherwise.
  3. Move EXT's workspaces to the main monitor, so no window disappears. Named
     workspaces have negative ids, which no dispatcher accepts, so they are
     addressed as `name:<name>`. `special:` workspaces are skipped, because
     Hyprland moves those itself.
  4. Run `hyprctl output remove`. The workspace rule is dropped even if the
     output was already gone.
- wf-recorder and the GStreamer pipeline are started with `PR_SET_PDEATHSIG`,
  so they exit with the daemon even if it is killed with SIGKILL. Without that,
  an orphaned wf-recorder could keep capturing a monitor that is being removed.

### Stage 0b: virtual monitor on GNOME (`backends/gnome.py`)

Mutter has no "create output" command. Its ScreenCast D-Bus API can create a
virtual monitor, though. This is the same mechanism GNOME Remote Desktop uses
for extending the desktop:

```
org.gnome.Mutter.ScreenCast.CreateSession({})              -> session
session.RecordVirtual({cursor-mode: 1 (embedded), is-platform: true}) -> stream
session.Start()   ->  stream emits PipeWireStreamAdded(node_id)
```

The monitor appears once a PipeWire consumer negotiates a format, and its
resolution is whatever the consumer asks for. The `width,height` caps in the
GStreamer pipeline therefore set the monitor's mode. The daemon watches
`org.gnome.Mutter.DisplayConfig` for the new connector (`Meta-0` and so on),
then places it with `ApplyMonitorsConfig` using the *temporary* method. That
writes nothing to `monitors.xml` and shows no "keep changes?" dialog. The
layout is rebuilt in one step (`place_monitor`):

- Every other monitor is sent back exactly as Mutter reports it: mode id,
  scale, transform, primary flag, relative position, and, when Mutter reports
  them, underscanning, `color-mode` (HDR) and `rgb-range`. The whole layout is
  only shifted so its top-left corner is at `0,0`, which Mutter requires.
- EXT goes beside the anchor, the primary monitor (or the top-left-most one
  when there is no primary), on the chosen side (`place_beside`). If the spot
  flush against the anchor is taken, EXT goes to the outermost edge of the
  whole layout on that side, lined up with the edge monitor nearest the anchor.
  It never overlaps and always shares an edge, because Mutter rejects detached
  monitors.
- If Mutter made the virtual monitor primary, the primary role moves to the
  anchor, since a config with no primary is rejected.
- The global `layout-mode` is passed only when Mutter says it can be changed.
  With `global-scale-required`, EXT gets the other monitors' scale. Otherwise
  the scale is snapped to a value Mutter supports for that mode.
- Nothing is sent when the layout is already right, so re-checking causes no
  flicker. While a monitor has no current mode (the layout is still changing),
  placement fails with a clear error and is retried.

After capture starts, the placement thread (`_place_when_ready`) polls every
0.25 s for up to 15 s, and keeps checking for 5 s after the first good
placement. Mutter sometimes re-creates the virtual monitor under a new
connector name, and that late copy still gets placed. A lock keeps that thread
and a live side change from applying on the same serial.

The virtual monitor lives exactly as long as the daemon's D-Bus connection.
When the daemon exits, even if it was killed, Mutter removes the monitor and
moves its windows back.

### Stage 1: capture and encode

On Hyprland, `wf-recorder` (wlr-screencopy, DMA-BUF) feeds FFmpeg `h264_vaapi`
or `h264_nvenc`, whichever matches the compositor's GPU (section 3):

```bash
LIBVA_DRIVER_NAME=radeonsi wf-recorder -o EXT-1 -c h264_vaapi -d /dev/dri/renderD128 \
    -p g=60 -p bf=0 -p b=12000000 -p maxrate=12000000 -p bufsize=400000 \
    -p rc_mode=CBR -p async_depth=1 -B 60 --muxer=h264 -f /dev/stdout
# compositor on NVIDIA (zero-copy; <node> comes from the xdph session):
gst-launch-1.0 -q pipewiresrc path=<node> do-timestamp=true keepalive-time=1000 ! 'video/x-raw(memory:DMABuf)' \
    ! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream \
    ! glupload ! glcolorconvert ! 'video/x-raw(memory:GLMemory),format=NV12' \
    ! nvh264enc rc-mode=cbr bitrate=12000 gop-size=60 bframes=0 zerolatency=true rc-lookahead=0 \
        vbv-buffer-size=400 repeat-sequence-header=true preset=p1 tune=ultra-low-latency \
    ! h264parse config-interval=-1 ! 'video/x-h264,stream-format=byte-stream,alignment=au' ! fdsink sync=false
# compositor on NVIDIA, fallback (costs Hyprland a readback per frame):
wf-recorder -o EXT-1 -c h264_nvenc -p g=60 -p bf=0 -p b=12000000 -p maxrate=12000000 \
    -p bufsize=400000 -p preset=p1 -p tune=ull -p zerolatency=1 -p rc=cbr -p delay=0 \
    -p forced-idr=1 -B 60 --muxer=h264 -f /dev/stdout
```

The numbers are for 12 Mbit/s at 60 fps. Details that matter:

- **No `-r`.** wf-recorder's `-r` adds an FFmpeg fps filter that holds the
  newest frame until the next one arrives and, after an idle gap, emits
  `idle seconds × fps` duplicates, keyframes included, in one burst. That was
  the "freezes when I drag a window onto it" bug. Capture stays damage-driven
  (a variable frame rate). `-B <fps>` only tells the encoder's rate control the
  expected rate, and is passed only when this wf-recorder supports
  `--buffrate`.
- **GOP of about 1 s.** The keyframe interval is `max(--gop, fps)` frames
  (`keyframe_interval`). Capture is damage-driven, so a short frame-based GOP
  meant 2 to 4 keyframe bursts per second of motion. Recovery does not need
  them: a phone that loses a frame asks for one (`KEYQ`, stage 3) and gets the
  cached GOP replayed.
- **A 2-frame rate-control buffer** (`VBV_FRAMES = 2`) on every encoder:
  `maxrate` equals the bitrate and `bufsize` holds two frames' worth. A 1 s
  buffer let keyframes grow to several hundred KB and stall the link for the
  frames behind them. Trade-off: right after a keyframe the picture is softer
  for a moment and sharpens over the next frames. The bitrate is passed in bits
  per second.
- **libx264** gets `x264-params=sliced-threads=0:slices=1`, because
  `tune=zerolatency` turns on sliced threads, one slice per thread, and strict
  phone decoders take those NALs for separate frames. It runs `threads=1` up to
  the pixel rate of 1080p60 and `threads=2` above (each frame thread adds a
  frame of delay).
- **libopenh264** gets `rc_mode=bitrate allow_skip_frames=0 slices=1`.
- Every property beyond the basics on `nvh264enc` is probed with
  `gst-inspect-1.0`, so an older nvcodec plugin gets the subset it understands
  instead of failing to parse. Older plugins without `tune` get
  `preset=low-latency-hp`.
- gst-launch runs without `-e`. With it, SIGINT waits for an end-of-stream to
  drain through the pipeline, which can hang a stop on a stalled source.

The scale is snapped first (`valid_scale`). Hyprland accepts only scales that
give whole logical pixels in both dimensions, in steps of 1/120. At 1.5, a
2772×1280 output would be 853.3 px tall. Hyprland then shows *"invalid scale"*,
substitutes its own guess, and the side position, computed from 1.5, overlaps
the laptop screen (*"Monitor EXT-1 overlaps with other monitor(s)"*). The daemon
uses the nearest valid scale instead (4/3 here) and logs the substitution.

On GNOME, `gst-launch-1.0` reads the PipeWire node:

```
pipewiresrc path=<node> do-timestamp=true keepalive-time=1000   ← keepalive only if the plugin has it
  ! video/x-raw,width=W,height=H            ← sizes the virtual monitor
  ! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream
  ! <encoder chain>
  ! h264parse config-interval=-1             ← SPS/PPS before every IDR
  ! video/x-h264,stream-format=byte-stream,alignment=au
  ! fdsink fd=3 sync=false                   ← a pipe to the daemon
```

The leaky queue is the single frame of buffering between capture and encoder.
When the encoder falls behind, the oldest raw frame is dropped. A queue's
default of 200 buffers or 1 s would hold seconds of lag. `videoconvert` gets
`n-threads=min(4, CPUs)` where the element has it. gst-launch is started in its
own session (`start_new_session=True`), so Ctrl+C or a hangup on the launching
terminal cannot kill capture behind the daemon's back. It still dies with the
daemon: its next write to the pipe fails, and Mutter drops the PipeWire node
when the daemon's D-Bus connection closes.

The encoder is chosen by checking which GStreamer elements exist, hardware
first: `vah264enc`, then `vah264lpenc`, `vaapih264enc`, `x264enc` and
`openh264enc`. The VA plugins only register an element when the driver can
really encode H.264, so "the element exists" is a reliable test. `--encoder`
overrides the choice.

Every encoder is configured the same way, for the same reasons:

| Setting | Reason |
| --- | --- |
| No B-frames | B-frames need future frames, which adds reorder delay |
| A keyframe every `max(gop, fps)` frames, about 1 s (Hyprland and xdph), or every `gop` frames (GNOME) | Fast joins. A lost frame is recovered by `KEYQ` and GOP replay, not by frequent keyframes |
| CBR at the quality preset's bitrate | No bitrate spikes that stall the link |
| A rate-control buffer of 2 frames (`cpb-size` on `vah264enc`/`vah264lpenc`, `cpb-length` on `vaapih264enc`, `vbv-buf-capacity` on `x264enc`, `vbv-buffer-size` on `nvh264enc`), each set only if the element has it | Encoders defaulted to 0.6 to 1.5 s, which let each keyframe arrive as one big burst and caused a latency spike every GOP. `openh264enc` has no such property |
| One slice per frame (`sliced-threads=false` for x264) | The phone's hardware decoder wants one complete access unit per input buffer |
| Raw Annex-B, no container | The decoder reads it directly |
| `keepalive-time=1000` (GNOME) | Mutter only sends frames when something changes, so the last frame is resent every second |

### Stage 2: NAL parsing and broadcast (`tandemd.py`)

The daemon splits the Annex-B byte stream into NAL units:

```
…00 00 00 01 [67 …SPS…] 00 00 00 01 [68 …PPS…] 00 00 00 01 [65 …IDR…] 00 00 00 01 [41 …P…]…
               type 7                  type 8                 type 5                 type 1
```

| Type | Name | Role |
| --- | --- | --- |
| 7 | SPS | Resolution and profile. The decoder cannot start without it |
| 8 | PPS | Slice decoding parameters |
| 5 | IDR | A keyframe, decodable on its own |
| 1 | non-IDR | A P-frame, which holds only the difference from earlier frames |

The parser uses `bytes.find`, which runs at C speed. A per-byte Python loop
would use about one CPU core at 20 Mbit/s. The splitting itself is a pure class,
`AnnexBSplitter`, with no I/O, and `parse_annexb` feeds it from the pipe. A NAL
is complete when the next start code arrives. The last NAL of a burst has no
successor until the next frame, so the buffered tail is flushed as complete once
the pipe stays quiet. How long depends on the last read:

- **1 ms** (`TAIL_QUIET`) after a read whose size is not a multiple of 4096
  bytes. GStreamer's `fdsink` writes one access unit per `write()`, and FFmpeg
  writes a packet in full 32 KiB buffers plus a short final flush. Pipe writes
  that fit are atomic for the reader, so such a read ended where a write ended,
  on an access unit boundary.
- **30 ms** (`TAIL_QUIET_ALIGNED`) after a page-aligned read, which is likely
  the middle of a packet (a full FFmpeg buffer, or a write that filled the
  pipe).

Without this flush, the last frame of a burst, such as the final state after
you stop moving a window, would wait until something else changed. If bytes
arrive that continue a tail already flushed, that NAL went out cut short. The
daemon logs it and resyncs every phone on the next keyframe
(`stream_corrupted`). A failed `F_SETPIPE_SZ` (enlarging the pipe) is logged.

`Broadcaster.feed()` then applies these rules:

```
for each NAL:
    drop filler, SEI and AUD (NAL_DROP)
    cache SPS / PPS
    if IDR (first slice): GOP cache = [SPS, PPS, IDR]
    else:                 GOP cache += NAL     (dropped if it grows past 2 MB)
    for each client:
        started  → enqueue NAL; if its queue is too old or too big:
                   drop its queued video, replay the GOP cache (or wait for an IDR)
        not yet  → wait for an IDR, then enqueue SPS, PPS, IDR → started
on connect:
    enqueue banner (settings JSON), then replay the whole GOP cache → started
on KEYQ from a phone (at most once per 500 ms per phone):
    drop its queued video, replay SPS, PPS and the GOP cache → started,
    or, with no cache, ask the encoder for a keyframe and wait
```

The GOP cache is capped at 2 MB (`MAX_GOP_CACHE`, half the per-phone queue
limit), so a replay always fits in a phone's queue. A phone is marked started
only once its replay is actually queued. New phones get the banner even when the
GOP is large.

GOP replay makes joining instant, even on a static desktop. A new phone receives
the last keyframe and every P-frame after it, decodes them in a burst, and shows
the current picture within milliseconds. Measured: 2 ms from accept to first
picture data. Without replay, a phone that joins an idle desktop could wait for
a change that never comes, and stay black.

### Stage 3: transport (framed TCP inside TLS)

The TCP connection runs inside TLS. A phone receives nothing until it has proved
it scanned this computer's QR code, which carries the pairing key and the
certificate fingerprint. See [`SECURITY.md`](./SECURITY.md). Inside the TLS
connection, every message in both directions is a 4-byte type, a 4-byte
big-endian length, and a body:

| Type | Direction | Body | When |
| --- | --- | --- | --- |
| `NALU` | PC to phone | One Annex-B NAL, start code included, passed straight to the decoder | Video |
| `ASX1` | PC to phone | JSON settings: `side width height fps scale quality bitrate codec gop host backend` | On connect, and after any live config change |
| `PING` | Both | Empty | After 1 s with nothing else to send. Phone to PC only from current apps |
| `HELO` `CHAL` `AUTH` `OKAY` `DENY` | Both | The pairing handshake ([`SECURITY.md`](./SECURITY.md) §3) | Once, before anything else. A `DENY` can also come before `OKAY` when the computer is full or the phone is locked out |
| `INPT` | PC to phone | JSON `{"touch": true\|false, "reason": "<why not>"}` | Right after the phone is admitted |
| `TUCH` | Phone to PC | 6 bytes: `action u8, finger u8, x u16, y u16` (big-endian) | Every touch change (stage 5) |
| `KEYQ` | Phone to PC | Empty | The phone's decoder needs a fresh keyframe (see *Keyframe requests and resync* below) |

After the handshake, both ends ignore unknown message types, so new types can be
added without breaking older peers. An older daemon ignores `KEYQ` and the
phone's `PING`, and an older app never sends them. The daemon refuses
phone-to-PC frames larger than 4 KB, and 1 KB during the handshake.

Each phone gets one I/O thread. Its queues hold whole frames, and the same
thread sends them and reads what the phone sends back. It uses `select()` on
the non-blocking TLS socket and a wake-up pipe. A TLS connection must not be
used by two threads at once (see [`SECURITY.md`](./SECURITY.md) §4.8). The
wake-pipe write and its close both happen under the client lock, so a closing
phone can never write into a reused file descriptor. The parser only enqueues
frames, so one slow phone cannot stall the encoder pipe or other phones. An
error while handling one of the phone's messages is logged and does not close
the connection.

**Two queues per phone.** Control frames (the banner, `INPT`) sit in their own
queue, which is never dropped and is always sent first. Video sits in a second
queue that is bounded by age, not only by size:

| Limit | Value | What happens |
| --- | --- | --- |
| Oldest queued video frame (`MAX_QUEUE_AGE`) | 0.3 s | The phone has fallen behind: its queued video is dropped and the GOP cache is replayed at once, or it waits for the next keyframe |
| Queued video bytes (`MAX_PENDING`) | 4 MB | The same |
| A queued GOP replay (`RESYNC_GRACE`) | up to 1 s | A replay is queued as one batch that skips the age check until it has been sent, so a slow link can drain it |

A queue measured in bytes alone let a phone on weak Wi-Fi sit seconds behind
before anything was dropped. The frame being sent is never touched. A half-sent
frame is never cut short, because the phone would read the leftover bytes as a
frame length and drop the connection.

Sends go out in 64 KiB slices (`SEND_SLICE`). That way a send that makes no
progress for 5 s (`SEND_TIMEOUT`) really means the phone is dead, and the
phone's touches are read between slices.

**Socket options**, each best effort (`_tune_socket`):

| Option | Value | Why |
| --- | --- | --- |
| `TCP_NODELAY` | on | No Nagle delay on small frames |
| TCP keepalive | 3 s idle, then 1 s between 3 probes | Detects a dead idle link |
| `TCP_USER_TIMEOUT` | 5000 ms | Keepalive probes do not run while data is unacknowledged, which is always the case on a dead link once a `PING` is queued. Without this, the kernel retransmits for about 15 minutes |
| `TCP_NOTSENT_LOWAT` | 128 KiB | Keeps the kernel's unsent backlog small, so queued video stays in Tandem's queue, where whole frames can be dropped. `SO_SNDBUF` 512 KiB is the fallback where the option is missing |
| DSCP AF41 (`IP_TOS` 0x88, plus `IPV6_TCLASS` on IPv6) | | Marks the stream as interactive video. Wi-Fi (WMM) maps it to the video access category, ahead of bulk traffic. The app marks its socket the same way. Networks that ignore DSCP are unaffected |

**Liveness** works on both ends. The daemon sends `PING` after 1 s of silence,
and the phone gives up after a 4 s read timeout. Current apps also send `PING`
after 1 s with nothing else to send. The first `PING` from a phone turns on a
5 s read timeout (`READ_TIMEOUT`) for that phone only. Old apps never send it,
so they never get a read timeout. Together with TCP keepalive and
`TCP_USER_TIMEOUT`, a dead link is detected within seconds on both sides,
instead of hanging half open.

**Refusals before `OKAY`.** When four phones are already streaming
(`MAX_CLIENTS`), a new phone gets `DENY` *"already 4 phones connected to this
computer"* before any `OKAY`. An address that failed pairing 5 times within
60 s gets *"too many failed pairing attempts from this phone; try again in a
minute"*. Both texts contain a phrase the app treats as temporary, so it retries
after a pause instead of showing a final error. "refused" is logged only when a
`DENY` was actually sent, not for the app's abandoned parallel connects. Plain
transport errors (a reset, a timeout, EOF in the middle of the handshake) do not
count toward the lock-out. Only failed proofs and malformed messages do.

#### Keyframe requests and resync

A phone's decoder sometimes needs to start again from a keyframe: an access
unit was dropped or too big for its buffer, the codec reported an error, the
decoder watchdog reset it, or the phone skipped a stale backlog. It then sends
`KEYQ` (empty body). The daemon handles it in `Broadcaster.resync`:

1. At most once per 500 ms per phone (`KEYQ_MIN_INTERVAL`). The app delays a
   request rather than dropping it, and asks again every 1.5 s while it is still
   waiting.
2. That phone's queued video is dropped and it is marked not started.
3. If the SPS, PPS and the GOP since the last keyframe are cached, they are
   replayed straight away, and the phone is back on the current picture.
4. Otherwise the daemon asks the backend for a keyframe. A backend may expose
   an optional `request_keyframe() -> bool` (True when a keyframe will come
   soon). The daemon looks it up with `getattr`, calls it on its own thread
   (`KeyframeSource`), at most every 0.25 s. No current backend has it:
   wf-recorder and `gst-launch` cannot be told to send a keyframe at runtime.
5. Otherwise the phone waits for the next natural keyframe. Every second
   (`RESYNC_RETRY`) the parser thread checks again whether the cache can start
   it, which matters on a still desktop.
6. If a phone has waited 4 s (`RESYNC_RESTART_AFTER`) with no cache and no
   keyframe on the way, the daemon restarts the encoder, at most once per 15 s.
   Every encoder starts with SPS, PPS and a keyframe. The 4 s clock starts when
   the phone begins waiting, and repeated `KEYQ`s do not reset it.

With a long `--gop` at a high bitrate, the GOP is often over the 2 MB cache cap.
New and resyncing phones then wait for steps 4 to 6. Raising the cap would bring
back multi-second replays on slow links.

Before starting the daemon, the TUI (and `start-tandem.sh`) runs
`core.choose_port`. It test-binds the port exactly as the daemon will listen:
dual-stack `[::]:<port>` (IPv4 and IPv6), or `0.0.0.0:<port>` where IPv6 is
off. A program holding the port on only `127.0.0.1` or only `[::1]` therefore
counts as taking it.
If another program holds the port, Tandem takes the next free port, up to 50
above the original. Other programs are never killed. The program is named with
`ss -ltnp`. A Flask server on `127.0.0.1:5000`, for example, still blocks
`0.0.0.0:5000`. Tandem saves the new port to the config, shows the reason in the
live view, and the QR carries the new port. A leftover `tandemd.py` on the port
does not count as another program. The new daemon stops it (`kill_stale`). The
same holds when `ss` cannot name the port's owner but one of this user's
`tandemd.py` processes is running: the port is kept, because moving it would
make every paired phone rescan for nothing.

The daemon listens dual-stack on `[::]:5000` with `IPV6_V6ONLY` off, so phones
reach it over IPv4 and IPv6 (for example a hostname that resolves to IPv6
first). Where IPv6 is turned off it falls back to `0.0.0.0`. It logs
*"listening on every interface (IPv4 + IPv6) port 5000"*, and IPv4-mapped peers
are logged as plain IPv4. `--host` still picks one address. In the accept loop,
errors that only affect one connection are skipped. Running out of file
descriptors or memory (`EMFILE`, `ENFILE`, `ENOBUFS`, `ENOMEM`) pauses 0.1 s and
carries on. Any other error logs `error: listener failed` and stops the daemon.

A phone reaches it in three ways:

- USB tethering, without USB debugging. The phone becomes a USB network device
  and gives the PC an address on `192.168.42.0/24`.
- Wi-Fi, using the PC's LAN address.
- `adb reverse tcp:5000 tcp:5000`, where the phone connects to `127.0.0.1`.

The pairing QR encodes
`tandem://c?h=<ip1>,<ip2>&p=5000&n=<hostname>&k=<pairing key>&f=<cert fingerprint>&v=2`.
It lists every plausible address, with the USB tether first or the LAN first,
depending on the *Preferred link* setting. Docker, VPN and VM bridges are left
out. The TUI draws the code with Unicode half-blocks. `qr.py` does this without
any dependencies, and `zbarimg` verifies the output. The app connects to all the
listed hosts in parallel and keeps the first one that answers, so the same QR
works over USB and Wi-Fi. `k` is the pairing key the phone must prove it knows
before it can connect at all. `f` pins the computer's TLS certificate
([`SECURITY.md`](./SECURITY.md)). Under the QR, the TUI also shows the key as a
typable *pairing code*, `xxxxx-xxxxx-xxxxx-xxxxx`, for entering an address by
hand.

> TCP has one weakness here. On lossy Wi-Fi, a lost packet stalls everything
> behind it (head-of-line blocking). The roadmap moves Wi-Fi to RTP over UDP with
> forward error correction.

### Stage 4: decode and display (Android)

The phone feeds whole access units to the hardware `MediaCodec` decoder and
renders every frame as it arrives. Reading the socket, decoding and sending run
on separate threads, so a busy decoder never stops the phone from reading, and
a stale backlog is skipped instead of decoded late. When the decoder needs a
keyframe, the phone sends `KEYQ` (stage 3). See [`ANDROID.md`](./ANDROID.md).

### Stage 5: touch input (phone to PC)

Touching the phone's screen acts on the virtual monitor, the way it would on a
touchscreen monitor, and the computer's mouse pointer does not move. This is the
central design decision. The phone's touches are injected as a virtual
touchscreen, never as a mouse. A touchscreen delivers each touch to the window
under the finger (Wayland `wl_touch`), so the laptop's touchpad keeps working
while someone taps the phone.

```
phone: MotionEvent ─▶ TouchForwarder ─▶ TouchQueue ─▶ TUCH (fraction of the video picture)
   ─TLS─▶ client I/O thread ─▶ PhoneInput (touch on?) ─▶ InputWorker queue
   ─▶ TouchRouter (slots) ─▶ backend touch surface ─▶ compositor ─▶ window under the finger
```

**Touches never block video.** Injection runs on one dedicated thread,
`InputWorker`, fed by a queue of at most 256 events. A new move replaces the
same finger's queued move, while down, up and cancel keep their order. If the
queue still fills, it is cleared and the affected phones get a cancel, so no
finger is left down. Injection errors are logged (the first three) and never
reach the phone's I/O thread. When a phone disconnects, its fingers are lifted
through the same queue, so the lift lands after that phone's queued touches. A
phone is only authorized for touch if it is still connected when its admission
is processed. On the phone, `TouchQueue` coalesces the same way: an unsent move
is replaced by that finger's newer one, and all pointers of one `MotionEvent`
are sent together.

**Coordinates.** The phone sends positions as fractions of the video picture,
scaled to 0..65535. Its video view is letterboxed to the stream's exact aspect
ratio, so a fraction of the view is the same fraction of the virtual monitor.
Neither side needs to know the other's resolution, and the black bars never
produce touches. Each backend converts the fractions into its own coordinate
space.

**Fingers.** Android pointer IDs travel as the `finger` byte. `TouchRouter`
(`phone_input.py`) gives every (phone, finger) pair its own slot on the shared
device, with 10 slots in total. Two phones touching at once never collide. When
a phone disconnects mid-gesture, its fingers are lifted, so no touch gets stuck.
All of its slots are freed first and then lifted one by one, so one failing lift
cannot leak the others.
`action` is 0 for down, 1 for move, 2 for up, and 3 for cancel, which lifts every
finger of that phone.

**Pairing decides who may touch.** Only phones that passed the pairing handshake
are connected at all ([`SECURITY.md`](./SECURITY.md)). So every phone the daemon
streams to has scanned this computer's QR code or typed its pairing code. Touch
then depends only on touch input being turned on and a virtual touchscreen being
available. The daemon tells each admitted phone with `INPT`. Choosing
**Configure → Paired phones → forget all** creates a new key. The daemon then
disconnects every phone at once, and each one must scan the new QR.

**Hyprland: a uinput touchscreen** (`uinput_touch.py`). The daemon creates a
kernel input device called `tandem-touch` through `/dev/uinput`. It uses pure
Python ioctls and no dependencies. The device speaks the multi-touch protocol
type B, which real touchscreens use. It sets `ABS_MT_SLOT`,
`ABS_MT_TRACKING_ID`, `ABS_MT_POSITION_X/Y`, plus `BTN_TOUCH` and `ABS_X/Y`,
which libinput requires, and marks itself `INPUT_PROP_DIRECT`. Its axes span the
virtual monitor's pixels, and Hyprland is told to map it onto that monitor only:

```
hyprctl eval 'hl.device({ name = "tandem-touch", output = "EXT-1" })'
```

The legacy fallback is `hyprctl keyword device[tandem-touch]:output EXT-1`. The
mapping is applied again after every live side or scale change, because a config
reload drops it. The device lives as long as the daemon, and windows only see
touch support while it exists.

`/dev/uinput` is root-only on most distributions, so this needs one-time setup:
a udev rule that gives access to the user logged in at the seat (`uaccess`, the
same mechanism that gives you your sound card), and the `uinput` module loaded
at boot:

```
/etc/udev/rules.d/70-tandem-uinput.rules
  KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"
/etc/modules-load.d/tandem-uinput.conf
  uinput
```

The installer offers this step. When it is missing, the TUI's live view shows
*Touch: needs one-time setup, press t*, and `core.touch_setup_check` detects it.

**GNOME: Mutter RemoteDesktop** (`backends/gnome.py`). This path needs neither
root nor uinput. The backend first creates an `org.gnome.Mutter.RemoteDesktop`
session. It then links the ScreenCast session to it with
`CreateSession({"remote-desktop-session-id": …})`, and starts the pair through
the RemoteDesktop session. Touches go to `NotifyTouchDown/Motion(stream, slot, x,
y)` and `NotifyTouchUp(slot)`. Here `stream` is the virtual monitor's stream, and
`x, y` are in that monitor's pixel space. Mutter ties the session to the D-Bus
connection that created it, so it lives exactly as long as the daemon. If the
RemoteDesktop session cannot be created, the backend falls back to a plain
ScreenCast session. Video still works, and phones are told why touch is
unavailable.

The D-Bus calls run on their own thread with its own GLib main loop
(`_DBusWorker`, one per touch surface), so a slow Mutter reply cannot stall
anything. Calls are asynchronous, use `NO_AUTO_START` and a 1 s timeout, and
warn once on error. Down and up are always sent, in order, and a finger's
held-back move goes first so its last position is not lost. While four calls
(`MAX_IN_FLIGHT`) are waiting for replies, moves are coalesced per finger.
Closing the surface lifts every finger and waits up to 1 s for the queue to
empty.

**What it can and cannot do.** Taps, drags, scrolling, long-press menus and
pinch-zoom work in apps that support touch on Wayland. That includes GTK, Qt,
Chromium and Electron, and Firefox. The apps interpret the gestures, exactly as
they would with a real touchscreen. Tapping a window focuses it, so keyboard
input follows your taps. There is no hover, and an app that ignores touch events
ignores the phone too.

**Verified on 2026-10-16.** On Hyprland 0.56.2, with the daemon: taps land at the
right logical position on EXT-1, two fingers get separate slots, disconnecting
lifts a finger that was left down, and the cursor does not move. On GNOME, in
containers on Mutter 46.2 (Ubuntu 24.04) and 48.8 (Fedora 42), observed with
`wev`. A wrong pairing key gets `touch: false` and injects nothing. On a real
phone, over USB with `adb reverse` and Hyprland with NVENC at 2560×1440 and 120
Hz: taps, scrolling and pinch all work smoothly. The test recorded 138 touches,
and every finger-down had a matching finger-up.

---

## 5. End-to-end latency budget

Glass to glass over USB, with Hyprland and VA-API:

| Stage | Typical |
| --- | --- |
| Capture (DMA-BUF, zero copy) | 1 to 2 ms |
| VA-API encode (no B-frames) | 3 to 8 ms |
| NAL parse, enqueue and TCP send | Under 1 ms |
| USB tethering transport | 2 to 5 ms |
| MediaCodec decode (low latency) | 5 to 10 ms |
| Surface render (vsync) | 8 to 16 ms |
| **Total** | **About 25 to 45 ms** |

On GNOME with a software encoder, add about 5 to 15 ms for `videoconvert` and
x264. Frame threading (`threads=2`) costs about one frame. A VA-API GStreamer
encoder brings the total back in line with Hyprland.

Over Wi-Fi the transport row becomes about 5 to 20 ms, so about 30 to 60 ms in
total. It is lowest on the phone's own hotspot, a direct link with no router
and no other devices sharing the air. These Wi-Fi figures are an estimate and
have not been measured yet.

---

## 6. Process and lifecycle

```
tandem (python.sh: /usr/bin/python3) ──▶ tui.py
tui.py ──spawns──▶ tandemd.py --keep-output --banner-file … --parent-pid <tui pid>
                       │                           (same interpreter, its own session)
                       ├─ backend.setup()          create the virtual monitor
                       ├─ backend.start_capture()  wf-recorder | gst-launch-1.0  → pipe
                       ├─ supervise_capture()      parse the pipe; restart the encoder if it exits
                       ├─ start_touch()            virtual touchscreen (uinput / RemoteDesktop) + InputWorker
                       ├─ accept thread            TCP [::]:5000, one I/O thread per phone
                       ├─ keyframe thread          request_keyframe() / encoder restart for stuck phones
                       ├─ control thread           SIGUSR1 → re-read banner → apply side/scale live
                       └─ on exit                  stop capture, close clients, close the input worker,
                                                   remove the touch device, remove monitor
                                                   (unless --keep-output on Hyprland)
```

**Signals.**

| Signal | Effect |
| --- | --- |
| SIGTERM, SIGINT, SIGHUP | Stop cleanly, with teardown. A terminal hangup is a stop, not a reload |
| SIGUSR1 | Live reload: re-read `--banner-file` and apply side and scale |
| SIGUSR2 | Sent by the kernel when the `--parent-pid` process dies (`PR_SET_PDEATHSIG`). Stops the daemon and removes the virtual monitor even with `--keep-output` (*"the launcher exited; removing the virtual monitor"*) |

Signal handlers are installed first, so a stop requested during setup is
honoured, and everything after `backend.setup()` runs in `try`/`finally`.
`--parent-pid PID` is optional. Where `prctl` is unavailable the daemon logs a
note and carries on. It also checks for a parent that died before the `prctl`
call. `start-tandem.sh` does not pass it: its daemon runs in the terminal's
foreground process group, so closing the terminal sends SIGHUP and it stops
cleanly. The daemon's `log()` never raises. If stderr is gone (a closed
terminal or a dead TUI), it appends to `$XDG_RUNTIME_DIR/tandemd.log`, or the
temp directory.

**Capture supervision.** Encoders die mid-session for ordinary reasons:
PipeWire renegotiates after a mode or modifier change, a driver hiccups, a GPU
resets. When the encoder's pipe reaches EOF, `supervise_capture` calls
`stop_capture()`, resets the stream state (`stream_restarted`: caches cleared,
every phone resyncs on the new stream's first keyframe), then `start_capture()`
again with a backoff of 0.2, 0.5, 1 and 2 s. A run of 10 s or more resets the
backoff. After 6 quick failures in a row it logs `error: the encoder keeps
failing; stopping` and the daemon exits with code 1. The virtual monitor, the
listener and connected phones are kept throughout: a phone stays connected and
resumes on the new encoder's first keyframe. The same path runs when a phone
has waited 4 s for a keyframe (stage 3).

- **The system Python is always used.** The launchers (`tandem`,
  `start-tandem.sh` and `install.sh`) source `python.sh`, which picks
  `/usr/bin/python3` instead of the first `python3` on `PATH`. Distribution
  packages such as PyGObject exist only for the system interpreter. A conda
  Python also bundles its own ncurses, which cannot find the system terminfo
  database. That breaks the GNOME dependency check and crashes curses at
  startup. The TUI starts the daemon with `sys.executable`, so both use the same
  interpreter. `tui.setup_terminal()` still recovers when the TUI is run directly
  under such a Python.
- The TUI owns the daemon, so the stream survives leaving the live view. `q`
  keeps streaming. `d` disconnects and removes the monitor. The UI shows
  "Disconnecting…" while this runs, so the pause is not mistaken for a hang.
- The TUI starts the daemon in its own session (`start_new_session=True`), so a
  Ctrl+C in the terminal reaches only the TUI, which then stops the daemon in
  order. Quit, Ctrl+C, a terminal hangup, SIGTERM or a crash of the TUI all lead
  to the same cleanup, which removes the virtual monitor. The TUI tracks this
  with `Stream.needs_teardown`, which is true from `start()` until the monitor
  has been removed, so cleanup also runs when the daemon has already died and
  left the monitor behind (`--keep-output`). If the dead daemon's output had a
  different name from the new config's, that old monitor is removed before the
  next start. A TUI killed outright (SIGKILL, out of memory) is covered by
  `--parent-pid`.
- While a stream is running, the TUI re-applies `adb reverse` whatever screen is
  showing: whenever the device list changes, and every 30 s anyway, because an
  adb server restart drops the mappings without changing the list.
- Shutdown never blocks inside the signal handler. SIGTERM only sets a stop flag.
  The parser loop notices within 0.5 s, and the `finally` block stops capture,
  closes clients and removes the touch device. The TUI waits up to 8 s before it
  sends SIGKILL. The monitor is removed only after the daemon has exited.
- Side and scale can change without a restart. The TUI rewrites
  `~/.config/tandem/daemon-banner.json` atomically and sends `SIGUSR1`. The
  daemon's control thread moves and rescales the monitor through the backend,
  then pushes a fresh `ASX1` frame to connected phones. Changes to resolution,
  fps, quality, GOP, port or backend restart the daemon. On Hyprland the monitor
  survives (`--keep-output`). On GNOME it is recreated.
- A stale daemon from an earlier run is found and killed by inspecting `/proc`
  argv. The daemon never uses `pkill -f`, which would also match the parent shell.

---

## 7. Failure modes and the rules that prevent them

Each rule below answers a failure seen while building Tandem. The code comments
next to each rule explain the details.

| Failure | Cause | Rule in the design |
| --- | --- | --- |
| App loops between connecting and disconnecting | A finished session cleared the current session. The reconnect then created a second decoder on the same Surface, which failed, and the cycle repeated | Every callback carries its session, and stale callbacks are ignored. The Surface is released synchronously in `surfaceDestroyed` |
| Drops under load or on weak Wi-Fi | A resync cut off a half-sent frame, so the phone read garbage lengths | Queues hold whole frames. Only frames that have not been sent yet are dropped |
| Black screen when joining an idle desktop | No damage, so no frames and no keyframe for the new client | GOP cache replayed on connect. `keepalive-time` on GNOME |
| Last frame of a motion missing until the next change | The tail NAL was held until the next start code | Quiet-pipe tail flush |
| Silent hangs | No liveness check at either end | `PING` heartbeats both ways, read timeouts on both ends, TCP keepalive and `TCP_USER_TIMEOUT` |
| Freezes at native phone resolution | Decoder input buffers were sized for a 1080p guess, so large IDRs were dropped | `KEY_MAX_INPUT_SIZE` comes from the real stream resolution in the banner |
| "Reconnecting…" forever with a wrong address | Every failure, even the first, was retried | Automatic retry only after a stream has worked. First-time failures show a specific error |
| Hyprland capture dies at once (`Unsupported format: bgra`) | Encoder on a different GPU from the compositor, such as Hyprland on NVIDIA with VA-API on the iGPU and the NVDEC driver | The encoder follows the compositor's GPU: NVENC on NVIDIA, and VA-API with the right `LIBVA_DRIVER_NAME` elsewhere |
| Hyprland *"invalid scale"* or *"EXT-1 overlaps"* popups | The requested scale gave fractional logical pixels, and the position was computed from it. A restart could also overlap the old daemon's teardown | The nearest valid scale is used. A new daemon waits for the old one to exit |
| Main screen drops frames while streaming on Hyprland | With Hyprland on NVIDIA, wf-recorder's CPU copies ran on the compositor's render thread (87 % busy, against 13 % with capture paused) | Zero-copy capture through xdph and GStreamer NVENC (section 3). wf-recorder is only a fallback |
| Pressing `d` sometimes froze the system | The daemon could be SIGKILLed before it stopped wf-recorder. The capture kept running on EXT-1 while the output was removed under it, sometimes while it still had focus | A non-blocking signal handler, an 8 s grace period, `PR_SET_PDEATHSIG`, and an ordered `remove_output` (stop capture, focus primary, move workspaces, remove) |
| Live view uses about 5 % CPU while idle | The QR code was re-encoded in pure Python every 150 ms | The QR is re-encoded only when its URI changes |
| Resolution, scale or side ignored on Hyprland 0.55 and later | `hyprctl keyword` and `dispatch` do not work with the Lua parser | The `hyprctl eval` Lua API, chosen by a runtime dialect check, with every reply checked |
| The laptop screen reset to defaults or moved after connecting or disconnecting (Hyprland 0.55 and later) | Tandem pinned the user's monitor with a partial rule and set it back to `auto` on disconnect | Tandem never modifies your existing monitors. EXT is placed with `auto-<side>`, which Hyprland lays out last |
| Workspaces 1 to 10 forced onto the laptop | Fixed workspace rules for both screens | One workspace, chosen at runtime above the user's, bound to EXT only and disabled on disconnect |
| EXT jumps to the wrong mode or position after saving `hyprland.conf` | A config reload rebuilds all rules from the files | `EventWatcher` re-applies EXT's rule, workspace and touch mapping after `configreloaded` and monitor hotplug, with echo suppression |
| The phone freezes for a moment when a window is dragged onto it | wf-recorder's `-r` held frames and, after an idle gap, emitted seconds of duplicates with keyframes in one burst | No `-r`. Capture is damage-driven, and `-B` only informs rate control |
| A latency spike every keyframe | Encoders' default 0.6 to 1.5 s rate-control buffers let keyframes grow to several hundred KB | A 2-frame buffer on every encoder, and a GOP of about 1 s |
| The phone stays frozen or garbled after a lost or oversized frame | The phone could only wait for the next natural keyframe | `KEYQ`: the daemon replays the cached GOP, asks for a keyframe, or restarts the encoder after 4 s |
| A phone seconds behind on weak Wi-Fi | The queue was bounded by bytes only | Video queues are bounded by age (0.3 s), and a phone that falls behind gets the GOP replayed |
| The stream stops for good when the encoder exits | Capture ended the daemon | `supervise_capture` restarts the encoder with backoff, keeping the monitor and the phones |
| Ctrl+C or a closed terminal leaves the virtual monitor behind | The daemon and encoder shared the terminal's process group, and cleanup ran only while the daemon was alive | The daemon runs in its own session, `--parent-pid` stops it with the TUI, and `needs_teardown` drives cleanup |
| Touches stall the video | Touch injection ran on the phone's I/O thread, and Mutter's D-Bus replies blocked it | `InputWorker` on Hyprland and `_DBusWorker` on GNOME inject on their own threads |

---

## 8. Planned work

- RTP over UDP with forward error correction for lossy Wi-Fi. TCP stays for USB.
- HEVC decoding in the app. The Hyprland backend can already encode HEVC, but
  the daemon's keyframe handling only parses H.264, so `--codec hevc` logs a
  warning.
- A runtime keyframe request (`request_keyframe()`) for at least one backend,
  for example by running the GStreamer pipeline in-process.
- Address discovery (mDNS or broadcast), so a phone finds the computer after
  its address changes without rescanning the QR.
- A KDE Plasma backend, using KWin virtual outputs through `kwin_wayland` screencast.
- Pen input with pressure and hover, and a keyboard on the phone.
- A latency overlay for tuning.
