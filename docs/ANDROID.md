# Tandem — Android App

The phone is a thin client: it connects, decodes H.264 with the hardware decoder,
puts every frame on screen the moment it's ready, and sends your touches back so
the phone works as a touch monitor. This document covers the
UI, the components, how a frame travels through the app, and how to develop and
debug it. The Linux side is in [`ARCHITECTURE.md`](./ARCHITECTURE.md).

---

## 1. Build setup & packages

### Toolchain

| Item | Value |
| --- | --- |
| Gradle / AGP / Kotlin | 8.11.1 / 8.9.1 / 2.0.21 |
| compileSdk / targetSdk / minSdk | 36 / 36 / 26 (Android 8.0) |
| JDK for CLI builds | **17–21** — use Android Studio's JBR (`/opt/android-studio/jbr`); the system JDK 26 is too new |

### Dependencies (`app/build.gradle.kts`)

| Library | Why |
| --- | --- |
| `androidx.core`, `appcompat`, `activity-ktx` | window insets, back handling, base activity |
| `com.google.android.material:material:1.12.0` | **Material 3**: dynamic color, cards, buttons, outlined text fields, chips |
| `com.google.android.gms:play-services-code-scanner:16.1.0` | **Google code scanner**: Play-services-hosted QR scanner UI. The app needs **no camera permission** — the scanner runs in Play services |

The manifest asks Play services to download the scanner module at install time
(`com.google.mlkit.vision.DEPENDENCIES = barcode_ui`) so the first scan is
instant. Phones without Google Play services can still enter the address
manually, or scan the QR with the stock camera (see §2).

### Platform APIs

`MediaCodec` (hardware H.264 decode → `Surface`), `SurfaceView`, `RenderEffect` +
`RuntimeShader` (AGSL, Android 13+, for the water transition), `WifiManager`
low-latency lock, plain `java.net.Socket`.

### Permissions

`INTERNET`, `ACCESS_NETWORK_STATE`, `ACCESS_WIFI_STATE` + `WAKE_LOCK` (the
low-latency Wi-Fi lock that stops power-save stutter while streaming). No camera,
no location, no storage.

---

## 2. The user experience

```
 Connect screen ── Scan QR / deep link / manual ──▶ "Ready to connect"
        │                                                 │ tap Connect
        │                                                 ▼
        │                         ripples spread from your finger while connecting
        │                                                 │ first frame decoded
        │                                                 ▼
        │                   water reveal: a rippling hole opens and swallows the
        │                   connect screen, uncovering the live desktop
        ▼                                                 │
  error shown in the card ◀── first connect fails          ▼
                                                   LIVE (immersive, full bleed)
                                     link drops ──▶ "Reconnecting…" (auto, backoff)
```

**Connect screen** (Material 3, landscape two-pane): app identity on the left; on
the right a card that is either *Connect to your computer* (big **Scan QR code**
button) or *Ready to connect* (the computer's name and addresses, **Connect**,
*Scan again*, *Enter address manually*). Colors come from the wallpaper on
Android 12+ (Material You); older phones get a Google-blue Material 3 palette.
Light/dark follows the system.

**Getting the address, three ways:**

1. **Scan QR code** in the app (Google code scanner).
2. **Stock camera app**: the QR is a `tandem://` link and the app registers that
   scheme, so pointing the camera at the terminal opens the app pre-filled.
3. **Enter address manually**: `192.168.42.129` or `192.168.42.129:5000`.

The QR lists *every* address the computer has (USB tether, Wi-Fi…); the app
tries them all in parallel and keeps the first that answers.

**The launch splash** (`AsciiSplashView`): the same as the website's. On a fresh
launch the word "Tandem" appears in ASCII characters on black; drops spread from
the centre (or wherever you tap) and bend it like water, then a hole with a
bright rim opens onto the connect screen. About 1.5 s in all, and skipped when
the system's animations are off. The launcher icon is an ASCII "T" on black
(`drawable-nodpi/ic_launcher_foreground.png`, also the monochrome themed icon,
and the logo tile at the top of the connect screen);
the full wordmark is in `logo/` at the repo root.

