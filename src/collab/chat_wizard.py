from __future__ import annotations
import argparse
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, cast
import collab.boot as m_boot
import collab.text as m_text
import collab.i18n as m_i18n
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


def open_scenarios(ctx: ChatCommandContext) -> None:
    open_scenarios_into(ctx.wizard, m_store.room_language(m_store.load_config()))


def open_scenarios_into(wizard: dict[str, Any], lang: str = "en") -> None:
    """«What will we do?»: the ready teams, and no team at all. Nothing starts before «Launch the team»."""
    wizard.update(
        step="scenario_pick",
        from_cmd="/team",
        prompt=m_i18n.words_in("team.what", lang),
        # First, and taken by Enter: a room as before — nothing automatic.
        options=[(m_i18n.words_in("team.none", lang), "none")]
        + [(f"{m_store.scenario_title(name, lang)} — {m_store.scenario_hint(name, lang)}", name) for name in m_store.SCENARIOS],
    )


def agents_words(n: int, lang: str = "en") -> str:
    return m_i18n.words_in("n.agents", lang, n=n)


def offer_team(config: dict[str, Any]) -> bool:
    """A room start has just created, still without agents or a team: «What will we do?» comes first — once; a
    room made before, or left empty on purpose, opens as ever."""
    path = m_store.offer_team_path()
    if not path.exists():
        return False
    path.unlink(missing_ok=True)
    people = m_store.people_ids(config)
    return not m_store.team_of(config) and all(p["id"] in people for p in config["participants"])


def room_is_empty(config: dict[str, Any]) -> bool:
    people = m_store.people_ids(config)
    return all(p["id"] in people for p in config["participants"])


AGENT_FIELDS = {"role": "Role", "topic": "Topic", "use_model": "Model", "context_trim": "Memory", "reports_to": "Reports to"}
ROOM_FIELDS = {"lead": "Lead", "goal": "Goal", "round": "Lead's rounds", "output": "Result file", "without_person": "Without you"}
MODELS = {"claude": ["opus", "sonnet", "haiku"], "codex": []}


def open_team_menu(ctx: ChatCommandContext, cfg: dict[str, Any]) -> None:
    """/team on a running team: change an agent, the room's settings, or look back and undo."""
    agents = m_deliver.agents_of(cfg)
    waiting = m_store.pending_proposals(m_log.read_messages())
    ctx.wizard.update(
        step="team_menu",
        from_cmd="/team",
        prompt="Change the team?",
        options=[(f"Proposal: {m_store.proposal_words(cfg, m['proposal'])}", f"proposal:{m['id']}") for m in waiting]
        + [(f"Change {m_text.visible(p.get('label') or p['id'])}…", f"agent:{p['id']}") for p in agents]
        + [("The room: lead, goal, result file, what is done without you…", "room"), ("Changes — look back, undo…", "history")],
    )


def setting_shown(key: str, value: object, cfg: dict[str, Any]) -> str:
    if value is None:
        return "default"
    if key == "context_trim":
        return m_store.memory_words(value)
    if key in ("reports_to", "lead"):
        return m_store.whom(str(value), cfg)
    if key == "without_person":
        return "; ".join(m_store.WITHOUT_PERSON[a] for a in cast("list[str]", value)) or "nothing external"
    return m_text.visible(str(value))


def when_words(cfg: dict[str, Any], who: str, key: str) -> str:
    """When a change starts to act — said before it is saved."""
    if who == "room":
        return "The agents get it with their next assignment."
    if key == "use_model":
        # Not /model: Claude saves it as the person's own default for every new session.
        return f"A model takes effect when the program starts: /restart {m_store.label_of(cfg, who)} — its conversation goes on."
    if key == "context_trim":
        return "From its next free minute; what it remembers now is not cut by this."
    return "It gets its new assignment after its current task."


