from __future__ import annotations
import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any
import collab.boot as m_boot
import collab.text as m_text
import collab.i18n as m_i18n
import collab.store as m_store
import collab.tmuxio as m_tmuxio
import collab.log as m_log
import collab.agents as m_agents
import collab.chat_base as m_chat_base


@m_store.room_locked
def mark_read(owner: str, ids: set[str]) -> None:
    rp = m_store.room().paths
    m_log.write_set(rp.receipts_dir, owner, m_log.read_set(rp.receipts_dir, owner) | ids)


def dm_keys(pending: str, in_paste: bool) -> tuple[list[tuple[Any, ...]], str, bool]:
    """The /dm panel's keys: what came from the terminal, as events — («paste», text), («tab», ±1),
    («scroll», ±1, page), («close»,), («chat»,), («enter»,), («back»,), («clear»,), («char», c) — and what
    is left: a sequence still coming, or a lone Esc (whether more follows, the panel waits to see).
    Out of cmd_dm, so the keys are told apart in one place a test can reach."""
    events: list[tuple[Any, ...]] = []
    while pending:
        if in_paste:
            end = pending.find("\033[201~")
            events.append(("paste", pending if end < 0 else pending[:end]))
            pending, in_paste = ("", True) if end < 0 else (pending[end + 6 :], False)
        elif pending.startswith("\033[200~"):
            pending, in_paste = pending[6:], True
        elif pending.startswith(("\033[1;3C", "\033f", "\033[1;3D", "\033b")):  # Alt+→/←, as WebStorm sends them
            events.append(("tab", 1 if pending.startswith(("\033[1;3C", "\033f")) else -1))
            pending = pending[6:] if pending.startswith("\033[") else pending[2:]
        elif pending.startswith("\033[Z"):  # Shift+Tab: the previous tab
            events.append(("tab", -1))
            pending = pending[3:]
        elif pending.startswith(("\033[A", "\033[B", "\033OA", "\033OB")):
            events.append(("scroll", -1 if pending[2] == "A" else 1, False))
            pending = pending[3:]
        elif pending.startswith(("\033[5~", "\033[6~")):  # PgUp/PgDn: a panel's worth
            events.append(("scroll", -1 if pending[2] == "5" else 1, True))
            pending = pending[4:]
        elif pending == "\033":
            break  # a lone Esc: the key, or the start of one still coming
        elif pending.startswith("\033[") or pending.startswith("\033O"):
            j = 2
            while j < len(pending) and not ("@" <= pending[j] <= "~"):
                j += 1
            if j >= len(pending):
                break  # the rest of the sequence is still coming
            pending = pending[j + 1 :]
        elif pending.startswith("\033"):
            pending = pending[1:]
        else:
            ch, pending = pending[0], pending[1:]
            if ch == "\x1d":  # Ctrl+]
                events.append(("close",))
            elif ch == "\t":  # Tab: the next tab (keys with no Alt: not every keyboard has one)
                events.append(("tab", 1))
            elif ch == "\x0f":  # Ctrl+O
                events.append(("chat",))
            elif ch in "\r\n":
                events.append(("enter",))
            elif ch in ("\x7f", "\x08"):
                events.append(("back",))
            elif ch == "\x15":
                events.append(("clear",))
            elif ch >= " ":
                events.append(("char", ch))
    return events, pending, in_paste


def file_stamp(path: Path) -> tuple[int, int, int] | None:
    """A file's identity and version, cheaply: its inode, size and time to the nanosecond; None if not there."""
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_ino, info.st_size, info.st_mtime_ns


DM_FULL_READ_EVERY = 30.0  # s: at least this often /dm reads the whole log — its ⚠ judges the past


