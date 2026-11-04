#!/usr/bin/env python3
"""
tandemd - Tandem streaming daemon (Hyprland / GNOME).

Pipeline:
    compositor backend (see backends/)
        hyprland : headless output -> wf-recorder (VA-API)
        gnome    : Mutter virtual monitor -> PipeWire -> GStreamer encoder
        -> raw Annex-B H.264 on a pipe (no container, no B-frames)
        -> this daemon splits NAL units, caches SPS/PPS + the current GOP,
           and broadcasts framed NALs over TCP
        -> phone feeds whole access units into MediaCodec -> Surface

Reliability rules (each one fixed a real "connects then drops" or "stops when
I drag something onto it" bug):
  * Every client has its own I/O thread and a queue of *whole* frames. We
    only ever drop whole queued frames, never the tail of a half-sent one, so
    the framing on the wire can't desync. Control frames (settings, touch
    state) sit in their own queue that is never dropped.
  * A client's backlog is bounded by *age* (and a hard byte cap), not just
    bytes: a phone that falls ~0.3 s behind is resynced instead of being fed
    seconds-old video.
  * A resync (fell behind, joined, or the phone asked with KEYQ) replays the
    cached GOP (SPS+PPS+IDR+P-frames since) immediately, so the phone is back
    on "now" even when the desktop is static and no new keyframe is coming.
    Without a cache it asks the backend for a keyframe.
  * The encoder is supervised: if it exits mid-session it is restarted with
    backoff, keeping the virtual monitor and the connected phones.
  * A PING frame goes out after 1s of silence; TCP_USER_TIMEOUT and an
    optional read timeout detect a phone that vanished.
  * The tail NAL is flushed as soon as the encoder pipe goes quiet (~1 ms), so
    the last frame of a burst isn't held hostage until the next frame arrives.
    A tail that turns out to have been cut short is detected and resynced
    rather than silently patched over.
  * Touches are injected on their own thread (phone_input.py): a slow
    compositor can never stall the video.

The virtual monitor only appears once a phone has paired, at that phone's
screen size (its hello says it; --resolution can fix a size instead). So no
size has to be configured, and no empty monitor sits on the desktop while no
phone is watching.

Transport is TCP on every interface, IPv4 and IPv6 (USB tethering, Wi-Fi, adb
reverse all work), wrapped in TLS. A connection receives nothing until it
passes the pairing handshake in secure_link.py (certificate fingerprint +
mutual proof of the pairing key from the QR code); see that module for the
threat model.

Signals: SIGTERM / SIGINT / SIGHUP stop the daemon cleanly; SIGUSR1 re-reads
--banner-file and applies live side/scale changes. With --parent-pid, the
death of that process (the TUI) stops the daemon too (SIGUSR2 via
PR_SET_PDEATHSIG).
"""

import argparse
import ctypes
import errno
import fcntl
import json
import os
import select
import signal
import socket
import ssl
import sys
import tempfile
import threading
import time
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import about  # noqa: E402
import backends  # noqa: E402
import secure_link  # noqa: E402
from phone_input import MAX_INCOMING_FRAME, PhoneInput  # noqa: E402

# H.264 NAL unit types (nal_unit_header & 0x1F)
NAL_NON_IDR = 1
NAL_IDR = 5
NAL_SPS = 7
NAL_PPS = 8
# SEI, access-unit delimiter, filler: the phone's decoder ignores them, and
# NVENC in CBR mode emits a filler NAL after nearly every frame.
NAL_DROP = (6, 9, 12)


_log_file = None          # set once stderr is gone (terminal closed, TUI died)


def log(*a):
    """Log to stderr; never raise.

    stderr can disappear under us: a closed terminal (EIO) or a TUI that died
    (EPIPE). A logging failure must not kill a thread or skip the shutdown
    path, so we fall back to a file in $XDG_RUNTIME_DIR.
    """
    global _log_file
    line = " ".join(["[tandemd]"] + [str(x) for x in a])
    if _log_file is None:
        try:
            print(line, file=sys.stderr, flush=True)
            return
        except (OSError, ValueError):
            path = os.path.join(os.environ.get("XDG_RUNTIME_DIR")
                                or tempfile.gettempdir(), "tandemd.log")
            try:
                _log_file = open(path, "a", buffering=1)
            except OSError:
                _log_file = False
    if _log_file:
        try:
            _log_file.write(line + "\n")
        except (OSError, ValueError):
            pass


def kill_stale():
    """Kill a leftover daemon from a previous run ('Address already in use').

    Inspects each process's real argv via /proc (a loose `pkill -f` would match
    the parent shell/TUI and kill it). Own PID and parent PID are excluded.
    """
    me, parent = os.getpid(), os.getppid()
    killed = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) in (me, parent):
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                args = [a.decode("utf-8", "replace")
                        for a in f.read().split(b"\x00") if a]
        except OSError:
            continue
        if (any(a.endswith("tandemd.py") for a in args)
                and any("python" in a for a in args)):
            try:
                os.kill(int(entry), signal.SIGTERM)
                log(f"killed stale daemon pid {entry}")
                killed.append(int(entry))
            except OSError:
                pass
    # Wait for them to finish tearing down: an old daemon removing its output
    # while we create ours leaves a half-configured, overlapping monitor.
    deadline = time.time() + 6
    while killed and time.time() < deadline:
        killed = [p for p in killed if os.path.exists(f"/proc/{p}")]
        time.sleep(0.1)


# --------------------------------------------------------------------------- #
# Framed wire protocol
# --------------------------------------------------------------------------- #
#
# Every message is:  4-byte type + 4-byte big-endian length + body.
# PC -> phone:
#   NALU -> one Annex-B NAL (with start code), straight to the decoder
#   ASX1 -> JSON of the active settings, shown on the phone (any time)
#   PING -> empty keepalive, sent after HEARTBEAT seconds of silence
#   INPT -> JSON: is touch allowed for this phone? (phone_input.py)
# phone -> PC:
#   TUCH -> one touch event (phone_input.py)
#   KEYQ -> empty: "my decoder needs a fresh keyframe" (it dropped or failed
#           to decode something). Answered with a GOP replay / new IDR.
#   PING -> empty heartbeat. A phone that sends them is timed out after
#           READ_TIMEOUT of silence (older apps never send one: no timeout).
# Before any of these, the handshake frames HELO / CHAL / AUTH / OKAY / DENY
# (secure_link.py). Unknown types are ignored on both ends, so new ones stay
# compatible.

FRAME_NAL = b"NALU"
FRAME_BANNER = b"ASX1"
FRAME_PING = b"PING"
FRAME_KEYFRAME_REQUEST = b"KEYQ"


def frame(ftype, body=b""):
    return ftype + len(body).to_bytes(4, "big") + body


PING = frame(FRAME_PING)


def build_banner_payload(args, backend_name=""):
    """JSON body describing the active settings."""
    return json.dumps({
        "side": args.side,
        "width": args.width,
        "height": args.height,
        "fps": args.fps,
        "scale": args.scale,
        "quality": args.quality,
        "bitrate": args.bitrate,
        "codec": args.codec,
        "gop": args.gop,
        "host": socket.gethostname(),
        "backend": backend_name,
    }).encode("utf-8")


