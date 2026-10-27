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

Reliability rules (each one fixed a real "connects then drops" bug):
  * Every client has its own sender thread and a queue of *whole* frames. We
    only ever drop whole queued frames, never the tail of a half-sent one, so
    the framing on the wire can't desync.
  * A PING frame goes out after 1s of silence. The phone uses a read timeout,
    so a dead link is detected on both ends instead of hanging half-open.
  * The current GOP (SPS+PPS+IDR+P-frames since) is cached and replayed to new
    clients, so a phone joining a *static* desktop gets a picture immediately
    instead of waiting for damage that may never come.
  * The tail NAL is flushed as soon as the encoder pipe goes quiet, so the last
    frame of a burst isn't held hostage until the next frame arrives.

Transport is TCP on 0.0.0.0 (USB tethering, Wi-Fi, adb reverse all work),
wrapped in TLS. A connection receives nothing until it passes the pairing
handshake in secure_link.py (certificate fingerprint + mutual proof of the
pairing key from the QR code); see that module for the threat model.

Touch input flows the other way on the same connection: each client's I/O
thread hands the phone's frames (touches) to phone_input.PhoneInput, which
replays them on the backend's virtual touchscreen. See phone_input.py.
"""

import argparse
import fcntl
import json
import os
import select
import signal
import socket
import ssl
import sys
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


def log(*a):
    print("[tandemd]", *a, file=sys.stderr, flush=True)


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
#   NALU -> one Annex-B NAL (with start code), straight to the decoder
#   ASX1 -> JSON of the active settings, shown on the phone (any time)
#   PING -> empty keepalive, sent after HEARTBEAT seconds of silence
#   INPT -> JSON: is touch allowed for this phone? (phone_input.py)
# Before any of these, the handshake frames HELO / CHAL / AUTH / OKAY / DENY
# (secure_link.py). Phone -> PC touches (TUCH) are handled in phone_input.py.
# Unknown types are ignored on both ends, so new ones stay compatible.

FRAME_NAL = b"NALU"
FRAME_BANNER = b"ASX1"
FRAME_PING = b"PING"


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

    The TUI keeps --banner-file up to date and sends SIGHUP so live changes
    (side / scale) reach already-connected phones.
    """
    if args.banner_file:
        try:
            with open(args.banner_file, "rb") as f:
                data = json.loads(f.read() or b"{}")
            if data:
                data.setdefault("host", socket.gethostname())
                data.setdefault("backend", backend_name)
                return json.dumps(data).encode("utf-8")
        except (OSError, ValueError):
            pass
    return build_banner_payload(args, backend_name)


# --------------------------------------------------------------------------- #
# Clients and broadcaster
# --------------------------------------------------------------------------- #

HEARTBEAT = 1.0                    # seconds of silence before a PING
SEND_TIMEOUT = 5.0                 # a send blocked this long = dead peer
MAX_PENDING = 4 * 1024 * 1024      # queued bytes before a slow client resyncs
MAX_GOP_CACHE = 16 * 1024 * 1024   # stop caching a GOP that grows past this
MAX_CLIENTS = 4                    # phones streaming at once


