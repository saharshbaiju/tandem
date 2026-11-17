# Tandem Android app

The phone is a thin client. It connects, decodes H.264 with the hardware
decoder, puts each frame on screen as soon as it is ready, and sends touches,
key presses and pointer events back, so the phone works as a touch monitor with a
keyboard. This document covers the UI, the
components, how a frame moves through the app, and how to develop and debug it.
The Linux side is described in [`ARCHITECTURE.md`](./ARCHITECTURE.md).

---

## 1. Build setup and packages

### Toolchain

| Item | Value |
| --- | --- |
| Gradle, AGP, Kotlin | 8.11.1, 8.9.1, 2.0.21 |
| compileSdk, targetSdk, minSdk | 36, 36, 26 (Android 8.0) |
| JDK for command-line builds | 17 to 21. Use Android Studio's JBR (`/opt/android-studio/jbr`). The system JDK 26 is too new |

### Dependencies (`app/build.gradle.kts`)

| Library | Purpose |
| --- | --- |
| `androidx.core`, `appcompat`, `activity-ktx` | Window insets, back handling, base activity |
| `com.google.android.material:material:1.12.0` | Material 3 cards, buttons, outlined text fields and chips, themed for OLED black |
| `com.google.android.gms:play-services-code-scanner:16.1.0` | Google's code scanner, hosted by Play services. The app needs no camera permission, because the scanner runs inside Play services |

The manifest asks Play services to download the scanner module at install time
(`com.google.mlkit.vision.DEPENDENCIES = barcode_ui`), so the first scan starts
right away. Phones without Google Play services can still enter the address by
hand, or scan the QR code with the stock camera (see section 2).

### Platform APIs

`MediaCodec` (hardware H.264 decoding into a `Surface`), `SurfaceView`,
`RenderEffect` with `RuntimeShader` (AGSL, Android 13 and later, for the water
transition), the `WifiManager` low-latency lock, and plain `java.net.Socket`.

### Permissions

`INTERNET`, `ACCESS_NETWORK_STATE`, `ACCESS_WIFI_STATE`, and `WAKE_LOCK`. The
wake lock is the low-latency Wi-Fi lock, which stops power-save from causing
stutter while streaming. The app requests no camera, location or storage access.

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

**Connect screen** (Material 3, two panes in landscape). The left pane shows
"Tandem" in ASCII (`AsciiWordmarkView`), the tagline and the credit. The right
pane is a card that shows either *Connect to your computer* with a large **Scan
QR code** button, or *Ready to connect* with the computer's name and addresses,
**Connect**, *Scan again* and *Enter address manually*. In landscape, the card
scrolls when manual entry does not fit on a short screen.

**Theme.** The app always uses OLED black, matching the website's palette. It
has no wallpaper colours and no light mode. Surfaces are pure black, so those
pixels stay off. Text is near-white. Secondary text, outlines and cards use
greys: `#121314` for the connect card, `#1C1D1F` for the saved-computer row, and
`#0B0C0C` for the side sheet. The primary button is white with black text, like
the solid buttons on the website. Errors use the site's red. Every colour is a
named token in `values/colors.xml`, mapped onto the Material roles in
`values/themes.xml`.

**Three ways to get the address:**

1. **Scan QR code** inside the app, using Google's code scanner.
2. **Stock camera app.** The QR code is a `tandem://` link, and the app
   registers that scheme. Pointing the camera at the terminal opens the app with
   the address filled in.
3. **Enter address manually**, such as `192.168.42.129` or `192.168.42.129:5000`.

The QR code lists every address the computer has, including USB tethering and
Wi-Fi. The app tries them all in parallel and keeps the first one that answers,
except that the USB cable wins over Wi-Fi when both answer. If the cable is pulled
mid-stream the app carries on over Wi-Fi, and when it is plugged back in the
stream moves back to the cable by itself within a few seconds.

**Launch splash** (`AsciiSplashView`). It matches the website's splash. "Tandem"
appears in ASCII characters on black. Drops spread from the centre, or from
wherever you tap, and bend the letters like water. Then a hole with a bright rim
opens onto the connect screen. The whole sequence takes about 1.5 s. It is
skipped when the system's animations are turned off. The launcher icon is an
ASCII "T" on black (`drawable-nodpi/ic_launcher_foreground.png`, also used as the
monochrome themed icon). The full wordmark is in `logo/` at the repository root.

**Water transition** (`RippleRevealLayout`). On Android 13 and later, an AGSL
shader displaces the connect screen's pixels along travelling waves, which
refract the image, with soft highlights on the crests. Three staggered drops
repeat while the app connects. When the first frame is decoded, a hole with a
wobbling, rippling rim opens at the same point and grows over about 0.9 s, using
Material's emphasized-decelerate curve. It reveals the desktop underneath. The
video `SurfaceView` sits behind the connect screen the whole time. Android 8
through 12 get the same sequence, drawn with canvas rings and a clipped circular
reveal.

**Live view.** The picture is full bleed and immersive, with no live indicator
over the desktop. The only chrome is a slim handle on the right edge. It fades
after about 3 s, and a tap on the video brings it back. The handle opens a side
sheet with the stream's details: the computer's name, the link (USB tethering,
Wi-Fi or adb) and address, resolution, refresh rate, scale, position, quality,
desktop (Hyprland, GNOME or KDE Plasma), and touch (on, off and why, or
*Pointer*). Under the details are a **Touch / Pointer** switch (a finger icon
and an arrow icon, see *Pointer mode* below), a **rotate** button (see
*Portrait*), a **Keyboard** button (see *Typing*) and **Disconnect**. Back
closes the sheet. While live, Back opens the sheet
instead of disconnecting, so an accidental edge swipe never drops the stream.
Disconnecting is a deliberate tap in the sheet. Back while connecting cancels the
attempt. A status pill appears only while the picture may be frozen: *Connecting…*
while connecting, and *Reconnecting…* when a working link drops.

