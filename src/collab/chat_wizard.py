from __future__ import annotations
import argparse
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, cast
import time
import collab.boot as m_boot
import collab.text as m_text
import collab.qr as m_qr
import collab.i18n as m_i18n
import collab.store as m_store
import collab.tmuxio as m_tmuxio
import collab.log as m_log
import collab.screen as m_screen
import collab.deliver as m_deliver
import collab.telegram as m_telegram
import collab.agents as m_agents
import collab.lifecycle as m_lifecycle
import collab.chat_base as m_chat_base


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


def show_draft(ctx: ChatCommandContext, say_: Callable[[str], None], lang: str) -> None:
    """The team before its launch, as it is now — the scenario's or as the person changed it — and what can be done
    with it: launch, change, add, remove. Nothing starts before «Launch the team»."""
    name, draft = ctx.wizard["scenario"], ctx.wizard["draft"]
    missing = m_agents.scenario_unavailable({"seats": draft})
    back = (m_i18n.words_in("team.back", lang), "back")
    edits = [(m_i18n.words_in(f"team.{x}", lang), x) for x in ("edit", "add", "remove")]
    options = (
        [back] if missing else [(m_i18n.words_in("team.launch", lang, agents=agents_words(len(draft), lang)), "go"), *edits, back]
    )
    question = (
        m_i18n.words_in("team.unavailable", lang, what="; ".join(missing))
        if missing
        else m_i18n.words_in("team.launch.question", lang)
    )
    ctx.wizard.update(step="scenario_go", prompt=question, options=options)
    # Room under the rows for the choice: the menu is drawn over the feed's bottom lines.
    say_("\n".join(m_store.scenario_preview(name, lang, draft)) + "\n" * (len(options) + 2))


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
        + [
            (m_i18n.tr("{p0} — {p1}", p0=m_store.scenario_title(name, lang), p1=m_store.scenario_hint(name, lang)), name)
            for name in [*m_store.SCENARIOS, *m_store.own_scenarios(list(m_screen.PRESETS))[0]]
        ],
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


AGENT_FIELDS = {
    "role": lambda: m_i18n.tr("Role"),
    "topic": lambda: m_i18n.tr("Topic"),
    "use_model": lambda: m_i18n.tr("Model"),
    "context_trim": lambda: m_i18n.tr("Memory"),
    "reports_to": lambda: m_i18n.tr("Reports to"),
}
ROOM_FIELDS = {
    "lead": lambda: m_i18n.tr("Lead"),
    "goal": lambda: m_i18n.tr("Goal"),
    "round": lambda: m_i18n.tr("Lead's rounds"),
    "output": lambda: m_i18n.tr("Result file"),
    "without_person": lambda: m_i18n.tr("Without you"),
}
MODELS = {"claude": ["opus", "sonnet", "haiku"], "codex": []}


def field_title(key: str) -> str:
    """A field's name, in the chat's language when it is shown."""
    return (ROOM_FIELDS | AGENT_FIELDS)[key]()


def open_team_menu(ctx: ChatCommandContext, cfg: dict[str, Any]) -> None:
    """/team on a running team: change an agent, the room's settings, or look back and undo."""
    agents = m_deliver.agents_of(cfg)
    waiting = m_store.pending_proposals(m_log.read_messages())
    ctx.wizard.update(
        step="team_menu",
        from_cmd="/team",
        prompt=m_i18n.tr("Change the team?"),
        options=[
            (m_i18n.tr("Proposal: {p0}", p0=m_store.proposal_words(cfg, m["proposal"])), f"proposal:{m['id']}") for m in waiting
        ]
        + [(m_i18n.tr("Change {p0}…", p0=m_text.visible(p.get("label") or p["id"])), f"agent:{p['id']}") for p in agents]
        + [
            (m_i18n.tr("The room: lead, goal, result file, what is done without you…"), "room"),
            (m_i18n.tr("Changes — look back, undo…"), "history"),
        ]
        + ([(m_i18n.tr("Save as your own scenario…"), "save")] if agents else [])
        + (
            [(m_i18n.tr("Continue the lead's rounds"), "rounds")]
            if "reached" in m_store.read_json(m_deliver.rounds_path())
            else []
        ),
    )


def setting_shown(key: str, value: object, cfg: dict[str, Any]) -> str:
    if value is None:
        return "default"
    if key == "context_trim":
        return m_store.memory_words(value)
    if key in ("reports_to", "lead"):
        return m_store.whom(str(value), cfg)
    if key == "without_person":
        return "; ".join(m_store.WITHOUT_PERSON[a] for a in cast("list[str]", value)) or m_i18n.tr("nothing external")
    return m_text.visible(str(value))


def when_words(cfg: dict[str, Any], who: str, key: str) -> str:
    """When a change starts to act — said before it is saved."""
    if who == "room":
        return m_i18n.tr("The agents get it with their next assignment.")
    if key == "use_model":
        # Not /model: Claude saves it as the person's own default for every new session.
        return m_i18n.tr(
            "A model takes effect when the program starts: /restart {p0} — its conversation goes on.",
            p0=m_store.label_of(cfg, who),
        )
    if key == "context_trim":
        return m_i18n.tr("From its next free minute; what it remembers now is not cut by this.")
    return m_i18n.tr("It gets its new assignment after its current task.")