class Client:
    """One admitted phone: a queue of whole frames to send, and the frames it
    sends back (touches), both driven by a single I/O thread.

    One thread, not a sender + a reader: an SSL connection must not be used
    by two threads at the same time (OpenSSL's rule; CPython releases the GIL
    inside SSL_read/SSL_write). The socket is non-blocking and the thread
    select()s on it plus a wake-up pipe that push() pokes.
    """

    def __init__(self, sock, addr, on_dead, on_message=None):
        self.sock = sock
        self.addr = addr
        self.started = False        # True once it has a keyframe to decode from
        self.alive = True
        self._q = deque()
        self._qbytes = 0
        self._lock = threading.Lock()
        self._on_dead = on_dead
        self._on_message = on_message
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)
        sock.setblocking(False)
        threading.Thread(target=self._io_loop, daemon=True,
                         name=f"client-{addr[0]}").start()

    def send_message(self, frame_type, body=b""):
        """Queue a control frame (not video) for this phone."""
        self.push(frame(frame_type, body))

    def push(self, data):
        with self._lock:
            if not self.alive:
                return
            self._q.append(data)
            self._qbytes += len(data)
            if self._qbytes > MAX_PENDING:
                # Too slow (weak Wi-Fi). Drop the queued frames — whole frames
                # only, the one in flight is already out of the queue — and
                # restart this client at the next keyframe.
                self._q.clear()
                self._qbytes = 0
                self.started = False
                log(f"client {self.addr[0]} fell behind; resync at next keyframe")
        self._wake()

    def close(self):
        with self._lock:
            self.alive = False
        self._wake()

    def _wake(self):
        try:
            os.write(self._wake_w, b"x")
        except (BlockingIOError, OSError):
            pass                     # already poked / closing

    def _next_frame(self):
        with self._lock:
            if not self._q:
                return None
            data = self._q.popleft()
            self._qbytes -= len(data)
            return data

    def _io_loop(self):
        out, sent = b"", 0           # frame being sent, bytes of it already out
        last_send = time.monotonic()
        inbuf = bytearray()
        try:
            while self.alive:
                if sent == len(out):
                    out, sent = self._next_frame() or b"", 0
                    if not out and time.monotonic() - last_send >= HEARTBEAT:
                        out = PING
                now = time.monotonic()
                if out:
                    if now - last_send > SEND_TIMEOUT:
                        raise OSError("send stalled")   # dead or stuck peer
                    wait = 0.5
                else:
                    wait = max(0.0, HEARTBEAT - (now - last_send))
                readable, writable, _ = select.select(
                    [self.sock, self._wake_r], [self.sock] if out else [], [], wait)
                if self._wake_r in readable:
                    try:
                        while os.read(self._wake_r, 4096):
                            pass
                    except BlockingIOError:
                        pass
                if self.sock in readable and not self._receive(inbuf):
                    break
                if out and self.sock in writable:
                    try:
                        sent += self.sock.send(memoryview(out)[sent:])
                        last_send = time.monotonic()
                    except (ssl.SSLWantWriteError, ssl.SSLWantReadError):
                        pass         # buffer full / TLS wants a read first
        except OSError:
            pass
        finally:
            with self._lock:
                self.alive = False
                self._q.clear()
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
        """Read everything available and dispatch whole frames.

        False when the phone closed the connection or broke the protocol.
        """
        while True:
            try:
                chunk = self.sock.recv(65536)
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                break
            if not chunk:
                return False
            inbuf += chunk
        while len(inbuf) >= 8:
            length = int.from_bytes(inbuf[4:8], "big")
            if length > MAX_INCOMING_FRAME:
                log(f"client {self.addr[0]} sent an oversized frame; "
                    "disconnecting it")
                return False
            if len(inbuf) < 8 + length:
                break
            frame_type, body = bytes(inbuf[:4]), bytes(inbuf[8:8 + length])
            del inbuf[:8 + length]
            if self._on_message:
                self._on_message(self, frame_type, body)
        return True


def _tune_socket(sock):
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for opt, val in (("TCP_KEEPIDLE", 3), ("TCP_KEEPINTVL", 1),
                     ("TCP_KEEPCNT", 3)):
        if hasattr(socket, opt):
            sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
    # Keep the kernel send buffer modest: a huge one hides backpressure and
    # turns into latency instead of a resync.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 512 * 1024)