def journal_now(seen: dict[str, Any]) -> list[dict[str, Any]]:
    """The room's messages for /dm, and seen["version"] moved on whenever they changed. What came since —
    through the tick's reader; the whole log, read again and kept by the tick's reader too, when where
    signing began or the person's key changed, and at least every DM_FULL_READ_EVERY s: «⚠ not from the
    chat» judges the past, and the tick does not see an edit in place of the same length with its mtime
    put back. Kept by the reader too: else the next message brought back the text
    from before the edit, and the mark went."""
    stamp = file_stamp(m_store.room().paths.messages_path)
    judged = (m_log.signing_mark(), file_stamp(m_boot.paths().owner_key))
    clock = time.monotonic()
    if seen.get("judged") != judged or clock - seen.get("full_at", clock - DM_FULL_READ_EVERY) >= DM_FULL_READ_EVERY:
        messages = m_log.read_log_tick(fresh=True)
        seen.update(judged=judged, full_at=clock)
    elif stamp is None or seen.get("stamp") != stamp:
        messages = m_log.read_log_tick()
    else:
        return seen["messages"]
    seen["stamp"] = stamp
    if messages != seen.get("messages"):  # the tab laid out again only when they changed
        seen["messages"] = messages
        seen["version"] = seen.get("version", 0) + 1
    return seen["messages"]