def load_banner_payload(args, backend_name=""):
    """Current banner body: the live --banner-file if present, else from args.

    The TUI keeps --banner-file up to date and sends SIGUSR1 so live changes
    (side / scale) reach already-connected phones. The monitor's size always
    comes from args: only the daemon knows it (it follows the first phone).
    """
    if args.banner_file:
        try:
            with open(args.banner_file, "rb") as f:
                data = json.loads(f.read() or b"{}")
            if data:
                data.setdefault("host", socket.gethostname())
                data.setdefault("backend", backend_name)
                data["width"], data["height"] = args.width, args.height
                return json.dumps(data).encode("utf-8")
        except (OSError, ValueError):
            pass
    return build_banner_payload(args, backend_name)


# --------------------------------------------------------------------------- #
# Clients and broadcaster
# --------------------------------------------------------------------------- #

HEARTBEAT = 1.0                    # seconds of silence before a PING
SEND_TIMEOUT = 5.0                 # no send progress this long = dead peer
READ_TIMEOUT = 5.0                 # silence from a phone that sends heartbeats
SEND_SLICE = 64 * 1024             # bytes per send(): progress is visible and
#                                    touches are read between slices
MAX_QUEUE_AGE = 0.3                # oldest queued video older than this: resync
MAX_PENDING = 4 * 1024 * 1024      # hard cap on queued video bytes
MAX_GOP_CACHE = MAX_PENDING // 2   # a bigger GOP isn't kept (or replayed): it
#                                    could never be replayed under the cap
RESYNC_GRACE = 1.0                 # after a replay, let the phone drain it
RESYNC_RETRY = 1.0                 # re-ask for a keyframe this often
RESYNC_RESTART_AFTER = 4.0         # still no keyframe: restart the encoder
KEYQ_MIN_INTERVAL = 0.5            # rate limit for a phone's KEYQ
MAX_CLIENTS = 4                    # phones streaming at once
RECV_BUDGET = 256 * 1024           # bytes read from one phone per I/O pass: a
#                                    flood can't starve its own sending (or
#                                    grow the receive buffer without bound)
MAX_FRAMES_PER_SEC = 5000          # a real phone sends ~100-1200 (touch
#                                    moves + heartbeat); more is a flood

# Socket tuning (all best effort: a missing option is harmless)
NOTSENT_LOWAT = 128 * 1024         # unsent kernel bytes before select() blocks
USER_TIMEOUT_MS = 5000             # unacked data this long = dead link
DSCP_AF41 = 0x88                   # interactive video; Wi-Fi WMM -> AC_VI


class Client:
    """One admitted phone: queues of whole frames to send, and the frames it
    sends back (touches, keyframe requests), both driven by one I/O thread.

    One thread, not a sender + a reader: an SSL connection must not be used
    by two threads at the same time (OpenSSL's rule; CPython releases the GIL
    inside SSL_read/SSL_write). The socket is non-blocking and the thread
    select()s on it plus a wake-up pipe that push() pokes.

    Video frames carry their enqueue time so the backlog is bounded by age.
    The Broadcaster owns the resync state (started / resync_since / ...) and
    only touches it under its own lock.
    """

    def __init__(self, sock, addr, on_dead, on_message=None):
        self.sock = sock
        self.addr = addr
        self.started = False        # True once it has a keyframe to decode from
        self.resync_since = time.monotonic()   # waiting for a keyframe since
        self.last_keyframe_ask = 0.0
        self.last_keyq = 0.0        # last KEYQ we acted on (rate limit)
        self.alive = True
        self._video = deque()       # (frame, enqueue time)
        self._video_bytes = 0
        self._control = deque()     # banner / INPT: never dropped, sent first
        self._grace_until = 0.0     # no age check before this (GOP replay)
        self._replay_left = 0       # replayed frames still queued
        self._age_floor = 0.0       # ages count from here (replay drained)
        self._lock = threading.Lock()
        self._on_dead = on_dead
        self._on_message = on_message
        self._more = False          # RECV_BUDGET ran out: read again, don't wait
        self._rx_window = time.monotonic()    # incoming-frame rate limit
        self._rx_frames = 0
        self._wake_r, self._wake_w = os.pipe()
        self._wake_open = True
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)
        sock.setblocking(False)
        threading.Thread(target=self._io_loop, daemon=True,
                         name=f"client-{addr[0]}").start()

    # ---- producer side (any thread) --------------------------------------- #

    def send_message(self, frame_type, body=b""):
        """Queue a control frame (not video) for this phone."""
        with self._lock:
            if self.alive:
                self._control.append(frame(frame_type, body))
                self._wake_locked()

    def push_video(self, data, now):
        """Queue one video frame. False if the client is too far behind: its
        queued video was dropped and it must be resynced (Broadcaster)."""
        with self._lock:
            if not self.alive:
                return True
            if self._video and (
                    self._video_bytes + len(data) > MAX_PENDING
                    or (now >= self._grace_until
                        and now - max(self._video[0][1], self._age_floor)
                        > MAX_QUEUE_AGE)):
                # Weak Wi-Fi or a busy phone. Drop the queued frames (whole
                # frames only: the one in flight is already out of the queue).
                age = now - self._video[0][1]
                self._drop_video_locked()
                log(f"client {self.addr[0]} fell behind ({age * 1000:.0f} ms "
                    "queued); resyncing")
                return False
            self._video.append((data, now))
            self._video_bytes += len(data)
            self._wake_locked()
            return True

    def push_replay(self, frames, now):
        """Queue a GOP replay as one batch. It fits the byte cap by design
        (MAX_GOP_CACHE), and is exempt from the age check until it has been
        sent (at most RESYNC_GRACE): it is a burst on purpose. Frames queued
        behind it are aged from the moment it finished."""
        with self._lock:
            if not self.alive:
                return
            self._drop_video_locked()
            for f in frames:
                self._video.append((f, now))
                self._video_bytes += len(f)
            self._replay_left = len(frames)
            self._grace_until = now + RESYNC_GRACE
            self._wake_locked()

    def drop_video(self):
        with self._lock:
            self._drop_video_locked()

    def close(self):
        with self._lock:
            self.alive = False
            self._wake_locked()

    def _drop_video_locked(self):
        self._video.clear()
        self._video_bytes = 0
        self._replay_left = 0

    def _wake_locked(self):
        # Under self._lock and only while the pipe is open: the I/O thread
        # closes it under the same lock, so a stray b"x" can never land in a
        # recycled fd number (e.g. another phone's freshly accepted socket).
        if self._wake_open:
            try:
                os.write(self._wake_w, b"x")
            except OSError:
                pass                 # already poked (pipe full)

    def _next_frame(self):
        with self._lock:
            if self._control:
                return self._control.popleft()
            if not self._video:
                return None
            data, _t = self._video.popleft()
            self._video_bytes -= len(data)
            if self._replay_left:
                self._replay_left -= 1
                if not self._replay_left:          # replay drained: age
                    now = time.monotonic()          # checks resume from now
                    self._age_floor = self._grace_until = now
            return data

    # ---- I/O thread -------------------------------------------------------- #

    def _io_loop(self):
        out, sent = b"", 0           # frame being sent, bytes of it already out
        last_send = time.monotonic()   # last *progress*, not last whole frame
        self._last_recv = time.monotonic()
        self._peer_heartbeats = False  # the phone sends PINGs: time it out
        inbuf = bytearray()
        try:
            while self.alive:
                if sent == len(out):
                    out, sent = self._next_frame() or b"", 0
                    if not out and time.monotonic() - last_send >= HEARTBEAT:
                        out = PING
                now = time.monotonic()
                if (self._peer_heartbeats
                        and now - self._last_recv > READ_TIMEOUT):
                    log(f"client {self.addr[0]} went silent; dropping it")
                    break
                if out:
                    if now - last_send > SEND_TIMEOUT:
                        raise OSError("send stalled")   # dead or stuck peer
                    wait = 0.5
                else:
                    wait = max(0.0, HEARTBEAT - (now - last_send))
                    if self._peer_heartbeats:
                        wait = min(wait, 0.5)
                if self._more:
                    wait = 0.0       # more input is already waiting (TLS may
                    #                  hold it where select() can't see it)
                readable, writable, _ = select.select(
                    [self.sock, self._wake_r], [self.sock] if out else [], [], wait)
                if self._wake_r in readable:
                    try:
                        while os.read(self._wake_r, 4096):
                            pass
                    except BlockingIOError:
                        pass
                if ((self.sock in readable or self._more)
                        and not self._receive(inbuf)):
                    break
                if out and self.sock in writable:
                    try:
                        # A bounded slice: CPython's SSL send is all-or-nothing
                        # per call, so a whole 1 MB IDR on a slow link would
                        # show no progress for seconds. The retry after
                        # SSLWantWrite passes the same slice, as OpenSSL needs.
                        end = min(len(out), sent + SEND_SLICE)
                        sent += self.sock.send(memoryview(out)[sent:end])
                        last_send = time.monotonic()
                    except (ssl.SSLWantWriteError, ssl.SSLWantReadError):
                        pass         # buffer full / TLS wants a read first
        except OSError:
            pass
        finally:
            with self._lock:
                self.alive = False
                self._drop_video_locked()
                self._control.clear()
                self._wake_open = False
                for fd in (self._wake_r, self._wake_w):
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            try:
                self.sock.close()
            except OSError:
                pass
            self._on_dead(self)

    def _receive(self, inbuf):
        """Read what is available (at most RECV_BUDGET bytes) and dispatch
        whole frames as they complete.

        False when the phone closed the connection or broke the protocol.
        Reading is bounded and frames are checked as soon as their 8-byte
        header is in, so a phone that never stops sending can neither grow
        `inbuf` past one frame plus one chunk nor keep this thread from
        sending video.
        """
        budget = RECV_BUDGET
        while budget > 0:
            try:
                chunk = self.sock.recv(min(65536, budget))
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                break
            if not chunk:
                return False
            budget -= len(chunk)
            inbuf += chunk
            self._last_recv = time.monotonic()
            if not self._dispatch(inbuf):
                return False
        self._more = budget <= 0
        return True

    def _dispatch(self, inbuf):
        """Handle every whole frame in `inbuf`, keep the partial rest.
        False if the phone broke the protocol."""
        now = time.monotonic()
        if now - self._rx_window >= 1.0:
            self._rx_window, self._rx_frames = now, 0
        pos, size = 0, len(inbuf)
        while size - pos >= 8:
            length = int.from_bytes(inbuf[pos + 4:pos + 8], "big")
            if length > MAX_INCOMING_FRAME:
                log(f"client {self.addr[0]} sent an oversized frame; "
                    "disconnecting it")
                return False
            end = pos + 8 + length
            if end > size:
                break
            self._rx_frames += 1
            if self._rx_frames > MAX_FRAMES_PER_SEC:
                log(f"client {self.addr[0]} is flooding the connection; "
                    "disconnecting it")
                return False
            frame_type, body = bytes(inbuf[pos:pos + 4]), bytes(inbuf[pos + 8:end])
            pos = end
            if frame_type == FRAME_PING:
                self._peer_heartbeats = True
            elif self._on_message:
                try:
                    self._on_message(self, frame_type, body)
                except Exception as e:
                    # A touch/input bug must never end the video connection.
                    log(f"client {self.addr[0]}: error handling "
                        f"{frame_type!r} ({e!r})")
        del inbuf[:pos]            # once per pass, not once per frame
        return True