class Broadcaster:
    """Fans NAL frames out to every client; owns the SPS/PPS/GOP caches."""

    def __init__(self, banner_payload=b"", phone_input=None):
        self._clients = []
        self.phone_input = phone_input      # handles HELO / TUCH, or None
        self._lock = threading.Lock()
        self.sps = None             # framed
        self.pps = None             # framed
        self.gop = None             # framed NALs since the last IDR, or None
        self._gop_bytes = 0
        self.banner_payload = banner_payload

    def set_banner(self, payload):
        with self._lock:
            self.banner_payload = payload
            clients = list(self._clients)
        for c in clients:
            c.push(frame(FRAME_BANNER, payload))

    def add(self, sock, addr):
        """Start streaming to a phone that passed the pairing handshake."""
        with self._lock:
            if len(self._clients) >= MAX_CLIENTS:
                secure_link.deny(sock, f"already {MAX_CLIENTS} phones connected")
                sock.close()
                log(f"client {addr[0]} refused: {MAX_CLIENTS} phones already "
                    "connected")
                return
            c = Client(sock, addr, self._remove,
                       self.phone_input.on_message if self.phone_input else None)
            c.push(frame(FRAME_BANNER, self.banner_payload))
            if self.gop:
                # Replay the current GOP: the decoder catches up to "now"
                # instantly, even if the desktop is static.
                for f in self.gop:
                    c.push(f)
                c.started = True
            self._clients.append(c)
            n = len(self._clients)
        if self.phone_input:
            self.phone_input.on_admitted(c)       # tells the phone about touch
        log(f"client connected: {addr[0]}:{addr[1]} (total {n})"
            + ("  [gop replay]" if c.started else ""))

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

    def feed(self, nal_type, nal, first_slice):
        """Called per NAL by the parser thread."""
        if nal_type in NAL_DROP:
            return
        f = frame(FRAME_NAL, nal)
        keyframe = nal_type == NAL_IDR and first_slice
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
                    self.gop = None          # too big; joiners wait for an IDR

            for c in self._clients:
                if c.started:
                    c.push(f)
                elif keyframe and self.sps and self.pps:
                    # A resyncing client restarts on a keyframe, prefixed with
                    # the parameter sets so its decoder is valid immediately.
                    c.push(self.sps)
                    c.push(self.pps)
                    c.push(f)
                    c.started = True
                    log(f"client {c.addr[0]} started on keyframe")


# --------------------------------------------------------------------------- #
# Annex-B parser: split the encoder's byte stream into NAL units
# --------------------------------------------------------------------------- #

START = b"\x00\x00\x01"
F_SETPIPE_SZ = 1031
# How long the pipe must be idle before the buffered tail counts as a complete
# NAL. Encoders write an access unit as back-to-back write()s (microseconds
# apart), and a frame interval is >= 8 ms even at 120 fps.
TAIL_QUIET = 0.005


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


def parse_annexb(stream, broadcaster, stop_evt):
    """Read raw H.264 and hand each NAL (start code included) to feed().

    A NAL is complete once the next start code shows up. Encoders write whole
    access units per write(), so when the pipe goes quiet for a moment the
    buffered tail is a complete NAL too — we flush it right away instead of
    holding the last frame of a burst until the next frame arrives.
    """
    fd = stream.fileno()
    try:
        fcntl.fcntl(fd, F_SETPIPE_SZ, 1 << 20)   # whole AUs fit in one write
    except OSError:
        pass

    def emit(nal):
        t, first = _nal_header(nal)
        broadcaster.feed(t, nal, first)

    buf = bytearray()
    while not stop_evt.is_set():
        has_tail = buf.startswith(START) or buf.startswith(b"\x00" + START)
        ready, _, _ = select.select([fd], [], [], TAIL_QUIET if has_tail else 0.5)
        if not ready:
            if has_tail and len(buf) > 4:
                emit(bytes(buf))
                buf.clear()
            continue
        chunk = os.read(fd, 1 << 20)
        if not chunk:
            break
        buf += chunk

        start = buf.find(START)
        if start < 0:
            del buf[:-3]                # keep a possible partial start code
            continue
        if start > 0 and buf[start - 1] == 0:
            start -= 1                  # 4-byte start code
        search = start + 4
        while True:
            nxt = buf.find(START, search)
            if nxt < 0:
                break
            end = nxt - 1 if buf[nxt - 1] == 0 else nxt
            emit(bytes(buf[start:end]))
            start, search = end, nxt + 3
        del buf[:start]


# --------------------------------------------------------------------------- #
# TCP accept loop
# --------------------------------------------------------------------------- #

def accept_loop(host, port, broadcaster, gatekeeper, stop_evt):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    for attempt in range(15):
        try:
            srv.bind((host, port))
            break
        except OSError:
            if stop_evt.is_set():
                return
            if attempt == 14:
                log(f"error: port {port} still in use after retries")
                stop_evt.set()
                return
            time.sleep(0.3)
    srv.listen(8)
    srv.settimeout(0.5)
    log(f"listening on {host}:{port} (TLS, pairing required)")
    while not stop_evt.is_set():
        try:
            sock, addr = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        if gatekeeper.throttle.blocked(addr[0]):
            sock.close()              # locked out after repeated failures
            continue
        if not gatekeeper.pending.acquire(blocking=False):
            sock.close()              # too many handshakes in flight
            continue
        # The handshake waits on the phone; never let it stall accept().
        threading.Thread(target=admit, daemon=True, name=f"admit-{addr[0]}",
                         args=(sock, addr, broadcaster, gatekeeper)).start()
    srv.close()


