# Tandem — Security

What protects the connection between the phone and the computer, why, and how
it was tested. Applies from **Tandem 0.5.0** (app) and the matching `linux/`
daemon; both sides must be updated together (protocol v2).

Code: [`linux/secure_link.py`](../linux/secure_link.py) (computer) and
[`SecureChannel.kt`](../android/app/src/main/java/io/github/saharshbaiju/tandem/SecureChannel.kt)
(phone). The two files mirror each other.

---

## 1. What was wrong before (≤ 0.4.x)

The daemon listens on TCP port 5000 on **every** interface: Wi-Fi, USB
tethering and `127.0.0.1` (used by `adb reverse`). That is necessary for the
"one QR works on every link" design. It also meant:

| # | Problem | Impact |
| --- | --- | --- |
| 1 | **Anyone who could reach the port got the video.** No key was needed to connect. | Anyone on the same Wi-Fi (café, campus, office) or another account on the PC could watch the extended screen. |
| 2 | **Everything was plain text**: video, the settings banner (hostname), every touch, and the pairing key itself (`HELO {"key": …}`). | A passive sniffer on the network could record the screen and steal the key, then send touches. Touches on an on-screen keyboard reveal what you type. |
| 3 | **The phone couldn't tell whether it was talking to the real computer.** | Someone on the network (ARP spoofing, a fake hotspot) could impersonate the computer, show a fake desktop and collect your touches. |
| 4 | **Any app on the phone could make it connect anywhere**: a `tandem://` link replaced the saved computer silently, and the `autoconnect` intent extra connected immediately. | A malicious app or web page could redirect the phone (and your touches) to an attacker's machine. |
| 5 | **The pairing key went into backups** (`allowBackup="true"`). | The key ended up in Google Drive backups and phone-to-phone transfers. |
| 6 | **No limits on unauthenticated connections.** | Unlimited connections and attempts; slow clients could hold resources. |
| 7 | The key format (base64) wasn't meant to be typed, so typed addresses connected **without** a key (view only). | Encouraged unauthenticated connections. |

---

## 2. Threat model

**Protected against:**

- Anyone else on the same network (Wi-Fi, tethering link, LAN) **watching** the
  screen, **reading** touches, or **sending** touches.
- A **man in the middle** (ARP spoofing, rogue access point, a relay):
  impersonating the computer to the phone, or the phone to the computer.
- **Replaying** a recorded connection.
- **Another account on the same PC** connecting via `127.0.0.1` (the `adb
  reverse` path) or reading the key from a command line.
- **Other apps on the phone** silently redirecting it to another computer.
- Guessing the key, and simple resource exhaustion by misbehaving clients.

**Not protected against (out of scope):**

- Someone who can **see the QR code or pairing code** on your screen. Like a
  Wi-Fi password, showing it is pairing. Use *forget all* (§4.6) if it leaked.
- Malware **running as you** on the computer, or with root on the phone. It can
  read the key file, or the screen directly.
- **Denial of service** by flooding the network or the port. Nothing at this
  layer stops that, but it gives an attacker no access.
- Traffic analysis: an observer can still see *that* you are streaming and
  roughly how much the screen changes (packet sizes and timing).

---

## 3. Design overview

```
phone                                                    computer (tandemd)
  │  TCP connect (every advertised address in parallel)        │
  │ ─────────────────────────────────────────────────────────▶ │  accept → throttle check
  │  TLS 1.3 (1.2 on Android 8/9), self-signed cert            │  → handshake thread
  │ ◀════════════════════ encrypted from here ═══════════════▶ │
  │  cert SHA-256 == f from the QR?  else abort, send nothing  │
  │  HELO {v:2, nonce: nc}                                     │
  │ ─────────────────────────────────────────────────────────▶ │
  │                                    CHAL {v:2, nonce: ns}   │
  │ ◀───────────────────────────────────────────────────────── │
  │  AUTH {proof: HMAC(k, "client"|fp|nc|ns)}                  │
  │ ─────────────────────────────────────────────────────────▶ │  check proof
  │                 OKAY {proof: HMAC(k, "server"|fp|nc|ns)}   │  (or DENY + close)
  │ ◀───────────────────────────────────────────────────────── │
  │  check proof (else abort)                                  │
  │ ◀── ASX1 settings, INPT touch state, NALU video, PING ──── │  only now
  │ ─── TUCH touches ────────────────────────────────────────▶ │
```

The QR code carries everything the phone needs:

```
tandem://c?h=192.168.42.129,10.0.0.5&p=5000&n=titan-arch&k=2wrjsdt74szdswax4rq1&f=lJApVFO6…&v=2
```