**The water transition** (`RippleRevealLayout`): on Android 13+ an AGSL shader
displaces the connect screen's pixels along travelling waves (refraction) with
soft highlights on the crests, three staggered "drops" repeating while it
connects. When the first frame is decoded, a hole with a wobbling, rippling rim
opens at the same point and grows over ~0.9 s (Material emphasized-decelerate),
revealing the desktop underneath (the video `SurfaceView` is *behind* the
connect screen the whole time). Android 8–12 get the same choreography with
canvas-drawn rings and a clipped circular reveal.

**Live:** full-bleed, immersive. Chrome is a Google-style status pill
(*Live · 2400 × 1080*) that fades after ~3 s, and a slim handle on the right edge
that opens a **side sheet** with the stream's details — computer name, link
(USB tethering / Wi-Fi / adb) and address, resolution, refresh rate, scale,
position, quality, desktop (Hyprland / GNOME), **touch** (on / off and why) —
and **Disconnect**. Back closes the sheet; while live it *opens* the sheet instead of
disconnecting (an accidental edge swipe never drops the stream — Disconnect is a
deliberate tap in the sheet). Back while connecting cancels.

**Touch:** when the computer has touch input on, every touch
on the video goes to the computer (§4.6): tap, drag, scroll, long-press, pinch —
handled by the desktop apps like a real touchscreen, while the computer's mouse
pointer stays where it is. Touches on the black letterbox bars, the edge handle
and the open sheet stay on the phone. With touch off or unavailable on the
computer, a tap on the video peeks the status pill instead, and the sheet's
*Touch* row says why.

**Security:** the app connects only with the computer's pairing key, and from
a QR it also pins the computer's TLS certificate ([`SECURITY.md`](./SECURITY.md)).
Entering an address by hand asks for the **pairing code** shown under the QR in
tandem. A link opened from outside the app (camera, browser, another app) asks
*"Pair with <name>?"* before it replaces the saved computer. Pairing failures
are final (no retry loop) and say why: *wrong pairing code*, *this isn't the
computer you paired with*, *update tandem on the computer*.

**Errors are specific:** *"titan-arch is reachable, but Tandem isn't
running"* (connection refused) vs *"Couldn't reach titan-arch. Turn on USB
tethering, or join the same Wi-Fi"* (timeout) vs *"Connected, but no video
arrived"* (10 s watchdog).

---

## 3. Source layout

```
app/src/main/java/io/github/saharshbaiju/tandem/
  MainActivity.kt          UI state machine, scanner, transitions, side sheet, lifecycle
  StreamSession.kt         connect (parallel, all hosts) → TLS + pairing → framed
                           reader → MediaCodec; the touch sender thread
  SecureChannel.kt         TLS with certificate pinning, pairing-key proofs (HMAC),
                           pairing-code format (mirrors linux/secure_link.py)
  FrameReader.kt           the daemon's framed protocol: NALU / ASX1 / PING / INPT
  FrameWriter.kt           phone → computer frames: HELO / AUTH / TUCH
  TouchForwarder.kt        MotionEvent → TUCH (fractions of the video picture)
  ConnectTarget.kt         tandem:// QR / deep-link parsing, manual entry, persistence
  RippleRevealLayout.kt    water ripple + reveal (AGSL on 13+, canvas fallback)
  AsciiSplashView.kt       launch splash: ASCII wordmark, ripples, reveal (port of the website's)
  AspectRatioSurfaceView.kt  letterboxes the video to the stream's aspect ratio
app/src/main/res/
  layout/activity_main.xml connect screen, status pill, edge handle, side sheet, splash
  values/themes.xml        Material 3 DynamicColors theme + fallback palette overlay
  drawable/ic_*.xml        Material icons
  drawable-nodpi/ic_launcher_foreground.png  ASCII "T" (432 px layer, safe zone 264 px)
  mipmap-anydpi-v26/ic_launcher.xml  adaptive icon: that "T" on @color/brand (black)
```

