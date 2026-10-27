"""
secure_link.py - who may connect, and how the connection is protected.

The daemon listens on every interface, so anything on the same Wi-Fi (or a
USB-tethered link, or another account on this PC via adb's 127.0.0.1) can open
a TCP connection to it. Before this module, that was enough to *watch* the
screen, and the pairing key, the video and every touch crossed the network in
clear text. Now a connection gets nothing until it has proved it is a phone
that scanned this computer's QR code:

  1. TLS (1.2+, AEAD ciphers only) on the raw TCP connection, with a
     self-signed certificate created on this computer on first use. The video,
     the settings and the touches are encrypted and tamper-proof.

  2. The QR code carries the certificate's SHA-256 fingerprint (`f`), so the
     phone refuses a computer presenting any other certificate: someone on the
     network can't pretend to be your computer (man-in-the-middle).

  3. Mutual proof of the pairing key (`k`, also in the QR), bound to that
     certificate:

        phone -> PC   HELO  {"v": 2, "nonce": nc, "app": version}
        PC -> phone   CHAL  {"v": 2, "nonce": ns}
        phone -> PC   AUTH  {"proof": HMAC(key, "tandem-v2 client" | fp | nc | ns)}
        PC -> phone   OKAY  {"proof": HMAC(key, "tandem-v2 server" | fp | nc | ns)}
                   or DENY  {"reason": "..."} and the connection is closed

     nc / ns are 32 fresh random bytes per connection, so a recorded exchange
     can't be replayed, and the key itself never crosses the network. `fp`
     ties the proof to the certificate the phone actually saw: a relay with a
     different certificate gets a proof the real computer rejects. This also
     protects a phone that typed the address and pairing code by hand (no
     fingerprint to pin). The phone only accepts the stream after checking the
     PC's proof, so a look-alike computer can't receive its touches either.

Only after OKAY does the daemon send the settings banner, video, or accept
touches. Wrong code, wrong protocol, or too slow: DENY / close, logged, and
counted by FailureThrottle, which locks out an address that keeps failing.

The pairing key is 20 characters of Crockford base32 from the OS's CSPRNG
(`secrets`): 100 random bits, nothing derived from the hostname, time or
anything else someone could recompute. It is stored 0600 and never put on a
command line. Typed by hand it is shown as `xxxxx-xxxxx-xxxxx-xxxxx`; case,
dashes and the look-alikes i/l -> 1, o -> 0 don't matter.

Same framing as the stream (4-byte type + 4-byte length + body). Mirrored in
the Android app's SecureChannel.kt.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import threading
import time
from collections import deque

CONFIG_DIR = os.path.expanduser("~/.config/tandem")
# Before the rename to Tandem the config lived here. Moving it keeps the
# settings, the pairing key and the TLS certificate.
OLD_CONFIG_DIR = os.path.expanduser("~/.config/archscreenextender")
if os.path.isdir(OLD_CONFIG_DIR) and not os.path.exists(CONFIG_DIR):
    try:
        os.rename(OLD_CONFIG_DIR, CONFIG_DIR)
    except OSError:
        pass
PAIRING_KEY_PATH = os.path.join(CONFIG_DIR, "pairing-key")
TLS_DIR = os.path.join(CONFIG_DIR, "tls")

PROTOCOL_VERSION = 2

FRAME_HELLO = b"HELO"
FRAME_CHALLENGE = b"CHAL"
FRAME_AUTH = b"AUTH"
FRAME_OK = b"OKAY"
FRAME_DENY = b"DENY"

NONCE_BYTES = 32
HANDSHAKE_TIMEOUT = 6.0          # TLS + pairing proof, whole thing
MAX_HANDSHAKE_FRAME = 1024       # nothing in the handshake is bigger

# Crockford base32: no i, l, o, u, so nothing reads as another character.
KEY_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
KEY_LENGTH = 20                  # 20 x 5 bits = 100 random bits
_LOOKALIKES = str.maketrans({"i": "1", "l": "1", "o": "0"})


class AuthError(Exception):
    """The connecting side failed the handshake. str() is safe to log."""


class Abandoned(Exception):
    """The peer closed the connection before sending a single byte.

    Expected, not an attack: the app connects to every address in the QR at
    once and closes all but the first to answer. Not logged, not counted by
    FailureThrottle (otherwise a phone's own second address would get locked
    out after a few reconnects)."""


# --------------------------------------------------------------------------- #
# Pairing key
# --------------------------------------------------------------------------- #

def new_pairing_key() -> str:
    return "".join(secrets.choice(KEY_ALPHABET) for _ in range(KEY_LENGTH))


def normalize_key(text: str) -> str:
    """What the user typed -> canonical key (case, dashes, look-alikes)."""
    cleaned = "".join(ch for ch in text.lower() if ch.isalnum())
    return cleaned.translate(_LOOKALIKES)


def is_valid_key(key: str) -> bool:
    return len(key) == KEY_LENGTH and all(ch in KEY_ALPHABET for ch in key)


def display_key(key: str) -> str:
    """'abcde-fghjk-mnpqr-stvwx': easier to read out and type."""
    return "-".join(key[i:i + 5] for i in range(0, len(key), 5))


def load_pairing_key(path=PAIRING_KEY_PATH):
    """The key stored at `path`, or None if there isn't a valid one."""
    try:
        with open(path) as f:
            key = normalize_key(f.read())
    except OSError:
        return None
    return key if is_valid_key(key) else None