def admit(sock, addr, broadcaster, gatekeeper):
    """TLS + pairing proof, then hand the phone to the broadcaster."""
    try:
        _tune_socket(sock)
        tls, app = gatekeeper.handshake(sock)
    except secure_link.Abandoned:
        sock.close()              # the app's parallel connect: another address won
        return
    except (secure_link.AuthError, OSError) as e:
        try:
            sock.close()
        except OSError:
            pass
        locked = gatekeeper.throttle.failed(addr[0])
        log(f"client {addr[0]} rejected: {e}"
            + (" (locked out for a minute)" if locked else ""))
        return
    finally:
        gatekeeper.pending.release()
    gatekeeper.throttle.succeeded(addr[0])
    log(f"client {addr[0]} paired" + (f" (app {app})" if app else ""))
    try:
        broadcaster.add(tls, addr)
    except OSError as e:
        log(f"client {addr[0]} failed after pairing ({e})")


# --------------------------------------------------------------------------- #
# Touch input
# --------------------------------------------------------------------------- #

def start_touch(args, backend):
    """Create the backend's virtual touchscreen.

    Returns (surface, "") on success, or (None, reason) — the reason is shown
    on the phone, so it is phrased for the user.
    """
    if not args.touch:
        return None, "touch input is turned off on the computer"
    try:
        surface = backend.create_touch_surface()
    except Exception as e:
        log(f"touch input: unavailable ({e})")
        return None, str(e)
    log(f"touch input: ready ({surface.description})")
    return surface, ""


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

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
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
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
                    help="JSON settings file; re-read on SIGHUP for live "
                         "side/scale changes")
    ap.add_argument("--position", default=None,
                    help="(deprecated, ignored) placement now follows --side")
    ap.add_argument("--gop", type=int, default=30,
                    help="keyframe interval in frames")
    ap.add_argument("--device", default="/dev/dri/renderD128",
                    help="VA-API render node (Hyprland backend)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--keep-output", action="store_true",
                    help="don't remove the Hyprland headless output on exit")
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

    kill_stale()
    try:
        backend.setup()
    except Exception as e:
        log(f"error: could not create the virtual monitor ({e})")
        sys.exit(1)

    stop_evt = threading.Event()
    reload_evt = threading.Event()

    try:
        stream = backend.start_capture()
    except Exception as e:
        log(f"error: could not start capture ({e})")
        backend.teardown()
        sys.exit(1)

    touch_surface, touch_problem = start_touch(args, backend)
    phone_input = PhoneInput(touch_surface=touch_surface,
                             unavailable_reason=touch_problem)
    broadcaster = Broadcaster(load_banner_payload(args, backend.name),
                              phone_input)

    threading.Thread(target=accept_loop,
                     args=(args.host, args.port, broadcaster, gatekeeper,
                           stop_evt),
                     daemon=True).start()

    def control_loop():
        """Apply SIGHUP reloads off the signal handler (D-Bus / hyprctl), and
        drop every phone when the pairing key is reset."""
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

    def shutdown(*_):
        """Signal handler: only flag the stop. The parser loop sees it within
        0.5 s and the `finally` below stops capture — waiting for the encoder
        here would stall the handler and push the TUI past its kill timeout,
        leaving wf-recorder orphaned on an output that is about to vanish."""
        if stop_evt.is_set():
            return
        log("shutting down")
        stop_evt.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGHUP, lambda *_: reload_evt.set())

    try:
        parse_annexb(stream, broadcaster, stop_evt)
    finally:
        shutdown()
        backend.stop_capture()
        broadcaster.close_all()
        if touch_surface:
            touch_surface.close()
        # A GNOME virtual monitor dies with our session anyway; only Hyprland's
        # headless output can outlive the daemon (the TUI uses that on restart).
        if not args.keep_output or backend.name == "gnome":
            backend.teardown()
        log("stopped")


if __name__ == "__main__":
    main()