def _tune_socket(sock):
    """Low-latency, dead-peer-detecting socket options (all best effort)."""
    def opt(level, name, value, fallback=None):
        num = getattr(socket, name, fallback)
        if num is None:
            return False
        try:
            sock.setsockopt(level, num, value)
            return True
        except OSError:
            return False

    opt(socket.IPPROTO_TCP, "TCP_NODELAY", 1)
    opt(socket.SOL_SOCKET, "SO_KEEPALIVE", 1)
    for name, val in (("TCP_KEEPIDLE", 3), ("TCP_KEEPINTVL", 1),
                      ("TCP_KEEPCNT", 3)):
        opt(socket.IPPROTO_TCP, name, val)
    # Keepalive probes don't run while data is unacked (a PING always is on a
    # dead link): TCP_USER_TIMEOUT aborts those sockets in seconds, not the
    # ~15 minutes of retransmits.
    opt(socket.IPPROTO_TCP, "TCP_USER_TIMEOUT", USER_TIMEOUT_MS, 18)
    # Keep the kernel's *unsent* backlog small, so queued video stays in our
    # queue where whole frames can be dropped. Without TCP_NOTSENT_LOWAT, fall
    # back to a modest send buffer (a huge one hides backpressure).
    if not opt(socket.IPPROTO_TCP, "TCP_NOTSENT_LOWAT", NOTSENT_LOWAT, 25):
        opt(socket.SOL_SOCKET, "SO_SNDBUF", 512 * 1024)
    # Mark as interactive video: Wi-Fi (WMM) maps it to the video access
    # category ahead of bulk traffic. Networks that ignore DSCP are unaffected.
    opt(socket.IPPROTO_IP, "IP_TOS", DSCP_AF41)
    if sock.family == socket.AF_INET6:
        opt(socket.IPPROTO_IPV6, "IPV6_TCLASS", DSCP_AF41, 67)


class KeyframeSource:
    """Gets a fresh IDR out of the encoder on demand, off the caller's thread.

    Asks the backend's optional request_keyframe() (True = an IDR is coming).
    When that isn't available or didn't work and a phone has been stuck for a
    while (`urgent`), restarting the encoder is the one universal way: every
    encoder starts with SPS+PPS+IDR. Restarts are rare and rate-limited.
    """

    MIN_INTERVAL = 0.25          # between backend requests
    MIN_RESTART_INTERVAL = 15.0  # between encoder restarts for a keyframe

    def __init__(self, backend, restart_capture):
        self._request = getattr(backend, "request_keyframe", None)
        self._restart_capture = restart_capture
        self._cond = threading.Condition()
        self._wanted = False
        self._urgent = False
        self._last_restart = 0.0
        threading.Thread(target=self._run, daemon=True,
                         name="keyframe-requests").start()

    def request(self, urgent=False):
        """Non-blocking: called under no lock from any thread."""
        with self._cond:
            self._wanted = True
            self._urgent = self._urgent or urgent
            self._cond.notify()

    def _run(self):
        while True:
            with self._cond:
                while not self._wanted:
                    self._cond.wait()
                urgent, self._wanted, self._urgent = self._urgent, False, False
            coming = False
            if self._request:
                try:
                    coming = bool(self._request())
                except Exception as e:
                    log(f"warning: keyframe request failed ({e})")
            now = time.monotonic()
            if (urgent and not coming and self._restart_capture
                    and now - self._last_restart > self.MIN_RESTART_INTERVAL):
                self._last_restart = now
                log("no keyframe is coming; restarting the encoder for one")
                self._restart_capture()
            time.sleep(self.MIN_INTERVAL)