**Screen size.** Right after pairing, the app sends the computer its window size
in pixels (`SIZE`): the whole screen as held, since the app draws under the system
bars. It sends it again when that changes (a foldable unfolds, split screen). With
*Fit to device* on, the computer gives its virtual monitor that size, so the
picture fills the screen with no black bars. The app needs nothing else for it: a
new size arrives as a new SPS, and the decoder and the letterbox follow it as they
do for any resolution change. See *Fit to device* in
[`ARCHITECTURE.md`](./ARCHITECTURE.md).

**Portrait** (`OrientationWatcher`, `MainActivity.setPortrait`). The app runs
locked to landscape (`userLandscape` in the manifest), and while streaming it
keeps the screen locked to whichever orientation was picked. When the phone is
turned in the hand to the other orientation while live, a prompt appears at
once, drawn upright the way the phone is now held: *Turn the monitor to
portrait too?* (or *back to landscape?*) with a **Turn** button and a bar that
runs down for 5 s, after which the prompt goes away. There is no *don't ask
again*: the prompt only appears on a turn, and turning the phone back and
turning it again asks again. **Turn** switches the app to portrait
(`SCREEN_ORIENTATION_USER_PORTRAIT`) and at the same moment sends the turned
size in a `SIZE` frame marked `"now": true`, so the computer resizes its
virtual monitor to a tall one without waiting for the layout to settle. The
**rotate** button in the side sheet does the same at any time, in either
direction. No prompt is shown when the phone's auto-rotate is locked
(`Settings.System.ACCELEROMETER_ROTATION` is 0), when the side sheet is open,
or when the computer's banner says the monitor does not follow the phone
(`"fit": false`, *Fit to device* off). In that last case the rotate button
shows a toast instead: *To turn the monitor, switch on "Fit to device" in
Tandem on the computer.* A daemon that sends no `fit` field (one older than
0.9.0, where fitting is the default) is treated as fitting. Back on the connect screen the app is
landscape again.

**Typing** (`RemoteKeyboard`, `KeyboardTargetView`, `Keysyms`). The sheet's
**Keyboard** button opens the phone's own on-screen keyboard (Gboard or any
other). What it types goes to whichever window has focus on the computer. The
keyboard types into an invisible view (`KeyboardTargetView`) that holds no
text: every character becomes a key press sent to the computer. A **key bar**
sits at the bottom of the screen, above the keyboard, with **Esc**, **Tab**,
**Ctrl**, **Alt**, **Super**, **Del** and the four arrows, then a
**minimise** button and a **close** button:

- **Ctrl, Alt and Super are latched.** Tap Ctrl, then C, and the computer gets
  Ctrl+C; the latch then lets go by itself. A latched key is drawn white until
  it is used. Tapping a latched Ctrl or Alt again cancels it. Tapping a latched
  Super again presses Super on its own (the app launcher or overview on most
  desktops). Latches also apply to keys from the phone's keyboard.
- **Minimise** hides the phone's keyboard and keeps the key bar; tapped again,
  it brings the keyboard back. **Close** hides both and puts the picture back.
- **Back** first hides the phone's keyboard (the keyboard itself handles that),
  and a second Back closes the key bar.
- **Docked or floating.** With a docked keyboard the picture shrinks to fit the
  space above the key bar (it letterboxes, so the whole desktop stays in view).
  A floating keyboard, such as Gboard's *Floating* mode, takes no height, so the
  key bar stays at the bottom of the screen and the picture keeps nearly the
  whole screen. The activity uses `adjustResize`, and the key bar follows the
  IME insets.
- **A hardware keyboard** plugged into the phone (USB or Bluetooth) types on the
  computer whenever the stream is live and the sheet is closed, without opening
  anything. Back, Home, volume, power and the assistant keys stay the phone's.

The Keyboard button is dimmed, and shows a toast explaining why, when the
computer did not offer typing (`INPT "keys": false`: touch input is off on the
computer, the desktop could not give a keyboard, or the daemon predates 0.9.0).

**Pointer mode** (`PointerGestures`). Many Linux apps only drag with a mouse:
Chrome's tabs, window title bars, text selection. The sheet's **Touch /
Pointer** switch picks how touches act. **Touch** (the finger icon, the
default) sends them as a touchscreen, as described below. **Pointer** (the
arrow icon) makes the phone a mouse whose pointer goes where the finger is:

| Gesture | What the computer gets |
| --- | --- |
| Tap | Left click (two quick taps: a double click) |
| Touch, then move | Left button pressed where the finger first touched, then a drag |
| Touch and hold (500 ms) | Right click |
| Two-finger tap | Right click |
| Two-finger drag | Scroll; the content follows the fingers |

Unlike touch, pointer mode moves the computer's own mouse pointer. The choice
is saved (`input.pointer_mode`) and applies to every later session. When the
computer does not offer it (`INPT "pointer": false`), choosing Pointer shows
*Pointer mode needs Tandem 0.9 or newer on your computer* and the switch stays on
Touch. The sheet's touch row says *Pointer* while it is on, with a short hint.

**Touch.** When the computer has touch input on, every touch on the video goes
to the computer (section 4.6). Tap, drag, scroll, long-press and pinch are handled
by the desktop apps, as they would be on a real touchscreen. The computer's mouse
pointer stays where it is. Touches on the black letterbox bars, the edge handle
and the open sheet stay on the phone. When touch is off or unavailable on the
computer, a tap on the video brings back the edge handle instead, and the sheet's
*Touch* row explains why. In pointer mode, touches go to `PointerGestures`
instead (see *Pointer mode* above).

**Security.** The app connects only with the computer's pairing key. A QR code
also pins the computer's TLS certificate ([`SECURITY.md`](./SECURITY.md)).
Entering an address by hand asks for the pairing code shown under the QR code in
Tandem. A link opened from outside the app, such as from the camera, a browser or
another app, asks *"Pair with <name>?"* before it replaces the saved computer.
Pairing failures are final, with no retry loop, and each one says why: *wrong
pairing code*, *this isn't the computer you paired with*, or *update Tandem on the
computer*. Temporary refusals are retried on their own: *already 4 phones
connected to this computer*, and *too many failed pairing attempts from this
phone; try again in a minute*.