---

## 4. The components

### 4.1 `MainActivity` — state machine

`IDLE → CONNECTING → LIVE ⇄ RECONNECTING → IDLE`

- **Session identity.** Every `StreamSession.Listener` callback receives the
  session that fired it, and the activity ignores anything that isn't the current
  one. This fixed the connect/disconnect loop: an old session finishing *after* a
  new one started used to null the new one, a third decoder was created on the
  same Surface, failed, and so on forever.
- **Surface lifecycle.** `surfaceDestroyed` stops the session and *joins* it, so
  the decoder has released the Surface before the method returns; the field is
  nulled first so that session's own `onStopped` is recognised as stale.
- **Reconnect policy.** Auto-reconnect (backoff 0.3 → 0.8 → 1.5 → 2.5 → 3 s, then
  every 3 s) only after a stream has actually produced a frame. A first connection
  that fails settles the ripple and shows a specific error instead of looping.
- **Watchdog.** TCP connected but no frame within 10 s → stop and explain.
- `uiMode` is in `configChanges` so a light/dark switch can't kill a live stream;
  the activity re-themes itself only while idle.

### 4.2 `StreamSession` — the decode engine

Runs on its own thread:

1. **Connect** to every host in parallel (3 s timeout each); the first to accept
   wins, the others are closed. A *refused* connection is reported in preference
   to a timeout (it means "computer reachable, Tandem not running").
2. **Secure it** (`SecureChannel`, 5 s timeout): TLS, refusing any certificate
   but the one pinned by the QR; then `HELO` → `CHAL` → `AUTH` → `OKAY`, checking
   the computer's proof before anything else. `DENY`, a wrong proof or a
   certificate mismatch ends the session as *final* (`onStopped(…, final =
   true)`): the activity shows the reason and doesn't reconnect.