class Broadcaster:
    """Fans NAL frames out to every client; owns the SPS/PPS/GOP caches and
    every client's resync state.

    It runs before the virtual monitor exists: the first phone to pair
    decides the monitor's size. Phones admitted until then get heartbeats
    only; attach() sends them the settings and the touch state once the
    monitor is up, and the video follows on its first keyframe.
    """

    def __init__(self):
        self._clients = []
        self.phone_input = None             # PhoneInput, set by attach()
        self.keyframes = None               # KeyframeSource, set by attach()
        self._lock = threading.Lock()
        self.sps = None             # framed
        self.pps = None             # framed
        self.gop = None             # framed NALs since the last IDR, or None
        self._gop_bytes = 0
        self.banner_payload = None  # settings JSON, once the monitor is up

    def attach(self, banner_payload, phone_input, keyframes):
        """The monitor is up: tell the phones already here what they get."""
        with self._lock:
            self.banner_payload = banner_payload
            self.phone_input = phone_input
            self.keyframes = keyframes
            clients = list(self._clients)
        for c in clients:
            c.send_message(FRAME_BANNER, banner_payload)
            phone_input.on_admitted(c)

    def set_banner(self, payload):
        with self._lock:
            self.banner_payload = payload
            clients = list(self._clients)
        for c in clients:
            c.send_message(FRAME_BANNER, payload)

    def full(self):
        with self._lock:
            return len(self._clients) >= MAX_CLIENTS

    def add(self, sock, addr):
        """Start streaming to a phone that passed the pairing handshake."""
        with self._lock:
            if len(self._clients) >= MAX_CLIENTS:
                # Normally refused before the handshake (deny_before_okay);
                # this only catches two phones racing for the last slot.
                secure_link.deny(sock, f"already {MAX_CLIENTS} phones connected")
                sock.close()
                log(f"client {addr[0]} refused: {MAX_CLIENTS} phones already "
                    "connected")
                return
            c = Client(sock, addr, self._remove, self._on_client_message)
            # Read under the lock, with the append: a phone is either here
            # for attach() to tell, or sees what attach() set. Never both.
            phone_input = self.phone_input
            if self.banner_payload is not None:
                c.send_message(FRAME_BANNER, self.banner_payload)
            # Replay the current GOP: the decoder catches up to "now"
            # instantly, even if the desktop is static.
            replayed = self._start_locked(c, time.monotonic())
            self._clients.append(c)
            # Logged under the lock: the I/O thread is already running, and a
            # phone that drops at once must log "disconnected" *after* this
            # line, or the TUI's live count (which parses them) goes wrong.
            log(f"client connected: {addr[0]}:{addr[1]} "
                f"(total {len(self._clients)})"
                + ("  [gop replay]" if replayed else ""))
        if not replayed:
            self._ask_keyframe()
        if phone_input:
            phone_input.on_admitted(c)            # tells the phone about touch

    def _remove(self, c):
        with self._lock:
            if c in self._clients:
                self._clients.remove(c)
            n = len(self._clients)
        if self.phone_input:
            self.phone_input.on_disconnect(c)   # lift fingers it left down
        log(f"client disconnected: {c.addr[0]}:{c.addr[1]} (total {n})")

    def close_all(self):
        with self._lock:
            clients = list(self._clients)
        for c in clients:
            c.close()

    def _on_client_message(self, client, frame_type, body):
        """Frames a phone sends: keyframe requests here, the rest is input."""
        if frame_type == FRAME_KEYFRAME_REQUEST:
            self.resync(client, "phone asked for a keyframe", rate_limit=True)
        elif self.phone_input:
            self.phone_input.on_message(client, frame_type, body)

    # ---- resync ------------------------------------------------------------ #

    def _start_locked(self, c, now):
        """Replay SPS+PPS+GOP to `c` and mark it started. False if there is
        nothing to replay (it then waits for the next IDR)."""
        if not (self.gop and self.sps and self.pps):
            return False
        c.push_replay(self.gop, now)
        c.started = True
        return True

    def _desync_locked(self, c, now):
        # resync_since only moves when the phone *starts* waiting: the app
        # re-sends KEYQ every ~1.5 s while it waits, and resetting the clock
        # each time would keep RESYNC_RESTART_AFTER from ever firing.
        if c.started:
            c.resync_since = now
        c.started = False
        c.last_keyframe_ask = now

    def resync(self, c, reason, rate_limit=False):
        """Restart `c` from a keyframe right away (KEYQ, decoder trouble)."""
        now = time.monotonic()
        with self._lock:
            if c not in self._clients:
                return
            if rate_limit:
                if now - c.last_keyq < KEYQ_MIN_INTERVAL:
                    return
                c.last_keyq = now
            c.drop_video()
            self._desync_locked(c, now)
            replayed = self._start_locked(c, now)
        log(f"client {c.addr[0]}: {reason}; "
            + ("replayed the current GOP" if replayed
               else "waiting for a keyframe"))
        if not replayed:
            self._ask_keyframe()

    def stream_restarted(self):
        """The encoder was restarted: old caches are invalid, and every phone
        restarts on the new stream's first SPS+PPS+IDR."""
        now = time.monotonic()
        with self._lock:
            self.sps = self.pps = self.gop = None
            self._gop_bytes = 0
            for c in self._clients:
                c.drop_video()
                self._desync_locked(c, now)

    def stream_corrupted(self):
        """A NAL we already sent turned out truncated: the GOP (and every
        phone's decoder state) may hold it. Resync everyone on a new IDR."""
        now = time.monotonic()
        with self._lock:
            self.gop = None
            self._gop_bytes = 0
            for c in self._clients:
                c.drop_video()          # it decodes from the broken frame
                self._desync_locked(c, now)
            waiting = bool(self._clients)
        if waiting:
            self._ask_keyframe()

    def tick(self):
        """Periodic (parser thread): keep after phones still waiting for a
        keyframe, which matters on a static desktop that makes no new IDR."""
        now = time.monotonic()
        ask = urgent = False
        with self._lock:
            for c in self._clients:
                if c.started:
                    continue
                if self._start_locked(c, now):
                    log(f"client {c.addr[0]} started from the GOP cache")
                    continue
                if now - c.last_keyframe_ask >= RESYNC_RETRY:
                    c.last_keyframe_ask = now
                    ask = True
                    urgent = urgent or (now - c.resync_since
                                        >= RESYNC_RESTART_AFTER
                                        and self.sps is not None)
        if ask:
            self._ask_keyframe(urgent)

    def _ask_keyframe(self, urgent=False):
        if self.keyframes:
            self.keyframes.request(urgent)

    # ---- video ------------------------------------------------------------- #

    def feed(self, nal_type, nal, first_slice):
        """Called per NAL by the parser thread."""
        if nal_type in NAL_DROP:
            return
        f = frame(FRAME_NAL, nal)
        keyframe = nal_type == NAL_IDR and first_slice
        now = time.monotonic()
        ask = False
        with self._lock:
            if nal_type == NAL_SPS:
                self.sps = f
            elif nal_type == NAL_PPS:
                self.pps = f
            if keyframe:
                self.gop = [x for x in (self.sps, self.pps) if x] + [f]
                self._gop_bytes = sum(len(x) for x in self.gop)
            elif self.gop is not None:
                self.gop.append(f)
                self._gop_bytes += len(f)
                if self._gop_bytes > MAX_GOP_CACHE:
                    self.gop = None          # too big to replay; wait for IDR

            for c in self._clients:
                if c.started:
                    if c.push_video(f, now):
                        continue
                    # Fell behind: back on "now" from the cache (which already
                    # holds `f`), or wait for the next IDR.
                    self._desync_locked(c, now)
                    if not self._start_locked(c, now):
                        ask = True
                elif keyframe and self._start_locked(c, now):
                    # A resyncing client restarts on a keyframe (the GOP is
                    # just SPS+PPS+this IDR), so its decoder is valid at once.
                    log(f"client {c.addr[0]} started on keyframe")
        if ask:
            self._ask_keyframe()