**Specific errors.** *"titan-arch is reachable, but Tandem isn't running"*
(connection refused), *"Couldn't reach titan-arch. Turn on USB tethering, or join
the same Wi-Fi"* (timeout), and *"Connected, but no video arrived"* (a 10 s
watchdog).

---

## 3. Source layout

```
app/src/main/java/io/github/saharshbaiju/tandem/
  MainActivity.kt          UI state machine, scanner, transitions, side sheet, lifecycle
  StreamSession.kt         connect (raced on all hosts) → TLS + pairing → socket reader;
                           Feeder (access units, recovery), VideoDecoder (async MediaCodec),
                           Sender (touches, keys, pointer, KEYQ, PING, SIZE), BacklogMeter
  SecureChannel.kt         TLS with certificate pinning, pairing-key proofs (HMAC),
                           pairing-code format (mirrors linux/secure_link.py)
  FrameReader.kt           the daemon's framed protocol: NALU / ASX1 / PING / INPT
  FrameWriter.kt           phone-to-computer frames: HELO / AUTH / TUCH / KEYS / PNTR /
                           KEYQ / PING / SIZE
  TouchForwarder.kt        MotionEvent → TUCH (fractions of the video picture); TouchQueue
  PointerGestures.kt       pointer mode: taps, drags, holds, two-finger scrolls → PNTR
                           (PointerEvent)
  RemoteKeyboard.kt        typing: phone keyboard, key bar latches, hardware keyboards;
                           KeyboardTargetView (the invisible view the IME types into)
  Keysyms.kt               X keysyms for characters and Android key codes, the KEYS body;
                           TextToKeys (composing / autocorrect edits → key presses)
  OrientationWatcher.kt    how far the phone is turned from the screen, for the portrait prompt
  ConnectTarget.kt         tandem:// QR and deep-link parsing, manual entry, persistence
  UpdateCheck.kt           "a newer Tandem is out": latest GitHub release, each open
  RippleRevealLayout.kt    water ripple and reveal (AGSL on Android 13+, canvas fallback)
  AsciiSplashView.kt       launch splash: ASCII wordmark, ripples, reveal (a port of the website's)
  AsciiWordmarkView.kt     the connect screen's "Tandem" in ASCII (static)
  AsciiType.kt             shared by both: density ramp, letterforms, word → character cells
  AspectRatioSurfaceView.kt  letterboxes the video to the stream's aspect ratio, clear of the cutout
app/src/main/res/
  layout/activity_main.xml connect screen, (re)connecting pill, edge handle, side sheet,
                           rotate prompt, key bar, splash
  values/themes.xml        Material 3 dark theme, OLED black (palette in values/colors.xml)
  drawable/ic_*.xml        Material icons
  font/tandem_*.ttf        subset of Geist Mono Bold and Archivo Black for the ASCII views (see android/fonts/)
  drawable-nodpi/ic_launcher_foreground.png  ASCII "T" (432 px layer, safe zone 264 px)
  mipmap-anydpi-v26/ic_launcher.xml  adaptive icon: that "T" on @color/brand (black)
```

---

## 4. The components

### 4.1 `MainActivity`: state machine

The activity moves through `IDLE → CONNECTING → LIVE ⇄ RECONNECTING → IDLE`.

- **Session identity.** Every `StreamSession.Listener` callback receives the
  session that fired it. The activity ignores any callback that does not come
  from the current session. Without this check, an old session that finishes
  after a new one has started would clear the new one. A third decoder would then
  be created on the same Surface, fail, and repeat.
- **Surface lifecycle.** `surfaceDestroyed` stops the session and waits for it to
  finish. The decoder therefore releases the Surface before the method returns.
  The field is cleared first, so the session's own `onStopped` is recognised as
  stale.
- **Reconnect policy.** Automatic reconnection starts only after a stream has
  produced at least one frame. It backs off from 0.3 to 0.8, 1.5 and 2.5 s, then 3
  s, and keeps retrying every 3 s. A temporary refusal from the computer waits at
  least its `retry_after`. The pill shows *Reconnecting… (reason)*. An
  "update Tandem" error after the computer has already streamed is retried
  rather than final, because it is usually a transient handshake failure. A
  first connection that fails settles the ripple and shows a specific error,
  rather than looping.
- **Network changes** (`NetworkWatcher`). A `NetworkCallback` follows every
  network, including ones without internet access (USB tethering, a Wi-Fi with
  no uplink), and leaves out VPNs. A network change while reconnecting retries
  after 300 ms, but never sooner than the computer's `retry_after`. Losing the
  network that carries a live stream ends that session at once, instead of
  waiting for the read timeout.
- **Moving back to the cable** (`checkDirectLink`). A stream over Wi-Fi checks
  every 2 s whether one of the computer's addresses is on a *direct* link
  (`NetworkWatcher.isDirect`: USB tethering or the phone's own hotspot, or adb
  on 127.0.0.1). The phone is the router on such a link, so Android never
  reports it as a `Network` and no callback fires when the cable comes back:
  a direct link is an interface that is up, holds the address on its subnet and
  is not the interface of any known network, which works for every tether
  subnet and interface name. If Tandem answers there (a bare TCP connect,
  closed at once, which the daemon treats as a lost connect race: not logged,
  not counted as a failed pairing), the session ends and the reconnect lands on
  the cable. Before this, a stream that fell back to Wi-Fi when the cable was
  pulled stayed on Wi-Fi after it was plugged back in, with Wi-Fi's higher
  latency. If a switch lands back on the same Wi-Fi address (the direct link
  failed TLS, say), the check pauses for 30 s so it can't flap.
- **Wi-Fi lock.** The low-latency Wi-Fi lock is taken when a session is
  launched and released in `surfaceDestroyed`, when connecting fails, and on
  disconnect.
