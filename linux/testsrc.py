#!/usr/bin/env python3
"""
testsrc.py - stream a synthetic test pattern with the real daemon code.

Exercises the exact wire protocol, broadcaster, heartbeats and GOP replay of
tandemd.py, but the video comes from GStreamer's videotestsrc instead of a
virtual monitor — so you can test the phone app (or the emulator, which
reaches this PC at 10.0.2.2) without touching your desktop layout.

    python3 testsrc.py [--width 2400 --height 1080 --fps 60 --port 5000]
    python3 testsrc.py --static      # one frame then silence: tests GOP replay
    python3 testsrc.py --log-touch   # print the phone's touches

Phones connect exactly as to the real daemon: TLS + this computer's pairing
key (scan the QR / type the code printed at start-up).

Needs gst-launch-1.0 with x264enc or openh264enc (see backends/gnome.py).
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402
import tandemd  # noqa: E402
from backends import gnome  # noqa: E402
import secure_link  # noqa: E402
from phone_input import PhoneInput  # noqa: E402


class LoggingTouchscreen:
    """Stands in for a real touchscreen: prints each touch it receives."""

    slot_count = 10
    description = "touches are printed, not injected"

    def down(self, slot, x, y):
        tandemd.log(f"touch down  slot {slot}  x={x:.3f} y={y:.3f}")

    def move(self, slot, x, y):
        tandemd.log(f"touch move  slot {slot}  x={x:.3f} y={y:.3f}")

    def up(self, slot):
        tandemd.log(f"touch up    slot {slot}")

    def close(self):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--pattern", default="smpte",
                    help="videotestsrc pattern (smpte, ball, bar, …)")
    ap.add_argument("--static", action="store_true",
                    help="send 2 seconds of video, then keep the stream idle")
    ap.add_argument("--log-touch", action="store_true",
                    help="accept touches from paired phones and print them")
    args = ap.parse_args()

    enc = gnome.pick_encoder()
    if not enc:
        sys.exit("no GStreamer H.264 encoder (install gst-plugins-ugly or openh264)")
    r, w = os.pipe()
    frames = f"num-buffers={args.fps * 2} " if args.static else ""
    desc = (f"videotestsrc is-live=true {frames}pattern={args.pattern} "
            f"! video/x-raw,width={args.width},height={args.height},"
            f"framerate={args.fps}/1 "
            f"! timeoverlay FONT "
            f"! {gnome.encoder_chain(enc, 8000, 30)} "
            f"! h264parse config-interval=-1 "
            f"! video/x-h264,stream-format=byte-stream,alignment=au "
            f"! fdsink fd={w} sync=false")
    # gst-launch joins argv with spaces and re-parses, so a quoted value with a
    # space must travel as one argv element.
    cmd = ["gst-launch-1.0", "-q"] + [
        'font-desc="Sans 40"' if part == "FONT" else part for part in desc.split()]
    if args.static:
        # keep the pipe open after gst exits so the stream goes quiet, not EOF
        _keep_open = os.dup(w)  # noqa: F841
    proc = subprocess.Popen(cmd, pass_fds=(w,), stdin=subprocess.DEVNULL)
    os.close(w)

    banner = json.dumps({
        "side": "right", "width": args.width, "height": args.height,
        "fps": args.fps, "scale": 1.0, "quality": "test", "bitrate": "8M",
        "codec": "h264", "gop": 30, "host": socket.gethostname(),
        "backend": "testsrc"}).encode()
    phone_input = PhoneInput(
        touch_surface=LoggingTouchscreen() if args.log_touch else None,
        unavailable_reason="this is the test pattern (start it with "
                           "--log-touch to print touches)")
    cfg = core.load_config()
    cfg.port = args.port
    tandemd.log(f"pairing QR: {core.connect_uri(cfg)}")
    tandemd.log(f"pairing code: {core.pairing_code()}")
    gatekeeper = secure_link.Gatekeeper.from_files()
    b = tandemd.Broadcaster(banner, phone_input)
    stop = threading.Event()
    threading.Thread(target=tandemd.accept_loop,
                     args=("0.0.0.0", args.port, b, gatekeeper, stop),
                     daemon=True).start()
    tandemd.log(f"test pattern {args.width}x{args.height}@{args.fps} via {enc}"
                  f"{' (static)' if args.static else ''}; Ctrl+C to stop")
    def shutdown(*_):
        stop.set()
        proc.terminate()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        tandemd.parse_annexb(os.fdopen(r, "rb", buffering=0), b, stop)
    finally:
        stop.set()
        proc.terminate()
        b.close_all()


if __name__ == "__main__":
    main()