# --------------------------------------------------------------------------- #
# Annex-B parser: split the encoder's byte stream into NAL units
# --------------------------------------------------------------------------- #

START = b"\x00\x00\x01"
F_SETPIPE_SZ = 1031
F_GETPIPE_SZ = 1032
# How long the pipe must be idle before the buffered tail counts as a complete
# NAL. A NAL is only *known* complete when the next start code arrives, which
# would hold every frame back a whole frame interval; instead we use how
# encoders write: GStreamer's fdsink writes one access unit per write(), and
# ffmpeg (wf-recorder) writes a packet in full 32 KiB buffers plus a short
# final flush. Pipe writes that fit are atomic for the reader, so a read whose
# size isn't page-aligned ended on the end of a write = an access unit, and
# 1 ms of quiet is plenty. A page-aligned read is likely mid-packet (a full
# avio buffer, or a write that filled the pipe): wait longer before trusting
# the tail.
TAIL_QUIET = 0.001
TAIL_QUIET_ALIGNED = 0.030
PAGE = 4096
IDLE_TICK = 0.25                  # Broadcaster.tick() at least this often


def _nal_header(nal):
    """(nal_type, first_slice) for a NAL that still has its start code."""
    i = 3 if nal[2] == 1 else 4
    if len(nal) <= i:
        return -1, False
    t = nal[i] & 0x1F
    # first_mb_in_slice is ue(v); value 0 encodes as a single '1' bit, so the
    # first slice of a picture has the top bit of the slice header set.
    first = len(nal) > i + 1 and bool(nal[i + 1] & 0x80)
    return t, first


def _start_code_at_head(buf):
    """True / False if buf begins with a start code (after optional zero
    bytes), None if there aren't enough bytes yet to tell."""
    i = 0
    while i < len(buf) and buf[i] == 0:
        i += 1
    if i == len(buf):
        return None
    return i >= 2 and buf[i] == 1


class AnnexBSplitter:
    """Incremental Annex-B splitter: push() bytes, get complete NALs out.

    Pure (no I/O), so it is unit-testable. `on_nal(nal)` gets every NAL with
    its start code; `on_truncated()` is called when bytes arrive that
    continue a tail we already flushed as complete.
    """

    def __init__(self, on_nal, on_truncated=None):
        self.on_nal = on_nal
        self.on_truncated = on_truncated
        self.buf = bytearray()
        self._after_flush = False   # last NAL went out on a quiet pipe

    def has_tail(self):
        return len(self.buf) > 4 and _start_code_at_head(self.buf) is True

    def flush_tail(self):
        """The pipe went quiet: the buffered tail is a complete NAL."""
        if self.has_tail():
            self.on_nal(bytes(self.buf))
            self.buf.clear()
            self._after_flush = True

    def push(self, chunk):
        buf = self.buf
        buf += chunk
        if self._after_flush:
            head = _start_code_at_head(buf)
            if head is None:
                return                  # only zeros so far: can't tell yet
            self._after_flush = False
            if not head:
                # The flushed tail was cut short (writer paused mid-NAL). Its
                # rest has no start code: drop it, loudly, and resync.
                nxt = buf.find(START)
                lost = len(buf) if nxt < 0 else nxt
                log(f"warning: encoder output paused mid-frame; {lost} bytes "
                    "of a frame already sent were lost, resyncing")
                if self.on_truncated:
                    self.on_truncated()
        start = buf.find(START)
        if start < 0:
            del buf[:-3]                # keep a possible partial start code
            return
        if start > 0 and buf[start - 1] == 0:
            start -= 1                  # 4-byte start code
        search = start + 4
        while True:
            nxt = buf.find(START, search)
            if nxt < 0:
                break
            end = nxt - 1 if buf[nxt - 1] == 0 else nxt
            self.on_nal(bytes(buf[start:end]))
            start, search = end, nxt + 3
        del buf[:start]


def parse_annexb(stream, broadcaster, stop_evt, restart_evt=None):
    """Read raw H.264 and hand each NAL (start code included) to feed().

    Returns "stopped" (stop/restart requested) or "eof" (the encoder exited).
    """
    fd = stream.fileno()
    try:
        fcntl.fcntl(fd, F_SETPIPE_SZ, 1 << 20)   # whole AUs fit in one write
    except OSError as e:
        try:
            size = fcntl.fcntl(fd, F_GETPIPE_SZ)
        except OSError:
            size = 0
        if size:      # a pipe that stayed small (pipe-max-size, user quota)
            log(f"note: encoder pipe is {size // 1024} KiB ({e}); large "
                "frames arrive in pieces")

    def emit(nal):
        t, first = _nal_header(nal)
        broadcaster.feed(t, nal, first)

    splitter = AnnexBSplitter(emit, broadcaster.stream_corrupted)
    quiet = TAIL_QUIET
    last_tick = time.monotonic()
    while not stop_evt.is_set() and not (restart_evt and restart_evt.is_set()):
        if time.monotonic() - last_tick >= IDLE_TICK:
            broadcaster.tick()
            last_tick = time.monotonic()
        tail = splitter.has_tail()
        ready, _, _ = select.select([fd], [], [], quiet if tail else IDLE_TICK)
        if not ready:
            splitter.flush_tail()
            continue
        chunk = os.read(fd, 1 << 20)
        if not chunk:
            return "eof"
        quiet = TAIL_QUIET_ALIGNED if len(chunk) % PAGE == 0 else TAIL_QUIET
        splitter.push(chunk)
    return "stopped"


# --------------------------------------------------------------------------- #
# Capture supervisor
# --------------------------------------------------------------------------- #

RESTART_BACKOFF = (0.2, 0.5, 1.0, 2.0)
STABLE_RUN = 10.0            # a capture that ran this long resets the backoff
MAX_QUICK_FAILURES = 6       # this many short runs in a row: give up


def _stop_capture(backend):
    try:
        backend.stop_capture()
    except Exception as e:
        log(f"warning: stopping capture failed ({e})")


