from __future__ import annotations
import hashlib
import os
import re
import subprocess
import sys
import time
from typing import Any
import collab.screen as m_screen

# An agent's program asking the person a question in its window — read whole, to be answered from Telegram by a
# button: its words as the program shows them, every option, the one selected. Only known dialog shapes and versions;
# action keys are a separate permission. Anything uncertain — no buttons, the person answers on the computer.
# Kimi 2.4.1 Cache expired: the full frame and choices are parsed; no numeric callback is granted.
#
# Claude Code 2.1 (2.1.310 tried): the dialog sits under the last rule «────» exactly as wide as the window, its title
# on the next line; its last line is «Esc to cancel · Tab to amend»; options «❯ 1. Yes» / «  2. …», a long one wrapped
# by Claude itself onto lines indented under it. Every line of it but the program's «╌╌╌» is indented: the agent's
# command and words are, so a line at the first column inside it is not the program's. A digit selects AND confirms.
# Codex 0.163: the dialog begins at «Would you like to …?» and ends with «Press enter to confirm or esc to cancel»;
# options «› 1. Yes, proceed (y)». A digit selects and confirms at once.
CLAUDE_VERSION = re.compile(r"2\.1\.\d+")
CODEX_VERSION = re.compile(r"0\.163\.\d+")
CLAUDE_END = re.compile(r"^\s*Esc to cancel · Tab to amend\s*$")
CODEX_END = re.compile(r"^\s*Press enter to confirm or esc to cancel\s*$")
CODEX_TITLE = re.compile(r"^  Would you like to [^\n?]+\?$")
RULE = re.compile(r"^─{20,}$")
CLAUDE_TITLE = re.compile(r"^ \S.*$")
DOTS = re.compile(r"^╌{20,}$")
OPTION = {
    "claude": re.compile(r"^ (❯| ) (\d)\. (\S.*)$"),
    "codex": re.compile(r"^(›| ) (\d)\. (\S.*)$"),
}
MAX_LINES = 60  # a dialog longer than this is not read as one: the person answers on the computer
KIMI_VERSION = re.compile(r"2\.4\.1")


def deadline_left(deadline: float | None) -> float | None:
    return None if deadline is None else deadline - time.monotonic()


def deadline_expired(deadline: float | None) -> bool:
    return deadline is not None and deadline - time.monotonic() <= 0


def kimi_process_executable(pane_pid: str) -> str:
    if sys.platform != "linux":
        return ""
    try:
        path = os.readlink(f"/proc/{pane_pid}/exe")
    except OSError:
        return ""
    return path if path.startswith("/") and path.rsplit("/", 1)[-1] == "kimi" else ""


