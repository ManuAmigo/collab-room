from __future__ import annotations
import os
import shutil
import sys
from typing import Any, Callable
import collab.text as m_text


def ask_line(prompt: str) -> str:
    """input() that survives a terminal without UTF-8 line editing (macOS Terminal often): there
    Backspace erases one byte of a Cyrillic letter, the other byte stays, and writing the name
    crashed the room's creation with «surrogates not allowed» (earlier the stray
    letters «АФ» in the name were the same thing). readline erases whole letters; whatever broken
    bytes still come through are dropped. Raises EOFError like input()."""
    try:
        import readline  # noqa: F401  # pyright: ignore[reportUnusedImport] — importing it gives input() letter-wise editing
    except ImportError:
        pass
    raw = input(prompt)
    return raw.encode("utf-8", "surrogateescape").decode("utf-8", "ignore")


def confirm(question: str) -> bool:
    """A yes/no in the terminal, chosen with arrows like everything else («all from the
    menu, yes and no too; the line only for what must be typed»). No is first and is what Esc
    means: a question that guards a stop or a clear must be hard to answer yes by accident."""
    return pick([question], [("No", False), ("Yes", True)], ["↑/↓ choose · Enter · Esc — No"]) is True


def pick(intro: list[str], rows: list[tuple[str, Any]], outro: list[str], erase: Any = False) -> Any:
    """A column chosen with arrows and Enter; returns the chosen row's value, or None on Esc/q
    or without a terminal. Rows whose value is None are group titles: shown, never selected.
    Each row is cut to the window width — a long path wrapped onto two lines and the redraw
    left copies of rows behind. A list taller than the window scrolls with the
    choice (a folder of 40 projects). `erase` (True, or a test of the chosen value): the menu leaves
    no trace — the next one takes its place."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    pickable = [k for k, (_, v) in enumerate(rows) if v is not None]
    if not pickable:
        return None
    sel = 0
    width = max(20, shutil.get_terminal_size((80, 24)).columns - 1)
    # Cut the middle, not the end: the end of a path is the folder's name, the one thing that
    # tells two long paths apart (two rooms showed as the same «…/T/tmpn…»).
    fit: Callable[[str], str] = lambda t: t if len(t) <= width else t[: width // 2 - 1] + "…" + t[-(width - width // 2) :]
    old = termios.tcgetattr(fd)
    for line in intro:
        print(fit(line))
    height = max(3, shutil.get_terminal_size((80, 24)).lines - len(intro) - len(outro) - 1)
    shown = min(len(rows), height)

    def draw(first: bool) -> None:
        if not first:
            sys.stdout.write(f"\033[{shown + len(outro)}A")
        start = max(0, min(pickable[sel] - shown // 2, len(rows) - shown))
        for k, (label, value) in list(enumerate(rows))[start : start + shown]:
            if value is None:
                text = m_text.paint(fit(label), "2")
            elif k == pickable[sel]:
                text = m_text.paint(fit(f" › {label}"), "1")
            else:
                text = fit(f"   {label}")
            sys.stdout.write("\r\033[2K" + text + "\n")
        for line in outro:
            sys.stdout.write("\r\033[2K" + m_text.paint(fit(line), "2") + "\n")
        sys.stdout.flush()

    chosen = None
    try:
        tty.setcbreak(fd)
        draw(True)
        while True:
            ch = os.read(fd, 1)
            if ch == b"\x1b":
                seq = os.read(fd, 2) if select.select([fd], [], [], 0.05)[0] else b""
                # Arrows come as ESC [ A or, in a terminal's application-cursor mode (macOS
                # Terminal), as ESC O A.
                if seq in (b"[A", b"OA"):
                    sel = (sel - 1) % len(pickable)
                elif seq in (b"[B", b"OB"):
                    sel = (sel + 1) % len(pickable)
                elif not seq:
                    return None
                draw(False)
            elif ch in (b"\r", b"\n"):
                chosen = rows[pickable[sel]][1]
                return chosen
            elif ch in (b"q", b"\x03", b"\x04"):
                return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if erase is True or (callable(erase) and erase(chosen)):
            sys.stdout.write(f"\033[{len(intro) + shown + len(outro)}A\r\033[J")
            sys.stdout.flush()