def _close_quietly(stream):
    try:
        if stream:
            stream.close()
    except OSError:
        pass


def supervise_capture(backend, stream, broadcaster, stop_evt, restart_evt):
    """Parse the encoder's output; restart the encoder if it exits.

    Encoders die mid-session for ordinary reasons (PipeWire renegotiates
    after a resolution / modifier change, a driver hiccup, a GPU reset). The
    virtual monitor, the listener and connected phones are kept; phones are
    resynced onto the new stream. Returns True when stop_evt is set, False
    after repeated quick failures (stop_evt is then set too, error logged).
    """
    quick_failures = 0
    while True:
        started = time.monotonic()
        reason = parse_annexb(stream, broadcaster, stop_evt, restart_evt)
        if stop_evt.is_set():
            return True
        if reason == "eof":
            log("warning: the encoder exited; restarting capture")
        restart_evt.clear()
        _stop_capture(backend)
        _close_quietly(stream)
        stream = None
        broadcaster.stream_restarted()
        if time.monotonic() - started >= STABLE_RUN or reason != "eof":
            quick_failures = 0
        while stream is None:
            if quick_failures >= MAX_QUICK_FAILURES:
                log("error: the encoder keeps failing; stopping (see the "
                    "encoder messages above)")
                stop_evt.set()
                return False
            delay = RESTART_BACKOFF[min(quick_failures,
                                        len(RESTART_BACKOFF) - 1)]
            quick_failures += 1
            if stop_evt.wait(delay):
                return True
            try:
                stream = backend.start_capture()
            except Exception as e:
                log(f"warning: could not restart capture ({e})")
                _stop_capture(backend)
                stream = None
            else:
                if stream is None:
                    log("warning: capture restart produced no stream")
        log("capture restarted")


# --------------------------------------------------------------------------- #
# TCP accept loop
# --------------------------------------------------------------------------- #

# accept() errors that only concern the one connection being accepted
ACCEPT_SKIP = {errno.ECONNABORTED, errno.EPROTO, errno.ENETDOWN,
               errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EHOSTDOWN,
               errno.ENOPROTOOPT, errno.EOPNOTSUPP, errno.EPERM,
               getattr(errno, "ENONET", -1)}
# resource exhaustion: back off a moment and keep serving
ACCEPT_PAUSE = {errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM}


def _peer(addr):
    """(host, port) with IPv4-mapped IPv6 shown as plain IPv4."""
    host = addr[0]
    if host.startswith("::ffff:") and "." in host:
        host = host[len("::ffff:"):]
    return host, addr[1]


ANY_HOST = ("0.0.0.0", "", "::")


def _listen_socket(host):
    """A listening-ready socket for `host`.

    The default (0.0.0.0, or "::") listens dual-stack on "::" with
    IPV6_V6ONLY off, so phones reach us over IPv4 *and* IPv6 (a hostname that
    resolves to an AAAA record first). Falls back to IPv4 where IPv6 is off.
    """
    if host in ANY_HOST and socket.has_ipv6:
        try:
            srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            srv.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            return srv, "::"
        except OSError:
            pass                       # IPv6 disabled in the kernel
    if host in ANY_HOST:
        host = "0.0.0.0"
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    return socket.socket(family, socket.SOCK_STREAM), host


def _bind_with_retries(host, port, stop_evt):
    """Bound socket, or None. A stale daemon may still hold the port briefly."""
    srv, bind_host = _listen_socket(host)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    for attempt in range(15):
        try:
            srv.bind((bind_host, port))
            return srv
        except OSError as e:
            if stop_evt.is_set():
                break
            if bind_host == "::" and e.errno in (errno.EADDRNOTAVAIL,
                                                 errno.EAFNOSUPPORT):
                srv.close()            # IPv6 disabled: plain IPv4
                srv, bind_host = (socket.socket(socket.AF_INET,
                                                socket.SOCK_STREAM), "0.0.0.0")
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                continue
            if attempt == 14:
                log(f"error: port {port} still in use after retries")
                stop_evt.set()
                break
            time.sleep(0.3)
    srv.close()
    return None


def accept_loop(host, port, broadcaster, gatekeeper, on_paired, stop_evt):
    srv = _bind_with_retries(host, port, stop_evt)
    if srv is None:
        return
    srv.listen(128)       # a burst queues here, not as dropped SYNs
    srv.settimeout(0.5)
    if host in ANY_HOST:
        host = ("every interface (IPv4 + IPv6)"
                if srv.family == socket.AF_INET6 else "every interface (IPv4)")
    log(f"listening on {host} port {port} (TLS, pairing required)")
    last_full_log = 0.0
    try:
        while not stop_evt.is_set():
            try:
                sock, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError as e:
                if e.errno in ACCEPT_SKIP:
                    continue
                if e.errno in ACCEPT_PAUSE:
                    log(f"warning: can't accept connections right now ({e})")
                    stop_evt.wait(0.1)
                    continue
                log(f"error: listener failed ({e})")
                stop_evt.set()       # visible to the TUI, unlike a dead thread
                break
            addr = _peer(addr)
            slot = gatekeeper.slots.acquire(addr[0])
            if slot is None:
                sock.close()      # this host (or everyone) has too many in flight
                now = time.monotonic()
                if now - last_full_log > 30:      # a flood must not flood the log
                    last_full_log = now
                    log(f"too many pending handshakes; dropping connections "
                        f"(latest from {addr[0]})")
                continue
            # Locked out after repeated failures: say so (a silent close looks
            # like a TLS failure to the app, i.e. "update Tandem"). It still
            # needs a handshake slot, so a flood can't take more than those.
            if gatekeeper.throttle.blocked(addr[0]):
                target, targs = refuse, (sock, addr, gatekeeper, LOCKED_OUT,
                                         slot)
            else:
                target, targs = admit, (sock, addr, broadcaster, gatekeeper,
                                        slot, on_paired)
            # The handshake waits on the phone; never let it stall accept().
            try:
                threading.Thread(target=target, args=targs, daemon=True,
                                 name=f"admit-{addr[0]}").start()
            except RuntimeError as e:     # can't start a thread
                gatekeeper.slots.release(slot)
                sock.close()
                log(f"warning: could not handle {addr[0]} ({e})")
    finally:
        srv.close()


# DENY reasons sent before any OKAY (the app shows them to the user)
PHONES_FULL = f"already {MAX_CLIENTS} phones connected to this computer"
LOCKED_OUT = ("too many failed pairing attempts from this phone; try again "
              "in a minute")


def deny_before_okay(sock, gatekeeper, reason):
    """Tell a phone why it can't connect, *before* any OKAY. True if told.

    The phone shows a DENY that answers its hello (that's how it learns about
    an outdated app or a wrong code), but ignores one after OKAY. So: TLS,
    read the hello, DENY. No pairing proof is exchanged; nothing is streamed.
    """
    deadline = time.monotonic() + secure_link.HANDSHAKE_TIMEOUT
    tls = None
    try:
        sock.settimeout(secure_link.FIRST_BYTE_TIMEOUT)
        if not sock.recv(1, socket.MSG_PEEK):
            return False              # the app's parallel connect: abandoned
        sock.settimeout(secure_link.HANDSHAKE_TIMEOUT)
        tls = gatekeeper.context.wrap_socket(sock, server_side=True)
        secure_link.read_json_frame(tls, deadline)
        secure_link.deny(tls, reason)
        return True
    except (secure_link.AuthError, OSError, ValueError):
        return False
    finally:
        for s in (tls, sock):
            try:
                if s:
                    s.close()
            except OSError:
                pass


