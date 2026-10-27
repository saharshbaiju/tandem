"""
qr.py - tiny dependency-free QR code encoder for the TUI pairing screen.

Byte mode (UTF-8) only, versions 1..15, error correction L/M/Q/H. The core
algorithm (Reed-Solomon ECC, interleaving, function patterns, mask penalty
scoring) is a faithful port of Project Nayuki's QR Code generator (MIT).

    matrix = qr.encode("tandem://c?h=...", "M")   # list[list[bool]], True = dark
    lines = qr.render_halfblocks(matrix)          # text rows, glyph = dark
"""

MAX_VERSION = 15

# Format-info bits for each error correction level.
ECL_BITS = {"L": 1, "M": 0, "Q": 3, "H": 2}

# Per version (index 0 unused): ECC codewords per block, and number of blocks.
ECC_PER_BLOCK = {
    "L": (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22),
    "M": (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24),
    "Q": (-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30),
    "H": (-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24),
}
NUM_BLOCKS = {
    "L": (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6),
    "M": (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10),
    "Q": (-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12),
    "H": (-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18),
}

# Penalty weights used when picking the mask.
N1, N2, N3, N4 = 3, 3, 40, 10

# The 8 standard mask patterns: True means "flip this module".
MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


def _bit(value, i):
    return (value >> i) & 1 != 0


# ---------------------------------------------------------------- capacity

def _raw_data_modules(ver):
    """Modules left for data+ECC after all function patterns are drawn."""
    n = (16 * ver + 128) * ver + 64
    if ver >= 2:
        numalign = ver // 7 + 2
        n -= (25 * numalign - 10) * numalign - 55
        if ver >= 7:
            n -= 36
    return n


def _data_codewords(ver, ecl):
    return _raw_data_modules(ver) // 8 - ECC_PER_BLOCK[ecl][ver] * NUM_BLOCKS[ecl][ver]


# ---------------------------------------------------------------- Reed-Solomon

def _gf_mul(x, y):
    """Multiply in GF(2^8) modulo x^8 + x^4 + x^3 + x^2 + 1."""
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _add_ecc_and_interleave(data, ver, ecl):
    """Split data into blocks, append ECC to each, then interleave."""
    numblocks = NUM_BLOCKS[ecl][ver]
    ecclen = ECC_PER_BLOCK[ecl][ver]
    raw = _raw_data_modules(ver) // 8
    numshort = numblocks - raw % numblocks
    shortlen = raw // numblocks

    divisor = _rs_divisor(ecclen)
    blocks = []
    k = 0
    for i in range(numblocks):
        n = shortlen - ecclen + (0 if i < numshort else 1)
        dat = data[k:k + n]
        k += n
        ecc = _rs_remainder(dat, divisor)
        if i < numshort:
            dat.append(0)  # placeholder so all blocks line up; skipped below
        blocks.append(dat + ecc)

    result = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != shortlen - ecclen or j >= numshort:
                result.append(blk[i])
    return result


# ---------------------------------------------------------------- matrix build

