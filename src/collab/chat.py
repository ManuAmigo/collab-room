from __future__ import annotations
import argparse
import contextlib
import io
import os
import shlex
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any
import collab.boot as m_boot
import collab.text as m_text
import collab.store as m_store
import collab.tmuxio as m_tmuxio
import collab.log as m_log
import collab.screen as m_screen
import collab.installer as m_installer
import collab.deliver as m_deliver
import collab.agents as m_agents
import collab.chat_base as m_chat_base
import collab.chat_help as m_chat_help
import collab.chat_menu as m_chat_menu
import collab.chat_views as m_chat_views
import collab.chat_wizard as m_chat_wizard
import collab.chat_cmds as m_chat_cmds
import collab.chat_input as m_chat_input
import collab.chat_feed as m_chat_feed


def cmd_chat(args: argparse.Namespace) -> None:
    """A person's seat: the room scrolls above, a fixed bottom area holds states, a rule and the input.

    Layout, top to bottom at the end of the window: rule · input · states (the very last row,
    which also keeps the input off the bottom edge). The feed lives in
    a terminal scroll region above it, so incoming messages and state changes never leave copies
    of the bottom area in the history — the earlier "move the cursor up N lines" redraw did exactly
    that after every Enter and on wrapped lines. Without a terminal
    (pipes, tests) the seat falls back to plain line output.
    """
    inst = m_boot.paths()
    rp = m_store.room().paths
    ui = m_chat_base.ChatState()
    born = m_installer.copy_name()  # the collab this chat runs, by its content
    ui.mine_count = 0  # «● N for you» over the input
    dm_focus = [False, True]  # a /dm panel is open beside this chat; the input is here
    folded: list[Any] = [None, None]  # the agent whose /dm line is the feed's last, and that line
    stale_seen: list[Any] = [0.0, False]
    told_old: set[Any] = set()
    if sys.stdin.isatty() and sys.stdout.isatty():
        m_store.chat_mode().sink = []
        m_store.chat_mode().on = True
    import threading

    try:
        import readline
    except ImportError:
        readline = None  # type: ignore[assignment]
    config = m_store.load_config()
    m_store.participant(config, args.as_)
    if args.as_ in m_store.people_ids(config):
        m_log.begin_signing()
    known = m_store.ids_of(config)
    me = m_store.label_of(config, args.as_)
    tty = sys.stdout.isatty() and sys.stdin.isatty() and not args.plain

    # It signs only typed words, in the window the room opened for this person
    # («printf … | collab chat --as owner» got a signature). An agent could still start a chat in
    # a terminal and type into it — a higher bar, not a lock.
    def signs() -> bool:
        # At sending, not at start: the room writes down the chat's window only after opening it. From the
        # checked file: read by its path, a link put in its place decided whose window signs.
        with contextlib.redirect_stderr(io.StringIO()):  # its one line would land on the person's screen
            panes = m_store.read_panes().get("panes")
        own = panes.get(args.as_) if isinstance(panes, dict) else None
        return sys.stdin.isatty() and bool(own) and os.environ.get("TMUX_PANE") == own

    # A message to this person by name is shown whole: cut, its rest could not be read in the chat
    # at all («the message was cut — it's a bug»). Long ones to everyone (commit
    # reports) are still cut, so they don't flood the conversation.
    # One column short of the window, as the feed wraps: a line pretty made the window's full width
    # was cut again there, its last letter alone on the next line.
    # The window's width at each message, not at the start: with /dm opened beside it the chat is a third
    # narrower, and every long line's tail began at column 0 — where only a head begins.
    show = (
        (
            lambda m: m_chat_base.feed_block(
                config, m, shutil.get_terminal_size((100, 24)).columns - 1, 0 if args.as_ in m.get("to", []) else args.max_lines
            )
        )
        if not args.plain
        else (lambda m: m_chat_base.fmt(m, with_id=False, config=config))
    )
    names = m_store.aliases(config)

    def complete(text: str, state: int) -> str | None:
        options = m_store.address_candidates(config, args.as_, text) if text.startswith("@") else []
        return options[state] + " " if state < len(options) else None

    if readline is not None and tty:
        readline.set_completer(complete)
        readline.set_completer_delims(" ")
        # macOS Python is linked with libedit, which wants its own binding syntax.
        readline.parse_and_bind("bind ^I rl_complete" if "libedit" in (readline.__doc__ or "") else "tab: complete")
    lock = threading.RLock()
    printed = {m["id"] for m in m_log.read_messages()}
    stop = threading.Event()
    shown_state: dict[str, str] = {}
    ui.bar = ""
    ui.menu_line = ""

    if tty:
        prompt = f"\001\033[1;{m_store.color_of(config, args.as_)}m\002{me} ›\001\033[0m\002 "
    else:
        prompt = f"{args.as_}> "

    ui.extra_rows = 0  # the status cells' rows beyond the first; everything else sits above them

    def rows() -> int:
        """The bottom row of the chat's own layout: the terminal's height less the extra status rows.
        A change here repaints the screen (pump), so the feed and the input move up by themselves."""
        return shutil.get_terminal_size((100, 24)).lines - ui.extra_rows

    feed_log: list[str] = []  # what the feed shows, to repaint it after a resize
    ui.viewing = None  # /view: {"pid", "pane", "label"} while an agent's screen is shown
    ui.reading = None  # /mine: {"ids", "at"} while one message is shown on its own
    # History («I can't scroll the chat up»; the mouse is off so text can be copied,
    # and a MacBook has no PgUp). The chat scrolls its own feed, one message per ↑/↓ («count
    # messages, not lines»); a message taller than the window goes in parts; Esc — the newest.
    # tmux's copy mode was tried: its arrows move a cursor first, and binding them would change
    # keys of the person's other tmux sessions.
    blocks: list[list[str]] = []  # the feed as it came: one entry per message or notice
    ui.top = None  # first line on screen while reading history; None — not reading
    ui.unseen = 0  # messages that came while the person reads history: shown on return
    ui.older = None  # index (in the room's messages) of the oldest one in the feed

    def feed(text: str, remember: bool = True) -> None:
        return m_chat_feed.chat_append(feed_context, text, remember)

    def draw_bottom() -> None:
        if not tty:
            return
        h, w = rows(), shutil.get_terminal_size((100, 24)).columns
        sys.stdout.write(m_chat_base.bottom_frame(ui.menu_line, ui.bar, h, w))
        sys.stdout.flush()

    def setup() -> None:
        rp = m_store.room().paths
        if not tty:
            return
        # Name the pane once: an earlier version wrote states into the pane title, and tmux kept
        # that stale line above the panel after the chat stopped updating it.
        sys.stdout.write(f"\033]2;collab-room · {me}\033\\")
        m_agents.border_title(m_chat_base.BORDER_CHAT.format(room=rp.root.name.lstrip(".") or rp.root.name))
        h = rows()
        sys.stdout.write("\n" * 3 + f"\033[1;{h - 3}r")
        ui.laid = (h, shutil.get_terminal_size((100, 24)).columns)
        # The /dm panel beside it puts its rule and input on the chat's own rows («the
        # input and the output on one level»): it reads how many rows lie under the chat's rule.
        if os.environ.get("TMUX_PANE"):
            m_tmuxio.tmux("set-option", "-p", "-t", os.environ["TMUX_PANE"], "@collab_below", str(ui.extra_rows + 2), check=False)
        draw_bottom()
        sys.stdout.write(f"\033[{h - 1};1H\033[2K")
        sys.stdout.flush()

    def draw_view() -> None:
        v = ui.viewing
        if not v or not tty:
            return
        h, w = rows(), shutil.get_terminal_size((100, 24)).columns
        shot = m_tmuxio.tmux("capture-pane", "-e", "-p", "-t", v["pane"], check=False)
        out, ui.menu_line = m_chat_views.view_frame(v["label"], shot.returncode, shot.stdout, h, w)
        sys.stdout.write(out)
        draw_bottom()

    def wrapped() -> tuple[list[str], list[int]]:
        """The feed's lines as the window shows them, and where each message starts among them."""
        return m_chat_base.feed_lines(blocks, shutil.get_terminal_size((100, 24)).columns)

    def load_older() -> int:
        """Earlier messages from the room's log, when history reaches the top of what is shown:
        the chat starts with the latest ten, and its memory is not the room's whole history.
        Returns how many lines came in above."""
        if not ui.older:
            return 0
        before = len(wrapped()[0])
        start = max(0, ui.older - 20)
        new = [show(m).split("\n") for m in m_log.read_messages()[start : ui.older]]
        blocks[:0] = new
        feed_log[:0] = [line for block in new for line in block]
        ui.older = start
        return len(wrapped()[0]) - before

    def draw_history() -> None:
        if not tty or ui.top is None:
            return
        h = rows()
        area = max(1, h - 4)
        lines, _ = wrapped()
        sys.stdout.write(m_chat_views.history_frame(lines, ui.top, area))
        ui.menu_line = history_label()
        draw_bottom()

    def history_label() -> str:
        return m_chat_views.history_hint(ui.older, ui.top, ui.unseen, shutil.get_terminal_size((100, 24)).columns)

    def history(direction: int, page: bool = False) -> None:
        top = m_chat_views.history_step(ui.top, max(1, rows() - 4), direction, page, wrapped, load_older, lambda: ui.older)
        if top is None:
            leave_history()
            return
        ui.top = top
        draw_history()

    def leave_history() -> None:
        ui.top = None
        ui.unseen = 0
        ui.menu_line = ""
        del feed_log[:-5000]
        del blocks[:-1000]
        repaint()

    def draw_reading() -> None:
        rp = m_store.room().paths
        r = ui.reading
        if not r or not tty:
            return
        h, w = rows(), shutil.get_terminal_size((100, 24)).columns
        out, menu, m = m_chat_views.reading_frame(config, args.as_, rp.root, r, m_log.read_messages(), h, w, m_chat_base.pretty)
        if m is not None:
            m_log.write_set(rp.receipts_dir, args.as_, m_log.read_set(rp.receipts_dir, args.as_) | {m["id"]})
        sys.stdout.write(out)
        ui.menu_line = menu
        count_mine()
        draw_bottom()

    def stop_reading() -> None:
        ui.reading = None
        ui.menu_line = ""
        repaint()
        run_wizard_step("/mine")  # back to the list, what is read is gone from it

    def open_dm(pid: str) -> None:
        """Open the panel beside this chat (or switch it) on a tab for `pid`; the focus goes there."""
        rp = m_store.room().paths
        state = m_chat_base.dm_tabs(args.as_)
        m_chat_base.set_dm_tabs(args.as_, state["tabs"] + ([pid] if pid not in state["tabs"] else []), pid)
        rec = m_chat_base.dm_window(args.as_)
        if m_chat_base.dm_window_alive(args.as_):
            m_tmuxio.tmux("select-pane", "-t", rec["pane"], check=False)
            return
        own = os.environ.get("TMUX_PANE", "")
        if not tty or not signs():
            say_ = lambda text: feed(m_text.paint(text, "38;5;252"))
            say_("The private-talk panel opens only beside the person's own chat window.")
            m_chat_base.set_dm_tabs(args.as_, [], "")
            return
        width = m_tmuxio.tmux("display-message", "-p", "-t", own, "#{pane_width}", check=False).stdout.strip()
        if not width.isdigit() or int(width) < 90:
            feed(
                m_text.paint("! The window is too narrow for a panel beside the chat — use /answer, or write here.", "1;38;5;196")
            )
            m_chat_base.set_dm_tabs(args.as_, [], "")
            return
        # This chat's own interpreter, not `python3` from PATH: the panel signs only while its window's
        # process is itself, and a wrapper running Python as its child (no exec) left every private
        # word of the person marked ⚠.
        cmd = f"{shlex.quote(sys.executable)} {shlex.quote(m_boot.SELF)} dm --as {shlex.quote(args.as_)}"
        out = m_tmuxio.tmux(
            "split-window",
            "-h",
            "-l",
            "33%",
            "-t",
            own,
            "-P",
            "-F",
            "#{pane_id} #{pane_pid}",
            "-c",
            m_tmuxio.tmux_dir(str(rp.root)),
            *m_tmuxio.pane_env(),
            m_tmuxio.in_dir(str(rp.root), cmd),
            check=False,
        )
        pane, _, ppid = out.stdout.strip().partition(" ")
        if out.returncode != 0 or not pane:
            feed(m_text.paint(f"! Could not open the panel: {out.stderr.strip()}", "1;38;5;196"))
            m_chat_base.set_dm_tabs(args.as_, [], "")
            return
        # Written by this chat alone, with the window's process: only that window signs as the person.
        m_chat_base.remember_dm_window(args.as_, pane, ppid)
        # The border of the half where typing goes is bright — this window only, not the person's others.
        m_tmuxio.tmux("set-option", "-w", "-t", own, "pane-active-border-style", "fg=colour214", check=False)
        m_tmuxio.tmux("set-option", "-w", "-t", own, "pane-border-style", "fg=colour240", check=False)

    def close_dm() -> None:
        rec = m_chat_base.dm_window(args.as_)
        m_chat_base.set_dm_tabs(args.as_, [], "")
        if rec and m_chat_base.dm_window_alive(args.as_):
            m_tmuxio.tmux("kill-pane", "-t", rec["pane"], check=False)
        m_chat_base.forget_dm_window(args.as_)

    def stop_view() -> None:
        ui.viewing = None
        ui.menu_line = ""
        repaint()

    def repaint() -> None:
        """After a resize: clear, rebuild the fixed area for the new height and replay the end of
        the feed — the old screen was garbled and an open /add question was lost from view."""
        h = rows()
        sys.stdout.write(m_chat_base.SYNC_ON + "\033[r\033[2J\033[H")  # one frame: render ends it (SYNC_OFF)
        setup()
        tail = feed_log[-max(1, h - 4) :]
        feed("\n".join(tail), remember=False)
        draw_bottom()
        if ui.top is not None:
            draw_history()
        ui.redraw_input()

    def stale_chat() -> bool:
        """collab on disk is no longer the one this chat runs (checked at most every 10 s)."""
        if time.time() - stale_seen[0] > 10:
            stale_seen[0] = time.time()
            with contextlib.suppress(OSError):
                stale_seen[1] = m_installer.current_name() != born
        return stale_seen[1]

    def old_versions() -> list[str]:
        return m_chat_feed.chat_old_versions(feed_context)

    def states() -> list[str]:
        return m_chat_feed.chat_states(feed_context)

    def fold_into_dm(m: dict[str, Any]) -> bool:
        return m_chat_feed.chat_fold_into_dm(feed_context, m)

    def count_mine(messages: list[dict[str, Any]] | None = None) -> None:
        """«● N for you»: unread messages for the person and agents waiting for them right now. The feed
        passes the log it read this tick: read twice a tick, a long log cost twice."""
        rp = m_store.room().paths
        read = m_log.read_set(rp.receipts_dir, args.as_)
        st = m_screen.read_status()
        messages = m_log.read_messages() if messages is None else messages
        # A quiet note is not counted: «● N for you» is what wakes the person.
        mine = [m for m in m_log.for_person(messages, args.as_) if m["id"] not in read and not m_deliver.is_quiet(m, config)]
        ui.mine_count = len(mine) + sum(
            1 for pid, x in st.items() if pid != args.as_ and pid in known and x.get("state") == "needs_owner"
        )

    held_alerts: list[str] = []

    def pump() -> None:
        return m_chat_feed.chat_pump(feed_context)

    if not tty:
        print(f"collab-room as {args.as_}. Write `@id text` to address, plain text goes to all. Ctrl-D to leave.")
    else:
        setup()
    welcome = tty and not inst.welcomed_path.exists() and args.as_ in m_store.people_ids(config)
    feed_context = m_chat_feed.ChatFeedContext(
        STALE_CHAT=m_chat_base.STALE_CHAT,
        args=args,
        blocks=blocks,
        config=config,
        count_mine=count_mine,
        dm_focus=dm_focus,
        dm_tabs=m_chat_base.dm_tabs,
        dm_talk=m_chat_base.dm_talk,
        dm_window_alive=m_chat_base.dm_window_alive,
        draw_bottom=draw_bottom,
        draw_history=draw_history,
        draw_view=draw_view,
        feed=feed,
        feed_frame=m_chat_base.feed_frame,
        feed_log=feed_log,
        fold_into_dm=fold_into_dm,
        folded=folded,
        held_alerts=held_alerts,
        known=known,
        lock=lock,
        me=me,
        names=names,
        old_versions=old_versions,
        printed=printed,
        refresh_people=lambda *a, **k: refresh_people(*a, **k),  # defined further down
        repaint=repaint,
        rows=rows,
        show=show,
        shown_state=shown_state,
        stale_chat=stale_chat,
        states=states,
        status_bar_rows=m_chat_base.status_bar_rows,
        stop=stop,
        targets=lambda: targets,
        told_old=told_old,
        tty=tty,
        ui=ui,
        wizard=lambda: wizard,
    )
    if tty and not welcome:
        feed(m_chat_help.HELP(me, ids=known))
    # The latest messages last, right above the input: under the guide they scrolled out of a
    # small window, and with the mouse off the reopened room looked empty.
    everything = m_log.read_messages()
    recent = everything[-10:]
    ui.older = len(everything) - len(recent)
    for m in recent:
        feed(show(m))
    if tty and recent and not welcome:
        feed(m_text.paint("  /help — the guide · ↑ — earlier messages (Esc — back)", "2"))
    if welcome:
        # First visit of this person (once per user, not per room): a short welcome and a
        # choice; nothing is sent and no agent is started by it. Last, above the choice: printed
        # before the latest messages it scrolled out of sight.
        feed(
            m_text.paint(
                "Welcome to your room\n  Write to your agents here.\n"
                "  Tab chooses who receives your message. Enter sends it.\n"
                "  Type / to choose an action. You don't need to remember commands.\n"
                "  Each agent works in a window of its own — you never need to go there:\n"
                "  /view shows its screen, /answer opens it to reply, Ctrl+] brings you back.",
                "1;38;5;252",
            )
            + "\n" * 4
        )  # room for the choice under it: the menu is drawn over the feed's bottom
    for alert in states():
        feed("\n" + alert)
    with contextlib.suppress(Exception):
        m_log.mine_since(args.as_, start=True)
        count_mine()
    draw_bottom()

    # Recipients cycled with Tab / Shift+Tab («@ и Tab неудобно»): Всем first.
    targets = ["all"] + [pid for pid in known if pid != args.as_]
    ui.redraw_input = lambda: None  # set once the input line exists
    ui.selected = "all"  # the chosen id, not an index: survives participants joining
    wizard: dict[str, Any] = {}
    if welcome:
        wizard.update(
            step="welcome", prompt="Welcome to your room", options=[("Show me around", "tour"), ("Skip for now", "skip")]
        )
    if tty and not welcome and args.as_ in m_store.people_ids(config) and m_chat_wizard.offer_team(config):
        # A new room: «What will we do?» first — a ready team, or none (Esc, or «No ready team»).
        m_chat_wizard.open_scenarios_into(wizard)
    if tty and args.as_ in m_store.people_ids(config) and m_store.name_asked(args.as_):
        # Named after the computer's account («Andreynaumov»): asked before anything else, the welcome after.
        mine = m_store.label_of(config, args.as_)
        wizard.update(
            step="own_name",
            then=dict(wizard),
            options=None,
            prompt=f"The room calls you {mine} — your account's name. Your name (Enter keeps {mine}):",
        )
        feed("\n" + wizard["prompt"])
    ui.cfg_mtime = rp.config_path.stat().st_mtime if rp.config_path.exists() else 0.0

    def refresh_people() -> bool:
        return m_chat_feed.chat_refresh_people(feed_context)

    def step(delta: int) -> None:
        if ui.selected not in targets:
            ui.selected = targets[0]
            return
        i = targets.index(ui.selected)
        ui.selected = targets[(i + delta) % len(targets)]

    holder = {"config": config}

    def add_wizard(line: str) -> None:
        return m_chat_cmds.chat_add_wizard(command_context, line)

    def wizard_back() -> None:
        return m_chat_cmds.chat_wizard_back(command_context)

    def ask_exit() -> None:
        return m_chat_cmds.chat_ask_exit(command_context)

    def stay_open() -> None:
        return m_chat_cmds.chat_stay_open(command_context)

    def show_folders(cur: Path) -> None:
        return m_chat_cmds.chat_show_folders(command_context, cur)

    def run_wizard_step(line: str) -> None:
        return m_chat_wizard.chat_run_wizard_step(command_context, line)

    def submit(line: str) -> None:
        return m_chat_cmds.chat_submit(command_context, line)

    def handle(line: str) -> None:
        return m_chat_cmds.chat_handle(command_context, line)

    command_context = m_chat_wizard.ChatCommandContext(
        add_wizard=add_wizard,
        after_key=lambda: after_lock,
        args=args,
        blocks=blocks,
        buffer=lambda: buf,
        clipboard=m_chat_base.copy_to_clipboard,
        close_dm=close_dm,
        commands=lambda: COMMANDS,
        config=config,
        count_mine=count_mine,
        draw_reading=draw_reading,
        draw_view=draw_view,
        feed=feed,
        feed_log=feed_log,
        format_message=m_chat_base.pretty,
        handle=handle,
        help_tasks=m_chat_help.HELP_TASKS,
        help_text=m_chat_help.HELP,
        holder=holder,
        known=known,
        lock=lock,
        me=me,
        menu_items=lambda: menu_items(),
        names=names,
        open_dm=open_dm,
        people_for=lambda cmd: people_for(cmd),
        printed=printed,
        refresh_people=refresh_people,
        repaint=repaint,
        run_wizard_step=run_wizard_step,
        show=show,
        show_folders=show_folders,
        signs=signs,
        stale_chat=stale_chat,
        stale_notice=m_chat_base.STALE_CHAT,
        stay_open=stay_open,
        submit=submit,
        tty=tty,
        ui=ui,
        whole_line=m_chat_base.WHOLE,
        wizard=wizard,
        wizard_back=wizard_back,
    )

    ui.popup = 0  # how many popup rows are drawn over the feed now
    # The feed's thread starts only now, when all it reads is there: started before, its first tick could
    # come before `popup`, `wizard`, `refresh_people`… — a NameError ended the feed for good, the input
    # still sending; without a terminal it came every time, `popup` being set only further down.
    threading.Thread(target=pump, daemon=True).start()
    if not tty:
        while True:
            try:
                line = input(prompt)
            except (EOFError, KeyboardInterrupt):
                print()
                return
            try:
                submit(line)
            except EOFError:
                return

    import termios
    import tty as ttymod

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    ttymod.setcbreak(fd)
    # Ctrl+C comes as a key, not a signal: it asks before closing («спрашивать») —
    # one press used to close the chat at once, also when meant as «copy».
    attrs = termios.tcgetattr(fd)
    attrs[3] &= ~termios.ISIG
    # Ctrl+O is a key too (to the /dm panel): macOS's terminal driver took it as «discard output»
    # and the chat never saw it. No extended input processing — Ctrl+V/Ctrl+O come through as keys.
    attrs[3] &= ~termios.IEXTEN
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    # Bracketed paste: a pasted block arrives as one message. A blinking bar shows which half the
    # typing goes to, with a /dm panel beside («I can't see which chat I'm in»).
    sys.stdout.write("\033[?2004h" + m_chat_base.CURSOR_BLINK)
    buf: list[str] = []
    ui.pos = 0
    ui.menu_index = 0
    ui.menu_closed = False
    ui.menu_moved = False
    ui.suggested = False  # the draft is a suggested name: typing replaces it, ← → edit it
    ui.came_back = False  # Esc just stepped back from a command's menu to «/»
    after_lock: list[Any] = []  # what runs once a key is handled, without the chat's lock (the popup)
    COMMANDS = [
        ("/add", "start a new agent"),
        ("/remove", "disconnect a participant"),
        ("/mute", "room-wide messages stop; by name still arrive"),
        ("/unmute", "deliver to an agent again"),
        ("/unlimit", "an agent is back from its usage limit: deliver to it again"),
        ("/name", "rename yourself or an agent"),
        ("/view", "see an agent's screen (read-only)"),
        ("/answer", "respond in an agent terminal over the chat"),
        ("/open", "open an agent's window in a new tab"),
        ("/pause", "pause the room: agents get no messages"),
        ("/resume", "resume the room: waiting messages go out"),
        ("/clear", "clear the history (moved to an archive)"),
        ("/help", "what would you like to do? — tasks and the guide"),
        ("/mine", "what is for you: unread messages, agents waiting for you"),
        ("/dm", "talk privately with an agent in a panel beside the chat"),
        ("/restart", "restart an agent, its conversation continues"),
        ("/team", "the team: see it, or start a ready one"),
        ("/stop", "stop the room: close its windows and agents"),
        ("/exit", "close this chat panel"),
    ]
    slash_commands = tuple(c for c, _ in COMMANDS)

    def people_for(cmd: str) -> list[str]:
        return m_chat_cmds.chat_people_for(command_context, cmd)

    def menu_items() -> list[tuple[str, str, str]]:
        return m_chat_cmds.chat_menu_items(command_context)

    def command_matches() -> list[tuple[str, str, str]]:
        return m_chat_cmds.chat_command_matches(command_context)

    def choose_command() -> str | None:
        return m_chat_cmds.chat_choose_command(command_context)

    def render() -> None:
        return m_chat_menu.chat_render(menu_context)

    menu_context = m_chat_menu.ChatMenuContext(
        args=args,
        buf=buf,
        config=config,
        dm_focus=dm_focus,
        draw_bottom=draw_bottom,
        draw_reading=draw_reading,
        feed_log=feed_log,
        history_label=history_label,
        menu_items=menu_items,
        repaint=repaint,
        rows=rows,
        ui=ui,
        wizard=wizard,
    )

    ui.redraw_input = render
    with lock:
        render()
    input_context = m_chat_input.ChatInputContext(
        after_lock=after_lock,
        args=args,
        ask_exit=ask_exit,
        buf=buf,
        choose_command=choose_command,
        commands=COMMANDS,
        dm_window=m_chat_base.dm_window,
        dm_window_alive=m_chat_base.dm_window_alive,
        draw_reading=draw_reading,
        fd=fd,
        feed=feed,
        history=history,
        leave_history=leave_history,
        lock=lock,
        menu_items=menu_items,
        render=render,
        repaint=repaint,
        slash_commands=slash_commands,
        step=step,
        stop_reading=stop_reading,
        stop_view=stop_view,
        submit=submit,
        ui=ui,
        wizard=wizard,
        wizard_back=wizard_back,
    )
    try:
        m_chat_input.chat_read_keys(input_context)
    except KeyboardInterrupt:
        return
    finally:
        stop.set()
        with contextlib.suppress(Exception):
            close_dm()  # the panel closes with the chat
        if tty:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            except Exception:
                pass
            sys.stdout.write("\033[?2004l\033[r" + m_chat_base.CURSOR_PLAIN + f"\033[{rows()};1H\n")
            sys.stdout.flush()
            m_agents.leave_the_room_window()


