from __future__ import annotations
import unicodedata
import difflib
import time
from pathlib import Path
import collab.text as m_text
import collab.i18n as m_i18n
import collab.store as m_store
import collab.screen as m_screen
import collab.log as m_log
import collab.agents as m_agents
import collab.lifecycle as m_lifecycle
import collab.chat_wizard as m_chat_wizard


def chat_add_wizard(ctx: m_chat_wizard.ChatCommandContext, line: str) -> None:
    """Run one step and remember where it came from, so Esc goes back one step («back, everywhere»), not out of the whole question."""
    before = {k: v for k, v in ctx.wizard.items() if k != "back"}
    back = list(ctx.wizard.get("back", []))
    ctx.run_wizard_step(line)
    if before.get("step") == "exit_confirm":
        return  # «Stay» put back what was there; the question itself is no step to return to
    if ctx.wizard and before.get("step") and ctx.wizard.get("step") != before.get("step"):
        ctx.wizard["back"] = back + [before]
    elif ctx.wizard and before.get("step"):
        ctx.wizard["back"] = back


def chat_wizard_back(ctx: m_chat_wizard.ChatCommandContext) -> None:
    """Esc inside a question: the previous step; from the first step — the command menu."""
    back = ctx.wizard.get("back") or []
    origin = ctx.wizard.get("from_cmd")
    if ctx.wizard.get("step") == "exit_confirm" and "draft" in ctx.wizard:
        ctx.stay_open()  # Ctrl+C, then Esc: everything as it was
        return
    if ctx.wizard.get("step") == "own_name":
        then = ctx.wizard.get("then") or {}
        ctx.wizard.clear()
        ctx.wizard.update(then)  # Esc keeps the name; the welcome still comes
        ctx.feed(m_text.paint("/name changes your name any time.", "38;5;252"))
        return
    if ctx.wizard.get("step") == "welcome":
        m_agents.mark_welcomed()  # Esc is «Skip for now»: the welcome does not come back
        ctx.wizard.clear()
        ctx.feed(m_text.paint("You can open the guide any time: type /help.", "38;5;252"))
        if m_chat_wizard.offer_team(m_store.load_config()):
            m_chat_wizard.open_scenarios(ctx)  # as «Skip for now» does: a new room's question comes next
        return
    ctx.wizard.clear()
    if back:
        ctx.wizard.update(back[-1])
        ctx.wizard["back"] = back[:-1]
        ctx.buffer().clear()  # a suggested name must not hide the previous step's choices
        ctx.ui.pos = 0
        ctx.ui.suggested = False
        if not ctx.wizard.get("options"):  # a typed step: say its question again
            ctx.feed(m_text.paint(ctx.wizard.get("prompt", ""), "38;5;252"))
        return
    if origin == "/remove-name":
        ctx.buffer()[:] = list("/remove ")
    elif origin:
        ctx.buffer()[:] = list("/")
        ctx.ui.menu_index = next((i for i, (c, _) in enumerate(ctx.commands()) if c == origin), 0)
        ctx.ui.came_back = True
    ctx.ui.pos = len(ctx.buffer())
    ctx.ui.menu_closed = False


def chat_ask_exit(ctx: m_chat_wizard.ChatCommandContext) -> None:
    """Ctrl+C: «Close chat?» as a menu («yes and no from the menu too»): No and Yes, No
    first, as every yes/no of the room (not «Stay»); Esc is No. The draft and an open
    question wait aside and come back on No."""
    if ctx.wizard.get("step") == "exit_confirm":
        raise EOFError  # Ctrl+C twice: close
    prev = {k: v for k, v in ctx.wizard.items()}
    draft = list(ctx.buffer())
    ctx.buffer().clear()
    ctx.ui.pos = 0
    ctx.wizard.clear()
    ctx.wizard.update(
        step="exit_confirm",
        prev=prev,
        draft=draft,
        prompt="Close chat? Agents keep running",
        options=[("No", "n"), ("Yes", "y")],
    )
    ctx.ui.menu_index = 0


def chat_stay_open(ctx: m_chat_wizard.ChatCommandContext) -> None:
    prev, draft = ctx.wizard.get("prev") or {}, ctx.wizard.get("draft") or []
    ctx.wizard.clear()
    ctx.wizard.update(prev)
    ctx.buffer()[:] = draft
    ctx.ui.pos = len(ctx.buffer())
    ctx.ui.menu_index = 0


def chat_show_folders(ctx: m_chat_wizard.ChatCommandContext, cur: Path) -> None:
    ctx.wizard.update(step="browse", cwd=str(cur), options=m_agents.folder_rows(cur), prompt=f"Folder: {m_text.tilde(str(cur))}")
    ctx.ui.menu_index = 0