def ensure_pairing_key(path=PAIRING_KEY_PATH) -> str:
    """Read the key, creating it (mode 0600) when missing or in an old format.

    Keys from versions before 0.5 (base64, sent in clear text) are replaced: phones have
    to scan the new QR anyway, as they need the certificate fingerprint too.
    """
    key = load_pairing_key(path)
    if key:
        return key
    key = new_pairing_key()
    _write_private(path, key + "\n")
    return key


def reset_pairing_key(path=PAIRING_KEY_PATH) -> str:
    """Forget every paired phone: each must scan the new QR code."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    return ensure_pairing_key(path)


def _write_private(path, text):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)     # O_CREAT's mode doesn't apply to an existing file


# --------------------------------------------------------------------------- #
# Certificate
# --------------------------------------------------------------------------- #

def certificate_paths(tls_dir=TLS_DIR):
    return os.path.join(tls_dir, "cert.pem"), os.path.join(tls_dir, "key.pem")


def ensure_certificate(tls_dir=TLS_DIR):
    """(cert_path, key_path), creating a self-signed P-256 certificate on first
    use. Nobody vouches for it (no CA): phones trust it because its
    fingerprint is in the QR code, and the pairing proof is bound to it."""
    cert, key = certificate_paths(tls_dir)
    if os.path.exists(cert) and os.path.exists(key):
        return cert, key
    openssl = shutil.which("openssl")
    if not openssl:
        raise RuntimeError("openssl is needed to create the encryption "
                           "certificate (install the 'openssl' package)")
    os.makedirs(tls_dir, mode=0o700, exist_ok=True)
    os.chmod(tls_dir, 0o700)
    tmp_cert, tmp_key = cert + ".tmp", key + ".tmp"
    old_umask = os.umask(0o077)               # the private key is born 0600
    try:
        subprocess.run(
            [openssl, "req", "-x509", "-newkey", "ec",
             "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
             "-keyout", tmp_key, "-out", tmp_cert, "-days", "3650",
             "-subj", "/CN=Tandem " + socket.gethostname()[:40]],
            check=True, capture_output=True, timeout=30)
    except (subprocess.SubprocessError, OSError) as e:
        raise RuntimeError(f"could not create the encryption certificate ({e})")
    finally:
        os.umask(old_umask)
    os.replace(tmp_key, key)
    os.replace(tmp_cert, cert)
    return cert, key


def certificate_fingerprint(cert_path) -> bytes:
    """SHA-256 of the certificate (DER), as the phone computes it."""
    with open(cert_path) as f:
        der = ssl.PEM_cert_to_DER_cert(f.read())
    return hashlib.sha256(der).digest()


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    text = str(text)
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def server_context(cert_path, key_path) -> ssl.SSLContext:
    """TLS 1.2+ with forward secrecy and AEAD ciphers only (TLS 1.2 is the
    newest Android 8 and 9 speak; 1.3 is negotiated whenever possible)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20")
    ctx.options |= ssl.OP_NO_COMPRESSION | ssl.OP_NO_TICKET
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


