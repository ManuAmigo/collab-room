from __future__ import annotations
import argparse
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
import collab.boot as m_boot
import collab.text as m_text
import collab.store as m_store
import collab.tmuxio as m_tmuxio
import collab.log as m_log
import collab.screen as m_screen
import collab.deliver as m_deliver
import collab.agents as m_agents
import collab.lifecycle as m_lifecycle


@dataclass(frozen=True)
class ChatCommandContext:
    """The chat's existing objects and callbacks; late-created input objects are read on demand."""

    add_wizard: Callable[..., Any]
    after_key: Callable[[], list[Any]]
    args: argparse.Namespace
    blocks: list[list[str]]
    buffer: Callable[[], list[str]]
    clipboard: Callable[..., Any]
    close_dm: Callable[..., Any]
    commands: Callable[[], list[tuple[str, str]]]
    config: dict[str, Any]
    count_mine: Callable[..., Any]
    draw_reading: Callable[..., Any]
    draw_view: Callable[..., Any]
    feed: Callable[..., Any]
    feed_log: list[str]
    format_message: Callable[..., Any]
    handle: Callable[..., Any]
    help_tasks: list[Any]
    help_text: Callable[..., Any]
    holder: dict[str, Any]
    known: list[str]
    lock: Any
    me: str
    menu_items: Callable[..., Any]
    names: dict[str, str]
    open_dm: Callable[..., Any]
    people_for: Callable[..., Any]
    printed: set[str]
    refresh_people: Callable[..., Any]
    repaint: Callable[..., Any]
    run_wizard_step: Callable[..., Any]
    show: Callable[..., Any]
    show_folders: Callable[..., Any]
    signs: Callable[..., Any]
    stale_chat: Callable[..., Any]
    stale_notice: str
    stay_open: Callable[..., Any]
    submit: Callable[..., Any]
    tty: bool
    ui: Any
    whole_line: str
    wizard: dict[str, Any]
    wizard_back: Callable[..., Any]