class _Builder:
    def __init__(self, ver, ecl):
        self.ver = ver
        self.ecl = ecl
        self.size = ver * 4 + 17
        self.mod = [[False] * self.size for _ in range(self.size)]
        self.func = [[False] * self.size for _ in range(self.size)]

    def set_func(self, x, y, dark):
        self.mod[y][x] = dark
        self.func[y][x] = True

    def alignment_positions(self):
        if self.ver == 1:
            return []
        numalign = self.ver // 7 + 2
        step = (self.ver * 8 + numalign * 3 + 5) // (numalign * 4 - 4) * 2
        result = [self.size - 7 - i * step for i in range(numalign - 1)] + [6]
        return list(reversed(result))

    def draw_function_patterns(self):
        size = self.size
        for i in range(size):  # timing patterns
            self.set_func(6, i, i % 2 == 0)
            self.set_func(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):  # finders + separators
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        self.set_func(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = self.alignment_positions()
        last = len(pos) - 1
        for i, ax in enumerate(pos):
            for j, ay in enumerate(pos):
                # Skip the three corners occupied by finder patterns.
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_func(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format_bits(0)  # reserve the area; real mask drawn later
        self.draw_version()

    def draw_format_bits(self, mask):
        data = ECL_BITS[self.ecl] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        size = self.size
        # First copy, around the top-left finder.
        for i in range(0, 6):
            self.set_func(8, i, _bit(bits, i))
        self.set_func(8, 7, _bit(bits, 6))
        self.set_func(8, 8, _bit(bits, 7))
        self.set_func(7, 8, _bit(bits, 8))
        for i in range(9, 15):
            self.set_func(14 - i, 8, _bit(bits, i))
        # Second copy, split between the other two finders.
        for i in range(0, 8):
            self.set_func(size - 1 - i, 8, _bit(bits, i))
        for i in range(8, 15):
            self.set_func(8, size - 15 + i, _bit(bits, i))
        self.set_func(8, size - 8, True)  # the always-dark module

    def draw_version(self):
        if self.ver < 7:
            return
        rem = self.ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = self.ver << 12 | rem
        for i in range(18):
            a, b = self.size - 11 + i % 3, i // 3
            self.set_func(a, b, _bit(bits, i))
            self.set_func(b, a, _bit(bits, i))

    def draw_codewords(self, data):
        """Zig-zag the codeword bits up and down two-column strips."""
        size = self.size
        i = 0
        right = size - 1
        while right >= 1:
            if right == 6:  # skip the vertical timing column
                right = 5
            upward = ((right + 1) & 2) == 0
            for vert in range(size):
                y = size - 1 - vert if upward else vert
                for j in range(2):
                    x = right - j
                    if not self.func[y][x] and i < len(data) * 8:
                        self.mod[y][x] = _bit(data[i >> 3], 7 - (i & 7))
                        i += 1
            right -= 2

    def apply_mask(self, mask):
        fn = MASKS[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.func[y][x] and fn(x, y):
                    self.mod[y][x] = not self.mod[y][x]

    # -- penalty scoring (Nayuki's finder-like pattern detector) --

    def _add_history(self, runlen, history):
        if history[0] == 0:
            runlen += self.size  # light border before the first run
        history.pop()
        history.insert(0, runlen)

    @staticmethod
    def _count_patterns(h):
        n = h[1]
        core = n > 0 and h[2] == h[4] == h[5] == n and h[3] == n * 3
        return ((1 if core and h[0] >= n * 4 and h[6] >= n else 0)
                + (1 if core and h[6] >= n * 4 and h[0] >= n else 0))

    def _terminate_and_count(self, color, runlen, history):
        if color:  # close a dark run, then add the light border after it
            self._add_history(runlen, history)
            runlen = 0
        runlen += self.size
        self._add_history(runlen, history)
        return self._count_patterns(history)

    def _line_penalty(self, line):
        result = 0
        color, run, history = False, 0, [0] * 7
        for cell in line:
            if cell == color:
                run += 1
                if run == 5:
                    result += N1
                elif run > 5:
                    result += 1
            else:
                self._add_history(run, history)
                if not color:
                    result += self._count_patterns(history) * N3
                color, run = cell, 1
        return result + self._terminate_and_count(color, run, history) * N3

    def penalty(self):
        m, size = self.mod, self.size
        result = 0
        for y in range(size):
            result += self._line_penalty(m[y])
        for x in range(size):
            result += self._line_penalty([m[y][x] for y in range(size)])
        for y in range(size - 1):  # 2x2 same-colour blocks
            for x in range(size - 1):
                c = m[y][x]
                if c == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    result += N2
        dark = sum(sum(row) for row in m)
        total = size * size
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        return result + k * N4


# ---------------------------------------------------------------- public API

def encode(text, ecl="M"):
    """Encode text as a QR code. Returns the module matrix (True = dark),
    without quiet zone. Picks the smallest version 1..15 that fits."""
    ecl = ecl.upper()
    if ecl not in ECL_BITS:
        raise ValueError("ecl must be one of L, M, Q, H")
    payload = text.encode("utf-8")

    for ver in range(1, MAX_VERSION + 1):
        countbits = 8 if ver <= 9 else 16
        capacity = _data_codewords(ver, ecl) * 8
        if 4 + countbits + len(payload) * 8 <= capacity:
            break
    else:
        raise ValueError("text too long for QR version %d" % MAX_VERSION)

    # Bit stream: byte-mode indicator, length, data, terminator, padding.
    bits = []

    def put(value, n):
        bits.extend((value >> i) & 1 for i in reversed(range(n)))

    put(0b0100, 4)
    put(len(payload), countbits)
    for b in payload:
        put(b, 8)
    put(0, min(4, capacity - len(bits)))
    put(0, -len(bits) % 8)
    pad = 0xEC
    while len(bits) < capacity:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11

    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]

    qr = _Builder(ver, ecl)
    qr.draw_function_patterns()
    qr.draw_codewords(_add_ecc_and_interleave(data, ver, ecl))

    # Try every mask (applying twice undoes it) and keep the lowest penalty.
    best, best_score = 0, None
    for mask in range(8):
        qr.apply_mask(mask)
        qr.draw_format_bits(mask)
        score = qr.penalty()
        if best_score is None or score < best_score:
            best, best_score = mask, score
        qr.apply_mask(mask)
    qr.apply_mask(best)
    qr.draw_format_bits(best)
    return qr.mod


def render_halfblocks(matrix, quiet=2):
    """Render a module matrix as text, two module rows per line, with a
    quiet zone of `quiet` modules. Printed glyph pixels are DARK modules,
    so draw the result black-on-white."""
    size = len(matrix)
    full = size + 2 * quiet

    def dark(x, y):
        x, y = x - quiet, y - quiet
        return 0 <= x < size and 0 <= y < size and matrix[y][x]

    glyphs = {(False, False): " ", (True, False): "▀",
              (False, True): "▄", (True, True): "█"}
    lines = []
    for y in range(0, full, 2):
        lines.append("".join(glyphs[dark(x, y), dark(x, y + 1)] for x in range(full)))
    return lines