# --------------------------------------------------------------------------- #
# Pairing proof
# --------------------------------------------------------------------------- #

def _proof(role, key, fingerprint, client_nonce, server_nonce):
    msg = (b"tandem-v2 " + role + b"\x00" + fingerprint
           + client_nonce + server_nonce)
    return hmac.new(key.encode(), msg, hashlib.sha256).digest()


def client_proof(key, fingerprint, client_nonce, server_nonce):
    return _proof(b"client", key, fingerprint, client_nonce, server_nonce)


def server_proof(key, fingerprint, client_nonce, server_nonce):
    return _proof(b"server", key, fingerprint, client_nonce, server_nonce)


# --------------------------------------------------------------------------- #
# Lock-out for addresses that keep failing
# --------------------------------------------------------------------------- #

class FailureThrottle:
    """After `limit` failed handshakes from one address within `window`
    seconds, refuse that address for `lockout` seconds. The key is far too
    long to guess; this keeps a misbehaving device from filling the log and
    tying up handshake slots."""

    def __init__(self, limit=5, window=60.0, lockout=60.0):
        self.limit, self.window, self.lockout = limit, window, lockout
        self._failures = {}             # ip -> deque of timestamps
        self._locked_until = {}         # ip -> time
        self._lock = threading.Lock()

    def blocked(self, ip) -> bool:
        with self._lock:
            until = self._locked_until.get(ip)
            if until is None:
                return False
            if time.monotonic() < until:
                return True
            del self._locked_until[ip]
            return False

    def failed(self, ip) -> bool:
        """Record a failure; True if `ip` is now locked out."""
        now = time.monotonic()
        with self._lock:
            times = self._failures.setdefault(ip, deque())
            times.append(now)
            while times and now - times[0] > self.window:
                times.popleft()
            if len(times) >= self.limit:
                times.clear()
                self._locked_until[ip] = now + self.lockout
                return True
            return False

    def succeeded(self, ip):
        with self._lock:
            self._failures.pop(ip, None)


# --------------------------------------------------------------------------- #
# Server side of the handshake
# --------------------------------------------------------------------------- #