def chat_run_wizard_step(ctx: ChatCommandContext, line: str) -> None:
    """/add, /remove, /mute, /name, /exit and the others: every answer is chosen in the menu;
    only what has to be written — a name, a path as the last resort — is typed."""
    rp = m_store.room().paths
    say_ = lambda text: ctx.feed(m_text.paint(text, "38;5;252"))
    if line in ("/exit", "/quit"):
        ctx.wizard.clear()
        ctx.wizard.update(
            step="exit_confirm",
            from_cmd="/exit",
            prompt="Close chat? Agents keep running",
            options=[("No", "n"), ("Yes", "y")],
        )
        return
    if line in ("/mute", "/unmute", "/unlimit"):
        ctx.wizard.clear()
        people = ctx.people_for(line)
        if not people:
            say_({"/mute": "No one to mute.", "/unmute": "No one is muted.", "/unlimit": "No one is out of its limit."}[line])
            return
        prompt = {
            "/mute": "Mute whom? The room stops sending them messages",
            "/unmute": "Unmute whom?",
            "/unlimit": "Whose limit is over? The room sends its messages again",
        }[line]
        ctx.wizard.update(
            step="mute_pick",
            verb=line,
            from_cmd=line,
            prompt=prompt,
            options=[(m_store.label_of(ctx.config, x), x) for x in people],
        )
        return
    if line == "/name":
        ctx.wizard.clear()
        ctx.wizard.update(
            step="name_pick",
            from_cmd="/name",
            prompt="Rename whom?",
            options=[(m_store.label_of(ctx.config, x) + (" (you)" if x == ctx.args.as_ else ""), x) for x in ctx.known],
        )
        return
    if line.startswith("/remove "):
        pid = ctx.names.get(line[len("/remove ") :].strip().lower().lstrip("@"))
        if not pid or pid in ("all", ctx.args.as_):
            say_("Choose someone from the list.")
            return
        ctx.wizard.clear()
        ctx.wizard.update(
            step="rm_confirm",
            pid=pid,
            options=[("No", "n"), ("Yes, disconnect", "y")],
            prompt=f"Disconnect {m_store.label_of(ctx.config, pid)}?",
            from_cmd="/remove-name",
        )
        say_(f"Disconnect {m_store.label_of(ctx.config, pid)}? Room messages will stop. The agent will keep running.")
        return
    if line == "/remove":
        people = [pid for pid in ctx.known if pid != ctx.args.as_]
        if not people:
            say_("No one to disconnect.")
            return
        ctx.wizard.clear()
        ctx.wizard.update(
            step="rm_pick",
            people=people,
            from_cmd="/remove",
            options=[(m_store.label_of(ctx.config, x), str(i)) for i, x in enumerate(people, 1)],
        )
        ctx.wizard["prompt"] = "Choose an agent to disconnect"
        say_(ctx.wizard["prompt"] + ".")
        return
    if line == "/help":
        ctx.wizard.clear()
        paused = m_store.load_config().get("paused")
        options = []
        for key, title, _, _, _ in ctx.help_tasks:
            if key == "pause":
                title = "Resume the room" if paused else "Pause the room"
            options.append((title, key))
        ctx.wizard.update(step="help_pick", from_cmd="/help", prompt="What would you like to do?", options=options)
        return
    if line in ("/restart", "/add") and ctx.stale_chat():
        # Old code in memory would start the agent with yesterday's rule — a new Codex then asked
        # the person for every answer. Reopening the chat runs the new.
        ctx.wizard.clear()
        ctx.feed(m_text.paint("! " + ctx.stale_notice, "1;38;5;196"))
        return
    if line == "/dm" or line.startswith("/dm "):
        # Private talks in a panel beside the chat.
        arg = line[3:].strip()
        if arg == "close":
            ctx.close_dm()
            say_("The private-talk panel is closed.")
            return
        if arg:
            pid = ctx.names.get(arg.lower().lstrip("@"))
            if not pid or pid in ("all", ctx.args.as_):
                say_("Choose someone from the list.")
                return
            ctx.open_dm(pid)
            return
        ctx.wizard.clear()
        cfg = m_store.load_config()
        others = [q["id"] for q in cfg["participants"] if q["id"] != ctx.args.as_]
        if not others:
            say_("No agents in the room yet. Add one with /add.")
            return
        ctx.wizard.update(
            step="dm_pick",
            from_cmd="/dm",
            prompt="Talk privately with whom?",
            options=[(m_store.label_of(cfg, x), x) for x in others],
        )
        return
    if line == "/mine":
        ctx.wizard.clear()
        cfg = m_store.load_config()
        st = m_screen.read_status()
        waiting = [
            (
                f"‼ {m_store.label_of(cfg, pid)} waits for you: {m_text.clean_field(x.get('detail', ''), 60) or 'needs your input'}",
                "agent:" + pid,
            )
            for pid, x in st.items()
            if pid != ctx.args.as_ and pid in m_store.ids_of(cfg) and x.get("state") == "needs_owner"
        ]
        everything = m_log.read_messages()
        read = m_log.read_set(rp.receipts_dir, ctx.args.as_)
        unread = [m for m in m_log.for_person(everything, ctx.args.as_) if m["id"] not in read]
        # «Messages for you: unread and recent history» — it opens
        # always; Enter says where it leads; below the new, the last twenty already read.
        agents = set(m_store.ids_of(cfg)) - set(m_store.people_ids(cfg))

        def row(m: dict[str, Any], kind: str) -> tuple[str, str]:
            first = m_text.clean_field(m_text.visible(m["text"]).splitlines()[0] if m["text"].strip() else "", 50)
            where = "Open conversation" if m["from"] in agents else "Open"
            return f"{m_store.label_of(cfg, m['from'])} {m_text.local_hm(m['at'])} — {first}  · {where}", f"{kind}:{m['id']}"

        items = [row(m, "msg") for m in unread]
        earlier = [
            row(m, "old")
            for m in reversed([m for m in m_log.for_person(everything, ctx.args.as_, all_time=True) if m["id"] in read][-20:])
        ]
        waiting = [(label + "  · Open agent terminal", value) for label, value in waiting]
        if not waiting and not items and not earlier:
            say_("No messages for you yet.")
            return
        others = sum(
            1 for m in everything if m["from"] != ctx.args.as_ and "all" in m["to"] and m["id"] not in read and m not in unread
        )
        mark = [("✓ Mark all as read", "all:")] if items else []
        # The line between new and earlier only when there is something new above it: with nothing new
        # the choice started on it, and it looked chosen — the question says it then.
        line_ = [("── Earlier for you ──", "sep:")] if earlier and (items or waiting) else []
        new = len(waiting) + len(items)
        prompt = (f"For you · {new} new" if new else "No unread messages. Earlier messages are below.") + (
            f" · +{others} for everyone" if others else ""
        )
        ctx.wizard.update(step="mine_pick", from_cmd="/mine", options=waiting + items + mark + line_ + earlier, prompt=prompt)
        return
    if line == "/restart":
        ctx.wizard.clear()
        cfg = m_store.load_config()
        mine = [
            q["id"] for q in cfg["participants"] if q.get("started_by_room") and m_screen.agent_kind(q) in ("claude", "codex")
        ]
        if not mine:
            say_("No agent the room started. Add one with /add.")
            return
        ctx.wizard.update(
            step="restart_pick",
            from_cmd="/restart",
            prompt="Restart whom? The conversation continues",
            options=[(m_store.label_of(cfg, x), x) for x in mine],
        )
        return
    if line.startswith("/answer "):
        # «/answer Bob», from the command menu's names or typed.
        pid = ctx.names.get(line[len("/answer ") :].strip().lower().lstrip("@"))
        if not pid or pid in ("all", ctx.args.as_):
            say_("Choose someone from the list.")
            return
        ctx.wizard.clear()
        ctx.wizard.update(step="answer_pick", from_cmd="/answer")
        ctx.run_wizard_step(pid)
        return
    if line in ("/view", "/open", "/answer"):
        ctx.wizard.clear()
        cfg = m_store.load_config()
        panes = m_deliver.current_panes(cfg)
        others = [p["id"] for p in cfg["participants"] if p["id"] != ctx.args.as_]
        if not others:
            say_("No agents in the room yet. Add one with /add.")
            return
        prompt = {"/view": "View an agent", "/open": "Open an agent's window", "/answer": "Answer whom?"}[line]
        ctx.wizard.update(
            step=line[1:] + "_pick",
            from_cmd=line,
            prompt=prompt,
            options=[(m_store.label_of(cfg, x) + ("" if panes.get(x) else " — not in tmux"), x) for x in others],
        )
        say_(prompt + ".")
        return
    if line == "/clear":
        ctx.wizard.clear()
        ctx.wizard.update(
            step="clear_confirm",
            options=[("No", "n"), ("Yes, clear the history", "y")],
            prompt="Clear the room's history?",
            from_cmd="/clear",
        )
        say_(
            "Clear the room's history? Everyone's feed starts empty; the messages move to an archive "
            "in .collab/archive/, they are not deleted."
        )
        return
    if line == "/stop":
        ctx.wizard.clear()
        _, closing, staying = m_lifecycle.stop_plan(m_store.load_config())
        ctx.wizard.update(
            step="stop_confirm", options=[("No", "n"), ("Yes, stop the room", "y")], prompt="Stop the room?", from_cmd="/stop"
        )
        say_("Stop the room? " + m_lifecycle.describe_stop(closing, staying))
        return
    if line == "/add":
        # Only agents the room starts itself: a session already running somewhere
        # lives in its own folder, its status never reached the room.
        ctx.wizard.clear()
        kinds = [k for k in m_screen.PRESETS if shutil.which(m_screen.PRESETS[k]["command"][0])]
        if not kinds:
            say_("No Claude Code or Codex installation found.")
            return
        ctx.wizard.update(
            step="kind",
            kinds=kinds,
            prompt="Which agent to start?",
            from_cmd="/add",
            options=[(m_screen.PRESETS[k]["label"], str(i)) for i, k in enumerate(kinds, 1)],
        )
        say_("Add an agent. " + ctx.wizard["prompt"])
        return
    if ctx.wizard.get("step") == "own_name":
        problem = line and (
            m_text.name_problem(line)
            or ("that is already someone else's name here" if m_store.label_taken(ctx.config, ctx.args.as_, line) else "")
        )
        if problem:
            say_(f"! {problem}. Try another name, or Enter to keep yours.")
            return
        then = ctx.wizard.get("then") or {}
        ctx.wizard.clear()
        if line:
            ctx.handle(f"/name {ctx.args.as_} {line}")
        else:
            say_(f"You stay {m_store.label_of(ctx.config, ctx.args.as_)}. /name changes it any time.")
        ctx.wizard.update(then)  # the welcome, when this is the first visit — after the name is set
        return
    if line == "" and ctx.wizard.get("step") != "folder":  # in «folder» an empty Enter asks again
        ctx.wizard.clear()
        say_("Canceled.")
        return
    step_ = ctx.wizard.get("step")
    if step_ == "exit_confirm":
        if line == "y":
            raise EOFError
        if "draft" in ctx.wizard:
            ctx.stay_open()
        else:
            ctx.wizard.clear()
        return
    if step_ == "mine_pick":
        kind, _, ref = line.partition(":")
        options_ = ctx.wizard.get("options")
        ctx.wizard.clear()
        if kind == "all":
            m_log.mark_all_mine_read(ctx.args.as_)
            ctx.count_mine()
            say_("All marked as read. Only what comes from now on counts.")
            return
        if kind == "sep":
            ctx.run_wizard_step("/mine")  # the line between new and earlier is no choice: the list stays
            return
        if kind in ("msg", "old"):
            m = next((x for x in m_log.read_messages() if x["id"] == ref), None)
            agents = set(m_store.ids_of(m_store.load_config())) - set(m_store.people_ids(m_store.load_config()))
            if m is not None and m["from"] in agents and ctx.tty:
                # «Open conversation»: its /dm tab, to answer in the conversation.
                ctx.open_dm(m["from"])
                return
        if kind == "agent":
            # Enter on an agent waiting for you opens its window — /answer, over the chat
            # (/answer, not /open; without a popup it points to /open).
            ctx.wizard.update(step="answer_pick")
            ctx.run_wizard_step(ref)
            return
        # Read is what was opened, not what flashed by: the message on its own, over the
        # feed; ↑/↓ the others of the list; Enter or Esc — back to the list.
        ids = [v.split(":", 1)[1] for _, v in (options_ or []) if v.startswith(("msg:", "old:"))]
        if ref not in ids:
            return
        if not ctx.tty:
            m = next((x for x in m_log.read_messages() if x["id"] == ref), None)
            if m is not None:
                ctx.feed(ctx.format_message(ctx.config, m, 100, 0))
                m_log.write_set(rp.receipts_dir, ctx.args.as_, m_log.read_set(rp.receipts_dir, ctx.args.as_) | {ref})
            return
        ctx.ui.reading = {"ids": ids, "at": ids.index(ref)}
        ctx.draw_reading()
        return
    if step_ == "dm_pick":
        ctx.wizard.clear()
        if line in m_store.ids_of(m_store.load_config()):
            ctx.open_dm(line)
        return
    if step_ == "restart_pick":
        cfg = m_store.load_config()  # an agent added a moment ago is not in this chat's list yet
        if line not in m_store.ids_of(cfg):
            return
        ctx.wizard.update(
            step="restart_confirm",
            pid=line,
            options=[("No", "n"), ("Yes, restart", "y")],
            prompt=f"Restart {m_store.label_of(cfg, line)}?",
        )
        say_(
            f"Restart {m_store.label_of(cfg, line)}? Its program closes and starts again in the same window with "
            "the same conversation; what it was doing at this moment stops. It reads the room's newest rules."
        )
        return
    if step_ == "restart_confirm":
        pid = ctx.wizard["pid"]
        ctx.wizard.clear()
        if line != "y":
            say_("Canceled.")
            return
        cfg = m_store.load_config()
        q = next((x for x in cfg["participants"] if x["id"] == pid), None)
        if q is None:
            say_("It is no longer in the room.")
            return
        found = m_screen.agent_session(q)  # read once, here: restart_agent takes it, not a second search
        if not found:
            ctx.wizard.update(
                step="restart_fresh",
                pid=pid,
                options=[("No", "n"), ("Yes, a new conversation", "y")],
                prompt=f"{m_store.label_of(cfg, pid)}'s conversation was not found. Start a new one?",
            )
            say_(
                f"{m_store.label_of(cfg, pid)}'s conversation was not found. Start it with a new conversation? "
                "What it knew stays only in its files."
            )
            return
        line, step_ = "go", "restart_go"
    else:
        found = ""
    if step_ == "restart_fresh":
        pid = ctx.wizard["pid"]
        ctx.wizard.clear()
        if line != "y":
            say_("Canceled.")
            return
        step_ = "restart_new"
    if step_ in ("restart_go", "restart_new"):
        cfg = m_store.load_config()
        q = next((x for x in cfg["participants"] if x["id"] == pid), None)
        if q is None:
            say_("It is no longer in the room.")
            return
        try:
            m_agents.restart_agent(cfg.get("session", "collab-room"), q, fresh=step_ == "restart_new", sid=found)
        except (m_store.OperationFailed, SystemExit) as exc:
            ctx.feed(m_text.paint(f"! Could not restart: {exc}", "1;38;5;196"))
            return
        how = "with a new conversation" if step_ == "restart_new" else "with its conversation"
        say_(f"Restarting {m_store.label_of(cfg, pid)} {how}. Waiting for a connection check reply.")
        ctx.refresh_people()
        return
    if step_ == "mute_pick":
        verb = ctx.wizard["verb"]
        ctx.wizard.clear()
        if line in ctx.known:
            # By id: by name, two of one name muted the last of them.
            ctx.handle(f"{verb} {line}")
        return
    if step_ == "name_pick":
        if line not in ctx.known:
            return
        ctx.wizard.update(
            step="rename",
            pid=line,
            options=None,
            prompt=f"New name for {m_store.label_of(ctx.config, line)} (Enter to save, Esc to go back):",
        )
        say_(ctx.wizard["prompt"])
        return
    if step_ == "rename":
        pid, label = ctx.wizard["pid"], line
        problem = m_text.name_problem(label) or (
            "that is already someone else's name here" if m_store.label_taken(ctx.config, pid, label) else ""
        )
        if problem:
            say_(f"! {problem}. Try another name.")
            return
        ctx.wizard.clear()
        ctx.handle(f"/name {pid} {label}")
        return
    if step_ == "welcome":
        m_agents.mark_welcomed()
        ctx.wizard.clear()
        if line == "tour":
            ctx.run_wizard_step("/help")
        else:
            say_("You can open the guide any time: type /help.")
        return
    if step_ == "help_pick":
        task = next((t for t in ctx.help_tasks if t[0] == line), None)
        if task is None:
            return
        key, title, steps, action, command = task
        if key == "keys":
            ctx.feed("\n" + ctx.help_text(ctx.me, full=True, ids=ctx.known))
            return  # the menu stays open: Esc closes it
        if key == "pause":
            paused = m_store.load_config().get("paused")
            action, command = ("Resume the room", "/resume") if paused else ("Pause the room", "/pause")
        ctx.wizard.update(step="help_task", options=[(action, "go"), ("Back", "back")], prompt=title, command=command)
        # Room under the steps for the choice: the menu is drawn over the feed's bottom lines.
        say_(title + "\n" + "\n".join(f"  {i}. {x}" for i, x in enumerate(steps, 1)) + "\n" * 4)
        return
    if step_ == "help_task":
        if line == "back":
            ctx.wizard_back()
            return
        command = ctx.wizard.get("command", "")
        ctx.wizard.clear()
        if command in ("/pause", "/resume"):
            ctx.submit(command)
        elif command:
            ctx.run_wizard_step(command)  # the existing flow, with its own questions and confirmations
        return
    if step_ == "view_pick":
        ctx.wizard.clear()
        cfg = m_store.load_config()
        pane = m_deliver.current_panes(cfg).get(line)
        if not pane:
            say_("Live view unavailable: this agent is not in tmux.")
            return
        ctx.ui.viewing = {"pid": line, "pane": pane, "label": m_store.label_of(cfg, line)}
        ctx.draw_view()
        return
    if step_ == "answer_pick":
        # The agent's own window over the chat, at once — /open stays the way to a
        # new tab. Ctrl+] comes back to the chat where it was.
        ctx.wizard.clear()
        cfg = m_store.load_config()
        label = m_store.label_of(cfg, line)
        if not m_deliver.current_panes(cfg).get(line):
            say_(f"Cannot open {label}'s window: this agent is not in tmux.")
            return

        # Opened once the key is handled, outside the chat's lock: holding it while the popup was
        # open stopped the feed and the states under it — red until Ctrl+].
        def show_popup(pid: str = line, label: str = label) -> None:
            why = m_agents.open_popup(pid)
            with ctx.lock:
                if why:
                    say_(
                        f"Cannot open {label}'s window over the chat: {why}. Use /open for a command to paste into a new terminal tab."
                    )
                else:
                    ctx.repaint()
                    # «Answer whom?.» stayed the last line, as if still asked.
                    say_("Back in the room.")

        ctx.after_key().append(show_popup)
        return
    if step_ == "open_pick":
        # Choose the agent — and the steps are there, the command already copied.
        ctx.wizard.clear()
        cfg = m_store.load_config()
        if not m_deliver.current_panes(cfg).get(line):
            say_("Cannot open: this agent is not in tmux.")
            return
        command, label = m_agents.open_command(line), m_store.label_of(cfg, line)
        # Say plainly that the command is already in the clipboard — and show it,
        # in case that line goes unread.
        if ctx.clipboard(command):
            say_(
                "The command is already in your clipboard — you don't need to copy anything.\n"
                f"  1. Open a new terminal tab (+).\n"
                f"  2. Paste with Cmd+V and press Enter: {label}'s window opens there.\n"
                f"  3. Close that tab when done: {label} keeps running, the room stays here.\n"
                f"If pasting doesn't work, copy this command yourself:\n\n{ctx.whole_line}  {command}\n"
            )
        else:
            say_(
                "Could not copy automatically — copy this command yourself:\n\n"
                f"{ctx.whole_line}  {command}\n\n"
                f"  1. Open a new terminal tab (+).\n"
                f"  2. Paste the command and press Enter: {label}'s window opens there.\n"
                f"  3. Close that tab when done: {label} keeps running, the room stays here.\n"
            )
        return
    if step_ == "clear_confirm":
        ctx.wizard.clear()
        if line.lower() not in ("y", "yes", "д", "да"):
            say_("Canceled.")
            return
        where = m_log.archive_history()
        # The history ↑ goes too: it kept the old messages and «feed starts empty» was not
        # true; nothing older is left in the new feed.
        ctx.feed_log.clear()
        ctx.blocks.clear()
        ctx.ui.older, ctx.ui.top, ctx.ui.unseen = 0, None, 0
        ctx.repaint()
        say_(f"History cleared. The old messages are in {m_text.tilde(str(where))}.")
        return
    if step_ == "stop_confirm":
        ctx.wizard.clear()
        if line.lower() not in ("y", "yes", "д", "да"):
            say_("Canceled.")
            return
        say_("Stopping the room…")
        m_tmuxio.tmux("kill-session", "-t", f"={m_lifecycle.stop_plan(m_store.load_config())[0]}", check=False)
        return
    if step_ == "rm_pick":
        if not line.isdigit() or not 1 <= int(line) <= len(ctx.wizard["people"]):
            say_("Enter a number from the list.")
            return
        pid = ctx.wizard["people"][int(line) - 1]
        ctx.wizard.update(
            step="rm_confirm",
            pid=pid,
            options=[("No", "n"), ("Yes, disconnect", "y")],
            prompt=f"Disconnect {m_store.label_of(ctx.config, pid)}?",
        )
        say_(f"Disconnect {m_store.label_of(ctx.config, pid)}? Room messages will stop. The agent will keep running.")
        return
    if step_ == "rm_confirm":
        if line.lower() not in ("y", "yes", "д", "да"):
            ctx.wizard.clear()
            say_("Canceled.")
            return
        try:
            label = m_agents.remove_participant(ctx.wizard["pid"])
        except ValueError as exc:
            say_(f"! {exc}")
        else:
            say_(f"{label} disconnected from the room.")
        ctx.wizard.clear()
        ctx.refresh_people()
        return
    if step_ == "kind":
        if not line.isdigit() or not 1 <= int(line) <= len(ctx.wizard["kinds"]):
            say_("Enter a number from the list.")
            return
        folders = []
        for f in [str(rp.root), *m_agents.room_folders(ctx.config), *m_boot.recent_folders()]:
            if f not in folders:
                folders.append(f)
        folders = folders[:10]
        ctx.wizard.update(
            step="folder_pick",
            kind=ctx.wizard["kinds"][int(line) - 1],
            folders=folders,
            options=[(m_text.tilde(f), str(i)) for i, f in enumerate(folders, 1)]
            + [("Browse folders…", "browse"), ("Enter path manually…", "other")],
            prompt="Project folder",
        )
        say_("Project folder.")
        return
    if step_ == "browse":
        cur = Path(ctx.wizard["cwd"])
        if line == "use":
            line, step_ = str(cur), "folder"
        elif line == "up":
            ctx.show_folders(cur.parent)
            return
        elif line.startswith("in:"):
            ctx.show_folders(cur / line[3:])
            return
        elif line.startswith("denied:"):
            # In the menu's own header: a line in the feed would be under the open menu, unseen.
            # The choice stays on that row (it jumped to the top of a long list).
            # The folder's name, cleaned: the rows were, the header was not.
            ctx.wizard["prompt"] = f"Cannot open {m_text.clean_field(line[7:], 60)}/ — choose another folder"
            ctx.ui.menu_index = next((i for i, (_, v) in enumerate(ctx.wizard["options"]) if v == line), 0)
            return
        elif line == "type":
            ctx.wizard.update(step="folder", options=None, prompt="Type the folder path (Esc to go back):")
            say_(ctx.wizard["prompt"])
            return
        else:
            return
    if step_ == "folder_pick" and line == "browse":
        # Next to the room's own project: where the person's other projects usually are.
        ctx.show_folders(rp.root.parent if rp.root.parent != rp.root else rp.root)
        return
    if step_ == "folder_pick":
        if line == "other":
            ctx.wizard.update(step="folder", options=None, prompt="Type the folder path (Esc to go back):")
            say_(ctx.wizard["prompt"])
            return
        if not line.isdigit() or not 1 <= int(line) <= len(ctx.wizard["folders"]):
            say_("Choose a folder from the list.")
            return
        line = ctx.wizard["folders"][int(line) - 1]
        step_ = "folder"
    if step_ == "folder":
        if not line:
            # An empty path never means some other project (Enter after an error
            # started the agent in the room's folder).
            say_("Enter a folder path, or press Esc to choose from the list.")
            return
        try:
            folder = Path(line).expanduser()
        except RuntimeError:  # «~someone» with no such user: it dropped the whole /add
            say_(f"Folder not found: {m_text.clean_field(line, 60)}")
            return
        problem = m_agents.folder_problem(folder)
        if problem:
            say_(problem)
            return
        ctx.wizard.update(
            step="name_new",
            folder=str(folder.resolve()),
            options=None,
            prompt="Name this agent — Enter takes the suggestion, or type another (Esc to go back):",
        )
        say_(ctx.wizard["prompt"])
        base = m_screen.PRESETS[ctx.wizard["kind"]]["label"]
        suggestion = next(
            n for n in [base] + [f"{base} {i}" for i in range(2, 100)] if not m_store.label_taken(ctx.config, "", n)
        )
        ctx.buffer()[:] = list(suggestion)
        ctx.ui.pos = len(ctx.buffer())
        ctx.ui.suggested = True  # like selected text: the first letter typed replaces it
        return
    if step_ == "name_new":
        label = line
        if not label:
            say_("Enter a name.")
            return
        # The same checks as /name: /add gave two agents one name.
        problem = m_text.name_problem(label) or (
            "that is already someone else's name here" if m_store.label_taken(ctx.config, "", label) else ""
        )
        if problem:
            say_(f"! {problem}. Try another name.")
            return
        try:
            pid = m_agents.launch_new_agent(ctx.config, ctx.wizard["kind"], ctx.wizard["folder"], label)
        except SystemExit as exc:
            # fail() must not close the owner's seat: say it, with the reason, and stay.
            ctx.wizard.clear()
            ctx.feed(m_text.paint(f"! Could not start the agent: {exc}. Try /add again.", "1;38;5;196"))
            return
        say_(f"Starting {label} in room window {pid}. Waiting for a connection check reply.")
        ctx.wizard.clear()
        ctx.refresh_people()
        return