def refuse(sock, addr, gatekeeper, reason, slot):
    """Thread target for a locked-out address: DENY, free the handshake slot.
    Not logged: a misbehaving device would flood the log."""
    try:
        deny_before_okay(sock, gatekeeper, reason)
    finally:
        gatekeeper.slots.release(slot)


def _printable(text, limit=32):
    """`text` from a peer, safe for a log line the TUI parses: no newlines or
    other control characters, nothing that could pass as one of our messages."""
    return "".join(c for c in str(text)
                   if c.isascii() and (c.isalnum() or c in " ._-+"))[:limit]


def admit(sock, addr, broadcaster, gatekeeper, slot, on_paired):
    """TLS + pairing proof, then hand the phone to the broadcaster.

    on_paired(display) learns the screen size ((w, h) or None) of every
    phone that passed the proof, before it is streamed to.
    """
    try:
        _tune_socket(sock)
        if broadcaster.full():
            if deny_before_okay(sock, gatekeeper, PHONES_FULL):
                log(f"client {addr[0]} refused: {MAX_CLIENTS} phones already "
                    "connected")
            return
        tls, hello = gatekeeper.handshake(sock)
    except secure_link.Abandoned:
        sock.close()              # the app's parallel connect: another address won
        return
    except Exception as e:
        # AuthError / OSError expected; anything else (a malformed hello the
        # parser didn't anticipate) is still just a failed handshake.
        try:
            sock.close()
        except OSError:
            pass
        # Only protocol failures count toward the lock-out (it exists to stop
        # guessing the pairing proof). A reset or a dropped Wi-Fi packet in
        # the middle of the handshake is the network, not an attacker: counting
        # it locked flaky phones out for a minute.
        transport = isinstance(e, OSError)     # AuthError is not an OSError
        locked = not transport and gatekeeper.throttle.failed(addr[0])
        if not isinstance(e, (secure_link.AuthError, OSError)):
            # Type only: the message can echo what the (unauthenticated) peer
            # sent, and the TUI reads these lines.
            e = f"malformed handshake ({type(e).__name__})"
        log(f"client {addr[0]} rejected: {e}"
            + (" (locked out for a minute)" if locked else ""))
        return
    finally:
        gatekeeper.slots.release(slot)
    gatekeeper.throttle.succeeded(addr[0])
    gatekeeper.slots.mark_known(addr[0])
    app = _printable(hello.app)
    screen = "{}x{}".format(*hello.display) if hello.display else ""
    log(f"client {addr[0]} paired"
        + (f" (app {app})" if app else "")
        + (f", screen {screen}" if screen else ""))
    on_paired(hello.display)
    try:
        broadcaster.add(tls, addr)
    except OSError as e:
        log(f"client {addr[0]} failed after pairing ({e})")


# --------------------------------------------------------------------------- #
# The virtual monitor
# --------------------------------------------------------------------------- #

FALLBACK_SIZE = (1920, 1080)        # an app too old to report its screen


class FirstPhone:
    """Waits for the first phone to pair; its screen sizes the monitor.

    offer() is called from the admission threads, wait() from main().
    `display` is that phone's (w, h), or None from an app too old to say.
    """

    def __init__(self):
        self.display = None
        self._paired = threading.Event()
        self._lock = threading.Lock()

    def offer(self, display):
        with self._lock:
            if not self._paired.is_set():
                self.display = display
                self._paired.set()

    def wait(self, stop_evt):
        """True once a phone paired, False if the daemon stopped first."""
        while not self._paired.wait(0.25):
            if stop_evt.is_set():
                return False
        return True


