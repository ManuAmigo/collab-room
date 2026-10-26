"""What the chat and its /dm panel share: a message's lines, the bottom area's frames, the /dm
tabs and window — functions of their inputs, no state of a running chat."""

from __future__ import annotations
import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable
import collab.text as m_text
import collab.i18n as m_i18n
import collab.store as m_store
import collab.tmuxio as m_tmuxio
import collab.log as m_log
import collab.deliver as m_deliver


def fmt(message: dict[str, Any], with_id: bool = True, config: dict[str, Any] | None = None) -> str:
    # One line, whatever the fields hold: a line break in «to» faked an «[owner → …]» line.
    head = " ".join(
        m_text.visible(
            m_i18n.tr(
                "[{p0} → {p1}] {p2}",
                p0=message["from"],
                p1=", ".join(map(str, message["to"])),
                p2=m_text.local_hm(str(message["at"])),
            )
        ).split()
    )
    tail = m_i18n.tr("\n  id: {p0}", p0=" ".join(m_text.visible(str(message["id"])).split())) if with_id else ""
    warn = m_deliver.UNSIGNED + "\n" if config and m_log.unsigned_person(message, config) else ""
    # The text indented: a head starts at the line's first column, a line of a text never does.
    body = "\n".join("  " + line if line else "" for line in m_text.body_text(message["text"]).split("\n"))
    return f"{head}\n{warn}{body}{tail}"


# Synchronized output: the terminal shows a redraw as one frame, not line by line (the
# menu flickered). Terminals that don't know it ignore it; tmux 3.4+ holds the pane for it.
SYNC_ON, SYNC_OFF = "\033[?2026h", "\033[?2026l"
# A blinking bar where the person types (DECSCUSR 5), and the terminal's own cursor back on leaving.
CURSOR_BLINK, CURSOR_PLAIN = "\033[5 q", "\033[0 q"

WHOLE = "\x1e"  # a feed line that starts so is never cut into lines: a command to copy


