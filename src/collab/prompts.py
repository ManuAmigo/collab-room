from __future__ import annotations
import hashlib
import re
import subprocess
from typing import Any

# An agent's program asking the person a question in its window — read whole, to be answered from Telegram by a
# button: its words as the program shows them, every option, the one selected. Only programs and versions whose
# dialogs were read live and whose keys were tried; anything else — no buttons, the person answers on the computer.
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


def program_version(kind: str, program: str, pane_pid: str) -> str:
    """The version of the program running in an agent's window — not of the one installed now: Claude Code names its
    process by its version; a Codex runs its own release's helper, the version in its path. "" — not known."""
    if kind == "claude":
        return program if CLAUDE_VERSION.fullmatch(program) else ""
    if kind != "codex" or not pane_pid.isdigit():
        return ""
    kids = subprocess.run(["pgrep", "-P", pane_pid], capture_output=True, text=True).stdout.split()[:20]
    # Under a shell too (a window the person named): the Codex is its child, its release's helper one deeper.
    kids += [g for k in kids for g in subprocess.run(["pgrep", "-P", k], capture_output=True, text=True).stdout.split()[:20]]
    for pid in kids[:60]:
        args = subprocess.run(["ps", "-ww", "-o", "args=", "-p", pid], capture_output=True, text=True).stdout
        m = re.search(r"/releases/(\d+\.\d+\.\d+)-", args)
        if m:
            return m.group(1)
    return ""


def read_prompt(kind: str, version: str, screen: str, width: int) -> dict[str, Any] | None:
    """The question an agent's program asks now, from its window's plain text (wrapped lines joined) and its width:
    {"lines": its words as shown,
    "options": [{"n", "lines"}], "selected": n, "print": what tells this question from any other}. None — no
    question this version's dialog has, or not all of it on the screen: no option is guessed."""
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