def chat_submit(ctx: m_chat_wizard.ChatCommandContext, line: str) -> None:
    """One line of the person's. An operation that fails says so in the feed and the chat stays
    open (a failed `tmux new-window` in /add closed the person's chat without a
    word). /exit still leaves (EOFError); a stopped room ends the chat with its session."""
    try:
        ctx.handle(line)
    except (EOFError, KeyboardInterrupt):
        raise
    except (m_store.OperationFailed, SystemExit, Exception) as exc:  # noqa: BLE001
        text = exc.code if isinstance(exc, SystemExit) else str(exc)
        ctx.wizard.clear()
        ctx.feed(
            m_text.paint(f"! Couldn't do that: {text if isinstance(text, str) and text else type(exc).__name__}", "1;38;5;196")
        )


def chat_handle(ctx: m_chat_wizard.ChatCommandContext, line: str) -> None:
    # A command by either of its names — /добавить or /add — goes on as one: its English name.
    line = m_i18n.command(line.strip())
    if not line and not ctx.wizard:
        return
    if (
        ctx.wizard
        or line
        in (
            "/add",
            "/team",
            "/language",
            "/remove",
            "/stop",
            "/clear",
            "/view",
            "/open",
            "/answer",
            "/help",
            "/mute",
            "/unmute",
            "/unlimit",
            "/name",
            "/exit",
            "/quit",
            "/restart",
            "/mine",
            "/dm",
        )
        or line.startswith(("/remove ", "/answer ", "/dm "))
    ):
        ctx.add_wizard(line)
        return
    if line in ("/pause", "/resume"):
        m_lifecycle.set_paused(line == "/pause")
        ctx.feed(
            m_text.paint(
                "Room paused — agents get no messages, and what they say waits until /resume. A started turn may still finish."
                if line == "/pause"
                else "Room resumed — waiting messages go out now.",
                "1;38;5;214",
            )
        )
        return
    if line.split()[0:1] and line.split()[0] in ("/mute", "/unmute", "/unlimit"):
        verb, _, who = line.partition(" ")
        who = who.strip()
        if not who:
            ctx.feed(
                m_text.paint(
                    f"! {verb} <name>; names: {', '.join(m_store.label_of(ctx.config, x) for x in ctx.known if x != ctx.args.as_)}",
                    "1;38;5;196",
                )
            )
            return
        pid = ctx.names.get(who.lower().lstrip("@"))
        if not pid or pid == "all":
            ctx.feed(m_text.paint(f"! No participant {who}", "1;38;5;196"))
            return
        if verb == "/unlimit":
            # The limit was raised, the agent sits idle — its messages held until the recorded time.
            try:
                label, had = m_agents.clear_limit(pid)
            except ValueError as exc:
                ctx.feed(m_text.paint(f"! {exc}", "1;38;5;196"))
                return
            text = f"{label}'s usage limit cleared — messages go to it again." if had else f"{label} is not out of its limit."
            ctx.feed(m_text.paint(text, "38;5;252"))
            return
        try:
            label = m_agents.set_muted(pid, verb == "/mute")
        except ValueError as exc:
            ctx.feed(m_text.paint(f"! {exc}", "1;38;5;196"))
            return
        ctx.feed(
            m_text.paint(
                f"{label} {'muted — only messages to it by name reach it' if verb == '/mute' else 'unmuted — it hears new messages again'}.",
                "38;5;252",
            )
        )
        return
    if line in ("/exit", "/quit"):
        raise EOFError

    if line.startswith("/name"):
        parts = line.split(maxsplit=2)
        if len(parts) < 3 or parts[1] not in ctx.known:
            ctx.feed(m_text.paint(f"! /name <ID> <Name>; IDs: {', '.join(ctx.known)}", "1;38;5;196"))
            return
        if m_text.name_problem(parts[2]) or m_store.label_taken(ctx.config, parts[1], parts[2]):
            ctx.feed(
                m_text.paint(
                    f"! {m_text.name_problem(parts[2]) or 'that is already someone else' + chr(39) + 's name here'}",
                    "1;38;5;196",
                )
            )
            return
        with ctx.lock:
            old = m_store.label_of(ctx.config, parts[1])
            m_store.rename(parts[1], parts[2])
            ctx.holder["config"] = m_store.load_config()
            ctx.config.clear()
            ctx.config.update(ctx.holder["config"])
            ctx.names.clear()
            ctx.names.update(m_store.aliases(ctx.config))
            m = m_log.append_message(ctx.args.as_, ["all"], f"{old} is now called {parts[2]}.", sign=ctx.signs())
            ctx.printed.add(m["id"])
            ctx.feed(ctx.show(m))
        return
    if line.startswith("/") and not line.startswith("//"):
        # A typo of a command («/sotp») went to every agent as a message. Text that
        # really starts with «/» is sent with «//».
        word = line.split()[0]
        known_cmds = sorted({c for c, _ in ctx.commands()} | {"/exit", "/quit", "/name", "/mute", "/unmute", "/unlimit"})
        near = difflib.get_close_matches(word, known_cmds, n=1)
        ctx.feed(
            m_text.paint(
                f"! Unknown command {word}"
                + (f" — did you mean {near[0]}?" if near else "")
                + " To send text that starts with /, begin it with //.",
                "1;38;5;196",
            )
        )
        return
    if line.startswith("//"):
        line = line[1:]
    try:
        if line.startswith("@"):
            to, text = m_store.parse_addressing(line, ctx.known, ctx.names)
        elif ctx.ui.selected is None:
            ctx.feed(m_text.paint("! Choose a recipient with Tab first.", "1;38;5;196"))
            return
        else:
            to, text = [ctx.ui.selected], line
    except ValueError as exc:
        ctx.feed(m_text.paint(f"! {exc}", "1;38;5;196"))
        return
    if not text:
        ctx.feed(m_text.paint("! empty message", "1;38;5;196"))
        return
    with ctx.lock:
        m = m_log.append_message(ctx.args.as_, to, text, sign=ctx.signs())
        ctx.printed.add(m["id"])
        if ctx.tty:
            ctx.feed(ctx.show(m))  # your own message: once, in the feed, like everyone else's


