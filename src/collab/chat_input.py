"""The chat's keys: the loop that reads the terminal and turns what is typed into the draft,
the cursor, the menu's moves and the sending — moved out of cmd_chat as it was. What it needs of the
chat comes through ChatInputContext, as chat_cmds takes ChatCommandContext."""

from __future__ import annotations
import codecs
import os
from dataclasses import dataclass
from typing import Any
import collab.text as m_text
import collab.tmuxio as m_tmuxio


@dataclass
class ChatInputContext:
    """cmd_chat's objects and callbacks the key loop reads; each made once, before the loop."""

    after_lock: Any
    args: Any
    ask_exit: Any
    buf: Any
    choose_command: Any
    commands: Any
    dm_window: Any
    dm_window_alive: Any
    draw_reading: Any
    fd: Any
    feed: Any
    history: Any
    leave_history: Any
    lock: Any
    menu_items: Any
    render: Any
    repaint: Any
    slash_commands: Any
    step: Any
    stop_reading: Any
    stop_view: Any
    submit: Any
    ui: Any
    wizard: Any
    wizard_back: Any


def chat_read_keys(ctx: ChatInputContext) -> None:
    """Read the terminal until it ends, the chat closes or Ctrl+C; each key under the chat's lock."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
    pending = ""
    in_paste = False
    PASTE_END = "\033[201~"
    while True:
        chunk = os.read(ctx.fd, 1024)
        if not chunk:
            return
        pending += decoder.decode(chunk)
        while pending:
            with ctx.lock:
                if ctx.ui.reading:
                    # One message of /mine on show: ↑/↓ the others, Enter or Esc back to the list;
                    # nothing typed reaches the draft.
                    if pending == "\033":
                        import select

                        if select.select([ctx.fd], [], [], 0.06)[0]:
                            break
                        pending = ""
                        ctx.stop_reading()
                        ctx.render()
                        continue
                    if pending.startswith(("\033[A", "\033[B", "\033OA", "\033OB")):
                        r = ctx.ui.reading
                        r["at"] = max(0, min(len(r["ids"]) - 1, r["at"] + (-1 if pending[2] == "A" else 1)))
                        pending = pending[3:]
                        ctx.draw_reading()
                        continue
                    if pending[:1] in ("\r", "\n"):
                        pending = pending[1:]
                        ctx.stop_reading()
                        ctx.render()
                        continue
                    if pending.startswith("\033[") and len(pending) < 3:
                        break
                    pending = pending[1:]
                    continue
                if ctx.ui.viewing:
                    # Read-only: nothing typed here reaches the agent or the draft. A lone Esc
                    # (not the start of an arrow's sequence) goes back to the chat.
                    if pending == "\033":
                        import select

                        if select.select([ctx.fd], [], [], 0.06)[0]:
                            break
                        ctx.stop_view()
                    elif "\033" in pending and not pending[pending.index("\033") + 1 :].startswith(("[", "O")):
                        ctx.stop_view()  # Esc arrived together with other keys: still back
                    pending = ""
                    continue
                if in_paste:
                    # A paste is text to its end: its Tab, Ctrl-C, Ctrl-U, Ctrl-D were taken for
                    # keys — a Tab inside sent a private draft to everyone.
                    end = pending.find(PASTE_END)
                    if end >= 0:
                        literal, pending, in_paste = pending[:end], pending[end + len(PASTE_END) :], False
                    else:
                        # Its end may come in two reads; a cut «\033[20» was eaten as a key and
                        # the chat stayed «in a paste», Enter sending nothing.
                        keep = next(
                            (
                                k
                                for k in range(min(len(PASTE_END) - 1, len(pending)), 0, -1)
                                if PASTE_END.startswith(pending[-k:])
                            ),
                            0,
                        )
                        literal, pending = pending[: len(pending) - keep], pending[len(pending) - keep :]
                    if literal and ctx.ui.suggested:
                        ctx.buf[:], ctx.ui.pos, ctx.ui.suggested = [], 0, False
                    for c in m_text.pasted_text(literal):
                        ctx.buf.insert(ctx.ui.pos, c)
                        ctx.ui.pos += 1
                    if literal:
                        ctx.ui.menu_closed = ctx.ui.menu_moved = False
                    if in_paste and pending:
                        break  # the rest of its end is coming
                    continue
                if pending.startswith("\033[200~"):
                    in_paste, pending = True, pending[6:]
                elif pending.startswith("\033[201~"):
                    pending = pending[6:]  # an end without a start: nothing
                elif pending.startswith(("\033\r", "\033\n")):  # Option+Enter: new line
                    ctx.buf.insert(ctx.ui.pos, "\n")
                    ctx.ui.pos += 1
                    pending = pending[2:]
                elif pending.startswith(("\033[13;2u", "\033[27;2;13~")):  # Shift+Enter where the terminal tells it apart
                    ctx.buf.insert(ctx.ui.pos, "\n")
                    ctx.ui.pos += 1
                    pending = (
                        pending[pending.index("u") + 1 :] if pending.startswith("\033[13;2u") else pending[len("\033[27;2;13~") :]
                    )
                elif pending.startswith("\033[Z"):  # Shift+Tab
                    ctx.step(-1)
                    pending = pending[3:]
                elif pending.startswith(("\033[A", "\033[B", "\033OA", "\033OB")):
                    up = pending[2] == "A"
                    items = ctx.menu_items()
                    if items:
                        ctx.ui.menu_index = (ctx.ui.menu_index + (-1 if up else 1)) % len(items)
                        if items[ctx.ui.menu_index][1] == "pick:sep:" and len(items) > 1:
                            # A heading of a list («Earlier for you») is never chosen: stepped over.
                            ctx.ui.menu_index = (ctx.ui.menu_index + (-1 if up else 1)) % len(items)
                        ctx.ui.menu_moved = True
                    elif up or ctx.ui.top is not None:
                        ctx.history(-1 if up else 1)  # no menu open: earlier messages
                    pending = pending[3:]
                # ←/→/Home/End also in the SS3 form (ESC O D): terminals in application cursor
                # mode send it, and «OD» went into the draft.
                elif pending.startswith(("\033[D", "\033OD")):
                    ctx.ui.suggested = False
                    ctx.ui.pos = max(0, ctx.ui.pos - 1)
                    pending = pending[3:]
                elif pending.startswith(("\033[C", "\033OC")):
                    ctx.ui.pos = min(len(ctx.buf), ctx.ui.pos + 1)
                    pending = pending[3:]
                elif pending.startswith(("\033[H", "\033OH", "\033[F", "\033OF")):
                    ctx.ui.pos = 0 if pending[2] == "H" else len(ctx.buf)
                    pending = pending[3:]
                elif pending.startswith("\033O") and len(pending) >= 3:
                    pending = pending[3:]  # any other SS3 key: ignored, never typed
                elif pending.startswith(("\033[5~", "\033[6~")):  # PgUp / PgDn: a page
                    ctx.history(-1 if pending[2] == "5" else 1, page=True)
                    pending = pending[4:]
                elif pending.startswith("\033[") and len(pending) >= 3:
                    # other escape sequences (arrows up/down, function keys): ignore
                    j = 2
                    while j < len(pending) and not ("@" <= pending[j] <= "~"):
                        j += 1
                    if j >= len(pending):
                        break  # cut between two reads: «\033[1;» then «5D» typed «5D»
                    pending = pending[j + 1 :]
                elif pending == "\033":
                    import select

                    if select.select([ctx.fd], [], [], 0.06)[0]:
                        break  # more is coming: an escape sequence
                    # A lone Esc: one step back, everywhere — a question goes
                    # to its previous step, then to the command menu; a name after /mute
                    # goes back to the commands; the command menu closes.
                    pending = ""
                    value = "".join(ctx.buf)
                    if ctx.ui.top is not None:
                        ctx.leave_history()
                    elif ctx.wizard:
                        ctx.wizard_back()
                    elif value.startswith("/") and " " in value:
                        cmd = value.split(" ", 1)[0]
                        ctx.buf[:] = list("/")
                        ctx.ui.pos = 1
                        ctx.ui.menu_index = next((i for i, (c, _) in enumerate(ctx.commands) if c == cmd), 0)
                        ctx.ui.menu_closed = False
                    elif value.startswith("/"):
                        ctx.buf.clear()
                        ctx.ui.pos = 0
                    else:
                        ctx.ui.menu_closed = True
                elif pending == "\033O" or (pending.startswith("\033[") and len(pending) < 3):
                    break  # wait for the rest of the sequence
                else:
                    ch, pending = pending[0], pending[1:]
                    was_back, ctx.ui.came_back = ctx.ui.came_back, False
                    was_suggested, ctx.ui.suggested = ctx.ui.suggested, False
                    if ch == "\x0f":  # Ctrl+O: to the /dm panel, the same key comes back
                        rec = ctx.dm_window(ctx.args.as_)
                        if rec and ctx.dm_window_alive(ctx.args.as_):
                            m_tmuxio.tmux("select-pane", "-t", rec["pane"], check=False)
                        else:
                            ctx.feed(m_text.paint("No private-talk panel is open — /dm <name> opens one.", "38;5;252"))
                        ctx.render()
                        continue
                    if ch == "\x03":
                        try:
                            ctx.ask_exit()
                        except EOFError:
                            return
                    elif ch == "\t":
                        ctx.step(1)
                    elif (
                        ch in "\r\n"
                        and not in_paste
                        and ctx.menu_items()
                        and (ctx.ui.menu_moved or "".join(ctx.buf) not in ctx.slash_commands)
                    ):
                        to_send = ctx.choose_command()
                        if to_send is not None:
                            ctx.ui.popup = 0
                            ctx.repaint()
                            try:
                                ctx.submit(to_send)
                            except EOFError:
                                return
                    elif (
                        ch in "\r\n"
                        and not in_paste
                        and ctx.ui.selected is None
                        and ctx.buf
                        and not ctx.wizard
                        and not "".join(ctx.buf).lstrip().startswith(("/", "@"))
                    ):
                        # No recipient chosen: keep the draft, send nothing.
                        ctx.feed(m_text.paint("! Choose a recipient with Tab first — your text is kept.", "1;38;5;196"))
                    elif ch in "\r\n" and not in_paste:
                        if ctx.buf and ctx.buf[-1] == "\\":
                            # «\» at the end of the line + Enter: a new line, not sending.
                            ctx.buf[-1] = "\n"
                            ctx.render()
                            continue
                        line = "".join(ctx.buf)
                        ctx.buf.clear()
                        ctx.ui.pos = 0
                        if ctx.ui.top is not None:
                            ctx.leave_history()  # what you send appears among the newest
                        try:
                            ctx.submit(line)
                        except EOFError:
                            return
                    elif ch in "\r\n":
                        ctx.buf.insert(ctx.ui.pos, "\n")
                        ctx.ui.pos += 1
                    elif ch in ("\x7f", "\x08"):
                        if was_suggested:
                            ctx.buf[:], ctx.ui.pos = [], 0
                        if ctx.ui.pos > 0:
                            ctx.ui.menu_closed = False
                            ctx.ui.menu_moved = False
                            del ctx.buf[ctx.ui.pos - 1]
                            ctx.ui.pos -= 1
                    elif ch == "\x15":  # Ctrl-U: clear the line
                        ctx.ui.menu_closed = False
                        ctx.ui.menu_moved = False
                        ctx.buf.clear()
                        ctx.ui.pos = 0
                    elif ch == "\x04" and not ctx.buf:  # Ctrl-D on an empty line: asks, as Ctrl-C
                        # It closed the chat without a word.
                        try:
                            ctx.ask_exit()
                        except EOFError:
                            return
                    elif ch >= " ":
                        if was_suggested:
                            ctx.buf[:], ctx.ui.pos = [], 0
                        ctx.ui.menu_closed = False
                        ctx.ui.menu_moved = False
                        if not ctx.buf and ch == "/":
                            ctx.ui.menu_index = 0
                        if ch == "/" and was_back and ctx.buf == ["/"]:
                            # Esc from a command's menu came back with «/»; the person
                            # types «/mine» from habit — «//mine» went to the agents as text.
                            # Only right after that step back: «//text» typed anew still
                            # sends text that starts with «/».
                            pass
                        else:
                            ctx.buf.insert(ctx.ui.pos, ch)
                            ctx.ui.pos += 1
                ctx.render()
        while ctx.after_lock:
            ctx.after_lock.pop(0)()
