from __future__ import annotations

# A QR code of a short text, for the terminal (ISO/IEC 18004): byte mode, error correction M, versions 1–10 — a link
# of up to 213 bytes. The standard library only: Reed–Solomon over GF(256), the function patterns, the eight masks
# and the penalty that picks one. Drawn with half blocks, black on white whatever the terminal's colours.

# Per version: (EC codewords per block, [(blocks, data codewords per block), …]) for level M.
BLOCKS_M = {
    1: (10, [(1, 16)]),
    2: (16, [(1, 28)]),
    3: (26, [(1, 44)]),
    4: (18, [(2, 32)]),
    5: (24, [(2, 43)]),
    6: (16, [(4, 27)]),
    7: (18, [(4, 31)]),
    8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}
ALIGN = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
    10: [6, 28, 50],
}
REMAINDER = {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7, 7: 0, 8: 0, 9: 0, 10: 0}
LEVEL_M = 0b00

EXP = [0] * 512
LOG = [0] * 256
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


def gmul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else EXP[LOG[a] + LOG[b]]


def ec_codewords(data: list[int], n: int) -> list[int]:
    """The Reed–Solomon remainder of `data` by the generator of degree n."""
    gen = [1]
    for i in range(n):
        gen = [a ^ gmul(b, EXP[i]) for a, b in zip([*gen, 0], [0, *gen])]
    rest = list(data) + [0] * n
    for i in range(len(data)):
        c = rest[i]
        if c:
            for j, g in enumerate(gen):
                rest[i + j] ^= gmul(g, c)
    return rest[len(data) :]


def version_for(size: int) -> int:
    for v, (_, groups) in BLOCKS_M.items():
        capacity = sum(b * d for b, d in groups) * 8
        if 4 + (8 if v < 10 else 16) + 8 * size <= capacity:
            return v
    raise ValueError("too long for a QR code here")