def field_options(cfg: dict[str, Any], who: str, key: str) -> list[tuple[str, str]]:
    """The values to choose from for one field; «the default» takes the explicit one away."""
    if who == "room":
        if key == "lead":
            return [(m_text.visible(p.get("label") or p["id"]), p["id"]) for p in m_deliver.agents_of(cfg)]
        if key == "round":
            return [
                (m_i18n.tr("Every 15 minutes"), "15"),
                (m_i18n.tr("Every 30 minutes"), "30"),
                (m_i18n.tr("Every hour"), "60"),
                (m_i18n.tr("Off"), "off"),
            ]
        if key == "without_person":
            on = m_store.without_person(cfg)
            return [
                (m_i18n.tr("{p0} {words}", p0="✓" if a in on else "·", words=words), f"toggle:{a}")
                for a, words in m_store.WITHOUT_PERSON.items()
            ]
        return [(m_i18n.tr("Type it…"), "type"), (m_i18n.tr("None"), "default")]
    p = next(q for q in cfg["participants"] if q["id"] == who)
    kind = m_screen.agent_kind(p)
    if key == "role":
        rows = [(m_i18n.tr("{r} — {words}", r=r, words=words), r) for r, words in m_store.ROLES.items()]
    elif key == "use_model":
        rows = [(name, name) for name in MODELS.get(kind, [])] + [(m_i18n.tr("Another model — type its name…"), "type")]
    elif key == "context_trim":
        rows = [
            (m_i18n.tr("Compact past 150k"), "compact"),
            (m_i18n.tr("Clear after handing in work"), "clear"),
            (m_i18n.tr("Keep — leave it to the program"), "keep"),
        ]
    elif key == "reports_to":
        rows = [(m_i18n.tr("The person"), "person")] + [
            (m_text.visible(q.get("label") or q["id"]), q["id"]) for q in m_deliver.agents_of(cfg) if q["id"] != who
        ]
    else:
        rows = [(m_i18n.tr("Type it…"), "type")]
    return rows + [(m_i18n.tr("The default — the scenario's, or the room's"), "default")]


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
    if step_ == "team_menu" and line == "rounds":
        ctx.wizard.clear()
        state = m_store.read_json(m_deliver.rounds_path())
        state.pop("reached", None)
        m_store.write_json(m_deliver.rounds_path(), state)
        say_(m_i18n.tr("The lead's rounds go on."))
        return
    if step_ == "team_menu" and line == "save":
        ctx.wizard.update(
            step="team_save", options=None, prompt=m_i18n.tr("A name for this team (letters, digits, - and _; Esc to go back):")
        )
        say_(ctx.wizard["prompt"])
        return
    if step_ == "team_save":
        name = line.strip()
        if not m_store.OWN_NAME.fullmatch(name):
            say_(m_i18n.tr("! A name of letters, digits, - and _, up to 40. Try again."))
            return
        scenario = m_store.team_to_scenario(cfg, name)
        why = m_store.scenario_problems(scenario, list(m_screen.PRESETS))
        if why:
            ctx.wizard.clear()
            say_(m_i18n.tr("! Not saved: {why}.", why=m_text.clean_field("; ".join(why), 200)))
            return
        exists = m_store.own_scenario_path(name).exists()
        ctx.wizard.update(
            step="team_save_confirm",
            name=name,
            scenario=scenario,
            prompt=m_i18n.tr("Replace your scenario «{name}»?", name=name) if exists else m_i18n.tr("Save it?"),
            options=[(m_i18n.tr("Save"), "go"), (m_i18n.tr("Cancel"), "back")],
        )
        rows = [
            m_store.seat_line(
                seat["kind"],
                {"id": seat["seat"], **seat},
                {"participants": [{"id": x["seat"], **x} for x in scenario["seats"]], "team": scenario},
            )
            for seat in scenario["seats"]
        ]
        say_(
            m_i18n.tr("Your scenario «{name}»: the roles and settings, not the conversations, folders or history.", name=name)
            + "\n"
            + "\n".join("  " + m_text.visible(r) for r in rows)
            + "\n" * 4
        )
        return
    if step_ == "team_save_confirm":
        if line != "go":
            ctx.wizard_back()
            return
        name, scenario = ctx.wizard["name"], ctx.wizard["scenario"]
        ctx.wizard.clear()
        try:
            m_store.save_own_scenario(name, scenario)
        except (OSError, ValueError) as exc:
            say_(m_i18n.tr("! Not saved: {why}.", why=m_text.clean_field(str(exc), 200)))
            return
        say_(m_i18n.tr("Saved as your scenario «{name}»: a new room offers it in «What will we do?».", name=name))
        return
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
                say_(m_i18n.tr("No changes yet."))
                return
            ctx.wizard.update(
                step="team_history",
                prompt=m_i18n.tr("Undo a change?"),
                options=[
                    (
                        m_i18n.tr("{p0} · {p1}", p0=m_text.local_hm(r.get("at", "")), p1=m_store.change_words(cfg, r)),
                        f"undo:{r['id']}",
                    )
                    for r in reversed(records)
                ],
            )
            return
        who = "room" if line == "room" else line.partition(":")[2]
        fields = ROOM_FIELDS if who == "room" else AGENT_FIELDS
        if who != "room" and who not in m_store.ids_of(cfg):
            return
        rows = []
        for key, title_of in fields.items():
            title = title_of()
            if who == "room":
                value: object = m_store.lead_of(cfg) if key == "lead" else m_store.team_of(cfg).get(key)
            else:
                p = next(q for q in cfg["participants"] if q["id"] == who)
                value = m_store.effective(m_screen.agent_kind(p), p, cfg, key)
                if key == "reports_to":
                    value = m_store.reports_to(m_screen.agent_kind(p), p, cfg)
            own = who == "room" or key in next(q for q in cfg["participants"] if q["id"] == who)
            rows.append(
                (
                    m_i18n.tr(
                        "{title}: {p1}{p2}",
                        title=title,
                        p1=setting_shown(key, value, cfg),
                        p2="" if own else m_i18n.tr(" (the scenario’s)"),
                    ),
                    f"field:{key}",
                )
            )
        name = m_i18n.tr("the room") if who == "room" else m_store.label_of(cfg, who)
        ctx.wizard.update(step="team_card", who=who, prompt=m_i18n.tr("Change {name}: which?", name=name), options=rows)
        return
    if step_ == "team_history":
        rec_id = line.partition(":")[2]
        rec = next((r for r in m_store.read_changes() if r.get("id") == rec_id), None)
        if rec is None:
            return
        ctx.wizard.update(
            step="team_undo",
            record=rec_id,
            prompt=m_i18n.tr("Undo it?"),
            options=[(m_i18n.tr("Undo"), "go"), (m_i18n.tr("Cancel"), "back")],
        )
        say_(
            m_i18n.tr(
                "Undo: {p0}. Work done and money spent stay; history cleared from an agent does not come back.",
                p0=m_store.change_words(cfg, rec),
            )
            + "\n" * 4
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
            say_(m_i18n.tr("! Not undone: {p0}.", p0=m_text.clean_field(str(exc), 200)))
            return
        if rec["changes"]:
            say_(m_i18n.tr("Undone: {p0}.", p0=m_store.change_words(m_store.load_config(), rec)))
        for why in left:
            say_(m_i18n.tr("! Left as it is: {why}.", why=why))
        return
    if step_ == "team_card":
        key = line.partition(":")[2]
        ctx.wizard.update(
            step="team_field",
            key=key,
            prompt=f"{field_title(key)}:",
            options=field_options(cfg, ctx.wizard["who"], key),
        )
        return
    if step_ in ("team_field", "team_typed"):
        who, key = ctx.wizard["who"], ctx.wizard["key"]
        if step_ == "team_field" and line == "type":
            ctx.wizard.update(step="team_typed", options=None, prompt=m_i18n.tr("{p0} (Esc to go back):", p0=field_title(key)))
            say_(ctx.wizard["prompt"])
            return
        after = line if step_ == "team_typed" else chosen_value(key, line, cfg)
        lead = m_store.lead_of(cfg)
        if who != "room" and key == "role" and after == "lead" and who != lead:
            # «Lead» for another agent is the room's change of lead — the one way a team keeps one lead: the new one
            # leads, the one before becomes a worker, in one record (it asked the same question again, unsaid why).
            who, key, after = "room", "lead", who
        elif who != "room" and key == "role" and who == lead and after != "lead":
            # The lead itself: no team is left without one — said, and back to its card, never the same question.
            ctx.wizard_back()
            ctx.wizard["went_back"] = True  # the history as wizard_back left it: Esc from the card goes on back
            say_(
                m_i18n.tr(
                    "! {name} leads the room: to give the lead to another, choose «lead» in that agent's card.",
                    name=m_store.label_of(cfg, who),
                )
            )  # above the card's menu by the one rule (chat_run_wizard_step)
            return
        why = m_store.change_problem(cfg, who, key, after)
        if why:
            say_(
                m_i18n.tr("! {why}. Try again.", why=why) + "\n" * (len(ctx.wizard.get("options") or []) + 2)
            )  # above the menu still open
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
        ctx.wizard.update(
            step="team_confirm",
            changes=changes,
            prompt=m_i18n.tr("Save it?"),
            options=[(m_i18n.tr("Save"), "go"), (m_i18n.tr("Cancel"), "back")],
        )
        name = m_i18n.tr("the room") if who == "room" else m_store.label_of(cfg, who)
        say_(
            m_i18n.tr(
                "{name} — {p1}: {p2} → {p3}. ",
                name=name,
                p1=field_title(key).lower(),
                p2=setting_shown(key, before, cfg),
                p3=setting_shown(key, after, cfg),
            )
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
            say_(m_i18n.tr("! Not saved: {exc}.", exc=exc))
            return
        say_(m_i18n.tr("Saved: {p0}.", p0=m_store.change_words(m_store.load_config(), rec)))
        return


def chat_run_wizard_step(ctx: ChatCommandContext, line: str) -> None:
    """One step of a question, and one rule for what it said: a line printed while a menu is open goes above the menu,
    never under it — a refusal under the menu was covered by it, and the same question looked asked again unsaid."""
    said: list[str] = []
    wizard_step(ctx, line, said)
    rows = len(ctx.wizard.get("options") or []) + 2 if ctx.wizard.get("options") else 0
    if said and rows:
        # The feed's last lines are what the menu covers: blank ones as many as it takes, minus those said already.
        blank = len(said[-1]) - len(said[-1].rstrip("\n"))
        if blank < rows:
            ctx.feed("\n" * (rows - blank - 1))


def wizard_step(ctx: ChatCommandContext, line: str, said: list[str]) -> None:
    """/add, /remove, /mute, /name, /exit and the others: every answer is chosen in the menu;
    only what has to be written — a name, a path as the last resort — is typed."""
    rp = m_store.room().paths
    say_ = lambda text: (said.append(text), ctx.feed(m_text.paint(text, "38;5;252")))[1]
    if line in ("/exit", "/quit"):
        ctx.wizard.clear()
        ctx.wizard.update(
            step="exit_confirm",
            from_cmd="/exit",
            prompt=m_i18n.tr("Close chat? Agents keep running"),
            options=[(m_i18n.tr("No"), "n"), (m_i18n.tr("Yes"), "y")],
        )
        return
    if line in ("/mute", "/unmute", "/unlimit"):
        ctx.wizard.clear()
        people = ctx.people_for(line)
        if not people:
            say_(
                {
                    "/mute": m_i18n.tr("No one to mute."),
                    "/unmute": m_i18n.tr("No one is muted."),
                    "/unlimit": m_i18n.tr("No one is out of its limit."),
                }[line]
            )
            return
        prompt = {
            "/mute": m_i18n.tr("Mute whom? The room stops sending them messages"),
            "/unmute": m_i18n.tr("Unmute whom?"),
            "/unlimit": m_i18n.tr("Whose limit is over? The room sends its messages again"),
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
            prompt=m_i18n.tr("Rename whom?"),
            options=[(m_store.label_of(ctx.config, x) + (" (you)" if x == ctx.args.as_ else ""), x) for x in ctx.known],
        )
        return
    if line.startswith("/remove "):
        pid = ctx.names.get(line[len("/remove ") :].strip().lower().lstrip("@"))
        if not pid or pid in ("all", ctx.args.as_):
            say_(m_i18n.tr("Choose someone from the list."))
            return
        ctx.wizard.clear()
        ctx.wizard.update(
            step="rm_confirm",
            pid=pid,
            options=[(m_i18n.tr("No"), "n"), (m_i18n.tr("Yes, disconnect"), "y")],
            prompt=m_i18n.tr("Disconnect {p0}?", p0=m_store.label_of(ctx.config, pid)),
            from_cmd="/remove-name",
        )
        say_(
            m_i18n.tr(
                "Disconnect {p0}? Room messages will stop. The agent will keep running.", p0=m_store.label_of(ctx.config, pid)
            )
        )
        return
    if line == "/remove":
        people = [pid for pid in ctx.known if pid != ctx.args.as_]
        if not people:
            say_(m_i18n.tr("No one to disconnect."))
            return
        ctx.wizard.clear()
        ctx.wizard.update(
            step="rm_pick",
            people=people,
            from_cmd="/remove",
            options=[(m_store.label_of(ctx.config, x), str(i)) for i, x in enumerate(people, 1)],
        )
        ctx.wizard["prompt"] = m_i18n.tr("Choose an agent to disconnect")
        say_(ctx.wizard["prompt"] + ".")
        return
    if line == "/help":
        ctx.wizard.clear()
        cfg = m_store.load_config()
        paused, lang = cfg.get("paused"), m_store.room_language(cfg)
        options = []
        for key, title, _, _, _ in ctx.help_tasks:
            if key == "pause":
                title = "Resume the room" if paused else "Pause the room"  # English: help_words says it in the room's language
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
            say_(m_i18n.tr("The private-talk panel is closed."))
            return
        if arg:
            pid = ctx.names.get(arg.lower().lstrip("@"))
            if not pid or pid in ("all", ctx.args.as_):
                say_(m_i18n.tr("Choose someone from the list."))
                return
            ctx.open_dm(pid)
            return
        ctx.wizard.clear()
        cfg = m_store.load_config()
        others = [q["id"] for q in cfg["participants"] if q["id"] != ctx.args.as_]
        if not others:
            say_(m_i18n.tr("No agents in the room yet. Add one with /add."))
            return
        ctx.wizard.update(
            step="dm_pick",
            from_cmd="/dm",
            prompt=m_i18n.tr("Talk privately with whom?"),
            options=[(m_store.label_of(cfg, x), x) for x in others],
        )
        return
    if line == "/mine":
        ctx.wizard.clear()
        cfg = m_store.load_config()
        st = m_screen.read_status()
        waiting = [
            (
                m_i18n.tr(
                    "‼ {p0} waits for you: {p1}",
                    p0=m_store.label_of(cfg, pid),
                    p1=m_text.clean_field(x.get("detail", ""), 60) or "needs your input",
                ),
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
            where = m_i18n.tr("Open conversation") if m["from"] in agents else m_i18n.tr("Open")
            return m_i18n.tr(
                "{p0} {p1} — {first}  · {where}",
                p0=m_store.label_of(cfg, m["from"]),
                p1=m_text.local_hm(m["at"]),
                first=first,
                where=where,
            ), f"{kind}:{m['id']}"

        items = [row(m, "msg") for m in unread]
        earlier = [
            row(m, "old")
            for m in reversed([m for m in m_log.for_person(everything, ctx.args.as_, all_time=True) if m["id"] in read][-20:])
        ]
        waiting = [(label + m_i18n.tr("  · Open agent terminal"), value) for label, value in waiting]
        if not waiting and not items and not earlier:
            say_(m_i18n.tr("No messages for you yet."))
            return
        others = sum(
            1 for m in everything if m["from"] != ctx.args.as_ and "all" in m["to"] and m["id"] not in read and m not in unread
        )
        mark = [(m_i18n.tr("✓ Mark all as read"), "all:")] if items else []
        # The line between new and earlier only when there is something new above it: with nothing new
        # the choice started on it, and it looked chosen — the question says it then.
        line_ = [(m_i18n.tr("── Earlier for you ──"), "sep:")] if earlier and (items or waiting) else []
        new = len(waiting) + len(items)
        prompt = (
            m_i18n.tr("For you · {new} new", new=new) if new else m_i18n.tr("No unread messages. Earlier messages are below.")
        ) + (m_i18n.tr(" · +{others} for everyone", others=others) if others else "")
        ctx.wizard.update(step="mine_pick", from_cmd="/mine", options=waiting + items + mark + line_ + earlier, prompt=prompt)
        return
    if line == "/restart":
        ctx.wizard.clear()
        cfg = m_store.load_config()
        mine = [
            q["id"] for q in cfg["participants"] if q.get("started_by_room") and m_screen.agent_kind(q) in ("claude", "codex")
        ]
        if not mine:
            say_(m_i18n.tr("No agent the room started. Add one with /add."))
            return
        ctx.wizard.update(
            step="restart_pick",
            from_cmd="/restart",
            prompt=m_i18n.tr("Restart whom? The conversation continues"),
            options=[(m_store.label_of(cfg, x), x) for x in mine],
        )
        return
    if line.startswith("/answer "):
        # «/answer Bob», from the command menu's names or typed.
        pid = ctx.names.get(line[len("/answer ") :].strip().lower().lstrip("@"))
        if not pid or pid in ("all", ctx.args.as_):
            say_(m_i18n.tr("Choose someone from the list."))
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
            say_(m_i18n.tr("No agents in the room yet. Add one with /add."))
            return
        prompt = {
            "/view": m_i18n.tr("View an agent"),
            "/open": m_i18n.tr("Open an agent's window"),
            "/answer": m_i18n.tr("Answer whom?"),
        }[line]
        ctx.wizard.update(
            step=line[1:] + "_pick",
            from_cmd=line,
            prompt=prompt,
            options=[(m_store.label_of(cfg, x) + ("" if panes.get(x) else m_i18n.tr(" — not in tmux")), x) for x in others],
        )
        say_(prompt + ".")
        return
    if line == "/clear":
        ctx.wizard.clear()
        ctx.wizard.update(
            step="clear_confirm",
            options=[(m_i18n.tr("No"), "n"), (m_i18n.tr("Yes, clear the history"), "y")],
            prompt=m_i18n.tr("Clear the room's history?"),
            from_cmd="/clear",
        )
        say_(
            m_i18n.tr(
                "Clear the room's history? Everyone's feed starts empty; the messages move to an archive in .collab/archive/, they are not deleted."
            )
        )
        return
    if line == "/stop":
        ctx.wizard.clear()
        _, closing, staying = m_lifecycle.stop_plan(m_store.load_config())
        ctx.wizard.update(
            step="stop_confirm",
            options=[(m_i18n.tr("No"), "n"), (m_i18n.tr("Yes, stop the room"), "y")],
            prompt=m_i18n.tr("Stop the room?"),
            from_cmd="/stop",
        )
        say_(m_i18n.tr("Stop the room? ") + m_lifecycle.describe_stop(closing, staying))
        return
    if line == "/telegram":
        on = m_telegram.load().get("state") == "on"
        ctx.wizard.clear()
        options = (
            [
                (m_i18n.tr("New code"), "connect"),
                (m_i18n.tr("Turn Telegram off"), "off"),
                (m_i18n.tr("Unlink the account"), "unlink"),
            ]
            if on
            else [(m_i18n.tr("Connect Telegram"), "connect")]
        )
        ctx.wizard.update(
            step="telegram_pick",
            from_cmd="/telegram",
            prompt=m_i18n.tr("Telegram is on: write to the lead in the room's topic.")
            if on
            else m_i18n.tr("Telegram is off. Connect your account to write to this room's lead from Telegram."),
            options=options + [(m_i18n.words_in("team.back", m_store.room_language(m_store.load_config())), "back")],
        )
        return
    if ctx.wizard.get("step") == "telegram_pick":
        ctx.wizard.clear()
        if line == "back":
            return
        try:
            if line == "connect":
                code, expires, link = m_telegram.enable()
                minutes = max(1, int((expires - time.time()) // 60))
                if link:  # the link first: a tap opens the bot, its Start sends the code itself
                    say_(
                        m_i18n.tr(
                            "Open it in Telegram — the code goes with it ({minutes} min): {link}", minutes=minutes, link=link
                        )
                    )
                    # A phone scans it from the screen: its lines kept whole, black on white of their own.
                    ctx.feed("\n".join(m_chat_base.WHOLE + line for line in m_qr.draw(link)))
                say_(
                    m_i18n.tr(
                        "Open {bot} in Telegram and send: /start {code} — the code works {minutes} min.",
                        bot=m_telegram.BOT,
                        code=code,
                        minutes=minutes,
                    )
                )
                m_lifecycle.start_telegram(m_store.load_config().get("session", "collab-room"))
            elif line == "off":
                m_telegram.disable()
                say_(m_i18n.tr("Telegram is off for this room. Its link stays: `collab telegram on` turns it back on."))
            elif line == "unlink":
                n = m_telegram.unlink()
                say_(m_i18n.tr("Telegram is unlinked from this room; messages that waited: {n}, cancelled.", n=n))
        except (m_telegram.Refused, OSError) as exc:
            say_(m_i18n.tr("The bot did not answer ({exc}). Nothing was changed.", exc=exc))
        return
    if line == "/language":
        # Its own question in both: a person who cannot read the room's language still finds theirs. Applied at once
        # (a choice made and not confirmed was lost; another /language takes it back); «and for new rooms» is a choice
        # of its own in the same list.
        ctx.wizard.clear()
        options: list[tuple[str, str]] = []
        for x in m_i18n.LANGS:
            options.append((m_i18n.LANG_NAMES[x], x))
            options.append((m_i18n.words_in("lang.and_new", x, language=m_i18n.LANG_NAMES[x]), x + ":new"))
        ctx.wizard.update(step="language_pick", from_cmd="/language", prompt=m_i18n.words_in("lang.choose"), options=options)
        return
    if ctx.wizard.get("step") == "language_pick":
        target, _, new = line.partition(":")
        if target not in m_i18n.LANGS:
            return
        ctx.wizard.clear()
        m_store.set_room_language(target)
        m_i18n.speak(target)
        if new:
            m_store.set_user_language(target)
        ctx.refresh_people()  # the room's file changed: the chat reads it again, its menu and help follow
        say_(m_i18n.words_in("lang.changed", target))
        if new:
            say_(m_i18n.words_in("lang.new_rooms", target))
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
            say_(m_i18n.tr("The proposal waits: /team shows it."))
            if line.strip():
                ctx.submit(line)
            return
        if line == "later":
            say_(m_i18n.tr("It waits: /team shows it."))
            return
        if line == "decline":
            m_store.set_proposal_state(mid, "declined")
            m = next((x for x in m_log.read_messages() if x.get("id") == mid), {})
            if m.get("from") in m_store.ids_of(m_store.load_config()):
                # Unsigned and quiet: not a request a next proposal could name.
                m_log.append_message(m_text.ROOM, [m["from"]], m_i18n.tr("Your proposal is declined."), mid)
            say_(m_i18n.tr("Declined."))
            return
        try:
            lines = m_agents.apply_proposal(mid, ctx.args.as_, digest)
        except Exception as exc:  # noqa: BLE001 — a record anyone could write: said, and the chat stays
            say_(m_i18n.tr("! Not applied: {p0}.", p0=m_text.clean_field(str(exc), 200)))
            return
        except SystemExit as exc:
            say_(m_i18n.tr("! Not applied: {p0}.", p0=m_text.clean_field(str(exc.code), 200)))
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
        scenario = m_store.scenario_of(line, list(m_screen.PRESETS))
        if scenario is None:
            return
        ctx.wizard["draft"] = json.loads(json.dumps(scenario["seats"]))  # changed here before the launch
        ctx.wizard["scenario"] = line
        show_draft(ctx, say_, lang)
        return
    if ctx.wizard.get("step") == "scenario_go":
        if line == "back":
            ctx.wizard_back()
            return
        draft = ctx.wizard["draft"]
        seats = [(m_text.visible(seat["label"]), str(i)) for i, seat in enumerate(draft)]
        if line in ("edit", "remove"):
            ctx.wizard.update(step=f"draft_{line}", prompt=m_i18n.words_in("team.which", lang), options=seats)
            return
        if line == "add":
            roles = [(m_i18n.words_in(f"role.{r}", lang), r) for r in ("worker", "reviewer")]
            ctx.wizard.update(step="draft_add_role", prompt=m_i18n.words_in("team.add.role", lang), options=roles)
            return
        if line != "go":
            return
        name = ctx.wizard["scenario"]
        ctx.wizard.clear()
        if m_store.team_of(m_store.load_config()).get("scenario"):
            say_(m_i18n.words_in("team.has_one", lang))
            return
        say_(m_i18n.words_in("team.starting", lang, title=m_store.scenario_title(name, lang)))
        try:
            started, failed = m_agents.start_scenario(name, str(rp.root), draft)
        except ValueError as exc:
            say_(m_i18n.words_in("team.bad", lang, why=m_text.clean_field(str(exc), 200)))
            return
        ctx.feed(m_text.paint(m_agents.team_started(m_store.load_config(), started, failed), "38;5;252"))
        ctx.refresh_people()
        return
    if ctx.wizard.get("step") == "draft_remove":
        draft = ctx.wizard["draft"]
        if line.isdigit() and int(line) < len(draft):
            if draft[int(line)]["role"] == "lead":
                say_(m_i18n.words_in("team.lead.stays", lang))
            else:
                draft.pop(int(line))
        show_draft(ctx, say_, lang)
        return
    if ctx.wizard.get("step") == "draft_edit":
        if not line.isdigit() or int(line) >= len(ctx.wizard["draft"]):
            return
        seat = ctx.wizard["draft"][int(line)]
        fields = [(m_i18n.words_in(f"team.field.{f}", lang), f) for f in ("role", "topic", "model", "memory")]
        ctx.wizard.update(
            step="draft_field", seat=int(line), prompt=m_i18n.words_in("team.field", lang, label=seat["label"]), options=fields
        )
        return
    if ctx.wizard.get("step") == "draft_field":
        seat = ctx.wizard["draft"][ctx.wizard["seat"]]
        if line == "role":
            options = [(m_i18n.words_in(f"role.{r}", lang), r) for r in ("worker", "reviewer")]
        elif line == "model":
            options = [(x, x) for x in m_store.MODEL_ORDER.get(seat["kind"], [])] + [
                (m_i18n.words_in("team.model.other", lang), "type")
            ]
        elif line == "memory":
            options = [(m_i18n.words_in(f"team.memory.{x}", lang), x) for x in ("compact", "clear", "keep")]
        elif line == "topic":
            ctx.wizard.update(step="draft_typed", field="topic", options=None, prompt=m_i18n.words_in("team.type", lang))
            say_(ctx.wizard["prompt"])
            return
        else:
            return
        if line == "role" and seat["role"] == "lead":
            say_(m_i18n.words_in("team.lead.stays", lang))
            show_draft(ctx, say_, lang)
            return
        ctx.wizard.update(step="draft_value", field=line, options=options, prompt=m_i18n.words_in(f"team.field.{line}", lang))
        return
    if ctx.wizard.get("step") in ("draft_value", "draft_typed"):
        seat, field = ctx.wizard["draft"][ctx.wizard["seat"]], ctx.wizard["field"]
        if line == "type":
            ctx.wizard.update(step="draft_typed", options=None, prompt=m_i18n.words_in("team.type", lang))
            say_(ctx.wizard["prompt"])
            return
        key = str({"model": "use_model", "memory": "context_trim"}.get(field, field))
        value: object = m_store.MEMORY[line] if field == "memory" and line in m_store.MEMORY else line
        why = m_store.setting_problem(key, value, {"participants": []})
        if why:
            say_(m_i18n.words_in("team.bad", lang, why=why))
            return
        seat[key] = value
        show_draft(ctx, say_, lang)
        return
    if ctx.wizard.get("step") == "draft_add_role":
        if line not in ("worker", "reviewer"):
            return
        ctx.wizard.update(step="draft_add_topic", role=line, options=None, prompt=m_i18n.words_in("team.add.topic", lang))
        say_(ctx.wizard["prompt"])
        return
    if ctx.wizard.get("step") == "draft_add_topic":
        why = m_store.setting_problem("topic", line, {"participants": []})
        if why:
            say_(m_i18n.words_in("team.bad", lang, why=why))
            return
        draft, role = ctx.wizard["draft"], ctx.wizard["role"]
        base = m_i18n.words_in(f"role.{role}", lang).capitalize()
        taken = {seat["label"] for seat in draft} | {seat["seat"] for seat in draft}
        label = next(n for n in [base] + [m_i18n.tr("{base} {i}", base=base, i=i) for i in range(2, 100)] if n not in taken)
        sid = next(f"added{i}" for i in range(1, 100) if f"added{i}" not in taken)
        draft.append(
            {
                "seat": sid,
                "kind": "claude",
                "label": label,
                "role": role,
                "topic": line,
                "use_model": "sonnet",
                "context_trim": m_store.MEMORY["clear"],
            }
        )
        show_draft(ctx, say_, lang)
        return
    if line == "/add":
        # Only agents the room starts itself: a session already running somewhere
        # lives in its own folder, its status never reached the room.
        ctx.wizard.clear()
        kinds = [k for k in m_screen.PRESETS if shutil.which(m_screen.PRESETS[k]["command"][0])]
        if not kinds:
            say_(m_i18n.tr("No Claude Code or Codex installation found."))
            return
        ctx.wizard.update(
            step="kind",
            kinds=kinds,
            prompt=m_i18n.tr("Which agent to start?"),
            from_cmd="/add",
            options=[(m_screen.PRESETS[k]["label"], str(i)) for i, k in enumerate(kinds, 1)],
        )
        say_(m_i18n.tr("Add an agent. ") + ctx.wizard["prompt"])
        return
    if ctx.wizard.get("step") == "own_name":
        problem = line and (
            m_text.name_problem(line)
            or (
                m_i18n.tr("that is already someone else's name here")
                if m_store.label_taken(ctx.config, ctx.args.as_, line)
                else ""
            )
        )
        if problem:
            say_(m_i18n.tr("! {problem}. Try another name, or Enter to keep yours.", problem=problem))
            return
        then = ctx.wizard.get("then") or {}
        ctx.wizard.clear()
        if line:
            ctx.handle(f"/name {ctx.args.as_} {line}")
        else:
            say_(m_i18n.tr("You stay {p0}. /name changes it any time.", p0=m_store.label_of(ctx.config, ctx.args.as_)))
        ctx.wizard.update(then)  # the welcome, when this is the first visit — after the name is set
        return
    if line == "" and ctx.wizard.get("step") != "folder":  # in «folder» an empty Enter asks again
        ctx.wizard.clear()
        say_(m_i18n.tr("Canceled."))
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
            say_(m_i18n.tr("All marked as read. Only what comes from now on counts."))
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
            options=[(m_i18n.tr("No"), "n"), (m_i18n.tr("Yes, restart"), "y")],
            prompt=m_i18n.tr("Restart {p0}?", p0=m_store.label_of(cfg, line)),
        )
        say_(
            m_i18n.tr(
                "Restart {p0}? Its program closes and starts again in the same window with the same conversation; what it was doing at this moment stops. It reads the room's newest rules.",
                p0=m_store.label_of(cfg, line),
            )
        )
        return
    if step_ == "restart_confirm":
        pid = ctx.wizard["pid"]
        ctx.wizard.clear()
        if line != "y":
            say_(m_i18n.tr("Canceled."))
            return
        cfg = m_store.load_config()
        q = next((x for x in cfg["participants"] if x["id"] == pid), None)
        if q is None:
            say_(m_i18n.tr("It is no longer in the room."))
            return
        found = m_screen.agent_session(q)  # read once, here: restart_agent takes it, not a second search
        if not found:
            ctx.wizard.update(
                step="restart_fresh",
                pid=pid,
                options=[(m_i18n.tr("No"), "n"), (m_i18n.tr("Yes, a new conversation"), "y")],
                prompt=m_i18n.tr("{p0}'s conversation was not found. Start a new one?", p0=m_store.label_of(cfg, pid)),
            )
            say_(
                m_i18n.tr(
                    "{p0}'s conversation was not found. Start it with a new conversation? What it knew stays only in its files.",
                    p0=m_store.label_of(cfg, pid),
                )
            )
            return
        line, step_ = "go", "restart_go"
    else:
        found = ""
    if step_ == "restart_fresh":
        pid = ctx.wizard["pid"]
        ctx.wizard.clear()
        if line != "y":
            say_(m_i18n.tr("Canceled."))
            return
        step_ = "restart_new"
    if step_ in ("restart_go", "restart_new"):
        cfg = m_store.load_config()
        q = next((x for x in cfg["participants"] if x["id"] == pid), None)
        if q is None:
            say_(m_i18n.tr("It is no longer in the room."))
            return
        try:
            m_agents.restart_agent(cfg.get("session", "collab-room"), q, fresh=step_ == "restart_new", sid=found)
        except (m_store.OperationFailed, SystemExit) as exc:
            ctx.feed(m_text.paint(m_i18n.tr("! Could not restart: {exc}", exc=exc), "1;38;5;196"))
            return
        how = m_i18n.tr("with a new conversation") if step_ == "restart_new" else m_i18n.tr("with its conversation")
        say_(m_i18n.tr("Restarting {p0} {how}. Waiting for a connection check reply.", p0=m_store.label_of(cfg, pid), how=how))
        ctx.refresh_people()
        return
    if step_ == "mute_pick":
        verb = ctx.wizard["verb"]
        ctx.wizard.clear()
        if line in ctx.known:
            # By id: by name, two of one name muted the last of them.
            ctx.handle(m_i18n.tr("{verb} {line}", verb=verb, line=line))
        return
    if step_ == "name_pick":
        if line not in ctx.known:
            return
        ctx.wizard.update(
            step="rename",
            pid=line,
            options=None,
            prompt=m_i18n.tr("New name for {p0} (Enter to save, Esc to go back):", p0=m_store.label_of(ctx.config, line)),
        )
        say_(ctx.wizard["prompt"])
        return
    if step_ == "rename":
        pid, label = ctx.wizard["pid"], line
        problem = m_text.name_problem(label) or (
            m_i18n.tr("that is already someone else's name here") if m_store.label_taken(ctx.config, pid, label) else ""
        )
        if problem:
            say_(m_i18n.tr("! {problem}. Try another name.", problem=problem))
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
            say_(m_i18n.tr("You can open the guide any time: type /help."))
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
        say_(title + "\n" + "\n".join(m_i18n.tr("  {i}. {x}", i=i, x=x) for i, x in enumerate(steps, 1)) + "\n" * 4)
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
            say_(m_i18n.tr("Live view unavailable: this agent is not in tmux."))
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
            say_(m_i18n.tr("Cannot open {label}'s window: this agent is not in tmux.", label=label))
            return

        # Opened once the key is handled, outside the chat's lock: holding it while the popup was
        # open stopped the feed and the states under it — red until Ctrl+].
        def show_popup(pid: str = line, label: str = label) -> None:
            why = m_agents.open_popup(pid)
            with ctx.lock:
                if why:
                    say_(
                        m_i18n.tr(
                            "Cannot open {label}'s window over the chat: {why}. Use /open for a command to paste into a new terminal tab.",
                            label=label,
                            why=why,
                        )
                    )
                else:
                    ctx.repaint()
                    # «Answer whom?.» stayed the last line, as if still asked.
                    say_(m_i18n.tr("Back in the room."))

        ctx.after_key().append(show_popup)
        return
    if step_ == "open_pick":
        # Choose the agent — and the steps are there, the command already copied.
        ctx.wizard.clear()
        cfg = m_store.load_config()
        if not m_deliver.current_panes(cfg).get(line):
            say_(m_i18n.tr("Cannot open: this agent is not in tmux."))
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
            say_(m_i18n.tr("Canceled."))
            return
        where = m_log.archive_history()
        # The history ↑ goes too: it kept the old messages and «feed starts empty» was not
        # true; nothing older is left in the new feed.
        ctx.feed_log.clear()
        ctx.blocks.clear()
        ctx.ui.older, ctx.ui.top, ctx.ui.unseen = 0, None, 0
        ctx.repaint()
        say_(m_i18n.tr("History cleared. The old messages are in {p0}.", p0=m_text.tilde(str(where))))
        return
    if step_ == "stop_confirm":
        ctx.wizard.clear()
        if line.lower() not in ("y", "yes", "д", "да"):
            say_(m_i18n.tr("Canceled."))
            return
        say_(m_i18n.tr("Stopping the room…"))
        m_tmuxio.tmux("kill-session", "-t", f"={m_lifecycle.stop_plan(m_store.load_config())[0]}", check=False)
        return
    if step_ == "rm_pick":
        if not line.isdigit() or not 1 <= int(line) <= len(ctx.wizard["people"]):
            say_(m_i18n.tr("Enter a number from the list."))
            return
        pid = ctx.wizard["people"][int(line) - 1]
        ctx.wizard.update(
            step="rm_confirm",
            pid=pid,
            options=[(m_i18n.tr("No"), "n"), (m_i18n.tr("Yes, disconnect"), "y")],
            prompt=m_i18n.tr("Disconnect {p0}?", p0=m_store.label_of(ctx.config, pid)),
        )
        say_(
            m_i18n.tr(
                "Disconnect {p0}? Room messages will stop. The agent will keep running.", p0=m_store.label_of(ctx.config, pid)
            )
        )
        return
    if step_ == "rm_confirm":
        if line.lower() not in ("y", "yes", "д", "да"):
            ctx.wizard.clear()
            say_(m_i18n.tr("Canceled."))
            return
        try:
            label = m_agents.remove_participant(ctx.wizard["pid"])
        except ValueError as exc:
            say_(m_i18n.tr("! {exc}", exc=exc))
        else:
            say_(m_i18n.tr("{label} disconnected from the room.", label=label))
        ctx.wizard.clear()
        ctx.refresh_people()
        return
    if step_ == "kind":
        if not line.isdigit() or not 1 <= int(line) <= len(ctx.wizard["kinds"]):
            say_(m_i18n.tr("Enter a number from the list."))
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
            + [(m_i18n.tr("Browse folders…"), "browse"), (m_i18n.tr("Enter path manually…"), "other")],
            prompt=m_i18n.tr("Project folder"),
        )
        say_(m_i18n.tr("Project folder."))
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
            ctx.wizard["prompt"] = m_i18n.tr("Cannot open {p0}/ — choose another folder", p0=m_text.clean_field(line[7:], 60))
            ctx.ui.menu_index = next((i for i, (_, v) in enumerate(ctx.wizard["options"]) if v == line), 0)
            return
        elif line == "type":
            ctx.wizard.update(step="folder", options=None, prompt=m_i18n.tr("Type the folder path (Esc to go back):"))
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
            ctx.wizard.update(step="folder", options=None, prompt=m_i18n.tr("Type the folder path (Esc to go back):"))
            say_(ctx.wizard["prompt"])
            return
        if not line.isdigit() or not 1 <= int(line) <= len(ctx.wizard["folders"]):
            say_(m_i18n.tr("Choose a folder from the list."))
            return
        line = ctx.wizard["folders"][int(line) - 1]
        step_ = "folder"
    if step_ == "folder":
        if not line:
            # An empty path never means some other project (Enter after an error
            # started the agent in the room's folder).
            say_(m_i18n.tr("Enter a folder path, or press Esc to choose from the list."))
            return
        try:
            folder = Path(line).expanduser()
        except RuntimeError:  # «~someone» with no such user: it dropped the whole /add
            say_(m_i18n.tr("Folder not found: {p0}", p0=m_text.clean_field(line, 60)))
            return
        problem = m_agents.folder_problem(folder)
        if problem:
            say_(problem)
            return
        ctx.wizard.update(
            step="name_new",
            folder=str(folder.resolve()),
            options=None,
            prompt=m_i18n.tr("Name this agent — Enter takes the suggestion, or type another (Esc to go back):"),
        )
        say_(ctx.wizard["prompt"])
        base = m_screen.PRESETS[ctx.wizard["kind"]]["label"]
        suggestion = next(
            n
            for n in [base] + [m_i18n.tr("{base} {i}", base=base, i=i) for i in range(2, 100)]
            if not m_store.label_taken(ctx.config, "", n)
        )
        ctx.buffer()[:] = list(suggestion)
        ctx.ui.pos = len(ctx.buffer())
        ctx.ui.suggested = True  # like selected text: the first letter typed replaces it
        return
    if step_ == "name_new":
        label = line
        if not label:
            say_(m_i18n.tr("Enter a name."))
            return
        # The same checks as /name: /add gave two agents one name.
        problem = m_text.name_problem(label) or (
            m_i18n.tr("that is already someone else's name here") if m_store.label_taken(ctx.config, "", label) else ""
        )
        if problem:
            say_(m_i18n.tr("! {problem}. Try another name.", problem=problem))
            return
        try:
            pid = m_agents.launch_new_agent(ctx.config, ctx.wizard["kind"], ctx.wizard["folder"], label)
        except SystemExit as exc:
            # fail() must not close the owner's seat: say it, with the reason, and stay.
            ctx.wizard.clear()
            ctx.feed(m_text.paint(m_i18n.tr("! Could not start the agent: {exc}. Try /add again.", exc=exc), "1;38;5;196"))
            return
        say_(m_i18n.tr("Starting {label} in room window {pid}. Waiting for a connection check reply.", label=label, pid=pid))
        say_(m_i18n.tr("Settings: {p0}.", p0=m_store.settings_words(ctx.wizard["kind"], {"id": pid}, m_store.load_config())))
        ctx.wizard.clear()
        ctx.refresh_people()
        return