def cmd_dm(args: argparse.Namespace) -> None:
    """The panel /dm opens beside the person's chat: a tab per agent — what the person and that agent
    say to each other — and an input that writes to the agent of the tab. Opened only by the chat; it
    signs as the person only in the very window and process the chat wrote down."""
    rp = m_store.room().paths
    import select
    import termios
    import tty as tty_

    owner = args.as_
    config = m_store.load_config()

    def signed() -> bool:
        # Read at sending: the chat writes the window down right after opening it.
        rec = m_chat_base.dm_window(owner)
        return bool(rec) and os.environ.get("TMUX_PANE") == rec["pane"] and str(os.getpid()) == rec["pid"]

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        m_store.fail(m_i18n.tr("the /dm panel needs a terminal: open it with /dm in the chat"))
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    buf: list[str] = []
    pending, in_paste, last = "", False, None
    sys.stdout.write("\033[?2004h\033[?25l")
    below = [3]  # rows under the chat's rule, as the chat says: the rule and the input stand level with its
    # History («scroll up in the right chat»), as in the chat: one message per ↑/↓, Esc —
    # the newest. top: the first line on show while reading back, None — the newest; frame: the last
    # frame's message starts, its lines and its height, for the keys to step by.
    top: list[Any] = [None]
    frame: list[Any] = [[], 0, 1]
    titled: list[Any] = [None]  # whose name the top border shows
    journal: dict[str, Any] = {}  # the messages as last read, and the journal's stamp then
    laid: dict[str, Any] = {}  # the active tab's talk as last laid out, and what it was made of

    def chat_pane() -> str:
        rp = m_store.room().paths
        return str((m_store.read_json(rp.panes_path).get("panes") or {}).get(owner) or "")

    def draw() -> None:
        rp = m_store.room().paths
        nonlocal config
        config = m_store.load_config()
        h, w = shutil.get_terminal_size((40, 24)).lines, shutil.get_terminal_size((40, 24)).columns
        state = m_chat_base.dm_tabs(owner)
        messages = journal_now(journal)
        read = m_log.read_set(rp.receipts_dir, owner)
        tabs = []
        for t in state["tabs"]:
            name = m_store.label_of(config, t)
            unread = sum(1 for m in m_chat_base.dm_talk(messages, owner, t) if m["from"] == t and m["id"] not in read)
            text = name + (f" ({unread})" if unread and t != state["active"] else "")
            tabs.append(m_text.paint(f"[{text}]", "1") if t == state["active"] else text)
        # Where the input is, its lines are bright and the head bold; the other half is dim:
        # one look tells where typing goes.
        focused = (
            m_tmuxio.tmux(
                "display-message", "-p", "-t", os.environ.get("TMUX_PANE", ""), "#{pane_active}", check=False
            ).stdout.strip()
            == "1"
        )
        rule = (
            (lambda text: m_text.paint(m_text.cut_cols(text + "━" * w, w - 1), "1;38;5;214"))
            if focused
            else (lambda text: m_text.paint(m_text.cut_cols(text + "┄" * w, w - 1), "2"))
        )
        head = m_chat_base.input_head(config, owner, state["active"] or None)
        name = m_store.label_of(config, state["active"]) if state["active"] else ""
        title = m_chat_base.BORDER_DM.format(name=name) if name else "Direct messages"
        if m_text.text_cols(title) + 4 > w:
            title = m_chat_base.BORDER_DM_SHORT.format(name=name) if name else "DM"
        if titled[0] != title:  # by the text: a /name or a narrower panel changes it too
            titled[0] = title
            m_agents.border_title(title)
        typed = "".join("⏎" if c == "\n" else c for c in buf)
        # The input is the rows under the rule; a long text wraps, its start goes up; incoming messages never move it.
        typing = m_text.wrap_visible(head + typed, w - 1)[-max(1, min(below[0], h - 6)) :]
        # The talk laid out again only when what it is made of changed: on every key it was the whole
        # tab through pretty() and the whole journal read — a second a letter at 250 messages.
        made = (
            journal["version"],
            state["active"],
            w,
            json.dumps(config, sort_keys=True, default=str),
            m_log.signing_mark(),  # «⚠ not from the chat» depends on where signing began…
            file_stamp(m_boot.paths().owner_key),  # …and on the person's key
        )
        if laid.get("made") != made:
            body: list[str] = []
            starts: list[tuple[int, dict[str, Any]]] = []
            talk = m_chat_base.dm_talk(messages, owner, state["active"]) if state["active"] else []
            for m in talk:
                starts.append((len(body), m))
                body += [
                    piece
                    for line in m_chat_base.pretty(config, m, w - 1, 0).split("\n")
                    for piece in m_text.wrap_visible(line, w - 1)
                ]
            laid.update(made=made, body=body, starts=starts)
        body, starts = laid["body"], laid["starts"]
        under = max(1, min(below[0], h - 6))
        # One tab: its name is the input's «Ann → Bob ›» already, and the row goes to the talk.
        # With more, the row names the others and what is unread there — nowhere else on show.
        # …and its rule under it: tmux's border is the line over the panel already («a second
        # line is not needed»). With tabs the rule parts them from the talk.
        heads = 2 if len(state["tabs"]) > 1 else 0
        rows = max(1, h - under - 1 - heads)
        first = max(0, len(body) - rows)
        if top[0] is not None:
            if top[0] >= first:
                top[0] = None  # back at the newest: the tab follows what comes in again
            else:
                first = top[0]
        frame[:] = [[at for at, _ in starts], len(body), rows]
        plain = " · ".join(m_text.ANSI.sub("", t) for t in tabs)
        line = " · ".join(tabs) if m_text.text_cols(plain) <= w - 1 else m_text.cut_cols(plain, w - 1)
        out = "\033[?2026h\033[H" + ("\033[2K" + line + "\r\n\033[2K" + "─" * (w - 1) + "\r\n" if heads else "")
        for row in range(rows):
            out += "\033[2K" + (body[first + row] if first + row < len(body) else "") + "\033[0m\r\n"
        # The keys are on the rule above the input, as in the chat: the two rules are one line across.
        hint = (
            m_i18n.tr("━ ↑ History · Ctrl+O chat · Tab tabs · Ctrl+] close · /dm name ")
            if focused
            else m_i18n.tr("┄ Ctrl+O here · Tab tabs ")
        )
        if top[0] is not None:
            hint = ("━" if focused else "┄") + m_i18n.tr(" ↑/↓ Earlier/later messages · Esc Latest ")
        out += f"\033[{h - under};1H\033[2K" + rule(hint)
        for i in range(under):
            out += f"\033[{h - under + 1 + i};1H\033[2K" + (typing[i] if i < len(typing) else "")
        # The cursor blinks where the next letter goes, while typing goes here.
        end = typing[-1] if typing else ""
        out += (
            f"\033[{h - under + len(typing)};{m_text.text_cols(m_text.ANSI.sub('', end)) + 1}H\033[?25h"
            + m_chat_base.CURSOR_BLINK
            if focused
            else "\033[?25l"
        )
        sys.stdout.write(out + "\033[?2026l")
        sys.stdout.flush()
        # Read is what is on show: a message whose lines are in view — not the whole tab.
        ends = [at for at, _ in starts[1:]] + [len(body)]
        shown = {
            m["id"]
            for (at, m), end_ in zip(starts, ends)
            if at < first + rows and end_ > first and m["from"] != owner and m["id"] not in read
        }
        if shown:
            mark_read(owner, shown)  # shown in the active tab is read: «● N for you» and /mine know it

    def scroll(direction: int, page: bool = False) -> None:
        """One message back (−1) or forward (+1); a message taller than the panel goes in parts;
        PgUp/PgDn — a panel's worth of lines."""
        starts, total, space = frame
        newest = max(0, total - space)
        at = newest if top[0] is None else top[0]
        if page:
            step = at + direction * max(1, space - 1)
            top[0] = None if step >= newest else max(0, step)
        elif direction < 0:
            target = max([x for x in starts if x < at], default=0)
            top[0] = max(0, at - (space - 1) if at - target > space else target)
        else:
            target = min([x for x in starts if x > at], default=total)
            top[0] = None if min(target, at + space - 1) >= newest else min(target, at + space - 1)

    def switch(step: int) -> None:
        top[0] = None  # another tab opens at its newest
        state = m_chat_base.dm_tabs(owner)
        if state["tabs"]:
            i = state["tabs"].index(state["active"]) if state["active"] in state["tabs"] else 0
            m_chat_base.set_dm_tabs(owner, state["tabs"], state["tabs"][(i + step) % len(state["tabs"])])

    try:
        tty_.setraw(fd)
        while True:
            state = m_chat_base.dm_tabs(owner)
            chat = chat_pane()
            seen = (
                m_tmuxio.tmux("display-message", "-p", "-t", chat, "#{pane_id}\t#{@collab_below}", check=False) if chat else None
            )
            if not state["tabs"] or not seen or seen.returncode:
                return  # closed with /dm close, or the chat is gone: the panel goes with it
            told = seen.stdout.strip().partition("\t")[2]
            below[0] = int(told) if told.isdigit() else 3
            focus = m_tmuxio.tmux(
                "display-message", "-p", "-t", os.environ.get("TMUX_PANE", ""), "#{pane_active}", check=False
            ).stdout
            snapshot = (
                json.dumps(state),
                rp.messages_path.stat().st_mtime if rp.messages_path.exists() else 0,
                "".join(buf),
                shutil.get_terminal_size((40, 24)),
                focus,
                below[0],
                top[0],
                int(time.monotonic() // DM_FULL_READ_EVERY),  # drawn at least so often: the whole log read again
            )
            if snapshot != last:
                last = snapshot
                draw()
            if not select.select([fd], [], [], args.interval)[0]:
                continue
            pending += os.read(fd, 4096).decode("utf-8", "ignore")
            events, pending, in_paste = dm_keys(pending, in_paste)
            if pending == "\033" and not select.select([fd], [], [], 0.06)[0]:
                pending = ""  # a lone Esc, nothing after it: the key itself
                events.append(("esc",))
            for event in events:
                kind = event[0]
                if kind == "paste":
                    buf.extend(m_text.pasted_text(event[1]))  # as the chat
                elif kind == "tab":
                    switch(event[1])
                elif kind == "scroll":
                    scroll(event[1], page=event[2])
                elif kind == "esc":
                    if top[0] is not None:
                        top[0] = None  # Esc: the newest first, then back to the chat
                    else:
                        m_tmuxio.tmux("select-pane", "-t", chat, check=False)
                elif kind == "close":  # Ctrl+]: the panel closes
                    m_chat_base.set_dm_tabs(owner, [], "")
                    return
                elif kind == "chat":  # Ctrl+O: to the chat, the same key comes back
                    m_tmuxio.tmux("select-pane", "-t", chat, check=False)
                elif kind == "enter":
                    text = "".join(buf).strip()
                    buf.clear()
                    # The tab as it is now, not as the tick began: Tab and Enter in one read sent the
                    # letter to the tab left behind.
                    to = m_chat_base.dm_tabs(owner)["active"]
                    if text and to:
                        m_log.append_message(owner, [to], text, sign=signed())
                elif kind == "back":
                    if buf:
                        buf.pop()
                elif kind == "clear":
                    buf.clear()
                elif kind == "char":
                    buf.append(event[1])
            last = None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\033[?2004l\033[?25h" + m_chat_base.CURSOR_PLAIN)
        sys.stdout.flush()
        m_chat_base.forget_dm_window(owner)