class Gatekeeper:
    """Everything the accept loop needs to admit (or refuse) a connection.

    The pairing key is re-read from its file for every handshake, so
    "forget paired phones" in the TUI takes effect without a restart
    (key_changed() lets the daemon also drop phones already connected).
    """

    MAX_PENDING_HANDSHAKES = 8

    def __init__(self, key_path, cert_path, cert_key_path):
        self.key_path = key_path
        self.context = server_context(cert_path, cert_key_path)
        self.fingerprint = certificate_fingerprint(cert_path)
        self.throttle = FailureThrottle()
        self.pending = threading.BoundedSemaphore(self.MAX_PENDING_HANDSHAKES)
        self._admitted_key = ensure_pairing_key(key_path)

    @classmethod
    def from_files(cls, key_path=PAIRING_KEY_PATH, tls_dir=TLS_DIR):
        cert, cert_key = ensure_certificate(tls_dir)
        return cls(key_path, cert, cert_key)

    def current_key(self):
        return load_pairing_key(self.key_path)

    def key_changed(self) -> bool:
        """True once after the key file changed (or vanished)."""
        key = self.current_key()
        if key == self._admitted_key:
            return False
        self._admitted_key = key
        return True

    def handshake(self, raw_sock):
        """TLS + mutual pairing proof on a freshly accepted socket.

        Returns (tls_socket, app_version) — blocking mode, ready for the
        stream — or raises AuthError / OSError. On AuthError the phone has
        already been told why (DENY) where possible.
        """
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT
        raw_sock.settimeout(HANDSHAKE_TIMEOUT)
        try:
            if not raw_sock.recv(1, socket.MSG_PEEK):
                raise Abandoned()
        except socket.timeout:
            raise AuthError("sent nothing (no TLS hello)")
        raw_sock.settimeout(max(0.1, deadline - time.monotonic()))
        try:
            tls = self.context.wrap_socket(raw_sock, server_side=True)
        except ssl.SSLError as e:
            if "CERTIFICATE_UNKNOWN" in str(e.reason):
                # The phone's QR pinned a different certificate.
                raise AuthError("the phone doesn't recognise this computer's "
                                "certificate (paired elsewhere, or the "
                                "certificate was recreated: rescan the QR)")
            # Commonly an app older than 0.5 speaking plain TCP.
            raise AuthError(f"TLS handshake failed ({e.reason or e}); "
                            "old app version?")
        try:
            return tls, self._prove(tls, deadline)
        except Exception:
            try:
                tls.close()
            except OSError:
                pass
            raise

    def _prove(self, tls, deadline):
        ftype, hello = read_json_frame(tls, deadline)
        if ftype != FRAME_HELLO or int(hello.get("v", 0)) < PROTOCOL_VERSION:
            deny(tls, "update the Tandem app on this phone")
            raise AuthError("outdated app (no v2 hello)")
        try:
            client_nonce = b64url_decode(hello.get("nonce", ""))
        except ValueError:
            client_nonce = b""
        if len(client_nonce) != NONCE_BYTES:
            deny(tls, "invalid hello")
            raise AuthError("malformed hello")

        server_nonce = secrets.token_bytes(NONCE_BYTES)
        write_frame(tls, FRAME_CHALLENGE, json.dumps(
            {"v": PROTOCOL_VERSION, "nonce": b64url(server_nonce)}).encode())

        ftype, auth = read_json_frame(tls, deadline)
        try:
            offered = b64url_decode(auth.get("proof", ""))
        except ValueError:
            offered = b""
        key = self.current_key()
        if not key:
            deny(tls, "the computer has no pairing code - restart tandem")
            raise AuthError("no pairing key on this computer")
        expected = client_proof(key, self.fingerprint,
                                client_nonce, server_nonce)
        if ftype != FRAME_AUTH or not hmac.compare_digest(offered, expected):
            deny(tls, "wrong pairing code - scan the QR code on the "
                      "computer again")
            raise AuthError("wrong pairing code")

        write_frame(tls, FRAME_OK, json.dumps({"proof": b64url(server_proof(
            key, self.fingerprint, client_nonce, server_nonce))}).encode())
        return str(hello.get("app", ""))[:32]


def write_frame(sock, ftype, body=b""):
    sock.sendall(ftype + len(body).to_bytes(4, "big") + body)


def deny(sock, reason):
    try:
        write_frame(sock, FRAME_DENY, json.dumps({"reason": reason}).encode())
    except OSError:
        pass


def read_json_frame(sock, deadline):
    """(type, dict) of one small handshake frame, honouring `deadline`."""
    header = _recv_exact(sock, 8, deadline)
    length = int.from_bytes(header[4:], "big")
    if length > MAX_HANDSHAKE_FRAME:
        raise AuthError("oversized handshake frame")
    body = _recv_exact(sock, length, deadline) if length else b""
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        raise AuthError("malformed handshake frame")
    if not isinstance(data, dict):
        raise AuthError("malformed handshake frame")
    return header[:4], data


def _recv_exact(sock, length, deadline):
    data = bytearray()
    while len(data) < length:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AuthError("handshake timed out")
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(length - len(data))
        except socket.timeout:
            raise AuthError("handshake timed out")
        if not chunk:
            raise AuthError("closed during handshake")
        data += chunk
    return bytes(data)