def field_options(cfg: dict[str, Any], who: str, key: str) -> list[tuple[str, str]]:
    """The values to choose from for one field; «the default» takes the explicit one away."""
    if who == "room":
        if key == "lead":
            return [(m_text.visible(p.get("label") or p["id"]), p["id"]) for p in m_deliver.agents_of(cfg)]
        if key == "round":
            return [("Every 15 minutes", "15"), ("Every 30 minutes", "30"), ("Every hour", "60"), ("Off", "off")]
        if key == "without_person":
            on = m_store.without_person(cfg)
            return [(f"{'✓' if a in on else '·'} {words}", f"toggle:{a}") for a, words in m_store.WITHOUT_PERSON.items()]
        return [("Type it…", "type"), ("None", "default")]
    p = next(q for q in cfg["participants"] if q["id"] == who)
    kind = m_screen.agent_kind(p)
    if key == "role":
        rows = [(f"{r} — {words}", r) for r, words in m_store.ROLES.items()]
    elif key == "use_model":
        rows = [(name, name) for name in MODELS.get(kind, [])] + [("Another model — type its name…", "type")]
    elif key == "context_trim":
        rows = [("Compact past 150k", "compact"), ("Clear past 100k", "clear"), ("Keep — leave it to the program", "keep")]
    elif key == "reports_to":
        rows = [("The person", "person")] + [
            (m_text.visible(q.get("label") or q["id"]), q["id"]) for q in m_deliver.agents_of(cfg) if q["id"] != who
        ]
    else:
        rows = [("Type it…", "type")]
    return rows + [("The default — the scenario's, or the room's", "default")]


def chosen_value(key: str, value: str, cfg: dict[str, Any]) -> object:
    if value == "default":
        return None
    if key == "context_trim":
        return m_store.MEMORY[value]
    if key == "round":
        return False if value == "off" else int(value)
    if key == "without_person":
        on = m_store.without_person(cfg)
        a = value.partition(":")[2]
        return [x for x in m_store.WITHOUT_PERSON if (x in on) != (x == a)]
    return value