3. **Read frames** with a 4 s socket timeout (the daemon PINGs every second).
4. `ASX1` → `ServerConfig` (shown in the side sheet; its width/height size the
   decoder's input buffers). `PING` → nothing (it just resets the timeout).
   `INPT {"touch": …, "reason": …}` → `onInputState` (touch on/off).
5. `NALU` → access-unit assembly → decoder.

**Writes never block the UI.** `sendTouch()` is called from the UI thread; it
packs the 6-byte `TUCH` body and hands it to a single-thread executor
(`stream-sender`) that writes through `FrameWriter`. One thread keeps frames in
order and whole; a write error is ignored there because the reader notices the
dead connection and ends the session.

**Access units, not NALs.** Hardware decoders (e.g. Qualcomm C2) reject partial
input; they need one complete frame per input buffer. NALs are accumulated and
flushed when a VCL slice (types 1–5) completes the frame. The daemon guarantees
single-slice frames, so this adds no latency.

**Decoder configuration** happens at the first IDR once SPS + PPS are known:
`csd-0`/`csd-1` = SPS/PPS, `KEY_LOW_LATENCY` (Android 11+), `KEY_PRIORITY` =
realtime, and `KEY_MAX_INPUT_SIZE` = width × height of the real stream — before
this, input buffers were sized for a 1080p guess and big keyframes at native phone
resolution were silently dropped. A **different SPS** (resolution change after a
daemon restart) releases the decoder and rebuilds it at the next keyframe.

**Output**: every output buffer is released with `render = true` immediately; the
first one fires `onFirstFrame` (which triggers the water reveal).
`INFO_OUTPUT_FORMAT_CHANGED` reports the *cropped* size (1080, not the encoder's
padded 1088) so the letterbox is exact.

### 4.3 `FrameReader`

`4-byte type + 4-byte big-endian length + body`, read fully; lengths over 32 MB
are treated as a corrupt stream. Types: `NALU`, `ASX1`, `PING`, `INPT` (and the
handshake's `CHAL` / `OKAY` / `DENY`, read by `SecureChannel`); unknown types
are skipped for forward compatibility. `FrameWriter` is the mirror image for
`HELO`, `AUTH` and `TUCH`: each frame is assembled and written in one synchronized
call, so two frames can never interleave on the socket.

### 4.4 `ConnectTarget`

Parses `tandem://c?h=<ip>[,<ip>…]&p=<port>&n=<name>&k=<pairing key>&f=<cert
fingerprint>&v=2`, validates hosts, key and fingerprint, and persists the last
target in `SharedPreferences` (excluded from backups). A `v=1` QR (versions before 0.5)
is reported as outdated. Manual entry takes `host`, `host:port` or a separate
port field, **plus the pairing code**; it keeps the saved name and pinned
certificate when the code is the saved computer's (you only corrected the
address). A target saved by an older version (no valid key) must be paired
again.

### 4.5 `RippleRevealLayout`

A `FrameLayout` that drives a `TimeAnimator`. On Android 13+ it updates the AGSL
uniforms (`origin, time, amp, reveal, edge, density`) and re-applies
`RenderEffect.createRuntimeShaderEffect` every frame; transparent output pixels
expose the video `SurfaceView` beneath. `startRipple(x, y)`, `reveal(onDone)`,
`settle()` (waves calm down after a failure), `reset()`.

### 4.5a `AsciiSplashView`

The top layer of `activity_main.xml`, `gone` until `MainActivity.onCreate` calls
`run {}` (only when `savedInstanceState == null`, so a light/dark re-theme
doesn't replay it). At size change it lays out a monospace character grid
(9 dp text on phones, 13 dp on tablets) and paints "Tandem" into a bitmap of
one pixel per cell (`sans-serif-black`, widened 1.15×). Each frame, every cell
samples that bitmap from a point pushed along the radius by three staggered
wave trains (the same `wave()` as `RippleRevealLayout`, in dp), maps brightness
onto the ramp ` .:-=+*#%@`, and draws one `drawText` per row. The black
background is drawn as a rect minus the growing circle (even-odd path), so the
connect screen shows through the hole. It eats touches until it hides itself.

### 4.6 Touch input (`TouchForwarder` + `MainActivity.forwardTouch`)

The video `SurfaceView` has a touch listener. While the state is **LIVE**, the
computer has answered `INPT touch: true`, and the side sheet is closed, it
consumes every event and passes it to `TouchForwarder`; otherwise it returns
`false`, so the normal click handling (peek the pill) runs.

`TouchForwarder` maps `MotionEvent`s to `TUCH` frames:

| MotionEvent | `TUCH` action | sent for |
| --- | --- | --- |
| `ACTION_DOWN`, `ACTION_POINTER_DOWN` | 0 down | the pointer at `actionIndex` |
| `ACTION_MOVE` | 1 move | every pointer |
| `ACTION_UP`, `ACTION_POINTER_UP` | 2 up | the pointer at `actionIndex` |
| `ACTION_CANCEL` | 3 cancel | all fingers (the computer lifts them) |

`finger` is the Android pointer id. `x`/`y` are the position divided by the
view's size, scaled to 0..65535. Because `AspectRatioSurfaceView` sizes the view
to the stream's exact aspect ratio, those fractions are fractions of the
computer's virtual monitor; touches on the letterbox bars never reach the view.
Gesture meaning (long-press, scroll, pinch) is left to the desktop apps.

Touch state resets to *off* whenever a session starts or stops, and turns on
only from the current session's `INPT`, so a stale answer can't enable it.

---

## 5. Why each choice keeps latency low

| Choice | Effect |
| --- | --- |
| Hardware `MediaCodec` → `Surface` | no copies to app memory, decode on the video block |
| `KEY_LOW_LATENCY`, no B-frames | decoder skips its reorder buffer |
| render on arrival (`releaseOutputBuffer(i, true)`) | no frame pacing / jitter buffer |
| whole access units per buffer | decoder never waits for the rest of a frame |
| GOP replay on connect (daemon) | first picture within milliseconds |
| low-latency Wi-Fi lock | no power-save wake-up delays between packets |

---

## 6. How a frame travels through the app

```
socket bytes ─▶ FrameReader.next() ─▶ "NALU" body (00 00 00 01 41 …)
   ─▶ handleNal: append to access unit; VCL slice? ─▶ feedAndDrain(AU)
   ─▶ dequeueInputBuffer → put → queueInputBuffer
   ─▶ drain: dequeueOutputBuffer → releaseOutputBuffer(render = true)
   ─▶ SurfaceFlinger composes it at the next vsync ─▶ your eyes
```

---

## 7. Developer install & debugging

End users need neither `adb` nor developer mode. This section needs **USB
debugging** (or wireless debugging).

### 7.1 Build + install

```bash
cd android
JAVA_HOME=/opt/android-studio/jbr ./gradlew installDebug
# or
JAVA_HOME=/opt/android-studio/jbr ./gradlew :app:assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

### 7.2 Drive it from the command line

```bash
# open with a computer pre-filled (what the QR does; the app asks to confirm).
# The full link, with k= and f=, is printed by testsrc.py / start-tandem.sh.
adb shell am start -a android.intent.action.VIEW -d "'tandem://c?h=192.168.42.129&p=5000&n=archbox&k=<key>&f=<fp>&v=2'"

# debug builds only: launch and connect to the saved computer immediately
adb shell am start -n io.github.saharshbaiju.tandem.debug/.MainActivity --ez autoconnect true
```

> Some phones (the original dev device included) block `adb shell input`
> injection; the two intents above reach every state without taps.

### 7.3 Test without touching your desktop

`linux/testsrc.py` streams a test pattern through the real daemon code (protocol,
heartbeats, GOP replay) without creating a monitor:

```bash
python3 linux/testsrc.py --width 2400 --height 1080 --fps 60   # port 5000
python3 linux/testsrc.py --static     # 2 s of video, then silence: tests GOP replay
python3 linux/testsrc.py --log-touch  # accept touches from paired phones, print them
```

Phones pair with it exactly as with the real daemon (it prints the QR link and
the pairing code). With `--log-touch`, every touch is printed as a fraction of
the picture (`touch down  slot 0  x=0.500 y=0.500`), so
the touch path can be tested with `adb shell input tap/swipe` on the emulator.

The **Android emulator** reaches the PC at `10.0.2.2`, so
the printed link with `h=10.0.2.2` connects an emulator to it. A Google Play system
image includes Play services, so even the QR scanner works there.

### 7.4 Logs

```bash
adb logcat --pid=$(adb shell pidof io.github.saharshbaiju.tandem)
adb logcat -s StreamSession          # connect / decoder / session-end reasons
```

### 7.5 Wireless debugging (no cable)

Android 11+: Settings → Developer options → **Wireless debugging** → *Pair device
with pairing code*, then

```bash
adb pair <phone-ip>:<pair-port>        # 6-digit code from the phone
adb connect <phone-ip>:<connect-port>  # the port on the main Wireless-debugging screen
```

Android 10 or older: `adb -d tcpip 5555`, unplug, `adb connect <phone-ip>:5555`.
Wireless adb only carries dev commands; the video still goes straight to the PC.

---

## 8. Limitations & next steps

- **TCP only** — great over USB; lossy Wi-Fi can briefly freeze (head-of-line
  blocking). Planned: RTP/UDP + FEC.
- **Touch only** — no pen pressure/hover, no on-screen keyboard to type on the
  computer yet; there is no hover, and desktop apps that ignore touch events
  ignore the phone too.
- **H.264 only** — HEVC would cut bitrate.
- The QR scanner needs Google Play services; without them use the stock camera
  or manual entry.