def kimi_process_identity(pane_pid: str, deadline: float | None = None) -> tuple[str, str] | None:
    if deadline_expired(deadline):
        return None
    path = kimi_process_executable(pane_pid)
    if not path or deadline_expired(deadline):
        return None
    try:
        started = subprocess.run(
            ["ps", "-ww", "-o", "lstart=", "-p", pane_pid],
            capture_output=True,
            text=True,
            timeout=2 if deadline is None else max(0.001, deadline - time.monotonic()),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if deadline_expired(deadline):
        return None
    start = started.stdout.strip()
    return (path, start) if started.returncode == 0 and start else None


def kimi_process_file_matches(pane_pid: str, path: str, deadline: float | None = None) -> bool:
    if deadline_expired(deadline):
        return False
    if sys.platform != "linux":
        return True
    try:
        running = os.stat(f"/proc/{pane_pid}/exe")
        executable = os.stat(path)
    except OSError:
        return False
    if deadline_expired(deadline):
        return False
    return (running.st_dev, running.st_ino) == (executable.st_dev, executable.st_ino)


def program_version(kind: str, program: str, pane_pid: str, deadline: float | None = None) -> str:
    """The version of the program running in an agent's window — not of the one installed now: Claude Code names its
    process by its version; a Codex runs its own release's helper, the version in its path; Kimi's own launcher answers.
    "" — not known."""
    if kind == "claude":
        return program if CLAUDE_VERSION.fullmatch(program) else ""
    if kind == "kimi":
        if not pane_pid.isdigit() or deadline_expired(deadline):
            return ""
        before = kimi_process_identity(pane_pid, deadline)
        if not before or deadline_expired(deadline) or not kimi_process_file_matches(pane_pid, before[0], deadline):
            return ""
        try:
            result = subprocess.run(
                [before[0], "--version"],
                capture_output=True,
                text=True,
                timeout=2 if deadline is None else max(0.001, deadline - time.monotonic()),
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        after = kimi_process_identity(pane_pid, deadline)
        if after != before or deadline_expired(deadline) or not kimi_process_file_matches(pane_pid, before[0], deadline):
            return ""
        version = result.stdout.strip()
        return version if not deadline_expired(deadline) and result.returncode == 0 and KIMI_VERSION.fullmatch(version) else ""
    if kind != "codex" or not pane_pid.isdigit() or deadline_expired(deadline):
        return ""
    kids = subprocess.run(
        ["pgrep", "-P", pane_pid],
        capture_output=True,
        text=True,
        timeout=None if deadline is None else max(0.001, deadline - time.monotonic()),
    ).stdout.split()[:20]
    # Under a shell too (a window the person named): the Codex is its child, its release's helper one deeper.
    grandchildren = []
    for kid in kids:
        if deadline_expired(deadline):
            return ""
        grandchildren += subprocess.run(
            ["pgrep", "-P", kid],
            capture_output=True,
            text=True,
            timeout=None if deadline is None else max(0.001, deadline - time.monotonic()),
        ).stdout.split()[:20]
    kids += grandchildren
    for pid in kids[:60]:
        if deadline_expired(deadline):
            return ""
        args = subprocess.run(
            ["ps", "-ww", "-o", "args=", "-p", pid],
            capture_output=True,
            text=True,
            timeout=None if deadline is None else max(0.001, deadline - time.monotonic()),
        ).stdout
        if deadline_expired(deadline):
            return ""
        m = re.search(r"/releases/(\d+\.\d+\.\d+)-", args)
        if m:
            return m.group(1) if not deadline_expired(deadline) else ""
    return ""


def read_prompt(kind: str, version: str, screen: str, width: int) -> dict[str, Any] | None:
    """The question an agent's program asks now, from its window's plain text (wrapped lines joined) and its width:
    {"lines": its words as shown,
    "options": [{"n", "lines"}], "selected": n, "print": what tells this question from any other}. None — no
    question this version's dialog has, or not all of it on the screen: no option is guessed."""
    if kind == "kimi":
        if not KIMI_VERSION.fullmatch(version):
            return None
        question = m_screen.kimi_cache_dialog(screen, width)
        if not question or not question.pop("complete"):
            return None
        shown = [*question["lines"], *(f"{o['n']}. " + "\n".join(o["lines"]) for o in question["options"])]
        question["print"] = hashlib.sha256("\n".join([kind, version, *shown, str(question["selected"])]).encode()).hexdigest()[
            :32
        ]
        return question
    if not (kind == "claude" and CLAUDE_VERSION.fullmatch(version)) and not (
        kind == "codex" and CODEX_VERSION.fullmatch(version)
    ):
        return None
    lines = [line.rstrip() for line in screen.rstrip("\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    if not lines or not (CLAUDE_END if kind == "claude" else CODEX_END).match(lines[-1]):
        return None
    if kind == "claude":
        top = max((i for i, line in enumerate(lines) if RULE.match(line)), default=-1)
        # The program's rule spans the window, its title right under it. A rule-like line the agent's words made —
        # a long line the terminal wrapped — is no top: the dialog's own lines above it would be hidden from the card.
        if top < 0 or len(lines[top]) != width or top + 1 >= len(lines) or not CLAUDE_TITLE.match(lines[top + 1]):
            return None
        inner = lambda line: not line or line.startswith(" ") or bool(DOTS.match(line))  # a line a dialog has
        if not all(inner(line) for line in lines[top + 1 : -1]):
            return None
        # Nor the rule of a dialog inside another: one more such rule above, only a dialog's lines between them.
        above = [i for i in range(max(0, top - MAX_LINES), top) if lines[i] == "─" * width]
        if above and all(inner(line) for line in lines[above[-1] + 1 : top]):
            return None
    else:
        # Its title is the program's own line: two spaces in, after an empty one. The command and its reason are the
        # agent's words inside the dialog — a «Would you like to …?» among them would move where the dialog seems to
        # begin and hide the command's start from the card: more than one, and it is not read at all.
        titles = [i for i, line in enumerate(lines) if CODEX_TITLE.match(line)]
        if len(titles) != 1 or titles[0] == 0 or lines[titles[0] - 1].strip():
            return None
        top = titles[0] - 1
    if top < 0 or len(lines) - top > MAX_LINES:
        return None
    region = lines[top + 1 : -1]
    words: list[str] = []
    options: list[dict[str, Any]] = []
    selected = 0
    for line in region:
        m = OPTION[kind].match(line)
        if m:
            n = int(m.group(2))
            if n != len(options) + 1:
                return None  # options out of their order: not this dialog
            options.append({"n": n, "lines": [m.group(3)]})
            if m.group(1) != " ":
                selected = n
        elif options and line.startswith("      ") and line.strip():
            options[-1]["lines"].append(line.strip())  # a long option, wrapped by the program onto its next lines
        elif options and line.strip():
            return None  # words after the options other than its last line: not a dialog read whole
        elif not options and line.strip() and not DOTS.match(line):
            words.append(line.strip())
    if len(options) < 2 or not selected or not words:
        return None
    if any("⧉" in line for line in words):
        return None  # what it asks about is open in an editor, not on the screen: not answered unseen
    shown = [*words, *(f"{o['n']}. " + "\n".join(o["lines"]) for o in options)]
    return {
        "lines": words,
        "options": options,
        "selected": selected,
        "print": hashlib.sha256("\n".join([kind, version, *shown, str(selected)]).encode()).hexdigest()[:32],
    }


def key_for(kind: str, n: int) -> str:
    """The one key that chooses option n in these programs' dialogs: its digit — it selects and confirms at once (both
    tried live; an Enter after it would go to the next screen)."""
    if kind not in ("claude", "codex") or type(n) is not int or not 1 <= n <= 9:
        raise ValueError(n)
    return "123456789"[n - 1]


def keys_for(kind: str, n: int, selected: int) -> list[str]:
    """The Cache dialog's selection moved to n, then confirmed: no numeric shortcuts."""
    if kind != "kimi" or type(n) is not int or type(selected) is not int or not 1 <= n <= 4 or not 1 <= selected <= 4:
        raise ValueError((n, selected))
    if n == selected:
        return ["Enter"]
    direction = "Down" if n > selected else "Up"
    return [direction] * abs(n - selected) + ["Enter"]