def parse_resolution(text):
    """`--resolution`: "auto" (None) or "WIDTHxHEIGHT"."""
    if text == "auto":
        return None
    try:
        w, h = (int(v) for v in text.lower().split("x"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not auto or WxH")
    if w <= 0 or h <= 0 or w % 2 or h % 2:
        raise argparse.ArgumentTypeError(f"{text!r}: need even, positive "
                                         "sizes")
    return w, h


class VirtualMonitor:
    """The virtual monitor and what lives on it: the backend's output, the
    encoder's stream and the touch device.

    close() undoes whatever start() got done, so it is safe after a start()
    that failed half-way, or one that never ran.
    """

    def __init__(self, backend, args):
        self.backend = backend
        self.args = args
        self.stream = None
        self.touch_surface = None
        self.phone_input = None
        self._created = False          # backend.setup() ran

    def start(self, size):
        """Create the monitor at `size` and start encoding it. Raises
        RuntimeError, with a message for the log, when that fails."""
        a = self.args
        a.width, a.height = size            # the backends read it from args
        log(f"virtual monitor: {a.width}x{a.height}")
        try:
            self.backend.setup()
        except Exception as e:
            raise RuntimeError(f"could not create the virtual monitor ({e})")
        self._created = True
        try:
            self.stream = self.backend.start_capture()
        except Exception as e:
            raise RuntimeError(f"could not start capture ({e})")
        self.touch_surface, problem = self._start_touch()
        self.phone_input = PhoneInput(touch_surface=self.touch_surface,
                                      unavailable_reason=problem)

    def _start_touch(self):
        """(surface, "") or (None, reason): the reason is shown on the
        phone, so it is phrased for the user."""
        if not self.args.touch:
            return None, "touch input is turned off on the computer"
        try:
            surface = self.backend.create_touch_surface()
        except Exception as e:
            log(f"touch input: unavailable ({e})")
            return None, str(e)
        log(f"touch input: ready ({surface.description})")
        return surface, ""

    def close(self, remove_output):
        """Stop the encoder and the touch device; with `remove_output`, take
        the monitor away too (otherwise the next daemon reuses it)."""
        if self._created:
            _stop_capture(self.backend)
        _close_quietly(self.stream)
        if self.phone_input:
            self.phone_input.close()
        if self.touch_surface:
            try:
                self.touch_surface.close()
            except Exception as e:
                log(f"warning: closing the touch device failed ({e})")
        if remove_output:
            # Also when start() never ran: a monitor kept by an earlier
            # daemon (--keep-output) is still ours to remove.
            self.backend.teardown()


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

PR_SET_PDEATHSIG = 1                # linux/prctl.h
PARENT_GONE_SIGNAL = signal.SIGUSR2


def exit_with_parent(parent_pid):
    """Get PARENT_GONE_SIGNAL when `parent_pid` (the TUI) dies, however it dies.

    The TUI starts us in our own session so terminal signals can't kill the
    encoder under us; the flip side is that a TUI killed outright (SIGKILL,
    OOM) would leave us streaming to a monitor nobody will ever remove.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_PDEATHSIG, int(PARENT_GONE_SIGNAL), 0, 0, 0):
            raise OSError(ctypes.get_errno(), "prctl failed")
    except (OSError, AttributeError) as e:
        log(f"note: can't follow the launcher's lifetime ({e})")
    if os.getppid() != parent_pid:      # it died before we asked
        os.kill(os.getpid(), PARENT_GONE_SIGNAL)

def main():
    # Raw text so --version prints the credit on its own line, unwrapped.
    ap = argparse.ArgumentParser(description="Tandem daemon",
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--version", action="version",
                    version=f"{about.CREDIT}\n{about.LICENSE}")
    ap.add_argument("--backend", default="auto",
                    choices=("auto",) + backends.NAMES,
                    help="compositor backend (auto-detected by default)")
    ap.add_argument("--output", default="EXT-1",
                    help="headless output name (Hyprland)")
    ap.add_argument("--resolution", type=parse_resolution, default=None,
                    metavar="auto|WxH",
                    help="virtual monitor size (default auto: the screen of "
                         "the first phone that pairs)")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--bitrate", default="12M")
    ap.add_argument("--codec", default="h264", choices=("h264", "hevc"),
                    help="video codec (the app decodes h264)")
    ap.add_argument("--encoder", default="auto",
                    help="GNOME: GStreamer element (vah264enc, x264enc, ...); "
                         "Hyprland: h264_nvenc or h264_vaapi (auto = the "
                         "GPU the compositor renders on)")
    ap.add_argument("--side", default="right",
                    choices=("right", "left", "up", "down"),
                    help="where the phone sits relative to the main screen")
    ap.add_argument("--quality", default="high",
                    help="quality preset label, reported to the phone")
    ap.add_argument("--banner-file", default=None,
                    help="JSON settings file; re-read on SIGUSR1 for live "
                         "side/scale changes")
    ap.add_argument("--position", default=None,
                    help="(deprecated, ignored) placement now follows --side")
    ap.add_argument("--gop", type=int, default=30,
                    help="keyframe interval in frames")
    ap.add_argument("--device", default="/dev/dri/renderD128",
                    help="VA-API render node (Hyprland backend)")
    ap.add_argument("--host", default="0.0.0.0",
                    help="address to listen on (default: every interface, "
                         "IPv4 and IPv6)")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--keep-output", action="store_true",
                    help="don't remove the Hyprland headless output on exit")
    ap.add_argument("--parent-pid", type=int, default=None,
                    help="stop when this process exits, and then remove the "
                         "output even with --keep-output (the TUI passes "
                         "its own PID)")
    ap.add_argument("--pairing-key-file", default=secure_link.PAIRING_KEY_PATH,
                    help="file holding the pairing key (created if missing); "
                         "only phones that prove they know it (from the QR "
                         "code) may connect")
    ap.add_argument("--tls-dir", default=secure_link.TLS_DIR,
                    help="directory with this computer's TLS certificate "
                         "(created if missing)")
    ap.add_argument("--no-touch", dest="touch", action="store_false",
                    help="don't accept touch input from phones")
    args = ap.parse_args()

    # Handlers first: a stop or reload that arrives during the (seconds-long)
    # setup must not kill us half-way with the default action. SIGHUP is a
    # stop too: it means our terminal went away.
    stop_evt = threading.Event()
    reload_evt = threading.Event()

    def shutdown(*_):
        """Signal handler: only flag the stop. The capture loop sees it within
        a fraction of a second and the `finally` below stops capture — waiting
        for the encoder here would stall the handler and push the TUI past its
        kill timeout, leaving wf-recorder orphaned on an output that is about
        to vanish."""
        if stop_evt.is_set():
            return
        log("shutting down")
        stop_evt.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGHUP, shutdown)
    signal.signal(signal.SIGUSR1, lambda *_: reload_evt.set())

    # The launcher died: nobody is left to reuse or remove the output.
    parent_gone = threading.Event()

    def on_parent_gone(*_):
        parent_gone.set()
        shutdown()

    if args.parent_pid:
        signal.signal(PARENT_GONE_SIGNAL, on_parent_gone)
        exit_with_parent(args.parent_pid)

    try:
        gatekeeper = secure_link.Gatekeeper.from_files(args.pairing_key_file,
                                                       args.tls_dir)
    except (RuntimeError, OSError, ssl.SSLError) as e:
        log(f"error: {e}")
        sys.exit(1)

    backend = backends.create(args.backend, args)
    missing = backend.missing()
    if missing:
        log(f"error: {backend.name} backend is missing: {', '.join(missing)}")
        sys.exit(1)
    log(f"backend: {backends.describe(backend.name)}")
    if args.codec != "h264":
        # Kept for experiments, but the NAL parsing here (GOP cache, resync)
        # and the Android app are H.264 only: say so instead of a black screen.
        log(f"warning: --codec {args.codec}: the Tandem app and this daemon's "
            "keyframe handling only support h264; phones will show nothing")

    kill_stale()
    broadcaster = Broadcaster()
    first_phone = FirstPhone()
    monitor = VirtualMonitor(backend, args)
    failed = False
    try:
        # Listen first: the monitor is created for the first phone that
        # pairs, at its screen size unless --resolution fixes one.
        threading.Thread(target=accept_loop,
                         args=(args.host, args.port, broadcaster, gatekeeper,
                               first_phone.offer, stop_evt),
                         daemon=True).start()
        if not first_phone.wait(stop_evt):
            return
        try:
            monitor.start(args.resolution or first_phone.display
                          or FALLBACK_SIZE)
        except RuntimeError as e:
            log(f"error: {e}")
            failed = True
            return
        if stop_evt.is_set():
            return

        restart_evt = threading.Event()
        broadcaster.attach(load_banner_payload(args, backend.name),
                           monitor.phone_input,
                           KeyframeSource(backend, restart_evt.set))

        def control_loop():
            """Apply SIGUSR1 reloads off the signal handler (D-Bus / hyprctl),
            and drop every phone when the pairing key is reset."""
            while not stop_evt.is_set():
                if gatekeeper.key_changed():
                    # "Forget paired phones": phones holding the old key must
                    # not keep watching / touching. They reconnect, fail the
                    # handshake and are told to scan the new QR code.
                    log("pairing key changed: disconnecting every phone")
                    broadcaster.close_all()
                if not reload_evt.wait(0.5):
                    continue
                reload_evt.clear()
                payload = load_banner_payload(args, backend.name)
                try:
                    cfg = json.loads(payload)
                    side = cfg.get("side", args.side)
                    scale = float(cfg.get("scale", args.scale))
                    if (side, scale) != (args.side, args.scale):
                        args.side, args.scale = side, scale
                        backend.apply_live(side, scale)
                except Exception as e:
                    log(f"warning: live reconfigure failed ({e})")
                broadcaster.set_banner(payload)
                log("config reloaded (live settings change)")

        threading.Thread(target=control_loop, daemon=True).start()

        failed = not supervise_capture(backend, monitor.stream, broadcaster,
                                       stop_evt, restart_evt)
    finally:
        shutdown()
        broadcaster.close_all()
        # A GNOME virtual monitor dies with our session anyway; only Hyprland's
        # headless output can outlive the daemon (the TUI uses that on restart,
        # so not once the TUI itself is gone).
        remove = (not args.keep_output or backend.name == "gnome"
                  or parent_gone.is_set())
        if remove and parent_gone.is_set():
            log("the launcher exited; removing the virtual monitor")
        monitor.close(remove_output=remove)
        log("stopped")
        if failed:
            sys.exit(1)


if __name__ == "__main__":
    main()