def codewords(data: bytes, v: int) -> list[int]:
    """The data in byte mode, padded, cut into blocks, each with its error correction, interleaved."""
    ec, groups = BLOCKS_M[v]
    total = sum(b * d for b, d in groups)
    bits = "0100" + format(len(data), "08b" if v < 10 else "016b") + "".join(format(x, "08b") for x in data)
    bits += "0" * min(4, total * 8 - len(bits))
    bits += "0" * (-len(bits) % 8)
    stream = [int(bits[i : i + 8], 2) for i in range(0, len(bits), 8)]
    pad = [0xEC, 0x11]
    while len(stream) < total:
        stream.append(pad[(len(stream) - len(bits) // 8) % 2])
    blocks, at = [], 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(stream[at : at + size])
            at += size
    out = [b[i] for i in range(max(len(b) for b in blocks)) for b in blocks if i < len(b)]
    ecs = [ec_codewords(b, ec) for b in blocks]
    return out + [e[i] for i in range(ec) for e in ecs]


def bch(value: int, poly: int, bits: int) -> int:
    v = value << bits
    while v.bit_length() > bits:
        v ^= poly << (v.bit_length() - poly.bit_length())
    return (value << bits) | v


MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def base(v: int) -> tuple[list[list[bool]], list[list[bool]]]:
    """The function patterns and which modules they take."""
    n = 17 + 4 * v
    m = [[False] * n for _ in range(n)]
    fixed = [[False] * n for _ in range(n)]

    def put(r: int, c: int, dark: bool) -> None:
        m[r][c], fixed[r][c] = dark, True

    for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)):
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = r0 + r, c0 + c
                if 0 <= rr < n and 0 <= cc < n:
                    ring = max(abs(r - 3), abs(c - 3))
                    put(rr, cc, ring != 2 and ring != 4)
    for i in range(8, n - 8):
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    pos = ALIGN[v]
    corners = {(pos[0], pos[0]), (pos[0], pos[-1]), (pos[-1], pos[0])} if pos else set()
    for r in pos:
        for c in pos:
            if (r, c) in corners:
                continue  # under a finder pattern; those on the timing lines are drawn over them
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    put(r + dr, c + dc, max(abs(dr), abs(dc)) != 1)
    for i in range(9):  # the format's places, filled later
        for rr, cc in ((8, i), (i, 8)):
            if not fixed[rr][cc]:
                put(rr, cc, False)
    for i in range(8):
        put(8, n - 1 - i, False)
        put(n - 1 - i, 8, False)
    put(n - 8, 8, True)  # the dark module
    if v >= 7:
        info = bch(v, 0x1F25, 12)
        for i in range(18):
            dark = bool(info >> i & 1)
            put(n - 11 + i % 3, i // 3, dark)
            put(i // 3, n - 11 + i % 3, dark)
    return m, fixed


def place(m: list[list[bool]], fixed: list[list[bool]], data: list[int], v: int) -> None:
    n = len(m)
    bits = [b >> (7 - i) & 1 for b in data for i in range(8)] + [0] * REMAINDER[v]
    k, upward, c = 0, True, n - 1
    while c > 0:
        if c == 6:
            c -= 1
        for r in range(n - 1, -1, -1) if upward else range(n):
            for cc in (c, c - 1):
                if not fixed[r][cc]:
                    m[r][cc] = bool(bits[k]) if k < len(bits) else False
                    k += 1
        upward = not upward
        c -= 2


def put_format(m: list[list[bool]], mask: int) -> None:
    n = len(m)
    info = bch(LEVEL_M << 3 | mask, 0x537, 10) ^ 0x5412
    bit = lambda i: bool(info >> i & 1)
    for i in range(6):
        m[i][8] = bit(i)
    m[7][8], m[8][8], m[8][7] = bit(6), bit(7), bit(8)
    for i in range(9, 15):
        m[8][14 - i] = bit(i)
    for i in range(8):
        m[8][n - 1 - i] = bit(i)
    for i in range(8, 15):
        m[n - 15 + i][8] = bit(i)


def penalty(m: list[list[bool]]) -> int:
    """The standard's four penalties (runs of five, 2×2 blocks, finder-like patterns, the balance of dark), as the
    reference implementations count them: the light quiet zone counts around a line."""
    n = len(m)
    score = 0

    def finder_like(history: list[int]) -> int:
        k = history[1]
        core = k > 0 and history[2] == history[4] == history[5] == k and history[3] == k * 3
        return int(core and history[0] >= k * 4 and history[6] >= k) + int(core and history[6] >= k * 4 and history[0] >= k)

    def add(history: list[int], run: int) -> None:
        if history[0] == 0:
            run += n  # the light border before the line
        history.insert(0, run)
        del history[7:]

    for line in [*m, *([m[r][c] for r in range(n)] for c in range(n))]:
        colour, run, history = False, 0, [0] * 7
        for x in line:
            if x == colour:
                run += 1
                if run == 5:
                    score += 3
                elif run > 5:
                    score += 1
            else:
                add(history, run)
                if not colour:
                    score += finder_like(history) * 40
                colour, run = x, 1
        if colour:
            add(history, run)
            run = 0
        add(history, run + n)  # the light border after the line
        score += finder_like(history) * 40
    for r in range(n - 1):
        for c in range(n - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    dark, total = sum(map(sum, m)), n * n
    score += ((abs(dark * 20 - total * 10) + total - 1) // total - 1) * 10
    return score


def matrix(text: str, only: int | None = None) -> list[list[bool]]:
    """The QR code of `text` (UTF-8, byte mode, level M) as rows of modules, True — dark; the mask of least penalty."""
    data = text.encode("utf-8")
    v = version_for(len(data))
    words = codewords(data, v)
    best: tuple[int, list[list[bool]]] | None = None
    for mask in range(8) if only is None else [only]:
        m, fixed = base(v)
        place(m, fixed, words, v)
        for r in range(len(m)):
            for c in range(len(m)):
                if not fixed[r][c] and MASKS[mask](r, c):
                    m[r][c] = not m[r][c]
        put_format(m, mask)
        score = penalty(m)
        if best is None or score < best[0]:
            best = (score, m)
    assert best is not None
    return best[1]


def draw(text: str, quiet: int = 2) -> list[str]:
    """The code in half blocks, two rows of modules to a line, black on white set by colour codes (a dark terminal
    would show it inverted), with a quiet zone around."""
    m = matrix(text)
    n = len(m) + 2 * quiet
    grid = [[False] * n for _ in range(n)]
    for r, row in enumerate(m):
        for c, x in enumerate(row):
            grid[r + quiet][c + quiet] = x
    if n % 2:
        grid.append([False] * n)
    out = []
    for r in range(0, len(grid), 2):
        chars = "".join(" ▄▀█"[grid[r][c] * 2 + grid[r + 1][c]] for c in range(n))
        out.append("\033[30;107m" + chars + "\033[0m")
    return out