def run_chat(args: argparse.Namespace) -> None:
    """The person's chat, with why it ended written to .collab/chat-exit.log: once both
    of the person's chats vanished and nothing could tell why. A window closed by tmux
    (SIGHUP) or a kill (SIGTERM) ends it like /exit does — the terminal is restored — and says so."""
    import signal
    import traceback

    def ended(reason: str) -> None:
        rp = m_store.room().paths
        try:
            m_store.write_state(
                rp.state_dir / "chat-exit.log", f"{m_store.now()} {args.as_} pid {os.getpid()}: {reason}\n", append=True
            )
        except (OSError, SystemExit, m_store.OperationFailed):
            pass  # the log must never be why the chat fails

    def on_signal(number: int, _frame: Any) -> None:
        raise SystemExit(f"signal {signal.Signals(number).name}")

    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, on_signal)
    code: Any = 0
    try:
        cmd_chat(args)
        ended("closed: /exit, Ctrl+C or end of input")
    except SystemExit as exc:
        ended(str(exc.code) if isinstance(exc.code, str) else f"exit code {exc.code}")
        code = exc.code
    except BaseException:
        ended("crashed:\n" + traceback.format_exc())
        with contextlib.suppress(OSError, ValueError):
            traceback.print_exc()
        code = 1

    # Not through the interpreter's own end: the feed thread may be inside a write to the terminal
    # just then (a busy tmux reads it late), holding stdout — and Python aborted, «Python quit
    # unexpectedly» on the person's screen six times in a day. What is left is written
    # out if it can be in a moment, and the chat ends there.
    def flush() -> None:
        with contextlib.suppress(OSError, ValueError):
            sys.stdout.flush()
            sys.stderr.flush()

    if isinstance(code, str):
        with contextlib.suppress(OSError, ValueError):
            print(code, file=sys.stderr)
    flushing = threading.Thread(target=flush, daemon=True)
    flushing.start()
    flushing.join(1.0)
    os._exit(code if isinstance(code, int) else (0 if code is None else 1))