- **Refresh rate** (`applyFrameRate`, `displayModeFor`). While live or
  reconnecting, the window asks for the highest-refresh display mode at the
  current resolution (`preferredDisplayModeId`), preferring a whole multiple of
  the stream's fps. A 60 fps stream on a 144 or 120 Hz panel gets 120 Hz, with no
  judder, and touches are sampled at the panel's full rate. The request is
  cleared when idle. `Surface.setFrameRate` is not used: it invites the system to
  drop a 120 Hz panel to 60 Hz, which doubles the vsync wait.
- **Back gesture** (Android 10 and later). While live with touch on (or pointer
  mode on and offered by the computer) and the sheet closed, a 32 × 200 dp strip in the middle of each side edge is excluded
  from the system back gesture, so horizontal drags near the edge reach the
  computer.
- **Display cutout.** The cutout insets are passed to the video view, which
  slides the picture within any spare letterbox room to stay clear of a
  punch-hole. The picture is never shrunk for it.
- **Watchdog.** If TCP connects but no frame arrives within 10 s, the app stops and
  explains why.
- `uiMode` is listed in `configChanges`, so a system light or dark switch cannot
  restart the activity and end a live stream. The app is always black anyway.
  `orientation` and `screenSize` are listed too, so turning the monitor to
  portrait relays out the views without restarting the activity or the stream.