def team_step(ctx: ChatCommandContext, line: str, say_: Callable[[str], None]) -> None:
    """/team's steps: an agent or the room → a field → a value → «before → after» and when it acts → Save."""
    step_ = ctx.wizard.get("step")
    cfg = m_store.load_config()
    if step_ == "team_menu":
        if line.startswith("proposal:"):
            m = next((x for x in m_log.read_messages() if x.get("id") == line.partition(":")[2]), None)
            if m is not None:
                ctx.wizard.clear()
                ctx.wizard.update(m_store.proposal_card(m, cfg))
            return
        if line == "history":
            records = m_store.read_changes()[-10:]
            if not records:
                say_("No changes yet.")
                return
            ctx.wizard.update(
                step="team_history",
                prompt="Undo a change?",
                options=[
                    (f"{m_text.local_hm(r.get('at', ''))} · {m_store.change_words(cfg, r)}", f"undo:{r['id']}")
                    for r in reversed(records)
                ],
            )
            return
        who = "room" if line == "room" else line.partition(":")[2]
        fields = ROOM_FIELDS if who == "room" else AGENT_FIELDS
        if who != "room" and who not in m_store.ids_of(cfg):
            return
        rows = []
        for key, title in fields.items():
            if who == "room":
                value: object = m_store.lead_of(cfg) if key == "lead" else m_store.team_of(cfg).get(key)
            else:
                p = next(q for q in cfg["participants"] if q["id"] == who)
                value = m_store.effective(m_screen.agent_kind(p), p, cfg, key)
                if key == "reports_to":
                    value = m_store.reports_to(m_screen.agent_kind(p), p, cfg)
            own = who == "room" or key in next(q for q in cfg["participants"] if q["id"] == who)
            rows.append((f"{title}: {setting_shown(key, value, cfg)}{'' if own else ' (the scenario’s)'}", f"field:{key}"))
        name = "the room" if who == "room" else m_store.label_of(cfg, who)
        ctx.wizard.update(step="team_card", who=who, prompt=f"Change {name}: which?", options=rows)
        return
    if step_ == "team_history":
        rec_id = line.partition(":")[2]
        rec = next((r for r in m_store.read_changes() if r.get("id") == rec_id), None)
        if rec is None:
            return
        ctx.wizard.update(
            step="team_undo",
            record=rec_id,
            prompt="Undo it?",
            options=[("Undo", "go"), ("Cancel", "back")],
        )
        say_(
            f"Undo: {m_store.change_words(cfg, rec)}. Work done and money spent stay; history cleared from an agent "
            "does not come back." + "\n" * 4
        )
        return
    if step_ == "team_undo":
        if line != "go":
            ctx.wizard_back()
            return
        rec_id = ctx.wizard["record"]
        ctx.wizard.clear()
        try:
            rec, left = m_store.undo_changes(rec_id, ctx.args.as_)
        except ValueError as exc:
            say_(f"! Not undone: {m_text.clean_field(str(exc), 200)}.")
            return
        if rec["changes"]:
            say_(f"Undone: {m_store.change_words(m_store.load_config(), rec)}.")
        for why in left:
            say_(f"! Left as it is: {why}.")
        return
    if step_ == "team_card":
        key = line.partition(":")[2]
        ctx.wizard.update(
            step="team_field",
            key=key,
            prompt=f"{(ROOM_FIELDS | AGENT_FIELDS)[key]}:",
            options=field_options(cfg, ctx.wizard["who"], key),
        )
        return
    if step_ in ("team_field", "team_typed"):
        who, key = ctx.wizard["who"], ctx.wizard["key"]
        if step_ == "team_field" and line == "type":
            ctx.wizard.update(step="team_typed", options=None, prompt=f"{(ROOM_FIELDS | AGENT_FIELDS)[key]} (Esc to go back):")
            say_(ctx.wizard["prompt"])
            return
        after = line if step_ == "team_typed" else chosen_value(key, line, cfg)
        if who != "room" and key == "role" and (after == "lead") != (who == m_store.lead_of(cfg)):
            # One lead: who leads changes in the room's card, where the one before is given another role.
            say_(
                "! The room has one lead: change who leads in /team → The room → Lead."
                + "\n" * (len(ctx.wizard.get("options") or []) + 2)
            )
            return
        why = m_store.change_problem(cfg, who, key, after)
        if why:
            say_(f"! {why}. Try again." + "\n" * (len(ctx.wizard.get("options") or []) + 2))  # above the menu still open
            return
        before = m_store.explicit_value(cfg, who, key)
        if who == "room" and key == "lead":
            before = m_store.lead_of(cfg) or None  # who leads now, set or found by role
        elif who != "room":
            # What acts now, its own or its seat's — «default» said sonnet was nothing.
            p = next(q for q in cfg["participants"] if q["id"] == who)
            kind = m_screen.agent_kind(p)
            before = m_store.reports_to(kind, p, cfg) if key == "reports_to" else m_store.effective(kind, p, cfg, key)
        changes = [{"who": who, "key": key, "after": after}]
        if who == "room" and key == "lead":
            # One lead: the new one takes the role, the one before becomes a worker — in one record.
            old = m_store.lead_of(cfg)
            changes += [{"who": str(after), "key": "role", "after": "lead"}]
            changes += [{"who": old, "key": "role", "after": "worker"}] if old and old != after else []
        ctx.wizard.update(step="team_confirm", changes=changes, prompt="Save it?", options=[("Save", "go"), ("Cancel", "back")])
        name = "the room" if who == "room" else m_store.label_of(cfg, who)
        say_(
            f"{name} — {(ROOM_FIELDS | AGENT_FIELDS)[key].lower()}: {setting_shown(key, before, cfg)} → {setting_shown(key, after, cfg)}. "
            + when_words(cfg, who, key)
            + "\n" * 4  # room for the choice under it
        )
        return
    if step_ == "team_confirm":
        if line != "go":
            ctx.wizard_back()
            return
        changes = ctx.wizard["changes"]
        ctx.wizard.clear()
        try:
            rec, _ = m_store.apply_changes(changes, ctx.args.as_)
        except ValueError as exc:
            say_(f"! Not saved: {exc}.")
            return
        say_(f"Saved: {m_store.change_words(m_store.load_config(), rec)}.")
        return


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
        cfg = m_store.load_config()
        paused, lang = cfg.get("paused"), m_store.room_language(cfg)
        options = []
        for key, title, _, _, _ in ctx.help_tasks:
            if key == "pause":
                title = "Resume the room" if paused else "Pause the room"
            options.append((m_i18n.help_words(title, lang), key))
        ctx.wizard.update(
            step="help_pick", from_cmd="/help", prompt=m_i18n.help_words("What would you like to do?", lang), options=options
        )
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
    if line == "/language":
        # Its own question in both: a person who cannot read the room's language still finds theirs.
        ctx.wizard.clear()
        ctx.wizard.update(
            step="language_pick",
            from_cmd="/language",
            prompt=m_i18n.words_in("lang.choose"),
            options=[(m_i18n.LANG_NAMES[x], x) for x in m_i18n.LANGS],
        )
        return
    if ctx.wizard.get("step") == "language_pick":
        if line not in m_i18n.LANGS:
            return
        target = line
        ctx.wizard.update(
            step="language_confirm",
            target=target,
            prompt=m_i18n.words_in("lang.change.question", target, language=m_i18n.LANG_NAMES[target]),
            options=[
                (m_i18n.words_in("lang.change.apply", target), "go"),
                (m_i18n.words_in("lang.change.apply", target) + " · " + m_i18n.words_in("lang.change.also_new", target), "also"),
                (m_i18n.words_in("lang.change.cancel", target), "back"),
            ],
        )
        return
    if ctx.wizard.get("step") == "language_confirm":
        target = ctx.wizard["target"]
        ctx.wizard.clear()
        if line not in ("go", "also"):
            say_(m_i18n.words_in("lang.unchanged", m_store.room_language(ctx.config)))
            return
        m_store.set_room_language(target)
        if line == "also":
            m_store.set_user_language(target)
        ctx.refresh_people()  # the room's file changed: the chat reads it again, its menu and help follow
        say_(m_i18n.words_in("lang.changed", target))
        return
    if line == "/team":
        ctx.wizard.clear()
        cfg = m_store.load_config()
        waiting = m_store.pending_proposals(m_log.read_messages())
        if m_store.team_of(cfg).get("scenario") or not room_is_empty(cfg) or waiting:
            open_team_menu(ctx, cfg)
            # Room under the rows for the choice: the menu is drawn over the feed's bottom lines.
            ctx.feed("\n".join(m_deliver.team_view(cfg, m_log.read_messages())) + "\n" * (len(ctx.wizard["options"]) + 2))
            return
        open_scenarios(ctx)
        return
    if ctx.wizard.get("step", "").startswith("team_"):
        team_step(ctx, line, say_)
        return
    if ctx.wizard.get("step") == "proposal":
        mid, digest = ctx.wizard["proposal"], ctx.wizard.get("digest", "")
        ctx.wizard.clear()
        if line not in ("go", "decline", "later"):
            # A draft typed while the card opened: it is a message, never a yes.
            say_("The proposal waits: /team shows it.")
            if line.strip():
                ctx.submit(line)
            return
        if line == "later":
            say_("It waits: /team shows it.")
            return
        if line == "decline":
            m_store.set_proposal_state(mid, "declined")
            m = next((x for x in m_log.read_messages() if x.get("id") == mid), {})
            if m.get("from") in m_store.ids_of(m_store.load_config()):
                # Unsigned and quiet: not a request a next proposal could name.
                m_log.append_message(m_text.ROOM, [m["from"]], "Your proposal is declined.", mid)
            say_("Declined.")
            return
        try:
            lines = m_agents.apply_proposal(mid, ctx.args.as_, digest)
        except Exception as exc:  # noqa: BLE001 — a record anyone could write: said, and the chat stays
            say_(f"! Not applied: {m_text.clean_field(str(exc), 200)}.")
            return
        except SystemExit as exc:
            say_(f"! Not applied: {m_text.clean_field(str(exc.code), 200)}.")
            return
        for text in lines:
            say_(text)
        ctx.refresh_people()
        return
    lang = m_store.room_language(ctx.config)
    if ctx.wizard.get("step") == "scenario_pick":
        if line == "none":
            ctx.wizard.clear()
            say_(m_i18n.words_in("team.none.said", lang))
            return
        if line not in m_store.SCENARIOS:
            return
        missing = m_agents.scenario_unavailable(m_store.SCENARIOS[line])
        ctx.wizard.update(
            step="scenario_go",
            scenario=line,
            prompt=m_i18n.words_in("team.unavailable", lang, what="; ".join(missing))
            if missing
            else m_i18n.words_in("team.launch.question", lang),
            options=[(m_i18n.words_in("team.back", lang), "back")]
            if missing
            else [
                (m_i18n.words_in("team.launch", lang, agents=agents_words(len(m_store.SCENARIOS[line]["seats"]), lang)), "go"),
                (m_i18n.words_in("team.back", lang), "back"),
            ],
        )
        # Room under the rows for the choice: the menu is drawn over the feed's bottom lines.
        say_("\n".join(m_store.scenario_preview(line, lang)) + "\n" * 4)
        return
    if ctx.wizard.get("step") == "scenario_go":
        if line == "back":
            ctx.wizard_back()
            return
        if line != "go":
            return
        name = ctx.wizard["scenario"]
        ctx.wizard.clear()
        if m_store.team_of(m_store.load_config()).get("scenario"):
            say_(m_i18n.words_in("team.has_one", lang))
            return
        say_(m_i18n.words_in("team.starting", lang, title=m_store.scenario_title(name, lang)))
        started, failed = m_agents.start_scenario(name, str(rp.root))
        ctx.feed(m_text.paint(m_agents.team_started(m_store.load_config(), started, failed), "38;5;252"))
        ctx.refresh_people()
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
            if offer_team(m_store.load_config()):
                open_scenarios(ctx)
        return
    if step_ == "help_pick":
        task = next((t for t in ctx.help_tasks if t[0] == line), None)
        if task is None:
            return
        key, title, steps, action, command = task
        lang = m_store.room_language(m_store.load_config())
        if key == "keys":
            ctx.feed("\n" + ctx.help_text(ctx.me, full=True, ids=ctx.known, lang=lang))
            return  # the menu stays open: Esc closes it
        if key == "pause":
            paused = m_store.load_config().get("paused")
            action, command = ("Resume the room", "/resume") if paused else ("Pause the room", "/pause")
        title, action = m_i18n.help_words(title, lang), m_i18n.help_words(action, lang)
        steps = [m_i18n.help_words(x, lang) for x in steps]
        back = m_i18n.words_in("team.back", lang)
        ctx.wizard.update(step="help_task", options=[(action, "go"), (back, "back")], prompt=title, command=command)
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
        lang = m_store.room_language(cfg)
        key = "open.copied" if ctx.clipboard(command) else "open.copy_failed"
        say_(m_i18n.words_in(key, lang, label=label, line=ctx.whole_line, command=command))
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
        say_(f"Settings: {m_store.settings_words(ctx.wizard['kind'], {'id': pid}, m_store.load_config())}.")
        ctx.wizard.clear()
        ctx.refresh_people()
        return