| Field | Meaning |
| --- | --- |
| `h`, `p`, `n` | addresses, port, display name (as before) |
| `k` | the **pairing key** (the shared secret) |
| `f` | base64url **SHA-256 fingerprint** of the computer's TLS certificate |
| `v` | protocol version; `2` = this design. The app refuses `v=1` QR codes with an "update tandem" message. |

---

## 4. What's implemented

### 4.1 The pairing key (QR `k`)

- **Random, not derivable.** 20 characters drawn with Python's `secrets`
  (the OS's cryptographic random generator) from the Crockford base32 alphabet
  `0-9 a-z` minus `i l o u`: **100 random bits**. Nothing comes from the
  hostname, time, MAC address or anything else, so knowing the algorithm (it's
  open source) doesn't help anyone reproduce a key. Each computer's key is
  independent; 1,000 generated keys in the tests were all distinct.
- **Typable.** Shown as `2wrjs-dt74s-zdswa-x4rq1` under the QR (TUI live view,
  `start-extender.sh`). Case, dashes and spaces don't matter, and the look-alike
  letters `i`/`l` → `1` and `o` → `0` are corrected, on both ends.
- **Stored privately.** `~/.config/tandem/pairing-key`, mode `0600`
  in a `0700` directory. It is never put on a command line (other local users
  can read `/proc/*/cmdline`): the daemon gets the *path*.
- **Never sent over the network.** Only HMAC proofs derived from it travel
  (§4.3), so recording a connection doesn't reveal it.
- **Old keys are replaced.** A key in the ≤ 0.4 format (base64, which used to
  travel in clear text) is regenerated automatically. Phones must rescan
  anyway, because they need the certificate fingerprint.
- **Why 100 bits is enough.** The only way to test a guess offline is to record
  a proof, and each guess is then one HMAC computation: 2¹⁰⁰ guesses is far out
  of reach. Online guessing is also locked out (§4.5).

### 4.2 Encryption: TLS with a pinned self-signed certificate (QR `f`)

- On first use the computer creates a **P-256 ECDSA certificate**
  (`openssl req -x509 -newkey ec …`, valid 10 years) in
  `~/.config/tandem/tls/`. The private key is born `0600` (umask
  `077`) and the directory is `0700`. `openssl` is now a required dependency
  (Dependencies screen and installer), installed by default on all three
  supported distros.
- **TLS 1.2 minimum, 1.3 preferred** (Android 10+ negotiates 1.3; Android 8 and
  9 only speak 1.2). Ciphers are restricted to **ECDHE + AES-GCM /
  ChaCha20-Poly1305**: forward secrecy and authenticated encryption only.
  Compression and session tickets are off. TLS 1.0/1.1 are refused (tested).
- Everything after the TCP connect is encrypted and integrity-protected: the
  pairing proofs, settings, video and touches. A tampered or injected byte
  breaks the connection instead of reaching the decoder or the touchscreen.
- **Pinning.** No certificate authority is involved. The phone trusts exactly
  the certificate whose SHA-256 matches `f` from the QR, checked *during* the
  TLS handshake (`PinningTrustManager`). A different certificate aborts the
  connection before the phone sends anything, and the user sees: *"This isn't
  the computer you paired with: its security certificate is different."*
  (Android lint reports `CustomX509TrustManager` for this class. That is
  expected: it is a pinning trust manager, not a trust-all one.)

### 4.3 Mutual authentication bound to the certificate

After TLS, both sides prove they know the key without revealing it:

```
proof_client = HMAC-SHA256(key, "tandem-v2 client" ‖ 0x00 ‖ fp ‖ nc ‖ ns)
proof_server = HMAC-SHA256(key, "tandem-v2 server" ‖ 0x00 ‖ fp ‖ nc ‖ ns)
```

- `nc`, `ns`: 32 fresh random bytes from each side, per connection, so a recorded
  exchange is useless later (**no replay**; tested).
- `fp`: the SHA-256 of the certificate **as the phone saw it**. This is what
  defeats a relay. If someone in the middle terminates TLS with their own
  certificate, the phone's proof is computed over *their* fingerprint, and the
  real computer rejects it (tested). This keeps an address typed by hand (no
  `f` to pin) safe too.
- **Different labels** (`client` / `server`), so one side's proof can never be
  reflected back as the other's.
- The computer answers `AUTH` **only after** checking the phone's proof. The
  phone checks the computer's proof **before** decoding any video or sending
  any touch. A look-alike computer that doesn't know the key can't show a fake
  desktop or receive touches.
- Proofs are compared in constant time (`hmac.compare_digest`,
  `MessageDigest.isEqual`).
- The key is re-read from its file on **every** handshake, so *forget all*
  takes effect immediately (§4.6).

### 4.4 Nothing before authentication

Before `OKAY` the daemon sends **nothing** but the handshake frames. In
particular there is no hostname banner, no video and no touch state. A
connection that fails gets `DENY {"reason": …}` (shown on the phone) and is
closed. The phone treats these failures as **final**: wrong code, different
computer, outdated version. It shows the reason and stops, instead of retrying
in a loop.

### 4.5 Limits against misbehaving clients

| Limit | Value | Where |
| --- | --- | --- |
| Whole handshake (TLS + proofs) | 6 s deadline, not per read (a client dripping one byte at a time doesn't keep the slot) | `HANDSHAKE_TIMEOUT` |
| Handshake frame size | 1 KB | `MAX_HANDSHAKE_FRAME` |
| Handshakes in flight | 8 at once; extra connections are closed immediately | `Gatekeeper.MAX_PENDING_HANDSHAKES` |
| Failed handshakes per address | 5 within 60 s → that address is refused for 60 s | `FailureThrottle` |
| Phones streaming at once | 4; the 5th gets `DENY "already 4 phones connected"` | `tandemd.MAX_CLIENTS` |
| Frames from an admitted phone | 4 KB max (unchanged); larger = disconnected | `MAX_INCOMING_FRAME` |

A connection closed **before sending a single byte** is not a failure: the app
connects to every address in the QR at once and closes the ones that lose the
race. Those are dropped silently (`secure_link.Abandoned`), so a phone's own
second address (Wi-Fi next to USB) is never locked out. A client that connects
and then stays silent still counts. Found on a real phone: before this fix, the
phone's unused Wi-Fi connection was logged as a failed pairing.

Handshakes run on their own threads, so a slow client never blocks `accept()`
for everyone else. Each rejection is logged with its address and reason
(`client 10.0.0.7 rejected: wrong pairing code (locked out for a minute)`).

### 4.6 Revoking phones: *forget all*

**Configure → Paired phones → Enter** in the TUI creates a new key. The running
daemon notices within half a second (`Gatekeeper.key_changed()`) and
**disconnects every phone**. Phones that try to reconnect fail the proof and are
told to scan the new QR. The certificate stays the same (it identifies the
computer, not the pairing).

To also replace the certificate (e.g. after the config directory was copied
somewhere it shouldn't be), delete `~/.config/tandem/tls/`. It is
recreated on the next start, and every phone has to scan the new QR.

### 4.7 Phone side

- **Links are confirmed.** An `tandem://` link from outside the app (camera,
  browser, another app) no longer replaces the saved computer silently. A dialog
  names the computer and its addresses: *"Pair with titan-arch? … Only continue
  if this is your computer: it will see your touches."* The in-app scanner,
  which the user started themselves, skips the dialog, as does a link for the
  same computer (same key and certificate) that only updates its addresses.
- **No remote-controlled auto-connect.** The `autoconnect` testing extra only
  works in **debug** builds, and never together with a link. Previously any app
  could combine an attacker's link with `autoconnect=true`.
- **No backups of the key.** `allowBackup="false"`, `fullBackupContent="false"`,
  and `data_extraction_rules.xml` excludes the app's preferences from cloud
  backup and device-to-device transfer (Android 12+). A new phone pairs by
  scanning the QR.
- **Cleartext traffic disabled** (`usesCleartextTraffic="false"`).
- **Pairing code field**: the keyboard opens in incognito mode
  (`flagNoPersonalizedLearning`), with no suggestions or autofill, so the code
  isn't learned by the keyboard.
- **Old saved computers** (≤ 0.4, no valid key) can't connect. The app asks for
  a rescan instead of trying an unauthenticated connection.
- One reader thread plus one writer thread on the TLS socket. Conscrypt (Android's
  TLS) supports this, so the existing threading model stays.

### 4.8 Computer side

- **One I/O thread per phone.** OpenSSL forbids using one TLS connection from
  two threads at once, and CPython releases the GIL inside `SSL_read` and
  `SSL_write`. The old design had a separate sender and reader thread per
  phone. Each phone now has a single thread that `select()`s on the
  non-blocking TLS socket plus a wake-up pipe, and both sends and receives. The
  existing guarantees are unchanged: whole frames only, drop-and-resync for slow
  phones, 1 s heartbeat, 5 s send-stall detection.
- The daemon refuses to start without a key or certificate (it creates them),
  and the TUI creates both **before** drawing the QR, so the QR and the daemon
  always agree. If `openssl` is missing, the TUI explains instead of starting.
- `testsrc.py` (test pattern) uses exactly the same gatekeeper, so app testing
  goes through the real security path.

---

## 5. Compatibility

| | 0.5 daemon | ≤ 0.4 daemon |
| --- | --- | --- |
| **0.5 app** | works | refused: *"Couldn't set up an encrypted connection. Update Tandem on the computer…"* |
| **≤ 0.4 app** | refused (TLS handshake fails, nothing sent in clear) | works (insecure) |

A `v=1` QR scanned with the 0.5 app shows *"This QR code is from an older version
without encryption…"*. Update both sides, then scan the new QR once.

---

## 6. Performance

Measured on the PC with the real daemon code over loopback, 12 Mbit/s at
60 fps (25 KB frames). The phone side uses hardware AES as well.

| | per-frame latency (median / p99) | CPU (both ends together) |
| --- | --- | --- |
| TLS 1.2, ECDHE-ECDSA-AES256-GCM | 0.28 ms / 0.70 ms | ≈ 2 % of one core |
| TLS 1.3, AES-256-GCM | 0.26 ms / 0.60 ms | ≈ 2 % of one core |

Encryption is not a meaningful part of the 25–45 ms end-to-end budget
([ARCHITECTURE.md](./ARCHITECTURE.md) §5). The handshake adds about two
round trips (a few ms on a LAN) once per connection.

---

## 7. How it was tested

**Protocol tests** (real `tandemd` broadcaster, accept loop and gatekeeper on a
local port, isolated key and certificate directory; a scripted phone and
attacker): **26/26 pass**.

- key format, `0600` / `0700` permissions, typed-code normalisation, 1,000
  unique keys
- correct key → `OKAY` with a valid computer proof → banner + touch state →
  video (300 KB keyframe intact) → 51 touches delivered while streaming →
  heartbeats
- wrong key → `DENY`, connection closed, no video
- ≤ 0.4 plain-text client → only a TLS alert, no banner or video bytes
- proof bound to a different certificate (relay) → rejected
- 10 connections closed without a byte (the app's parallel connect) → not
  counted, not locked out
- 5 failures → address locked out, even with the right key
- replayed proof → rejected
- silent client → dropped at the 6 s deadline; oversized handshake frame → closed
- TLS 1.3 + AEAD negotiated; TLS 1.0/1.1 refused
- 5th phone → `DENY "already 4 phones connected"`
- *forget all* detected once; old key refused, new key accepted

**App on the emulator** against `testsrc.py`:

- external link → confirmation dialog → *Pair* → *Connect* → video + touch
  (`touch down slot 0 x=0.500 y=0.500`)
- wrong key → *"Wrong pairing code - scan the QR code on the computer again."*,
  no retry loop
- right key, wrong certificate fingerprint → *"This isn't the computer you
  paired with…"*; the computer logs that the phone refused its certificate
- `v=1` QR → *"This QR code is from an older version without encryption…"*
- address + code typed by hand (`2WRJS-DT74S-ZDSWA-X4RQ1`, uppercase with
  dashes) → paired and streaming without a pinned fingerprint

**Real phone** (Android 16, `2510ERA8BI`, debug build over USB/`adb reverse`
against the real Hyprland daemon with NVENC): paired, touch enabled, decoder
started at 1920×1080. This test found the parallel-connect bug above.

**Not yet tested on real hardware:** a physical Android 8/9 phone (TLS 1.2
path; covered by the TLS 1.2 test on the PC), and *forget all* while a real
Hyprland/GNOME session is streaming (the detection and close path is
unit-tested).

---

## 8. Files

| File | Role |
| --- | --- |
| `linux/secure_link.py` | key generation and storage, certificate, TLS context, handshake, lock-out (new) |
| `linux/tandemd.py` | single-thread TLS I/O per phone, handshake threads, client cap, drops phones on key reset |
| `linux/phone_input.py` | touch only: admitted phones are told whether touch is on (`INPT`) |
| `linux/core.py` | QR with `k` + `f` + `v=2`, pairing code, `openssl` dependency |
| `linux/tui.py` | creates key + certificate before starting, shows the pairing code |
| `linux/start-extender.sh`, `linux/testsrc.py` | pairing code / same gatekeeper |
| `android/…/SecureChannel.kt` | TLS + pinning trust manager + handshake (new) |
| `android/…/ConnectTarget.kt` | `f` fingerprint, `v` check, pairing code for typed addresses |
| `android/…/StreamSession.kt` | TLS + proof before any frame; final vs. retryable failures |
| `android/…/MainActivity.kt` | link confirmation, debug-only autoconnect, pairing-code field |
| `AndroidManifest.xml`, `res/xml/data_extraction_rules.xml` | no backups, no cleartext |

## 9. Possible next steps

- Show the certificate fingerprint (short form) in the TUI and the phone's side
  sheet, for manual comparison.
- Optional per-phone keys, to revoke one phone instead of all.
- Bind to the chosen interfaces only (e.g. just the USB tether link) for people
  who never use Wi-Fi.
