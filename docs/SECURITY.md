# Tandem security

This document covers what protects the connection between the phone and the
computer, why each part is there, and how it was tested. It applies from
**Tandem 0.5.0** onward, in the Android app and in the matching `linux/` daemon.
Both sides must be updated together: they refuse each other unless their
protocol versions match exactly (currently version 3).

Code: [`linux/secure_link.py`](../linux/secure_link.py) (computer) and
[`SecureChannel.kt`](../android/app/src/main/java/io/github/saharshbaiju/tandem/SecureChannel.kt)
(phone). The two files mirror each other.

---

## 1. Risks this design removes

The daemon listens on TCP port 5000 on every interface: Wi-Fi, USB tethering and
`127.0.0.1` (used by `adb reverse`). That is what lets one QR code work on every
link. It also creates the risks below, each of which the protocol removes.

| # | Risk | Impact if unprotected |
| --- | --- | --- |
| 1 | Anyone who can reach the port receives the video | Anyone on the same network (a café, campus or office Wi-Fi), or another account on the PC, could watch the extended screen |
| 2 | Traffic is plain text: video, the settings banner with the hostname, every touch, and the pairing key itself | A passive sniffer could record the screen and steal the key, then send touches. Touches on an on-screen keyboard reveal what you type |
| 3 | The phone cannot tell whether it is talking to the real computer | Someone on the network (ARP spoofing, a fake hotspot) could impersonate the computer, show a fake desktop, and collect your touches |
| 4 | Any app on the phone can make it connect anywhere. A `tandem://` link can replace the saved computer silently, and an `autoconnect` intent extra connects at once | A malicious app or web page could redirect the phone, and your touches, to an attacker's machine |
| 5 | The pairing key could be included in app backups | The key could end up in cloud backups or phone-to-phone transfers |
| 6 | Connections that have not authenticated are unlimited | Unlimited connections and attempts, and slow clients could hold resources |
| 7 | Typed addresses could connect without a key, which allows view-only sessions | This encourages unauthenticated connections |

---

## 2. Threat model

**Protected against:**

- Anyone else on the same network (Wi-Fi, the tethering link, or the LAN) watching
  the screen, reading touches, or sending touches.
- A **man in the middle** (ARP spoofing, a rogue access point, or a relay) who
  impersonates the computer to the phone, or the phone to the computer.
- **Replaying** a recorded connection.
- **Another account on the same PC** connecting through `127.0.0.1` (the `adb
  reverse` path), or reading the key from a command line.
- **Other apps on the phone** silently redirecting it to another computer.
- Guessing the key, and simple resource exhaustion by misbehaving clients.

**Not protected against (out of scope):**

- Someone who can see the QR code or the pairing code on your screen. Like a Wi-Fi
  password, showing it is the same as pairing. If it leaks, use *forget all*
  (section 4.6).
- Malware running as you on the computer, or with root on the phone. It can read
  the key file, or read the screen directly.
- **Denial of service** by flooding the network or the port. Nothing at this layer
  stops that, but it does not give an attacker access.
- Traffic analysis. An observer can still see that you are streaming, and roughly
  how much the screen changes, from packet sizes and timing.

---

## 3. Design overview