def input_head(config: dict[str, Any], me: str, to: str | None, limit: int = 0) -> str:
    """The start of a person's input line, «Anna → Bob › »: names bold in their colours, the marks
    dim. One for the chat and its /dm panel — the two inputs look alike. With `limit`,
    the names give way so the head takes at most that many columns: in 24 columns, or with a long
    name, the input ran past the window onto the states row."""
    mine = m_store.label_of(config, me)
    theirs = m_i18n.tr("choose with Tab") if to is None else m_store.label_of(config, to)
    if limit and m_text.text_cols(mine) + m_text.text_cols(theirs) + 6 > limit:
        each = max(3, (limit - 6) // 2)
        mine = m_text.fit_cols(mine, each).rstrip() if m_text.text_cols(mine) > each else mine
        theirs = m_text.fit_cols(theirs, max(3, limit - 6 - m_text.text_cols(mine))).rstrip()
    who = m_text.paint(mine, "1;" + m_store.color_of(config, me))
    if to is None:
        target = m_text.paint(theirs, "1;38;5;196")
    else:
        target = m_text.paint(theirs, "1;" + (m_store.color_of(config, to) if to != "all" else "37"))
    return m_i18n.tr("{who} {p1} {target} {p3} ", who=who, p1=m_text.paint("→", "2"), target=target, p3=m_text.paint("›", "2"))


def pretty(config: dict[str, Any], m: dict[str, Any], width: int, max_lines: int) -> str:
    """One block per message: coloured author, readable header, indented and wrapped body.

    Long technical bodies are cut to `max_lines` with a pointer to `collab show <id>`, so a
    commit report does not flood the conversation.
    """
    rp = m_store.room().paths
    who = m_text.paint(m_store.label_of(config, m["from"]), "1;" + m_store.color_of(config, m["from"]))
    to = ", ".join(m_store.label_of(config, t) for t in m["to"])
    head = m_i18n.tr(
        "{who} {p1}", who=who, p1=m_text.paint("→ " + to + " · " + m_text.visible(m_text.local_hm(str(m.get("at", "")))), "2")
    )
    if m_log.unsigned_person(m, config):
        head += " " + m_text.paint(m_i18n.tr("⚠ not from the chat"), "1;38;5;196")
    elif m.get("via") == "telegram":
        head += " " + m_text.paint(m_i18n.tr("· via Telegram"), "2")  # signed with the mark: the person wrote it there
    body: list[str] = []
    # «→» in a text is drawn «->»: only the room draws a head with «→», and a body line «Anna →
    # Everyone · 13:05» read as a head.
    for para in m_text.body_text(m_i18n.retell(m)).splitlines() or [""]:
        body.extend(m_text.wrap_cols(para, max(20, width - 2)) or [""])
    if max_lines and len(body) > max_lines:
        rest = len(body) - (max_lines - 1)
        # With --room: the hint works from any folder, not only the room's.
        # The id quoted too: a forged log line with id «x; command» made the hint a command to paste.
        body = body[: max_lines - 1] + [
            m_text.paint(
                m_i18n.tr(
                    "… {rest} more lines — collab show {p1} --room {p2}",
                    rest=rest,
                    p1=shlex.quote(m_text.visible(str(m["id"]))),
                    p2=shlex.quote(m_text.tilde(str(rp.root))),
                ),
                "2",
            )
        ]
    return "\n" + head + "\n" + "\n".join("  " + line for line in body)


def feed_block(config: dict[str, Any], m: dict[str, Any], width: int, max_lines: int) -> str:
    """A message as the person's feed shows it. A quiet note (deliver.is_quiet: «Taking: …», «the machine is
    free», the queue's lines) is one pale line, cut to the width — shown, never hidden, and out of the
    way of what is for the person; everything else is pretty()'s block."""
    if not m_deliver.is_quiet(m, config):
        return pretty(config, m, width, max_lines)
    to = ", ".join(m_store.label_of(config, t) for t in m["to"])
    when = m_text.visible(m_text.local_hm(str(m.get("at", ""))))
    text = " ".join(m_text.body_text(m_i18n.retell(m)).split())
    line = m_i18n.tr("{p0} → {to} · {when}  {text}", p0=m_store.label_of(config, m["from"]), to=to, when=when, text=text)
    if m_text.text_cols(line) > width:
        line = m_text.cut_cols(line, max(1, width - 1)) + "…"
    return "\n" + m_text.paint(line, "2")


STALE_CHAT = "Your chat runs an older collab — reopen it: /exit, then collab."


def copy_to_clipboard(text: str) -> bool:
    """Put text on the system clipboard so the person pastes instead of selecting.
    COLLAB_CLIPBOARD overrides the command — tests point it at a file, never the real clipboard."""
    custom = os.environ.get("COLLAB_CLIPBOARD")
    candidates = [shlex.split(custom)] if custom else [["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]]
    for cmd in candidates:
        if not shutil.which(cmd[0]):
            continue
        try:
            if subprocess.run(cmd, input=text, text=True, capture_output=True, timeout=3).returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


@dataclass
class ChatState:
    """What the chat's parts share while it runs: one object instead of one-element lists read
    and set as x[0]. Each field's meaning is noted where cmd_chat sets it."""

    mine_count: int = 0
    proposals_shown: set[str] = field(default_factory=lambda: set[str]())  # a proposal's card opens by itself once
    bar: str = ""
    menu_line: str = ""
    extra_rows: int = 0
    # The size the fixed area was laid out for (setup): a resize after it, before the feed's thread
    # began, was never seen — tmux resets the scroll region on a resize, and the feed scrolled the
    # whole window over the input.
    laid: tuple[int, int] = (0, 0)
    viewing: Any = None
    reading: Any = None
    top: Any = None
    unseen: int = 0
    older: Any = None
    redraw_input: Callable[[], None] = lambda: None
    selected: str = "all"
    cfg_mtime: float = 0.0
    popup: int = 0
    pos: int = 0
    menu_index: int = 0
    menu_closed: bool = False
    menu_moved: bool = False
    suggested: bool = False
    came_back: bool = False
    ticks: int = 0  # the states line drawn so many times: the blink of a Ready agent's background work


def status_bar_rows(cells: list[tuple[str, str, str, str, str, int]], parts: list[str], size: os.terminal_size) -> list[str]:
    """Lay out the status cells for this terminal, without reading or changing chat state."""
    # Only five used to fit on a 15-inch screen, the sixth hidden, with no clear way to see it.
    # The cells share the whole width equally; inside, «to the edges»:
    # mark and name (and, where it fits, the state word) on the left, the model and the
    # working time on the right — no empty room under the time for a Ready agent. The order never changes.
    lead = sum(len(m_text.ANSI.sub("", x)) + 1 for x in parts)  # «Paused», on the first row
    space = size.columns - lead
    n = len(cells)
    # As many cells a row as fit at BAR_MIN columns or more; what doesn't fit goes to the next
    # row of the same cells (two letters can't tell «Claude» from «Claude-2»), spread evenly;
    # the feed gives up the rows, but keeps at least ten.
    lines = min(-(-n // max(1, min(n, (space + 1) // (BAR_MIN + 3)))) if n else 1, max(1, size.lines - 10))
    per = -(-n // lines) if n else 0
    # …but not wider than BAR_MAX: with three agents on a wide screen the name and the model drifted
    # half a screen apart; the rest of the row stays empty.
    inner = min(BAR_MAX, (space + 1) // max(1, per) - 3)

    def whole(name: str, model: str, short: str, mark: str, since: str) -> bool:
        right = " ".join(x for x in (model, since) if x)
        left = f"{mark.replace(' ', '')} {name}: {m_i18n.status_word(short)}"
        return m_text.text_cols(left) + (1 + m_text.text_cols(right) if right else 0) <= inner - 2

    # The state words while every cell holds its whole — mark, name, word, model and time, nothing cut;
    # else colour and mark say it, in every cell alike: a row half with words and half without is motley.
    words = all(whole(*cell[:5]) for cell in cells)
    rendered = []
    for name, model, short, mark, since, fill in cells:
        rendered.append(
            m_text.paint(
                f" {m_text.status_cell(mark, name, m_i18n.status_word(short) if words else '', model, since, inner - 2)} ",
                f"1;38;5;16;48;5;{fill}",
            )
        )
    rows_ = [" ".join(parts + rendered[:per])] + [
        " " * lead + " ".join(rendered[i : i + per]) for i in range(per, n, max(1, per))
    ]
    return rows_


def feed_frame(text: str, h: int, w: int) -> str:
    """What appends `text` to the feed above the fixed area: wrapped here, a WHOLE line kept whole."""
    out = "\0337" + f"\033[{h - 3};1H"
    for line in text.split("\n"):
        # Wrap here, not by the terminal: a line longer than the window lost its middle at the
        # edge of the scroll region — the /add invitation came out unusable.
        if line.startswith(WHOLE):
            # The terminal folds it softly, so a selection or `capture-pane -J` gets the command
            # whole; inserted line breaks broke the very command one had to copy by hand.
            out += "\r\n" + line[1:]
            continue
        for piece in m_text.wrap_kept(line, w - 1):
            out += "\r\n" + piece
    return out + "\0338"


def feed_lines(blocks: list[list[str]], w: int) -> tuple[list[str], list[int]]:
    """The feed's lines as a window `w` wide shows them, and where each message starts among them."""
    lines: list[str] = []
    starts: list[int] = []
    for block in blocks:
        lead = next((i for i, line in enumerate(block) if line.strip()), 0)
        starts.append(len(lines) + lead)  # the head, not the blank line above it
        lines.extend(piece for line in block for piece in m_text.wrap_kept(line.lstrip(WHOLE), w - 1))
    return lines, starts


def bottom_frame(menu_line: str, bar: str, h: int, w: int) -> str:
    """The rule (or the hint in its place) and the status rows under the input."""
    rule = m_text.paint("─" * min(w - 1, 120), "2")
    # One row, never more: a hint wider than a narrow window folded onto the input and wrote over it
    # Written over, the rest of each line erased — not blanked first (it flickered).
    line = m_text.wrap_visible(menu_line or rule, max(1, w - 1))[0] + "\033[0m"
    return (
        "\0337"
        + f"\033[{h - 2};1H"
        + line
        + "\033[K"
        + "".join(f"\033[{h + i};1H" + line + "\033[K" for i, line in enumerate(bar.split("\n")))
        + "\0338"
    )


def input_row(head: str, buf: list[str], pos: int, h: int, w: int) -> str:
    """The input row: the head, the part of the text that fits, the cursor where the person types."""
    head_w = m_text.text_cols(m_text.ANSI.sub("", head))
    shown_text = "".join("⏎" if c == "\n" else c for c in buf)
    space = max(1, w - head_w - 1)  # never past the window: the head gave way above
    # Columns, not characters: the text before the cursor that fits, then what
    # fits after it; the cursor goes where those columns end.
    before = m_text.cut_cols(shown_text[:pos], space - 1, from_end=True)
    in_view = before + m_text.cut_cols(shown_text[pos:], space - m_text.text_cols(before))
    return SYNC_ON + f"\033[{h - 1};1H" + head + in_view + f"\033[K\033[{h - 1};{head_w + 1 + m_text.text_cols(before)}H"


def input_hint(chars: int, mine: int, dm_open: bool, here: bool, history: str | None, menu: bool, wizard: bool, w: int) -> str:
    """The line over the input: history, the draft's size, or what to press — by situation."""
    # Above a draft, how to drop it at once (a long pasted text could not be
    # selected and deleted).
    hint = m_i18n.tr("── {chars} chars · Ctrl+U Clear draft ", chars=chars)
    # One line of help above the input, by situation («show how to see history when
    # nothing is typed, so nobody has to look for it» — above the input, not in it: a dim hint in
    # the input was once taken for typed text).
    idle = m_i18n.tr("── ↑ History · / Commands · Tab Recipient ")
    if mine:
        idle = m_i18n.tr("── ● {mine} for you · /mine ", mine=mine) + idle[3:]
    # With a /dm panel beside it: Ctrl+O goes there, and the half where typing goes is bright —
    # the other one dim («unclear how to get back to the chat»).
    if dm_open:
        idle = ("━━ " if here else "┄┄ ") + idle[3:] + m_i18n.tr("· Ctrl+O → DM ")
    fill = ("━" if here else "┄") if dm_open else "─"
    look = ("1;38;5;214" if here else "2") if dm_open else "2"
    return (
        history
        if history is not None and not menu
        else m_text.paint(hint + "─" * max(0, min(w, 120) - len(hint)), "2")
        if chars and not menu
        else m_text.paint((idle + fill * max(0, min(w, 120) - len(idle)))[: max(12, w - 1)], look)
        if not menu and not wizard
        else ""
    )


# ── /dm: a panel of private talks beside the chat ─────────────────────────────────────────────────


def dm_tabs(owner: str) -> dict[str, Any]:
    """This person's panel tabs and the active one, as their chat set them. One file, a record per
    person: shared, one person's /dm switched the other's tab, and their Enter went to an agent the
    other chose."""
    rp = m_store.room().paths
    data = m_store.read_json(rp.state_dir / "dm.json").get(owner)
    data = data if isinstance(data, dict) else {}
    tabs = [t for t in data.get("tabs", []) if isinstance(t, str)] if isinstance(data.get("tabs"), list) else []
    active = data.get("active") if data.get("active") in tabs else (tabs[0] if tabs else "")
    return {"tabs": tabs, "active": active}


@m_store.room_locked
def set_dm_tabs(owner: str, tabs: list[str], active: str) -> None:
    rp = m_store.room().paths
    data = m_store.read_json(rp.state_dir / "dm.json")
    data = {k: v for k, v in data.items() if isinstance(v, dict)}  # a file of before: one record for all, gone
    data[owner] = {"tabs": tabs, "active": active}
    m_store.write_json(rp.state_dir / "dm.json", data)


def dm_window(owner: str) -> dict[str, str]:
    """The panel's window and its process, as the person's chat wrote them — nobody else writes it."""
    rp = m_store.room().paths
    rec = (m_store.read_json(rp.panes_path).get("panes") or {}).get(owner + ":dm")
    return rec if isinstance(rec, dict) and isinstance(rec.get("pane"), str) and isinstance(rec.get("pid"), str) else {}


@m_store.room_locked
def remember_dm_window(owner: str, pane: str, pid: str) -> None:
    rp = m_store.room().paths
    data = m_store.read_json(rp.panes_path) or {"panes": {}}
    data.setdefault("panes", {})[owner + ":dm"] = {"pane": pane, "pid": pid}
    m_store.replace_state(rp.panes_path, json.dumps(data, indent=2) + "\n")


@m_store.room_locked
def forget_dm_window(owner: str) -> None:
    rp = m_store.room().paths
    data = m_store.read_json(rp.panes_path)
    if (data.get("panes") or {}).pop(owner + ":dm", None) is not None:
        m_store.replace_state(rp.panes_path, json.dumps(data, indent=2) + "\n")


def dm_window_alive(owner: str) -> bool:
    rec = dm_window(owner)
    out = m_tmuxio.tmux("display-message", "-p", "-t", rec.get("pane", ""), "#{pane_pid}", check=False) if rec else None
    return bool(out) and out.returncode == 0 and out.stdout.strip() == rec["pid"]


def dm_talk(messages: list[dict[str, Any]], owner: str, pid: str) -> list[dict[str, Any]]:
    """One tab: the person to this agent alone, and this agent to the person (with others too), and its
    answers to the person's own messages — they are the conversation too, even sent to everyone."""
    # Its answers by /mine's rule (for_person): to the person, or to everyone naming a message the person
    # sent to everyone or to this agent — not its letter to another agent naming one.
    mine = {m["id"]: m["to"] for m in messages if m["from"] == owner}
    return [
        m
        for m in messages
        if (m["from"] == owner and m["to"] == [pid])
        or (
            m["from"] == pid
            and (
                owner in m["to"]
                or ("all" in m["to"] and m.get("re") in mine and ("all" in mine[m["re"]] or pid in mine[m["re"]]))
            )
        )
    ]


BAR_MIN = 24  # an agent cell is at least this wide (columns of text); more agents — another row
BAR_MAX = 40  # and at most this wide