def chat_people_for(ctx: m_chat_wizard.ChatCommandContext, cmd: str) -> list[str]:
    cfg = {p["id"]: p for p in ctx.config["participants"]}
    others = [x for x in ctx.known if x != ctx.args.as_]
    if cmd == "/mute":
        return [x for x in others if not cfg.get(x, {}).get("muted")]
    if cmd == "/unmute":
        return [x for x in others if cfg.get(x, {}).get("muted")]
    if cmd == "/unlimit":
        limits = m_store.read_json(m_store.room().paths.limits_path)
        # Only a time still ahead: one passed is the deliverer's to take off on its next look.
        return [x for x in others if m_screen.limit_of(limits, x).get("until", 0) > time.time()]
    if cmd == "/name":
        return list(ctx.known)
    return others


def chat_menu_items(ctx: m_chat_wizard.ChatCommandContext) -> list[tuple[str, str, str]]:
    """What the vertical menu offers right now: (shown text, what Enter does, hint).
    Commands after «/»; then the next step is a choice too — names after
    /mute, /unmute, /remove, /name — and the options of an open /add or /remove question."""
    value = "".join(ctx.buffer())
    if "\n" in value:
        return []
    # A question of /add or /remove is always answered by choosing — even when the command
    # itself was picked from the menu, which closed it («it asks me to type»).
    if ctx.wizard.get("options") and not value:
        return [(label, "pick:" + v, "") for label, v in ctx.wizard["options"]]
    if ctx.ui.menu_closed:
        return []
    lang = m_store.room_language(ctx.config)
    if value.startswith("/") and " " not in value:
        # By either name: «/доб» finds /add in any room; each shown in the room's language.
        typed_ = unicodedata.normalize("NFC", value).casefold()
        return [
            (m_i18n.shown_command(c, lang), "cmd:" + c, m_i18n.words_in(f"cmd.{c[1:]}", lang))
            for c, _ in ctx.commands()
            if c.startswith(typed_) or m_i18n.shown_command(c, "ru").startswith(typed_)
        ]
    cmd, _, typed = m_i18n.command(value).partition(" ")
    if cmd in ("/mute", "/unmute", "/unlimit", "/remove", "/name", "/answer") and " " not in typed:
        return [
            (m_store.label_of(ctx.config, x), f"arg:{cmd}:{x}", "")
            for x in ctx.people_for(cmd)
            if m_store.label_of(ctx.config, x).lower().startswith(typed.lower()) or x.startswith(typed.lower())
        ]
    return []


def chat_command_matches(ctx: m_chat_wizard.ChatCommandContext) -> list[tuple[str, str, str]]:
    return ctx.menu_items()


def chat_choose_command(ctx: m_chat_wizard.ChatCommandContext) -> str | None:
    """Apply the highlighted item. Returns a line to submit right away, or None."""
    items = ctx.menu_items()
    if not items:
        return None
    ctx.ui.menu_index %= len(items)
    _, action, _ = items[ctx.ui.menu_index]
    ctx.ui.menu_moved = False
    kind, _, rest = action.partition(":")
    if kind == "cmd":
        # No pressing Enter twice, and a command does not land in the input line — everything from
        # the menu. Every command runs on the first Enter; its next step
        # (whom, yes or no) is the next menu.
        ctx.buffer().clear()
        ctx.ui.pos = 0
        ctx.ui.menu_index = 0
        return rest
    if kind == "pick":
        ctx.buffer().clear()
        ctx.ui.pos = 0
        ctx.ui.menu_index = 0
        return rest
    cmd, _, pid = rest.partition(":")
    ctx.ui.menu_index = 0
    if cmd == "/name":
        ctx.buffer()[:] = list(f"/name {pid} ")
        ctx.ui.pos = len(ctx.buffer())
        ctx.ui.menu_closed = True
        return None
    ctx.buffer().clear()
    ctx.ui.pos = 0
    return f"{cmd} {pid}"  # by id, not by name: names may repeat