```
phone                                                    computer (tandemd)
  │  TCP connect (every advertised address in parallel)        │
  │ ─────────────────────────────────────────────────────────▶ │  accept → throttle check
  │  TLS 1.3 (1.2 on Android 8/9), self-signed cert            │  → handshake thread
  │ ◀════════════════════ encrypted from here ═══════════════▶ │
  │  cert SHA-256 == f from the QR?  else abort, send nothing  │
  │  HELO {v:3, nonce: nc, display: {w, h}}                    │
  │ ─────────────────────────────────────────────────────────▶ │
  │                                    CHAL {v:3, nonce: ns}   │
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
| `h`, `p`, `n` | Addresses, port and display name |
| `k` | The **pairing key**, the shared secret |
| `f` | The base64url **SHA-256 fingerprint** of the computer's TLS certificate |
| `v` | Protocol version. `2` is this design. The app refuses `v=1` codes and asks for an update |

---

## 4. What is implemented

### 4.1 The pairing key (QR `k`)

- **Random and not derivable.** The key has 20 characters, drawn with Python's
  `secrets` module (the operating system's cryptographic random generator) from the
  Crockford base32 alphabet, which is `0-9 a-z` minus `i l o u`. That gives 100
  random bits. Nothing comes from the hostname, the time, the MAC address or
  anything else. Because the algorithm is open source, knowing it does not help
  anyone reproduce a key. Each computer's key is independent. In the tests, 1,000
  generated keys were all distinct.
- **Typable.** The key is shown as `2wrjs-dt74s-zdswa-x4rq1` under the QR code, in
  the TUI live view and in `start-tandem.sh`. Case, dashes and spaces do not
  matter. The look-alike letters `i` and `l` are read as `1`, and `o` as `0`, on
  both ends.
- **Stored privately.** The key lives in `~/.config/tandem/pairing-key`, with mode
  `0600` inside a `0700` directory. It is never placed on a command line, because
  other local users can read `/proc/*/cmdline`. The daemon receives only the
  *path*.
- **Never sent over the network.** Only HMAC proofs derived from the key travel,
  as described in section 4.3. Recording a connection therefore does not reveal
  the key.
- **Old-format keys are replaced.** A key in the base64 format from earlier
  releases is regenerated automatically. Phones must rescan anyway, because they
  also need the certificate fingerprint.
- **Why 100 bits is enough.** The only way to test a guess offline is to record a
  proof, and each guess then costs one HMAC computation. 2¹⁰⁰ guesses is far out of
  reach. Online guessing is also limited by lock-out (section 4.5).

### 4.2 Encryption: TLS with a pinned self-signed certificate (QR `f`)

- On first use, the computer creates a **P-256 ECDSA certificate** (`openssl req
  -x509 -newkey ec`, valid for 10 years) in `~/.config/tandem/tls/`. The private key
  is created with mode `0600` (umask `077`), and the directory is `0700`. `openssl`
  is a required dependency. The Dependencies screen and the installer handle it,
  and it is installed by default on all three supported distributions.
- **TLS 1.2 minimum, 1.3 preferred.** Android 10 and later negotiate 1.3. Android
  8 and 9 speak only 1.2. Ciphers are limited to **ECDHE with AES-GCM or
  ChaCha20-Poly1305**, which give forward secrecy and authenticated encryption
  only. Compression and session tickets are off. TLS 1.0 and 1.1 are refused, as
  tested.
- Everything after the TCP connection is encrypted and integrity-protected. That
  covers the pairing proofs, settings, video and touches. A tampered or injected
  byte breaks the connection, instead of reaching the decoder or the touchscreen.
- **Pinning.** No certificate authority is involved. The phone trusts exactly the
  certificate whose SHA-256 matches `f` from the QR code. The check runs during the
  TLS handshake, in `PinningTrustManager`. A different certificate aborts the
  connection before the phone sends anything. The user then sees *"This isn't the
  computer you paired with: its security certificate is different."* Android lint
  reports `CustomX509TrustManager` for this class. That warning is expected. The
  class is a pinning trust manager, not a trust-everything one.

### 4.3 Mutual authentication bound to the certificate

After TLS, both sides prove that they know the key, without revealing it:

```
proof_client = HMAC-SHA256(key, "tandem-v2 client" ‖ 0x00 ‖ fp ‖ nc ‖ ns)
proof_server = HMAC-SHA256(key, "tandem-v2 server" ‖ 0x00 ‖ fp ‖ nc ‖ ns)
```

- `nc` and `ns` are 32 fresh random bytes from each side, generated for each
  connection. A recorded exchange is therefore useless later. Replay is rejected,
  as tested.
- `fp` is the SHA-256 of the certificate **as the phone saw it**. This is what
  defeats a relay. If someone in the middle terminates TLS with their own
  certificate, the phone's proof is computed over *their* fingerprint, and the real
  computer rejects it, as tested. This also keeps an address typed by hand, which
  has no `f` to pin, safe.
- The two proofs use different labels, `client` and `server`, so one side's proof
  can never be reflected back as the other side's.
- The computer answers `AUTH` **only after** it has checked the phone's proof. The
  phone checks the computer's proof **before** it decodes any video or sends any
  touch. A look-alike computer that does not know the key cannot show a fake
  desktop or receive touches.
- Proofs are compared in constant time (`hmac.compare_digest`,
  `MessageDigest.isEqual`).
- The key is re-read from its file on **every** handshake, so *forget all* takes
  effect immediately (section 4.6).

### 4.4 Nothing is sent before authentication

Before `OKAY`, the daemon sends nothing except handshake frames. In particular, it
sends no hostname banner, no video and no touch state. A failed connection gets
`DENY {"reason": …}`, which the phone shows, and then it is closed. The phone treats
these failures as **final**: a wrong code, a different computer, or an outdated
version. It shows the reason and stops, instead of retrying in a loop.

Two refusals are temporary, and are sent as `DENY` before any `OKAY`, without
running the handshake: *"already 4 phones connected to this computer"* when every
streaming slot is taken, and *"too many failed pairing attempts from this phone;
try again in a minute"* for a locked-out address. The phone retries those after a
pause. It treats a `DENY` as temporary when it carries `"retry": true` or
`"retry_after": <seconds>`, or when its reason contains *phones connected*,
*busy*, *try again* or *too many*. Neither text reveals anything about the
computer beyond what a refused connection already does. Plain network errors in
the middle of the handshake (a reset, or EOF before the computer's proof) are
retried by the phone too. A certificate mismatch, a non-TLS answer, a wrong
proof and any other `DENY` stay final.

### 4.5 Limits against misbehaving clients

| Limit | Value | Where |
| --- | --- | --- |
| Whole handshake (TLS and proofs) | 6 s deadline, not per read. A client that sends one byte at a time cannot hold the slot | `HANDSHAKE_TIMEOUT` |
| First byte | 2 s. A real phone sends its TLS hello straight away, so a connection that stays silent is closed | `FIRST_BYTE_TIMEOUT` |
| Handshake frame size | 1 KB on the computer. The phone allows 4 KB and checks the length before it allocates anything | `MAX_HANDSHAKE_FRAME` (both sides) |
| Handshakes in progress | 4 per host and 32 in all. `127.0.0.1` and `::1` get 8, because every `adb reverse` phone shares them. Another 8 are kept for addresses that paired in the last hour. IPv6 hosts are counted per /64. Extra connections are closed immediately | `HandshakeSlots`, `ip_key()` |
| Failed handshakes per address | 5 within 60 s. That address is then told `DENY "too many failed pairing attempts…"` and refused for 60 s. Only failed proofs and malformed or unexpected messages count. A reset, a timeout or EOF mid-handshake (`OSError`) does not. `127.0.0.1` and `::1` are never locked out: every USB phone uses them, and a local program shouldn't be able to lock the USB phone out. The 100-bit key is what stops guessing | `FailureThrottle`, `tandemd.LOCKED_OUT` |
| Phones streaming at once | 4. The fifth gets `DENY "already 4 phones connected to this computer"` before any `OKAY` | `tandemd.MAX_CLIENTS`, `PHONES_FULL` |
| Frames from an admitted phone | 4 KB maximum. A larger frame disconnects the phone | `MAX_INCOMING_FRAME` |
| Frame rate from an admitted phone | 5000 per second (a real phone sends about 100 to 1200). Reading is capped at 256 KB per pass, so a flood can neither grow memory nor starve the video | `MAX_FRAMES_PER_SEC`, `RECV_BUDGET` |
| Fingers per phone | 5, so one phone can't take every slot of the shared touchscreen | `phone_input.MAX_FINGERS_PER_CLIENT` |

Text a peer sends (its app version, a malformed hello) is reduced to plain
letters and digits, or left out, before it reaches the log, because the TUI reads
the log.

A connection that closes **before sending a single byte** is not counted as a
failure. The app connects to every address in the QR code at once, and closes the
connections that lose the race. Those are dropped silently (`secure_link.Abandoned`).
This means a phone's second address, such as Wi-Fi next to USB, is never locked
out. A client that connects and then stays silent does count as a failure.

Handshakes run on their own threads, so a slow client never blocks `accept()` for
everyone else. The lock-out `DENY` is also sent from a thread that holds a
handshake slot, so a flood from a locked-out address can't take more than that
address's slots. When the slots are full, extra connections are closed without a
`DENY`, because answering each one would cost a thread, and one summary line is
logged at most every 30 s. Other rejections are logged with the address and the
reason, for example
`client 10.0.0.7 rejected: wrong pairing code (locked out for a minute)`.

### 4.6 Revoking phones: *forget all*

**Configure → Paired phones → Enter** in the TUI creates a new key. The running
daemon notices within half a second (`Gatekeeper.key_changed()`), and
**disconnects every phone**. Phones that try to reconnect fail the proof, and are
told to scan the new QR code. The certificate stays the same, because it identifies
the computer, not the pairing.

To replace the certificate as well, for example after the config directory was
copied somewhere it should not have been, delete `~/.config/tandem/tls/`. It is
recreated on the next start, and every phone must scan the new QR code.

### 4.7 Phone side

- **Links are confirmed.** A `tandem://` link opened from outside the app, such as
  from the camera, a browser or another app, never replaces the saved computer
  silently. A dialog names the computer and its addresses: *"Pair with
  titan-arch? … Only continue if this is your computer: it will see your touches."*
  The in-app scanner, which the user starts themselves, skips the dialog. So does a
  link for the same computer (same key and certificate) that only updates its
  addresses.
- **No remotely controlled auto-connect.** The `autoconnect` testing extra works
  only in **debug** builds, and never together with a link. A link therefore cannot
  combine a pairing with an immediate connection.
- **No backups of the key.** `allowBackup="false"` and `fullBackupContent="false"`
  are set. `data_extraction_rules.xml` excludes the app's preferences from cloud
  backup and device-to-device transfer on Android 12 and later. A new phone pairs by
  scanning the QR code.
- **Cleartext traffic is disabled** (`usesCleartextTraffic="false"`).
- **The pairing code field** opens the keyboard in incognito mode
  (`flagNoPersonalizedLearning`), with no suggestions or autofill. The keyboard
  therefore does not learn the code.
- **Saved computers without a valid key** (from before 0.5) cannot connect. The app
  asks for a rescan instead of trying an unauthenticated connection.
- The TLS socket has one reader thread and one writer thread. Conscrypt, Android's
  TLS implementation, supports this model.

### 4.8 Computer side

- **One I/O thread per phone.** OpenSSL forbids using one TLS connection from two
  threads at once. CPython releases the GIL inside `SSL_read` and `SSL_write`. Each
  phone therefore has a single thread that `select()`s on the non-blocking TLS
  socket and a wake-up pipe, and it both sends and receives. The guarantees are
  unchanged: whole frames only, drop-and-resync for slow phones, a 1 s heartbeat,
  and send-stall detection after 5 s.
- **Messages from an admitted phone** stay within the 4 KB limit. `KEYQ`
  (request a keyframe) and `PING` are empty. `KEYQ` is rate-limited to one per
  500 ms per phone, so a phone cannot make the daemon replay its GOP cache
  continuously. The last-resort encoder restart it can lead to happens at most
  once per 15 s. An exception while handling a phone's message is logged and
  does not affect other phones.
- **The daemon listens dual-stack** on `[::]` (IPv4 and IPv6) by default. The
  same pairing and TLS requirements apply to both. Use `--host` to limit it to
  one address.
- The daemon refuses to start without a key or a certificate, and it creates them
  itself if they are missing. The TUI creates both **before** it draws the QR code,
  so the QR code and the daemon always agree. If `openssl` is missing, the TUI
  explains the problem instead of starting.
- `testsrc.py`, the test pattern generator, uses the same gatekeeper. App testing
  therefore goes through the real security path.

---

## 5. Version compatibility

The app and the computer must speak the same protocol version (currently 3).
A mismatch is refused during the handshake, before anything else is sent, with
a message that says which side to update: *"update the Tandem app on this
phone"* from the computer, or *"update Tandem on the computer"* from the app.
Version 3 added the phone's screen size to the hello.

Before encryption (0.5):

| | 0.5 daemon | Daemon before 0.5 |
| --- | --- | --- |
| **0.5 app** | Works | Refused, with *"Couldn't set up an encrypted connection. Update Tandem on the computer…"* |
| **App before 0.5** | Refused. The TLS handshake fails and nothing is sent in clear | Works, but without encryption |

A `v=1` QR code scanned with the 0.5 app shows *"This QR code is from an older
version without encryption…"*. Update both sides, then scan the new QR code once.

---

## 6. Performance

Measured on a computer with the real daemon code over loopback, at 12 Mbit/s and
60 fps, with 25 KB frames. The phone side also uses hardware AES.

| | Per-frame latency (median / p99) | CPU (both ends together) |
| --- | --- | --- |
| TLS 1.2, ECDHE-ECDSA-AES256-GCM | 0.28 ms / 0.70 ms | About 2 % of one core |
| TLS 1.3, AES-256-GCM | 0.26 ms / 0.60 ms | About 2 % of one core |

Encryption is not a meaningful part of the 25 to 45 ms end-to-end budget
([ARCHITECTURE.md](./ARCHITECTURE.md) §5). The handshake adds about two round
trips, a few milliseconds on a LAN, once per connection.

---

## 7. How it was tested

**Protocol tests.** These use the real `tandemd` broadcaster, the accept loop and
the gatekeeper, on a local port, with an isolated key and certificate directory. A
scripted phone and a scripted attacker were used. **26 of 26 pass.** They cover:

- Key format, `0600` and `0700` permissions, typed-code normalisation, and 1,000
  unique keys.
- A correct key gives `OKAY` with a valid computer proof, then the banner and
  touch state, then video (a 300 KB keyframe arrives intact), then 51 touches
  delivered while streaming, then heartbeats.
- A wrong key gives `DENY`, the connection closes, and no video is sent.
- A plain-text client, from before 0.5, receives only a TLS alert. No banner and no
  video bytes.
- A proof bound to a different certificate (a relay) is rejected.
- Ten connections closed without a byte, as the app's parallel connect does, are not
  counted and do not lock anything out.
- Five failures lock the address out, even when the key is right.
- A replayed proof is rejected.
- A silent client is dropped at the 6 s deadline. An oversized handshake frame
  closes the connection.
- TLS 1.3 with an AEAD cipher is negotiated. TLS 1.0 and 1.1 are refused.
- A fifth phone gets `DENY "already 4 phones connected"`.
- *Forget all* is detected once. The old key is refused, and the new key is
  accepted.

**App on the emulator,** against `testsrc.py`:

- An external link shows the confirmation dialog. Pressing *Pair*, then *Connect*,
  gives video and touch (`touch down slot 0 x=0.500 y=0.500`).
- A wrong key shows *"Wrong pairing code - scan the QR code on the computer again."*
  There is no retry loop.
- The right key with the wrong certificate fingerprint shows *"This isn't the
  computer you paired with…"*. The computer logs that the phone refused its
  certificate.
- A `v=1` QR code shows *"This QR code is from an older version without
  encryption…"*.
- An address and code typed by hand (`2WRJS-DT74S-ZDSWA-X4RQ1`, in uppercase with
  dashes) pairs and streams, without a pinned fingerprint.

**Real phone.** Android 16, debug build, over USB with `adb reverse`, against the
Hyprland daemon with NVENC. It paired, touch was enabled, and the decoder started
at 1920×1080. This test found a parallel-connect problem, which is covered in
section 4.5.

**Not yet tested on real hardware:** a physical Android 8 or 9 phone, which uses
the TLS 1.2 path. That path is covered by the TLS 1.2 test on the PC. Also *forget
all* while a real Hyprland or GNOME session is streaming. The detection and close
path is unit-tested.

---

## 8. Files

| File | Role |
| --- | --- |
| `linux/secure_link.py` | Key generation and storage, certificate, TLS context, handshake, and lock-out |
| `linux/tandemd.py` | Single-thread TLS I/O per phone, handshake threads, client cap, drops phones on key reset |
| `linux/phone_input.py` | Touch only. Admitted phones are told whether touch is on (`INPT`) |
| `linux/core.py` | QR code with `k`, `f` and `v=2`, the pairing code, and the `openssl` dependency |
| `linux/tui.py` | Creates the key and certificate before starting, and shows the pairing code |
| `linux/start-tandem.sh`, `linux/testsrc.py` | Pairing code, and the same gatekeeper |
| `android/…/SecureChannel.kt` | TLS, the pinning trust manager, and the handshake |
| `android/…/ConnectTarget.kt` | The `f` fingerprint, the `v` check, and the pairing code for typed addresses |
| `android/…/StreamSession.kt` | TLS and proof before any frame. Final and retryable failures |
| `android/…/MainActivity.kt` | Link confirmation, debug-only autoconnect, and the pairing-code field |
| `AndroidManifest.xml`, `res/xml/data_extraction_rules.xml` | No backups and no cleartext |

## 9. Possible next steps

- Show the certificate fingerprint, in short form, in the TUI and in the phone's
  side sheet, so it can be compared by hand.
- Optional per-phone keys, so one phone can be revoked instead of all of them.
- Bind to the chosen interfaces only, for example just the USB tether link, for
  people who never use Wi-Fi.
