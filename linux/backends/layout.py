"""
layout.py - where the phone's monitor goes in a desktop layout.

Pure geometry on (x, y, w, h) rectangles in the compositor's logical
coordinates, with no compositor calls, so every backend that has to compute an
explicit position (GNOME's ApplyMonitorsConfig, kscreen-doctor) places
the phone the same way and the rules can be unit-tested on their own.
"""


# How messages to the user say where the phone goes relative to a monitor
# (the `side` setting is "left" / "right" / "up" / "down").
SIDE_PHRASES = {"left": "left of", "right": "right of", "up": "above",
                "down": "below"}


def side_phrase(side):
    """'left of', 'right of', 'above' or 'below' for a `side` setting."""
    return SIDE_PHRASES.get(side, f"{side} of")


def valid_scale(width, height, scale):
    """Nearest scale that gives a width x height mode whole logical pixels.

    Hyprland and KWin work in 1/120 scale steps and need both logical
    dimensions to be whole pixels; anything else is rejected or rounded
    (Hyprland pops an "invalid scale" error and guesses; KWin rounds the
    size and leaves a gap or an overlap beside the main monitor).
    2772x1280 @ 1.5 -> 853.3 px tall, so 4/3.
    """
    want = round(scale * 120)
    for d in range(0, 120):
        for k in (want - d, want + d):
            if k > 0 and (width * 120) % k == 0 and (height * 120) % k == 0:
                return round(k / 120, 6)
    return scale


def _overlaps(a, b):
    """True if rectangles (x, y, w, h) share any area (touching is fine)."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def place_beside(anchor, others, ext_size, side):
    """Top-left for a w x h monitor on `side` of `anchor`, overlapping none.

    `anchor` and `others` are (x, y, w, h) rectangles (anchor among others).
    Flush against the anchor, top/left-aligned with it, when that spot is
    free. If another monitor is in the way, go to the outermost edge of the
    whole layout on that side instead and line up with the monitor that
    forms that edge (the one nearest the anchor), so the new monitor still
    shares an edge with something (Mutter rejects detached monitors).
    """
    ax, ay, aw, ah = anchor
    ew, eh = ext_size
    horizontal = side in ("left", "right")
    if side == "left":
        spot = (ax - ew, ay)
    elif side == "up":
        spot = (ax, ay - eh)
    elif side == "down":
        spot = (ax, ay + ah)
    else:
        spot = (ax + aw, ay)
    if not any(_overlaps((*spot, ew, eh), r) for r in others):
        return spot

    # Monitor edge on `side` of each rectangle; the outermost one wins.
    def edge(r):
        x, y, w, h = r
        return {"left": x, "up": y, "down": y + h}.get(side, x + w)

    outer = (min if side in ("left", "up") else max)(edge(r) for r in others)
    # Line up with the edge-forming monitor closest to the anchor, along
    # the edge: same row for left/right, same column for up/down.
    along = (lambda r: abs(r[1] - ay)) if horizontal else \
        (lambda r: abs(r[0] - ax))
    rx, ry, _rw, _rh = min((r for r in others if edge(r) == outer), key=along)
    if side == "left":
        return outer - ew, ry
    if side == "up":
        return rx, outer - eh
    if side == "down":
        return rx, outer
    return outer, ry