- **Orientation watching.** `OrientationWatcher` (an `OrientationEventListener`
  plus the display's rotation) runs only while live and while the window has
  focus. It reports the phone's turn from the screen as drawn: 0, 90, 180 or
  270 degrees. A turn counts once the phone is within 30 degrees of it, so a
  phone held near 45 degrees does not flicker between two answers. 90 and 270
  show the portrait prompt; 0 hides it and lets the next turn ask again.
- **Input state.** `setInputState` takes the current session's `INPT`. If
  `keys` is false the key bar closes and any latches are dropped; if `pointer`
  is false a drag in progress is let go. Leaving LIVE closes the keyboard, and
  reaching IDLE turns the app back to landscape.

### 4.2 `StreamSession`: the decode engine

A session runs on four threads, so that no stage waits on another:

| Thread | Priority | Job |
| --- | --- | --- |
| Socket reader (`StreamSession.run`) | `DISPLAY` | Connects, runs the handshake, then only reads frames. It never waits on the decoder |
| `Feeder` (`stream-decode`) | `URGENT_DISPLAY` | Owns the codec. Builds access units, feeds the decoder, recovers locally |
| `VideoDecoder` callbacks (`stream-decoder`, a `HandlerThread`) | `URGENT_DISPLAY` | `MediaCodec` in asynchronous mode. Each decoded frame is released to the Surface at once |
| `Sender` | | Writes touches, `KEYS`, `PNTR`, `KEYQ`, `PING` and `SIZE` in batches |

The Sender is started before the Feeder, so a keyframe request the Feeder makes
straight away is not lost.

**Connect.** TCP connects are raced on every candidate host (the last address
that worked first, then the QR's list), with a 3 s timeout for each. A host that
fails TLS or the certificate pin falls through to the next one, and losing and
late sockets are closed. A direct link (`isDirect`, see *Moving back to the
cable*) beats Wi-Fi: a Wi-Fi socket that answers while a direct attempt is
still running is held for up to 400 ms (`DIRECT_GRACE_MS`) and used only if
the direct one fails or stays silent. Each socket is bound to the Android network whose
subnet holds the address (`networkFor`), so a Wi-Fi address goes over Wi-Fi
even when mobile data is the default network. Loopback is not bound, and if
binding fails the socket stays unbound. Each socket is marked DSCP AF41
(`trafficClass = 0x88`, the same mark the daemon uses), which puts touches and
keyframe requests in Wi-Fi's video queue. When every host fails, the best error
is reported: a TLS or pairing problem first, then a refused connection (the
computer is reachable and Tandem is not running), then a timeout.

**Secure the connection** (`SecureChannel`, 5 s timeout). Establish TLS,
accepting only the certificate pinned by the QR code. Then exchange `HELO`,
`CHAL`, `AUTH` and `OKAY`, and check the computer's proof before anything else.

**Final or retry** (`classify`). A session ends as *final*
(`onStopped(…, final = true)`), with the reason shown and no reconnect, only for
a certificate pin mismatch, a non-TLS answer during setup, a non-temporary
`DENY`, a wrong pairing proof, or a banner that announces a codec other than
H.264. A reset or EOF in the middle of the handshake is an ordinary network
error and is retried. `DENY` is handled after `OKAY` too. `parseDeny` reads the
optional fields `"retry": bool` and `"retry_after": seconds`. A refusal is
temporary if either field is set, or if the reason contains *phones connected*,
*busy*, *try again* or *too many*, which covers the daemon's *"already 4 phones
connected"* and *"too many failed pairing attempts … try again in a minute"*. A
temporary refusal is retried after `retry_after`, or after 5 s, and reaches the
activity as `onStopped(session, reason, final, retryAfterMs)`.

**Read frames** with a 4 s socket timeout. The daemon sends `PING` every
second, and the Sender sends one after 1 s with nothing else to send, which
turns on the daemon's read timeout for this phone. `ASX1` becomes a
`ServerConfig`, which the side sheet shows; its `fit` field (`fitsDevice`,
null from daemons that don't send it) says whether the monitor follows the
phone's screen. `INPT {"touch": …, "reason": …, "keys": …, "pointer": …}` becomes
an `InputState` and goes to `onInputState`, which sets touch on or off and
enables or disables typing and pointer mode. A daemon before 0.9.0 sends no
`keys` or `pointer`, which reads as false. `NALU` goes to the Feeder's
queue.

**Access units, not NAL units** (`AccessUnitAssembler`, `H264`). Hardware
decoders, such as Qualcomm's C2, reject partial input. They need one complete
frame per input buffer. A new access unit starts at an AUD, SPS, PPS, SEI or NAL
type 14 to 18 that follows a slice, or at a slice whose `first_mb_in_slice` is 0
(read as ue(v), skipping emulation-prevention bytes). The daemon drops AUD and
SEI before sending, so `first_mb_in_slice` is what actually splits frames. A
lone slice is flushed at once only after the stream has been seen to be
single-slice. A multi-slice stream waits up to 8 ms for the next NAL.
`H264.codedSize` reads the resolution from the SPS.

**Choosing a decoder** (`VideoDecoder.chooseDecoders`). Candidates are ranked by:

1. the decoder supports the size (`isSizeSupported`, in either orientation);
2. it is hardware;
3. it also supports the frame rate (`areSizeAndRateSupported`);
4. it has `FEATURE_LowLatency`;
5. otherwise the platform's order.

Size and rate used to be one check ranked above hardware. Hardware decoders
often under-report their frame rate, while `c2.android.avc.decoder` claims
nearly any rate, so a 90 or 120 fps stream on a mid-range phone picked the
software decoder. `.secure` and alias decoders are skipped.

**Configuring it** (`VideoDecoder.configure`). Each candidate is tried *tuned*,
then *plain*, then the next candidate is tried. Both set `csd-0` and `csd-1` to
SPS and PPS, `KEY_LOW_LATENCY` (Android 11 and later) and `KEY_PRIORITY` 0
(realtime). Tuned adds `KEY_OPERATING_RATE` and vendor low-latency keys. On
Android 12 and later, only keys the codec lists in `supportedVendorParameters`
are used. Tuned also sets the colour keys `KEY_COLOR_STANDARD` =
`COLOR_STANDARD_BT709`, `KEY_COLOR_RANGE` = `COLOR_RANGE_LIMITED` and
`KEY_COLOR_TRANSFER` = `COLOR_TRANSFER_SDR_VIDEO`: Tandem streams are BT.709,
limited range (`linux/colour.py`). The computer labels its stream the same
way, except with openh264, which writes no colour information; the format
then decides, instead of each phone's own guess, which differs between makers
(GitHub issue #7). A label in the stream still wins, so streams from older
computers look as before. On older versions they are chosen by the codec name's vendor (qti,
exynos, amlogic, hisi). Tuned also sets `KEY_MAX_INPUT_SIZE` to the largest of
width × height × 3/2, the largest access unit seen so far, and 1 MiB, capped at
24 MiB. Input buffers sized for a guessed 1080p resolution would silently drop
large keyframes at a phone's native resolution. A decoder that fails without
producing a single frame is skipped next time. Input timestamps are
`System.nanoTime()/1000`, strictly increasing. A new SPS, such as one from a
resolution change after a daemon restart, releases the decoder and rebuilds it
at the next keyframe.

**Output.** Each output buffer is released with `render = true` as soon as it is
decoded, on the decoder's own thread. The first one fires `onFirstFrame`, which
starts the water reveal. `INFO_OUTPUT_FORMAT_CHANGED` reports the cropped size
(1080 rather than the encoder's padded 1088), so the letterbox is exact.

**Recovery** (`checkHealth`, `resetDecoder`, `startResync`). Every case ends the
same way: skip to the next keyframe and send `KEYQ`, so the daemon replays its
cached GOP at once instead of the phone waiting for the next natural keyframe.

| Trouble | What happens |
| --- | --- |
| A transient `CodecException` | The codec is kept |
| Any other codec error | The codec is released and recreated |
| An access unit larger than the input buffer | The empty buffer is still handed back, the size limit is raised, and the codec is recreated |
| A null input buffer | The index is still handed back |
| No free input buffer for 500 ms | The decoder is reset |
| Watchdog: 2 s without output while 20 or more inputs went unanswered | The decoder is reset |
| No keyframe within 4 s of a resync, after video had started | The session ends as not final, and the app reconnects |

`KEYQ` goes out at most once per 500 ms. A request made sooner is delayed, not
dropped, because the one that matters is often the second. While still waiting
for a keyframe after a resync, the phone asks again every 1.5 s
(`KEYFRAME_REASK_MS`), but not while it is skipping a receive backlog. The
daemon may have swallowed an earlier request under its own 500 ms limit, or its
replay may have been skipped along with the backlog.

**Catching up when behind.** A frame decoded late is worse than a frame
skipped, so the phone drops stale video and asks for a keyframe:

- **Socket backlog** (`BacklogMeter`). When the unread bytes stay above the
  larger of 768 KiB and 0.4 s of the stream's bitrate for 200 ms, the reader
  skips them, at most once per 3 s. `KEYQ` goes out when skipping ends.
- **Decoder lag** (`isLagging`). A NAL that has waited more than 250 ms in the
  Feeder's queue also triggers catch-up. There is 1 s of grace after each
  configure and after each resync keyframe, because the daemon answers `KEYQ`
  with the whole GOP in one burst.
- **Queue cap** (`Feeder.offer`, 16 MiB). On overflow the queue is cleared and
  `KEYQ` is sent at once. All the stale data was in this queue, so there is
  nothing left in the socket to read past.

The app no longer sets `SO_RCVBUF`, so the kernel's auto-tuning sizes the
receive buffer.

**Writes never block the UI.** `sendTouches(List<TouchPoint>)` runs on the UI
thread. It hands every pointer of one `MotionEvent` to the Sender at once, and
the Sender writes them with `FrameWriter.sendAll`, several frames in one write
and one flush. A write error is ignored there, because the reader notices the
dead connection and ends the session.

### 4.3 `FrameReader`

Each message is a 4-byte type, a 4-byte big-endian length, and a body. The body is
read in full. A length over 32 MB is a corrupt stream and throws an
`IOException`, so the app reconnects instead of treating it as a clean end of
stream. The types are `NALU`, `ASX1`, `PING` and `INPT`, plus the handshake's
`CHAL`, `OKAY` and `DENY`, which `SecureChannel` reads. The common type tags are
matched without allocating a `String` per frame. Unknown types are skipped, so
newer daemons stay compatible. `FrameWriter` is the mirror image, for `HELO`,
`AUTH`, `TUCH`, `KEYQ` (`TYPE_KEYFRAME_REQUEST`, empty) and `PING`
(`TYPE_PING`, empty). It assembles each frame and writes it in one synchronised
call, so two frames can never interleave on the socket. `sendAll` writes
several frames with one write and one flush. Older daemons ignore `KEYQ` and the
phone's `PING`. The other phone-to-computer types are `KEYS` (`TYPE_KEY`) and
`PNTR` (`TYPE_POINTER`), sent only when the computer's `INPT` offered them,
and `SIZE` (`TYPE_SCREEN_SIZE`):

| Type | Body | Meaning |
| --- | --- | --- |
| `KEYS` | 5 bytes, big-endian: keysym u32, pressed u8 | One key going down (1) or up (0), as an X keysym (`Keysyms.encode`). A tap is a press and a release; key repeat is the computer's job |
| `PNTR` | 10 bytes, big-endian: action u8, button u8, x u16, y u16, dx i16, dy i16 | Pointer mode (`PointerEvent.encode`). Action 0 move, 1 button down, 2 button up, 3 scroll. Button 1 left, 2 right, 3 middle (the app sends only left and right). x/y are fractions of the picture scaled to 0..65535, as in `TUCH`. dx/dy are the scroll in 1/120 of a wheel notch, positive right / down |
| `SIZE` | JSON `{"width": W, "height": H}`, plus `"now": true` for a deliberate turn | This screen's size as held. `now` asks the computer to apply it at once rather than after its 0.6 s settle |

In the Sender, `KEYS` and `PNTR` share one ordered queue (at most 256 frames;
on overflow the oldest goes), so a key never overtakes a click or another key.
A pointer `MOVE` replaces a still-unsent `MOVE` at the end of that queue, like a
touch's move.

### 4.4 `ConnectTarget`

It parses `tandem://c?h=<ip>[,<ip>…]&p=<port>&n=<name>&k=<pairing key>&f=<cert
fingerprint>&v=2`, validates the hosts, key and fingerprint, and stores the last
target in `SharedPreferences`. That storage is excluded from backups. A QR code
with `v=1` comes from a version before 0.5, and the app reports it as outdated.
The host that last connected is saved as `lastGoodHost` (key
`target_last_good_host`) and tried first. Scanning a new QR code of the same
computer keeps it (`keepingLastGoodHostOf`).

Manual entry accepts `host`, `host:port`, or a separate port field. It also needs
the pairing code. If the code belongs to the saved computer, the app keeps that
computer's name and pinned certificate, so correcting only the address works. A
target saved without a valid key must be paired again.

### 4.5 `RippleRevealLayout`

A `FrameLayout` driven by a `TimeAnimator`. On Android 13 and later it updates the
AGSL uniforms (`origin, time, amp, reveal, edge, density`) and reapplies
`RenderEffect.createRuntimeShaderEffect` every frame. Transparent output pixels
show the video `SurfaceView` underneath. Its methods are `startRipple(x, y)`,
`reveal(onDone)`, `settle()` (the waves calm down after a failure) and `reset()`.

### 4.5a `AsciiSplashView`

It is the top layer of `activity_main.xml`, hidden until `MainActivity.onCreate`
calls `run {}`. That only happens when `savedInstanceState == null`, so a
recreated activity does not replay the splash. On a size change, the view lays out
a monospace character grid, 9 dp text on phones and 13 dp on tablets. It paints
"Tandem" into a bitmap with one pixel per cell, using Archivo Black widened by
1.15 times. On each frame, every cell samples that bitmap at a point pushed out
along the radius by three staggered wave trains. The wave function is the same one
`RippleRevealLayout` uses, measured in dp. Brightness maps onto the ramp
` .:-=+*#%@`, and each row is drawn with one `drawText`. The black background is
a rectangle minus the growing circle (an even-odd path), so the connect screen
shows through the hole. The view takes touches until it hides itself.

**The fonts are bundled, not taken from the phone.** Both ASCII views use two small
fonts in `res/font`, loaded through `AsciiType`. `tandem_ascii_mono.ttf` is Geist
Mono Bold, with only the ramp characters and "M", and provides the characters.
`tandem_wordmark.ttf` is Archivo Black, with only "Tandem", and provides the
letterforms. A row is a single `drawText`, so the grid lines up only if every
character has the same width. Phone makers often replace the system `monospace`
font, because AOSP has no bold monospace and falls back to a proportional face.
Some also drop the `sans-serif-black` alias. On one phone, that misaligned the
splash and the title. The fonts are licensed under the OFL. Their licences and the
subsetting script are in `android/fonts/`.

### 4.6 Touch input (`TouchForwarder` and `MainActivity.forwardTouch`)

The video `SurfaceView` has a touch listener. While the state is **LIVE**, the
computer has answered `INPT touch: true`, and the side sheet is closed, the view
consumes every event and passes it to `TouchForwarder`. Otherwise it returns
`false`, so normal click handling runs and the edge handle appears.

`TouchForwarder` maps `MotionEvent`s to `TUCH` frames:

| MotionEvent | `TUCH` action | Sent for |
| --- | --- | --- |
| `ACTION_DOWN`, `ACTION_POINTER_DOWN` | 0, down | The pointer at `actionIndex` |
| `ACTION_MOVE` | 1, move | Every pointer |
| `ACTION_UP`, `ACTION_POINTER_UP` | 2, up | The pointer at `actionIndex` |
| `ACTION_CANCEL` | 3, cancel | All fingers. The computer lifts them |

Every pointer of one `MotionEvent` is handed to the sender together, as a list.
Before sending, `TouchQueue` coalesces: an unsent move is replaced by the same
finger's newer move, down, up and cancel keep their order, and a cancel drops
pending moves. It holds at most 64 entries, so a slow link drops intermediate
positions instead of replaying old ones. `TouchPoint.encode()` packs the 6-byte
body.

`finger` is the Android pointer ID. `x` and `y` are the position divided by the
view's size, scaled to 0..65535. `AspectRatioSurfaceView` sizes the view to the
stream's exact aspect ratio, so those fractions are also fractions of the
computer's virtual monitor. Touches on the letterbox bars never reach the view.
Gestures such as long-press, scroll and pinch are interpreted by the desktop apps.

Touch state resets to *off* whenever a session starts or stops. It turns on only
from the current session's `INPT`, so a stale answer cannot switch it on. The
same holds for typing and pointer mode.

### 4.7 Typing (`RemoteKeyboard`, `KeyboardTargetView`, `Keysyms`, `TextToKeys`)

Keys travel as **X keysyms**: what the user meant (`a`, `É`, Return,
Control_L), never a key position. The phone can't know the computer's keyboard
layout, so the computer finds the key in its own layout
([`ARCHITECTURE.md`](./ARCHITECTURE.md)). `Keysyms.forCodePoint` maps a
character to its keysym: Latin-1 characters are their own keysym, everything
else is `0x01000000 | code point`, and newline, tab and backspace become
Return, Tab and BackSpace. `Keysyms.forKeyCode` maps non-character Android key
codes (arrows, Home/End, Page Up/Down, Insert, Delete, modifiers, Caps Lock,
F1 to F12, space) for hardware keyboards.

- **The on-screen keyboard** types into `KeyboardTargetView`, a focusable view
  with an `InputConnection` and no text. The editor info asks for no
  suggestions, no fullscreen (extract) mode in landscape, and Enter as a plain
  key. Keyboards don't only type characters: with swipe typing or autocorrect
  they show a word while composing it and then change it. `TextToKeys` keeps
  what it has typed for the current word and, on each change, presses
  Backspace for what no longer matches and types the rest, so autocorrect
  works on the computer too. The field is always empty apart from the word
  being composed, so Backspace on it reaches the app as a key event, which is
  what most keyboards send then.
- **Hardware keyboards.** `MainActivity.dispatchKeyEvent` passes key events to
  `RemoteKeyboard.onKeyEvent` while live, with typing offered and the sheet
  closed. Press and release are sent as they happen (so Shift and Ctrl can be
  held), repeats are dropped (the computer repeats a held key itself), and
  phone keys (Back, Home, App switch, volume, power, camera, search, assistant)
  are never sent.
- **Latches.** `toggleLatch` holds Ctrl, Alt or Super for the next key: that
  key's press is wrapped in the latched modifiers' press and release.
- **Letting go.** `releaseAll` releases anything still held when the keyboard
  closes or the link drops. The computer also releases every key a phone held
  when it disconnects.

### 4.8 Pointer mode (`PointerGestures`)

`forwardTouch` gives touches to `PointerGestures.onTouch` when pointer mode is
chosen and the computer's `INPT` said `"pointer": true`; otherwise to
`TouchForwarder` when touch is on. A touch first only moves the pointer there
(`MOVE`) and stays *pending* until it is clear what it is, so a tap never
starts a small accidental drag:

- moved more than 8 dp: `DOWN` left at the first touch point, then `MOVE`s
  (a drag);
- lifted: `DOWN` and `UP` left (a click);
- held for 500 ms: `DOWN` and `UP` right (a right click); moving afterwards only
  moves the pointer;
- a second finger: two-finger mode. Lifting without moving is a right click;
  moving scrolls by the fingers' centroid, 40 dp per wheel notch, sent as
  `SCROLL` in 1/120 notch steps (fingers moving up scroll down, as on a
  touchscreen).

`cancel()` lets go of a drag when the mode is switched, the stream stops or the
computer withdraws pointer mode. The computer also releases held buttons when a
phone disconnects.

---

## 5. Why each choice keeps latency low

| Choice | Effect |
| --- | --- |
| Hardware `MediaCodec` into a `Surface` | No copies into app memory, and decoding runs on the video block |
| `KEY_LOW_LATENCY` and no B-frames | The decoder skips its reorder buffer |
| Render on arrival (`releaseOutputBuffer(i, true)` in the decoder callback) | No frame pacing and no jitter buffer |
| Whole access units per buffer, split on `first_mb_in_slice` | The decoder never waits for the rest of a frame |
| Reader, decoder and sender on separate threads | A slow decoder never stops the socket from being read, and touches never wait behind video |
| Skip a stale backlog, then `KEYQ` | After a stall the phone jumps back to "now" instead of playing seconds of old frames |
| GOP replay on connect and on `KEYQ` (daemon) | The first picture, and recovery from a lost frame, take milliseconds |
| Hardware decoder first, with vendor low-latency keys | Software decoding adds tens of milliseconds at high resolutions |
| Highest refresh rate while live | Shorter vsync wait and faster touch sampling |
| Low-latency Wi-Fi lock and DSCP AF41 | No power-save wake-up delays between packets, and Wi-Fi's video queue |

---

## 6. How a frame travels through the app

```
socket bytes ─▶ FrameReader.next() ─▶ "NALU" body (00 00 00 01 41 …)    [reader thread]
   ─▶ Feeder.offer ─▶ queue (16 MiB cap)
   ─▶ AccessUnitAssembler: new AU at first_mb_in_slice = 0 ─▶ AU            [Feeder]
   ─▶ free input index (onInputBufferAvailable) → put → queueInputBuffer
   ─▶ onOutputBufferAvailable → releaseOutputBuffer(render = true)          [decoder thread]
   ─▶ SurfaceFlinger composes it at the next vsync ─▶ your eyes
```

---

## 7. Developer install and debugging

People using the app need neither `adb` nor developer mode. This section assumes
**USB debugging**, or wireless debugging, is enabled on the phone.

### 7.1 Build and install

```bash
cd android
JAVA_HOME=/opt/android-studio/jbr ./gradlew installDebug
# or
JAVA_HOME=/opt/android-studio/jbr ./gradlew :app:assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

### 7.2 Drive the app from the command line

```bash
# Open with a computer pre-filled, as the QR does. The app asks for confirmation.
# The full link, with k= and f=, is printed by testsrc.py and start-tandem.sh.
adb shell am start -a android.intent.action.VIEW -d "'tandem://c?h=192.168.42.129&p=5000&n=archbox&k=<key>&f=<fp>&v=2'"

# Debug builds only: launch and connect to the saved computer immediately
adb shell am start -n io.github.saharshbaiju.tandem.debug/.MainActivity --ez autoconnect true
```

> Some phones block `adb shell input` injection. The two intents above reach every
> state without any taps.

### 7.3 Test without touching your desktop

`linux/testsrc.py` streams a test pattern through the real daemon code, including
the protocol, heartbeats and GOP replay. It does this without creating a monitor:

```bash
python3 linux/testsrc.py --width 2400 --height 1080 --fps 60   # port 5000
python3 linux/testsrc.py --static     # 2 s of video, then silence: tests GOP replay
python3 linux/testsrc.py --log-touch  # accept touches, keys and pointer events, print them
```

Phones pair with it exactly as they do with the real daemon. It prints the QR link
and the pairing code. With `--log-touch`, every touch is printed as a fraction of
the picture (for example, `touch down  slot 0  x=0.500 y=0.500`), every key with
its keysym and character (`key down  0x61 'a'`), and every pointer-mode event
(`pointer move  x=0.500 y=0.500`, `pointer left down`, `pointer scroll dx=0
dy=120`). The daemon offers typing and pointer mode in that case, so the
Keyboard button and the Pointer switch work. This lets you test the input
paths with `adb shell input tap`, `swipe` or `text` on the emulator.

The Android emulator reaches the PC at `10.0.2.2`, so a printed link with
`h=10.0.2.2` connects the emulator to the test daemon. A Google Play system image
includes Play services, so the QR scanner works there too.

### 7.4 Logs

```bash
adb logcat --pid=$(adb shell pidof io.github.saharshbaiju.tandem)
adb logcat -s StreamSession          # connect, decoder and session-end reasons
```

### 7.5 Wireless debugging (no cable)

On Android 11 and later: Settings → Developer options → **Wireless debugging** →
*Pair device with pairing code*. Then run:

```bash
adb pair <phone-ip>:<pair-port>        # 6-digit code from the phone
adb connect <phone-ip>:<connect-port>  # the port on the main Wireless-debugging screen
```

On Android 10 or older, run `adb -d tcpip 5555`, unplug the phone, and run
`adb connect <phone-ip>:5555`. Wireless adb carries only developer commands. The
video still goes straight to the PC.

---

## 8. Limitations and next steps

- **TCP only.** This works well over USB. Lossy Wi-Fi can cause brief freezes,
  because of head-of-line blocking. RTP over UDP with FEC is planned.
- **No pen.** There is no pen pressure and no hover. Desktop apps that ignore
  touch events ignore touch mode; pointer mode works with them, as a mouse.
- **Keys are keysyms.** On Hyprland, and on KDE before Plasma 6.2, the computer
  types a character only if its own keyboard layout has a key for it; others
  are skipped. Key combinations the phone's system keeps (Back, Home) never
  reach the computer.
- **H.264 only.** HEVC would reduce the bitrate. A banner announcing another codec
  ends the session with a final, clear message.
- **Multi-slice streams wait up to 8 ms** for the next NAL, because the daemon
  sends no end-of-frame marker. Tandem's own encoders are single-slice.
- **The cutout is avoided only within letterbox room.** The phone does not report
  its safe area to the computer, so a picture that fills the panel can still sit
  under a punch-hole.
- **No address discovery.** The app tries the QR's addresses and the last address
  that worked. If the computer's address changes, scan the QR code again.
- The QR scanner needs Google Play services. Without them, use the stock camera or
  enter the address manually.

## Update check (`UpdateCheck.kt`)

The app is installed from GitHub Releases, not a store, so nothing would tell a
user that a new version exists. From 0.8.0 the app checks by itself:

- **When:** every time the app comes to the front (`onStart`), if the phone
  has a network with working internet (validated; a USB link to the computer
  alone doesn't count). Without one, the last answer is shown and the check
  runs as soon as such a network comes up while the app is open. The answer
  (the newest stable release and the newest pre-release, with their pages) and
  its ETag are kept in the app's preferences. Each ask sends that ETag
  (`If-None-Match`); while nothing changed GitHub answers `304 Not Modified`,
  which doesn't count against its limit of 60 anonymous requests an hour, and
  the kept answer stands. A check that is still running isn't started twice.
  The card also hides again when the app is up to date.
- **What is sent:** one HTTPS `GET` to
  `api.github.com/repos/saharshbaiju/tandem/releases?per_page=20`, with the
  generic User-Agent `Tandem-update-check`. No identifiers, no settings, nothing
  about the paired computer. It runs on its own thread, so a slow network never
  holds up the connect screen or the link probes.
- **What the user sees**, in a card under the credit on the connect screen:
  - **Update available** when the newest stable release is newer than the app:
    "Tandem 0.8.1 is out. You have 0.8.0." **Update** opens the release page;
    **Later** hides the card until a newer release.
  - Otherwise **Want to try the beta?** when a pre-release is newer than both
    the app and the newest stable release: "Tandem 0.9.0-beta.1 is out for
    testing. It has the newest features, and may still have bugs." **Try it**
    opens its release page; **No thanks** hides the card until a newer beta.
  - Every release and beta is signed with the same key, so its `Tandem.apk`
    installs over the app and keeps the pairing. A beta has a higher
    `versionCode` than the release before it, and its final release a higher
    one again, so a beta user moves on to the release as a normal update.
- **Never offered:** drafts, an old beta (one whose release is out, or older
  than the app), and anything when GitHub can't be reached, rate-limits, or
  answers with something unexpected. Those cases are logged at info level and
  otherwise ignored.
- **Ordering:** numbers compare as numbers (`0.10.0` is newer than `0.9.9`), a
  pre-release comes before its release (`0.9.0-beta.2` < `0.9.0`), and a
  `-debug` suffix is ignored. GitHub's *pre-release* flag decides whether a
  release is a beta. Covered by `app/src/test/…/UpdateCheckTest.kt`
  (`./gradlew testDebugUnitTest`).

The PC side checks stable releases only (`linux/update_check.py`): betas there
are installed by naming their tag, see [RELEASING.md](RELEASING.md#beta-releases).
