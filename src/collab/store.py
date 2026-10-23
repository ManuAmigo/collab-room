from __future__ import annotations
import contextlib
import datetime as dt
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, NoReturn, TypeVar, cast
import collab.boot as m_boot
import collab.text as m_text
import collab.i18n as m_i18n
import collab.term as m_term


@dataclass(frozen=True)
class RoomPaths:
    """Where one room keeps its files. One set, swapped whole by use_room: the chat's two threads
    would otherwise read half of one room's paths and half of another's."""

    root: Path
    base: Path
    config_path: Path
    state_dir: Path
    messages_path: Path
    receipts_dir: Path
    delivered_dir: Path
    panes_path: Path
    status_path: Path
    await_path: Path
    noreply_dir: Path
    held_path: Path
    limits_path: Path

    @classmethod
    def at(cls, root: Path) -> "RoomPaths":
        base = m_boot.room_base(root)
        state = base / ".collab"
        return cls(
            root=root,
            base=base,
            config_path=base / ".collab.json",
            state_dir=state,
            messages_path=state / "messages.jsonl",
            receipts_dir=state / "receipts",
            delivered_dir=state / "delivered",
            panes_path=state / "panes.json",
            status_path=state / "status.json",
            await_path=state / "awaiting.json",
            noreply_dir=state / "no-reply",
            held_path=state / "held.jsonl",
            limits_path=state / "limits.json",
        )


class Room:
    """The room this process works in; its paths change only by one assignment (use_room)."""

    def __init__(self, room_paths: RoomPaths) -> None:
        self.paths = room_paths


_ROOM = Room(RoomPaths.at(m_boot._start_dir()))  # pyright: ignore[reportPrivateUsage] — boot's, read once here


def room() -> Room:
    """Read once per operation: `rp = room().paths`, then rp.… to its end."""
    return _ROOM


DELIVERY_MODES = {"pull", "tmux"}


class OperationFailed(Exception):
    """fail() inside the chat: the operation stops, the chat stays."""


class ChatMode:
    """What a chat in this process asks of the code below it: fail() stops the operation, not the
    program; notes go to its feed, stderr being its screen. cmd_chat sets it once;
    one object, its fields change, it is never replaced."""

    def __init__(self) -> None:
        self.on = False
        self.sink: list[str] | None = None


_CHAT_MODE = ChatMode()


def chat_mode() -> ChatMode:
    return _CHAT_MODE


def warn(note: str) -> None:
    """A note for the person: into the chat's feed when a chat runs here, else to stderr."""
    sink = chat_mode().sink
    if sink is None:
        print(note, file=sys.stderr)
    else:
        sink.append(note)


def fail(message: str) -> NoReturn:
    if chat_mode().on:
        raise OperationFailed(message)  # printed to stderr it would land on the chat's screen
    print(f"collab: {message}", file=sys.stderr)
    raise SystemExit(2)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ── config ─────────────────────────────────────────────────────────────────────


def read_config_raw() -> dict[str, Any]:
    """.collab.json as it is on disk, to change and write back — from the checked file itself, as
    load_config reads it: sixteen places read it by path, through a link too."""
    rp = room().paths
    try:
        data: object = json.loads(read_state_text(rp.config_path, "strict"))
    except (OSError, ValueError) as exc:
        fail(f"cannot read {rp.config_path}: {exc}")
    if not isinstance(data, dict):
        fail(f"the room's settings must be a JSON object — in {m_text.tilde(str(rp.config_path))}; fix it there")
    return cast("dict[str, Any]", data)


_PANES_SAID: list[str] = []


def read_panes() -> dict[str, Any]:
    """panes.json, from the checked file itself; missing or unreadable reads as empty. A broken one
    raised on every tick of the deliverer, and delivery stopped for everyone, said only in its own
    hidden window: it is said once, and the agents named by their target go on."""
    rp = room().paths
    try:
        data: object = json.loads(read_state_text(rp.panes_path, "strict"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        if not _PANES_SAID:
            _PANES_SAID.append(str(exc))
            with contextlib.suppress(OSError):
                print(f"cannot read {rp.panes_path} ({exc}); read as empty", file=sys.stderr, flush=True)
        return {}
    if not isinstance(data, dict) or not isinstance(cast("dict[str, Any]", data).get("panes", {}), dict):
        return {}
    return cast("dict[str, Any]", data)


# What an agent does in the room and how it runs — its settings, each by layers over the one before: the
# built-in one of its kind, its seat in the room's scenario (`team.seats`), the room (only `context_trim`), the
# agent's own. Only what was set explicitly is written down; what applies is worked out live, so a change of a
# lower layer reaches every agent that set nothing of its own. A value that does not pass its check is skipped,
# and the layer below applies (setting_problems names it). `context_trim` — a /compact or /clear past a context
# threshold (deliver.trim_due); `false` in the room or the agent — never. `use_model`, not `model`: that is the
# screen's pattern for the model shown.
ROLES = {
    "lead": "you split the work, gather the result and the questions for the person",
    "worker": "you do your part of the work and hand it to whom you report to",
    "reviewer": "you look for mistakes in the result and say what to fix; you do not redo the work",
}
SETTINGS = ("role", "topic", "use_model", "context_trim", "reports_to")
ROOM_SETTINGS = ("context_trim",)
BUILTIN: dict[str, dict[str, Any]] = {"claude": {"context_trim": None}, "codex": {"context_trim": None}}
# Only kinds whose window takes a /compact or /clear: a Codex's /clear starts a new chat its connection check
# no longer finds.
TRIM_KINDS = ("claude",)
# A model's name goes into the agent's command line: letters, digits, dots and dashes only — no «#», which
# tmux would read as a format.
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,47}")
TOPIC_LIMIT = 80
# What the team may do without the person; anything else external waits for them. None by default.
WITHOUT_PERSON = {
    "merge": "merge into the main branch",
    "publish": "publish",
    "send": "write to anyone outside the room",
    "spend": "spend money",
}
OUTPUT_LIMIT = 200


def language_path() -> Path:
    """The person's own default language for new rooms and what is said outside a room — beside the first-visit
    mark, outside any project; not a setting of Claude or Codex."""
    return m_boot.paths().welcomed_path.parent / "language"


def user_language() -> str:
    """The saved default; none — the system's (a Russian locale), else English."""
    path = language_path()
    with contextlib.suppress(OSError, ValueError):
        saved = "" if path.is_symlink() else path.read_text(encoding="utf-8").strip()  # a link is not read
        if saved in m_i18n.LANGS:
            return saved
    locale = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    return "ru" if locale.lower().startswith("ru") else "en"


def language_chosen() -> bool:
    return language_path().is_file()


def set_user_language(lang: str) -> None:
    path = language_path()
    private_dir(path.parent, strict=False)
    if path.is_symlink():
        path.unlink()  # ours to write, never through a link
    path.write_text(m_i18n.lang_of(lang) + "\n", encoding="utf-8")


def room_language(config: dict[str, Any]) -> str:
    """The room's own language; a room made before it had one speaks English, as it did."""
    return m_i18n.lang_of(config.get("language"))


def seat_of(p: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """The agent's seat in the room's scenario, or {} — none."""
    team = config.get("team")
    seats = cast("dict[str, Any]", team).get("seats") if isinstance(team, dict) else None
    if not isinstance(seats, list) or not p.get("seat"):
        return {}
    for seat in cast("list[object]", seats):
        if isinstance(seat, dict) and cast("dict[str, Any]", seat).get("seat") == p["seat"]:
            return cast("dict[str, Any]", seat)
    return {}


def setting_problem(key: str, value: object, config: dict[str, Any], pid: str = "") -> str:
    """Why this value of a setting can't be used, or ""."""
    if key == "role":
        return "" if isinstance(value, str) and value in ROLES else f"role must be one of {', '.join(ROLES)}"
    if key == "topic":
        if not isinstance(value, str) or not value.strip() or len(value) > TOPIC_LIMIT:
            return f"topic must be a line of 1 to {TOPIC_LIMIT} characters"
        return "topic can't contain control or invisible characters" if m_text.visible(value) != value or "\n" in value else ""
    if key == "use_model":
        ok = isinstance(value, str) and MODEL_NAME.fullmatch(value)
        return "" if ok else "use_model must be a model's name: letters, digits, dots and dashes"
    if key == "context_trim":
        return (
            ""
            if value is False or trim_valid(value)
            else 'context_trim must be {"at": tokens, "do": "compact" or "clear"}, or false'
        )
    if key == "reports_to":
        others = [q for q in ids_of(config) if q != pid]
        return "" if value == "person" or value in others else "reports_to must be «person» or another participant's id"
    return f"{key} is not a setting"


def effective(kind: object, p: dict[str, Any], config: dict[str, Any], key: str) -> Any:
    """What applies to the agent for this setting, by the layers; None — nothing set anywhere. `kind` as the
    room's file gives it: a value that is not a word is no kind, and delivery does not stop on it."""
    word = kind if isinstance(kind, str) else ""
    value: Any = BUILTIN.get(word, {}).get(key)
    for layer in (seat_of(p, config), config if key in ROOM_SETTINGS else {}, p):
        if key in layer and not setting_problem(key, layer[key], config, p.get("id", "")):
            value = layer[key]
    if key == "context_trim":
        return value if word in TRIM_KINDS and trim_valid(value) else None
    return value


def effective_trim(kind: str, p: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    """The agent's /compact or /clear threshold and action by the layers, or None — never."""
    return cast("dict[str, Any] | None", effective(kind, p, config, "context_trim"))


def setting_problems(config: dict[str, Any]) -> list[str]:
    """Each explicit setting that does not pass its check, named: it is skipped, not repaired."""
    out: list[str] = list(team_problems(config))
    for p in config.get("participants", []):
        for layer, whose in ((seat_of(p, config), "its seat"), (p, "")):
            for key in SETTINGS:
                if key in layer and (why := setting_problem(key, layer[key], config, p.get("id", ""))):
                    out.append(f"{p.get('label') or p.get('id')}{' (' + whose + ')' if whose else ''}: {why}")
    return out


def trim_words(rule: dict[str, Any] | None) -> str:
    return f"/{rule['do']} past {rule['at'] // 1000}k tokens of context" if rule else "off"


def settings_words(kind: str, p: dict[str, Any], config: dict[str, Any]) -> str:
    """One line of what applies to an agent: «reviewer · tests · model sonnet · context trim off · reports to Mira»."""
    role, topic, model = (effective(kind, p, config, k) for k in ("role", "topic", "use_model"))
    return " · ".join(
        [
            role or "no role",
            *([topic] if topic else []),
            f"model {model or 'default'}",
            f"context trim {trim_words(effective_trim(kind, p, config))}",
            f"reports to {whom(reports_to(kind, p, config), config)}",
        ]
    )


def team_of(config: dict[str, Any]) -> dict[str, Any]:
    team = config.get("team")
    return cast("dict[str, Any]", team) if isinstance(team, dict) else {}


def lead_of(config: dict[str, Any]) -> str:
    """The room's one lead: `team.lead` when it names a participant, else the first whose role is lead; or ""."""
    lead = team_of(config).get("lead")
    if isinstance(lead, str) and lead in ids_of(config):
        return lead
    for q in config.get("participants", []):
        if effective(q.get("kind", ""), q, config, "role") == "lead":
            return str(q["id"])
    return ""


def reports_to(kind: str, p: dict[str, Any], config: dict[str, Any]) -> str:
    """Whom the agent reports to: its setting; with none — the lead, and the lead itself the person."""
    set_ = effective(kind, p, config, "reports_to")
    if set_:
        return str(set_)
    lead = lead_of(config)
    return lead if lead and lead != p.get("id") else "person"


def whom(pid: str, config: dict[str, Any]) -> str:
    return "the person" if pid == "person" else label_of(config, pid)


def output_of(config: dict[str, Any]) -> str:
    """The task's result file, a path inside the project folder; "" — none, or one that does not pass."""
    out = team_of(config).get("output")
    if not isinstance(out, str) or not out or len(out) > OUTPUT_LIMIT or m_text.visible(out) != out or "\n" in out:
        return ""
    if out.startswith(("/", "~")) or ".." in Path(out).parts:
        return ""
    return out


GOAL_LIMIT = 200


def goal_of(config: dict[str, Any]) -> str:
    """The room's goal, one or two lines the lead answers for; "" — none, or one that does not pass."""
    goal = team_of(config).get("goal")
    if not isinstance(goal, str) or not goal.strip() or len(goal) > GOAL_LIMIT or m_text.visible(goal) != goal:
        return ""
    return goal


def round_of(config: dict[str, Any]) -> int | None:
    """Minutes between the lead's rounds (team.round); None — off. Not given — 30."""
    value = team_of(config).get("round", 30)
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 24 * 60 else None


def team_problems(config: dict[str, Any]) -> list[str]:
    """What in the room's team block does not pass: named, and left out as if not set."""
    team = team_of(config)
    out: list[str] = []
    if "goal" in team and not goal_of(config):
        out.append(f"the room's goal must be text of 1 to {GOAL_LIMIT} characters without control characters")
    if "round" in team and team["round"] is not False and round_of(config) is None:
        out.append("the lead's round must be minutes from 1 to 1440, or false")
    if team.get("output") and not output_of(config):
        out.append("the result file must be a path inside the project folder")
    return out


def without_person(config: dict[str, Any]) -> list[str]:
    allowed = team_of(config).get("without_person")
    if not isinstance(allowed, list):
        return []
    return [a for a in WITHOUT_PERSON if a in cast("list[object]", allowed)]


# The memory a seat is given: «compact» folds a long conversation into a summary, «clear» starts it anew once it has
# grown, «keep» leaves it to the agent's program. The thresholds are a first guess, to be set by what the ledger shows.
MEMORY: dict[str, object] = {
    "compact": {"at": 150000, "do": "compact"},
    "clear": {"at": 100000, "do": "clear"},
    "keep": False,
}


def _seat(seat: str, kind: str, label: str, role: str, topic: str, model: str, memory: str) -> dict[str, Any]:
    return {
        "seat": seat,
        "kind": kind,
        "label": label,
        "role": role,
        "topic": topic,
        **({"use_model": model} if model else {}),
        "context_trim": MEMORY[memory],
    }


# The ready teams a room starts from. A seat names the program, the agent's name, and its settings; whom it
# reports to is left to the defaults: the lead to the person, everyone else to the lead. Applying one copies it
# into the room's file (team.seats): a later change here does not reach a room already started.
SCENARIOS: dict[str, dict[str, Any]] = {
    "development": {
        "title": "Development",
        "hint": "Split the work, check the changes, get code",
        "output": "",
        "seats": [
            _seat("lead", "claude", "Lead", "lead", "coordination", "opus", "clear"),
            _seat("frontend", "claude", "Frontend", "worker", "frontend", "opus", "compact"),
            _seat("backend", "claude", "Backend", "worker", "backend", "opus", "compact"),
            _seat("review", "codex", "Reviewer", "reviewer", "tests and security", "", "keep"),
        ],
    },
    "research": {
        "title": "Research",
        "hint": "Gather facts, check them, get a conclusion with sources",
        "output": "research.md",
        "seats": [
            _seat("lead", "claude", "Lead", "lead", "summary", "opus", "compact"),
            _seat("prices", "claude", "Prices", "worker", "prices and terms", "sonnet", "clear"),
            _seat("risks", "claude", "Reviews", "worker", "reviews and risks", "sonnet", "clear"),
            _seat("facts", "claude", "Fact-checker", "reviewer", "fact checking", "opus", "clear"),
        ],
    },
    "text": {
        "title": "Text",
        "hint": "Write a text and have it checked",
        "output": "text.md",
        "seats": [
            _seat("lead", "claude", "Author", "lead", "author", "opus", "compact"),
            _seat("editor", "claude", "Editor", "reviewer", "editing and tone", "sonnet", "clear"),
        ],
    },
    "decision": {
        "title": "Decision",
        "hint": "Compare the options, see the arguments and where they disagree",
        "output": "decision.md",
        "seats": [
            _seat("lead", "claude", "Lead", "lead", "recommendation", "opus", "compact"),
            _seat("money", "claude", "Money", "worker", "money", "sonnet", "clear"),
            _seat("risks", "claude", "Risks", "worker", "risks", "sonnet", "clear"),
            _seat("customers", "claude", "Customers", "worker", "customers", "sonnet", "clear"),
            _seat("critic", "claude", "Critic", "reviewer", "devil's advocate", "opus", "clear"),
        ],
    },
    "long": {
        "title": "Long work",
        "hint": "Run a project and keep its state between your visits",
        "output": "project-state.md",
        "seats": [
            _seat("lead", "claude", "Lead", "lead", "running the project", "opus", "compact"),
            _seat("worker", "claude", "Worker", "worker", "the project's topic", "sonnet", "compact"),
        ],
    },
    "assistant": {
        "title": "One assistant",
        "hint": "Work with one agent directly",
        "output": "",
        "seats": [_seat("lead", "claude", "Assistant", "lead", "everything", "opus", "compact")],
    },
}


def scenario_problems(scenario: dict[str, Any], kinds: list[str]) -> list[str]:
    """What does not pass in a scenario: each seat's settings by the same checks as an agent's, one lead, ids
    and names that are words, a program the room knows."""
    out: list[str] = []
    seats = scenario.get("seats")
    if not isinstance(seats, list) or not seats:
        return ["a scenario needs seats"]
    ids: list[object] = []
    for seat in cast("list[object]", seats):
        if not isinstance(seat, dict):
            out.append("a seat must be an object")
            continue
        seat_ = cast("dict[str, Any]", seat)
        name = seat_.get("seat")
        if not isinstance(name, str) or not WORD.fullmatch(name) or name in ids:
            out.append(f"seat ids must be distinct words: {name!r}")
        ids.append(name)
        label = seat_.get("label")
        if not isinstance(label, str) or m_text.name_problem(label):
            out.append(f"{name}: a name that can't be used: {label!r}")
        if seat_.get("kind") not in kinds:
            out.append(f"{name}: kind must be one of {', '.join(kinds)}")
        for key in SETTINGS:
            if key in seat_ and key != "reports_to" and (why := setting_problem(key, seat_[key], {"participants": []})):
                out.append(f"{name}: {why}")
    if sum(1 for q in cast("list[Any]", seats) if isinstance(q, dict) and cast("dict[str, Any]", q).get("role") == "lead") != 1:
        out.append("a scenario has one lead")
    output = scenario.get("output", "")
    if output and not output_of({"team": {"output": output}}):
        out.append(f"output must be a path inside the project folder: {output!r}")
    return out


def memory_words(rule: object, lang: str = "en") -> str:
    if not trim_valid(rule):
        return m_i18n.words_in("memory.keep", lang)
    r = cast("dict[str, Any]", rule)
    return m_i18n.words_in(f"memory.{r['do']}", lang, k=r["at"] // 1000)


def seat_line(kind: str, p: dict[str, Any], config: dict[str, Any], lang: str = "") -> str:
    """One row of a team: name · role · topic · model · memory · reports to — what applies, by the layers, in the
    room's language."""
    lang = lang or room_language(config)
    role, topic, model = (effective(kind, p, config, k) for k in ("role", "topic", "use_model"))
    trim = effective(kind, p, config, "context_trim") if kind in TRIM_KINDS else None
    to = reports_to(kind, p, config)
    return " · ".join(
        [
            str(p.get("label") or p.get("id") or ""),
            m_i18n.words_in(f"role.{role}" if role else "role.none", lang),
            topic or "—",
            model or m_i18n.words_in("model.default", lang),
            memory_words(trim, lang),
            m_i18n.words_in(
                "reports.to", lang, whom=m_i18n.words_in("whom.person", lang) if to == "person" else label_of(config, to)
            ),
        ]
    )


def scenario_title(name: str, lang: str) -> str:
    return m_i18n.words_in(f"scenario.{name}.title", lang)


def scenario_hint(name: str, lang: str) -> str:
    return m_i18n.words_in(f"scenario.{name}.hint", lang)


def scenario_preview(name: str, lang: str = "en") -> list[str]:
    """What a scenario would start, before anything starts: its team as rows, the result file, the bounds."""
    scenario = SCENARIOS[name]
    seats = [
        {"id": seat["seat"], "label": seat["label"], "kind": seat["kind"], "seat": seat["seat"]} for seat in scenario["seats"]
    ]
    config: dict[str, Any] = {"participants": seats, "team": scenario}
    rows = [seat_line(p["kind"], p, config, lang) for p in seats]
    return [
        m_i18n.words_in("team.preview.head", lang, title=scenario_title(name, lang), hint=scenario_hint(name, lang)),
        *(f"  {row}" for row in rows),
        "  "
        + m_i18n.words_in("team.preview.result", lang, output=scenario["output"] or m_i18n.words_in("team.result.chat", lang)),
    ]


BRIEF_HEAD = "Your assignment in this room"


def offer_team_path() -> Path:
    """A room just created by start: its first chat opens on «What will we do?», once."""
    return room().paths.state_dir / "offer-team"


def compacted_path() -> Path:
    """When each Codex last compacted its context, by its log (usage.increment): its assignment goes again."""
    return room().paths.state_dir / "compacted.json"


def brief(pid: str, config: dict[str, Any], version: int = 0) -> str:
    """An agent's assignment, made from the room's settings alone — never from what was said before: who it is
    here, its topic, whom it reports to, the lead, the result file, what is not done without the person.
    "" when the room sets nothing for it: a room without settings works as before."""
    p = next((q for q in config.get("participants", []) if q.get("id") == pid), None)
    if p is None:
        return ""
    kind = p.get("kind", "")
    role, topic = effective(kind, p, config, "role"), effective(kind, p, config, "topic")
    if not role and not topic and not team_of(config) and not any(k in p for k in ("reports_to", "seat")):
        return ""
    lead, to = lead_of(config), reports_to(kind, p, config)
    allowed = without_person(config)
    lang = room_language(config)

    def t(key: str, **kw: Any) -> str:  # this assignment's language, once
        return m_i18n.words_in(key, lang, **kw)

    def named(who: str, case: str) -> str:
        return t(case) if who == "person" else label_of(config, who)

    lines = [t("brief.head") + (t("brief.version", v=version) if version else "") + ":"]
    lines.append(t("brief.role", role=t(f"role.{role}"), what=t(f"role.{role}.what")) if role else t("brief.role.none"))
    if topic:
        lines.append(t("brief.topic", topic=topic))
    lines.append(t("brief.report", whom=named(to, "whom.person.to")))
    if lead and lead != pid:
        lines.append(t("brief.lead", lead=label_of(config, lead)))
    elif lead == pid:
        lines.append(t("brief.you_lead"))
        # Live, a lead with three workers and a reviewer did a whole research alone: its team, by name.
        people = people_ids(config)
        team = [
            q
            for q in config.get("participants", [])
            if q.get("id") not in (pid, *people) and reports_to(q.get("kind", ""), q, config) == pid
        ]
        if team:
            parts: list[str] = []
            for q in team:
                r = effective(q.get("kind", ""), q, config, "role")
                topic_ = effective(q.get("kind", ""), q, config, "topic")
                parts.append(
                    f"{label_of(config, q['id'])} — {t(f'role.{r}') if r else t('agent.word')}"
                    + (f", {topic_}" if topic_ else "")
                )
            lines.append(t("brief.team", team="; ".join(parts)))
        if goal := goal_of(config):
            lines.append(t("brief.goal", goal=goal))
    if out := output_of(config):
        lines.append(t("brief.result", output=out))
    if allowed:
        lines.append(t("brief.without.some", what="; ".join(t(f"without.{a}") for a in allowed)))
    else:
        lines.append(t("brief.without.none"))
    lines.append(t("brief.language"))
    # Live, a reviewer told its topic and no task began reviewing the folder on its own.
    lines.append(t("brief.not_task", whom=named(to, "whom.person.from")))
    lines.append(t("brief.foot"))
    return "\n".join(lines)


def trim_valid(rule: object) -> bool:
    """A room's or an agent's `context_trim`: past `at` tokens of context, an idle agent is given /compact
    or /clear (deliver.trim_due)."""
    if not isinstance(rule, dict):
        return False
    at, do = cast("dict[str, object]", rule).get("at"), cast("dict[str, object]", rule).get("do")
    return isinstance(at, int) and not isinstance(at, bool) and at > 0 and do in ("compact", "clear")


def load_config() -> dict[str, Any]:
    rp = room().paths
    if not rp.config_path.is_file():
        fail(f"no {rp.config_path.name}; run `collab init` first")
    # Read from the checked file itself: a link put in its place is not followed.
    data = read_config_raw()

    # The settings live under ~/.collab-room/rooms/…, not in the project: an error that does not say
    # where sent the person looking for a .collab.json that is not there.
    def bad(message: str) -> NoReturn:
        rp = room().paths
        fail(f"{message} — in {m_text.tilde(str(rp.config_path))}; fix it there")

    if not isinstance(data, dict):  # pyright: ignore[reportUnnecessaryIsInstance] — a seam may hand anything
        bad("the room's settings must be a JSON object")
    # The tmux session's name goes into `new-session -s` and every `-t =name:…`: tmux expands «#{…}» there,
    # so «x#{pid}y» started as «x35334y» and the room never found itself; «:», «.», a leading «=» break
    # the target the same way. The room writes only plain names.
    if not isinstance(data.get("session", "collab-room"), str) or not WORD.fullmatch(data.get("session", "collab-room")):
        bad("session must be 1 to 64 letters, digits, _ and -")
    limit = data.get("talk_limit", 0)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        bad("talk_limit must be a whole number, 0 or more (0: the room never pauses for agents' talk)")
    gather = data.get("gather_seconds", 0)
    if not isinstance(gather, int) or isinstance(gather, bool) or gather < 0:
        bad("gather_seconds must be a whole number, 0 or more (0: agents' messages are typed at once)")
    trim_rule = 'context_trim must be {"at": tokens, "do": "compact" or "clear"}'
    if "context_trim" in data and data["context_trim"] is not False and not trim_valid(data["context_trim"]):
        bad(f"{trim_rule}, or false (never)")
    participants = data.get("participants")
    if not isinstance(participants, list) or not participants:
        bad("config must contain a non-empty participants list")
    ids: list[str] = []
    for p in cast("list[dict[str, Any]]", participants):
        if not isinstance(p, dict):  # pyright: ignore[reportUnnecessaryIsInstance] — JSON may hold anything
            bad("each participant must be an object")
        pid = p.get("id")
        if not isinstance(pid, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", pid or ""):
            bad("participant ids must be non-empty and use only letters, digits, _ and -")
        if pid in m_text.SPECIAL:
            bad(f"participant id {pid!r} is reserved")
        ids.append(pid)
        command = p.get("command", [])
        if not isinstance(command, list) or not all(isinstance(x, str) for x in cast("list[object]", command)):
            bad(f"command for {pid} must be an array of strings")
        if "target" in p and (not isinstance(p["target"], str) or not p["target"]):
            bad(f"{pid}: target must be a tmux target such as 'codex' or 'work:0.1'")
        if "context_trim" in p and p["context_trim"] is not False and not trim_valid(p["context_trim"]):
            bad(f"{pid}: {trim_rule}, or false (never)")
        if "pane" in p and not isinstance(p["pane"], bool):
            bad(f"{pid}: pane must be true or false")
        delivery = p.get("delivery", "pull")
        if delivery not in DELIVERY_MODES:
            bad(f"delivery for {pid} must be one of {sorted(DELIVERY_MODES)}")
        if delivery == "tmux":
            if p.get("pane") is False and "target" not in p:
                bad(f'{pid}: delivery \'tmux\' needs a pane: drop "pane": false or set "target"')
            idle = p.get("idle")
            if not isinstance(idle, str) or not idle:
                bad(f"{pid}: delivery 'tmux' needs an `idle` regex (what the screen shows when it waits for input)")
            for key in ("attention", "attention_title", "draft"):
                if key in p and (not isinstance(p[key], str) or not p[key]):
                    bad(f"{pid}: {key} must be a non-empty regex")
            if "busy" in p and (not isinstance(p["busy"], str) or not p["busy"]):
                bad(f"{pid}: busy must be a non-empty regex")
            # Each by its own key: any broken one was called «bad idle regex».
            for key in ("idle", "busy", "attention", "attention_title", "draft"):
                try:
                    if key in p:
                        re.compile(p[key], re.MULTILINE)
                except re.error as exc:
                    bad(f"{pid}: bad {key} regex: {exc}")
    if len(set(ids)) != len(ids):
        bad("participant ids must be unique")
    return data


def participant(config: dict[str, Any], pid: str) -> dict[str, Any]:
    for p in config["participants"]:
        if p["id"] == pid:
            return p
    fail(f"unknown participant {pid!r}; known: {', '.join(p['id'] for p in config['participants'])}")
    raise AssertionError


def ids_of(config: dict[str, Any]) -> list[str]:
    return [p["id"] for p in config["participants"]]


# ── state ──────────────────────────────────────────────────────────────────────


def private_dir(path: Path, strict: bool = True) -> None:
    """`path` and every level made for it, 0700. mkdir(mode=0o700, parents=True) gave the mode to
    the last level only: ~/.collab-room and its rooms/ were 0755, and every user of the Mac could
    list the names of the person's projects. Ours made before are closed too,
    and said once — but only those others could read: one others could write is left as it is, so
    recorded_python refuses it and install says «fix its permissions» — closing it quietly would hide
    that someone may have changed what is inside. Strict — the room's state folder itself: a link there is refused, the folder's mode is set.
    Not strict: only what is missing is made; a folder that is there is not ours to chmod."""
    inst = m_boot.paths()
    if path.is_symlink():
        if strict:
            # A link in place of the room's folder: chmod would follow it.
            fail(f"refusing: {path} is a link — the room's state must be its own folder")
    else:
        for level in reversed([p for p in (path, *path.parents) if not os.path.lexists(p)]):
            try:
                os.mkdir(level, 0o700)
            except FileExistsError:
                pass
        if strict:
            os.chmod(path, 0o700)
    for level in (path, *path.parents):
        if level not in inst.own_dirs:
            continue
        info = os.lstat(level)
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and info.st_mode & 0o077 and not info.st_mode & 0o022:
            os.chmod(level, 0o700)
            note = f"note: {m_text.tilde(str(level))} was open to other users; now 700"
            warn(note)


def read_state_text(path: Path, errors: str = "replace") -> str:
    """A state file's text, read from the very file that was checked: folders opened refusing links,
    the file with O_NOFOLLOW, then fstat — a regular file with one name. A check by path and a read
    by path left a gap: a log switched to a link in between was read, 8 times in 150.
    A link is an OSError(ELOOP); no file, FileNotFoundError."""
    fd, name = state_dir(path, strict=False, create=False)
    try:
        f = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    finally:
        os.close(fd)
    with os.fdopen(f, "rb") as fh:
        info = os.fstat(fh.fileno())
        # A second name is refused; no name at all (0) is a file just replaced by a writer — on
        # APFS it is caught so in os.replace, and it was taken for a link. What was read
        # is the whole old version.
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise OSError(errno.ELOOP, "a link, not the room's own file", str(path))
        return fh.read().decode("utf-8", errors=errors)


def state_dir(path: Path, strict: bool = True, create: bool = True) -> tuple[int, str]:
    """The folder of a state file, opened refusing links on every step, and the file's name. Not
    strict: a link on the way is an OSError for the caller, not the end of the program. Not
    create: for a read — no folder made, no mode set; a sandbox forbids both, and inbox failed in
    Codex's. A missing folder is then FileNotFoundError."""
    rp = room().paths
    parts = path.relative_to(rp.base).parts
    if rp.base != rp.root and create:
        private_dir(rp.base, strict=False)
    fd = os.open(str(rp.base), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in parts[:-1]:
            if create:
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            inner = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = inner
            if create:
                os.fchmod(fd, 0o700)
    except OSError as exc:
        os.close(fd)
        if not strict:
            raise
        fail(f"refusing to write {path}: {exc.strerror} (a link in the room's state?)")
    return fd, parts[-1]


# «thread»: re-entrant within a thread, not within the process — the chat's feed and its input are two
# threads, and the second one saw depth > 0 and went in beside the first.
_ROOM_LOCK: dict[str, Any] = {"depth": 0, "fd": None, "thread": threading.RLock()}


# A decorated function keeps its own signature for the type checker.
_Fn = TypeVar("_Fn", bound=Callable[..., Any])


def room_locked(fn: _Fn) -> _Fn:
    """Run fn holding the room's lock: changes of .collab.json, of the read and
    no-reply marks, of held messages and /clear were read-modify-write without one, and concurrent
    ones lost a participant, the pause, marks — or delivered an archived message again. Re-entrant
    within a process; the lock goes with the process, so a crash never leaves the room locked."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        rp = room().paths
        with _ROOM_LOCK["thread"]:
            if _ROOM_LOCK["depth"] == 0:
                import fcntl

                dir_fd, name = state_dir(rp.state_dir / "room.lock")
                try:
                    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
                except OSError as exc:
                    fail(f"refusing the room lock: {exc.strerror} (a link in the room's state?)")
                finally:
                    os.close(dir_fd)
                fcntl.flock(fd, fcntl.LOCK_EX)
                _ROOM_LOCK["fd"] = fd
            _ROOM_LOCK["depth"] += 1
            try:
                return fn(*args, **kwargs)
            finally:
                _ROOM_LOCK["depth"] -= 1
                if _ROOM_LOCK["depth"] == 0:
                    os.close(_ROOM_LOCK["fd"])  # closing releases the flock
                    _ROOM_LOCK["fd"] = None

    return cast(_Fn, wrapper)


def replace_state(path: Path, text: str) -> None:
    """Write a whole state file at once: into a new temporary file of
    its own name, then renamed over the old one. A reader sees the old or the new file, never an
    empty one; two writers never share a temporary file; a link put in place of the file is
    replaced, not followed."""
    fd, name = state_dir(path)
    tmp = f".{name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(out, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
    except OSError as exc:
        try:
            os.unlink(tmp, dir_fd=fd)
        except OSError:
            pass
        fail(f"cannot write {path}: {exc.strerror}")
    finally:
        os.close(fd)


def state_file(path: Path, append: bool) -> int:
    """Open a file of the room's state for writing without following links on the way: a
    Codex rule runs collab outside the sandbox, and a sandboxed Codex working in
    the room's folder could have put a link (or a hard link) to any file of the person's in
    .collab — and had collab write into it. Every folder from the room down is opened refusing
    links, the file too, and a file with a second name is refused before anything is written."""
    rp = room().paths
    parts = path.relative_to(rp.base).parts
    if rp.base != rp.root:
        private_dir(rp.base, strict=False)
    fd = os.open(str(rp.base), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in parts[:-1]:
            try:
                os.mkdir(name, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            inner = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = inner
            os.fchmod(fd, 0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_APPEND if append else 0)
        out = os.open(parts[-1], flags, 0o600, dir_fd=fd)
    except OSError as exc:
        fail(f"refusing to write {path}: {exc.strerror} (a link in the room's state?)")
    finally:
        os.close(fd)
    info = os.fstat(out)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(out)
        fail(f"refusing to write {path}: not a plain file of the room")
    os.fchmod(out, 0o600)
    if not append:
        os.ftruncate(out, 0)  # only now: truncating at open would have emptied a linked file
    return out


def write_state(path: Path, text: str, append: bool = False) -> None:
    if not append:
        replace_state(path, text)
        return
    fd = state_file(path, append)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def aliases(config: dict[str, Any]) -> dict[str, str]:
    """Every way a person may address someone: id, full name, name without spaces, first word of
    the name (when it is not ambiguous) — case-insensitive; plus «всем»/«все»/all for everyone."""
    out: dict[str, str] = {}
    firsts: dict[str, list[str]] = {}
    for p in config["participants"]:
        pid, label = p["id"], (p.get("label") or "").strip()
        out[pid.lower()] = pid
        if label:
            out[label.lower()] = pid
            out[label.lower().replace(" ", "")] = pid
            firsts.setdefault(label.split()[0].lower(), []).append(pid)
    for first, pids in firsts.items():
        if len(set(pids)) == 1:
            out.setdefault(first, pids[0])
    # An id always means its own participant: a name like another's id must not take it over —
    # the chat's menus pick by id.
    out.update(m_text.EVERYONE)
    out.update({p["id"].lower(): p["id"] for p in config["participants"]})
    return out


def address_candidates(config: dict[str, Any], me: str, prefix: str) -> list[str]:
    """What Tab offers after «@»: one handy name per participant (first word of the name, or the
    id), plus «@всем»; never yourself."""
    names: list[str] = []
    for p in config["participants"]:
        if p["id"] == me:
            continue
        label = (p.get("label") or "").strip()
        first = label.split()[0] if label else p["id"]
        names.append("@" + (first if aliases(config).get(first.lower()) == p["id"] else p["id"]))
    names.append("@everyone")
    return [n for n in names if n.lower().startswith(prefix.lower())]


def parse_addressing(line: str, known: list[str], names: dict[str, str] | None = None) -> tuple[list[str], str]:
    """`@codex @Claude text` → (["codex", "claude"], "text"); no @ → all. Names are case-insensitive."""
    names = names or {pid.lower(): pid for pid in known} | {"all": "all"}
    to: list[str] = []
    words = line.split(" ")
    while words and words[0].startswith("@") and len(words[0]) > 1:
        name = words.pop(0)[1:].rstrip(",:")
        pid = names.get(name.lower())
        if not pid:
            raise ValueError(f"unknown participant @{name}")
        if pid not in to:
            to.append(pid)
    # An «@» that names nobody is not «everyone»: «@ text» went to the whole room past the recipient
    # chosen with Tab.
    if line.startswith("@") and not to:
        raise ValueError("@ needs a name right after it: @Name text, or @everyone text")
    return (["all"] if not to or "all" in to else to), " ".join(words).strip()


PALETTE = ["1;37", "38;5;208", "38;5;71", "38;5;75", "38;5;177", "38;5;179", "38;5;44"]


def _label_of(config: dict[str, Any], pid: str) -> str:
    if pid == "all":
        return "Everyone"
    if pid == m_text.ROOM:
        return "Room"
    for p in config["participants"]:
        if p["id"] == pid:
            return p.get("label") or pid
    return pid


def label_of(config: dict[str, Any], pid: str) -> str:
    return m_text.visible(_label_of(config, pid)).replace("\n", " ")


def color_of(config: dict[str, Any], pid: str) -> str:
    ids = ids_of(config)
    return PALETTE[ids.index(pid) % len(PALETTE)] if pid in ids else "0"


def is_chat_command(cmd: Any) -> bool:
    """The person's chat, recognised by its whole shape — [env VAR=… …] [python…] …/collab chat …
    — not by a word «chat» somewhere in it: «sh -c payload chat» passed as the chat and was neither
    asked about nor kept from being a «person»."""
    if not isinstance(cmd, list) or not all(isinstance(x, str) for x in cast("list[object]", cmd)):
        return False
    rest = list(cast("list[str]", cmd))
    if rest and Path(rest[0]).name == "env":
        rest = rest[1:]
        while rest and "=" in rest[0] and not rest[0].startswith("-"):
            rest = rest[1:]
    if rest and re.fullmatch(r"python[0-9.]*", Path(rest[0]).name):
        rest = rest[1:]
    return len(rest) >= 2 and re.fullmatch(r"collab(-[0-9a-f]{16})?", Path(rest[0]).name) is not None and rest[1] == "chat"


def trusted_chat_command(cmd: Any) -> bool:
    """The chat as this program writes it: [python3 or this Python] this collab (or its safe copy)
    chat … — nothing else is started unasked. is_chat_command() knows a chat by its shape only, and
    «…/proj/evil/collab chat» an agent wrote into a new project's .collab.json was started by the
    first `collab start` without a question."""
    inst = m_boot.paths()
    if not is_chat_command(cmd) or Path(cmd[0]).name == "env":
        return False
    program: str = cmd[0]
    if re.fullmatch(r"python[0-9.]*", Path(cmd[0]).name):
        if "/" in cmd[0] and os.path.realpath(cmd[0]) != os.path.realpath(sys.executable):
            return False
        program = cmd[1]
    if "/" not in program:
        program = shutil.which(program) or program
    real = Path(os.path.realpath(program))
    # This collab, the Codex rule's copy, or the person's installed copy: all where no agent writes.
    return real == Path(m_boot.SELF).resolve() or inst.safe_copies.resolve() in real.parents


def current_chat_command(cmd: list[str]) -> list[str]:
    """The person's chat as it is started: by this collab, not the copy named when the room was made.
    The room's file keeps the path of the copy `init` ran from, and after an install or update the
    delivery ran the new copy while the chat and its /dm stayed on the old one. Only a chat
    trusted_chat_command() lets start unasked is changed; anything else is started as written."""
    if not trusted_chat_command(cmd):
        return list(cmd)
    at = 1 if re.fullmatch(r"python[0-9.]*", Path(cmd[0]).name) else 0
    return [*cmd[:at], m_boot.SELF, *cmd[at + 1 :]]


def write_room(sample: dict[str, Any], ask: bool) -> None:
    rp = room().paths
    if ask:
        for p in sample["participants"]:
            p.pop("name_from_account", None)  # shown with the question and kept or changed: chosen
    if ask and len(sample["participants"]) == 1:
        p = sample["participants"][0]
        try:
            answer = m_term.ask_line(f"Your name [{p['label']}]: ").strip()
        except EOFError:
            answer = ""
        if answer:
            p["label"] = answer
    elif ask:
        print("Name the participants (Enter keeps the current name).")
        for p in sample["participants"]:
            try:
                answer = m_term.ask_line(f"  {p['id']} [{p['label']}]: ").strip()
            except EOFError:
                answer = ""
            if answer:
                p["label"] = answer
    if rp.base != rp.root:
        sample["root"] = os.path.realpath(str(rp.root))  # see record_room_root
    write_config(sample)  # in one step: a failed write once left an empty .collab.json
    private_dir(rp.state_dir)
    # The project's .gitignore is left alone: the room's files live outside the
    # project, and a new room wrote «.collab/» into the person's repository for nothing.


@room_locked
def set_room_language(lang: str) -> None:
    """The room's own language: its menus, help and the room's words to its agents — briefs go again with it."""
    data = read_config_raw()
    data["language"] = m_i18n.lang_of(lang)
    write_config(data)


@room_locked
def set_team(team: dict[str, Any]) -> None:
    """The room's `team` block, replaced whole under the room's lock."""
    data = read_config_raw()
    data["team"] = team
    write_config(data)
    note_settings(data)


# What the person changes in a running team (/team), and what the lead changes for them later: each a record
# of the room's history, «who · what · before → after», under the room's lock. «None» after: the explicit value
# goes, and the layer below applies again (its seat's, the room's, the built-in one).
ROOM_KEYS = ("lead", "goal", "round", "output", "without_person")


def changes_path() -> Path:
    return room().paths.state_dir / "team-changes.jsonl"


def explicit_value(data: dict[str, Any], who: str, key: str) -> Any:
    """What is set explicitly — the agent's own or the room's — or None."""
    if who == "room":
        return team_of(data).get(key)
    p = next((q for q in data.get("participants", []) if q.get("id") == who), None)
    return None if p is None else p.get(key)


def change_problem(data: dict[str, Any], who: str, key: str, after: object) -> str:
    """Why this change can't be made, or ""; None after (back to the layer below) is always one."""
    if who == "room":
        if key not in ROOM_KEYS:
            return f"{key} is not a setting of the room"
        if after is None:
            return ""
        if key == "lead":
            people = people_ids(data)
            return "" if after in ids_of(data) and after not in people else "the lead must be one of the room's agents"
        probe = {"team": {key: after}, "participants": data.get("participants", [])}
        if key == "without_person":
            ok = isinstance(after, list) and all(a in WITHOUT_PERSON for a in cast("list[object]", after))
            return "" if ok else f"without_person takes only {', '.join(WITHOUT_PERSON)}"
        return next(iter(team_problems(probe)), "")
    if who not in ids_of(data) or who in people_ids(data):
        return f"{who} is not one of the room's agents"
    if key == "seat":
        seats = [q.get("seat") for q in as_list(team_of(data).get("seats"))]
        return "" if after is None or after in seats else "seat must be one of the room's scenario seats"
    if key not in SETTINGS:
        return f"{key} is not a setting"
    return "" if after is None else setting_problem(key, after, data, who)


def read_changes() -> list[dict[str, Any]]:
    """The room's history of team changes, oldest first; a line that is not one is skipped."""
    out: list[dict[str, Any]] = []
    try:
        text = read_state_text(changes_path(), "strict")
    except (OSError, ValueError):
        return out
    for line in text.splitlines():
        with contextlib.suppress(ValueError):
            rec = json.loads(line)
            if isinstance(rec, dict) and change_record_ok(cast("dict[str, Any]", rec)):
                out.append(cast("dict[str, Any]", rec))
    return out


def change_record_ok(rec: dict[str, Any]) -> bool:
    """A record the program could have written: an id that is a word, changes of known keys of a word id or the
    room. The file is in the room's home, and a same-user agent can write it: anything else is not shown or undone."""
    changes: object = rec.get("changes")
    if not isinstance(rec.get("id"), str) or not WORD.fullmatch(rec["id"]) or not isinstance(changes, list):
        return False
    for c in cast("list[object]", changes):
        if not isinstance(c, dict):
            return False
        c_ = cast("dict[str, Any]", c)
        who, key = c_.get("who"), c_.get("key")
        if not isinstance(who, str) or not WORD.fullmatch(who):
            return False
        if key not in (ROOM_KEYS if who == "room" else (*SETTINGS, "seat")):
            return False
    return True


@room_locked
def apply_changes(changes: list[dict[str, Any]], by: str, re_: str = "", undo_of: str = "") -> tuple[dict[str, Any], list[str]]:
    """Make these changes — [{"who": an agent's id or "room", "key", "after", and "expect" for an undo: the value
    it must still have}] — in one write, and keep them as one record of the history. A change that does not pass
    is refused whole, before anything is written; an undo's field changed since is left as it is and named.
    (the record, the fields left)."""
    data = read_config_raw()
    for c in changes:
        why = change_problem(data, c["who"], c["key"], c.get("after"))
        if why:
            raise ValueError(why)
    done: list[dict[str, Any]] = []
    left: list[str] = []
    for c in changes:
        who, key, after = c["who"], c["key"], c.get("after")
        before = explicit_value(data, who, key)
        if "expect" in c and before != c["expect"]:
            left.append(f"{label_of(data, who) if who != 'room' else 'the room'} — {key}: changed since")
            continue
        if who == "room":
            team = dict(team_of(data))
            if after is None:
                team.pop(key, None)
            else:
                team[key] = after
            data["team"] = team
        else:
            p = next(q for q in data["participants"] if q.get("id") == who)
            if after is None:
                p.pop(key, None)
            else:
                p[key] = after
        done.append({"who": who, "key": key, "before": before, "after": after})
    record: dict[str, Any] = {"id": uuid.uuid4().hex[:12], "at": now(), "by": by, "changes": done}
    record.update({"re": re_} if re_ else {})
    record.update({"undo_of": undo_of} if undo_of else {})
    if done:
        write_config(data)
        private_dir(room().paths.state_dir)
        note_settings(data)
        write_state(changes_path(), json.dumps(record, ensure_ascii=False) + "\n", append=True)
    return record, left


def undo_changes(record_id: str, by: str) -> tuple[dict[str, Any], list[str]]:
    """The reverse of one record of the history: each field back to what it was, if it still holds what that
    record made it — a field changed since is left, and named."""
    rec = next((r for r in read_changes() if r.get("id") == record_id), None)
    if rec is None:
        raise ValueError("no such change in this room's history")
    back = [
        {"who": c["who"], "key": c["key"], "after": c.get("before"), "expect": c.get("after")} for c in reversed(rec["changes"])
    ]
    data = read_config_raw()
    if any(c["key"] in ("lead", "role") for c in back) and any(
        explicit_value(data, c["who"], c["key"]) != c["expect"] for c in back
    ):
        # Half a lead change back would leave two leads, or none.
        raise ValueError("the lead changed again since: undo the later change first")
    return apply_changes(back, by, undo_of=record_id)


def change_words(data: dict[str, Any], rec: dict[str, Any]) -> str:
    """«Ann — model sonnet → opus; the room — goal none → …»: what one record changed, for the person."""

    def shown(v: object) -> str:
        if v is None:
            return "the default"
        if isinstance(v, list):
            return ", ".join(str(x) for x in cast("list[object]", v)) or "nothing"
        return memory_words(cast("object", v)) if isinstance(v, dict) or v is False else str(v)

    parts: list[str] = []
    for c in rec.get("changes", []):
        who = "the room" if c.get("who") == "room" else label_of(data, str(c.get("who")))
        key = {"use_model": "model", "context_trim": "memory", "reports_to": "reports to"}.get(
            str(c.get("key")), str(c.get("key"))
        )
        named = c.get("key") in ("lead", "reports_to")  # an id: shown by its name
        before, after = (
            m_text.clean_field(whom(str(v), data) if named and v is not None else shown(v), GOAL_LIMIT)
            for v in (c.get("before"), c.get("after"))
        )
        parts.append(f"{who} — {key} {before} → {after}")
    return m_text.visible("; ".join(parts))


# What the lead — or a command line — asks to change in the team: a message with a `proposal`, which changes nothing
# itself. The person applies it with one Enter in the chat; a narrowing one, asked for by the person in a signed
# message to the lead, is applied at once and can be undone. A model's price, by name, within one kind of program:
# a model not in the list, or of another kind, is not «cheaper».
MODEL_ORDER = {"claude": ["haiku", "sonnet", "opus"]}
# One proposal is read whole on its card before Enter: so many changes, so many agents, and words that fit.
PROPOSAL_CHANGES, PROPOSAL_ADDS, PROPOSAL_WORDS = 8, 4, 400


def proposals_path() -> Path:
    return room().paths.state_dir / "proposals.json"


def narrows(data: dict[str, Any], c: dict[str, Any]) -> bool:
    """Does this change only lower spending or rights — so the lead may make it at the person's word: whom an agent
    reports to (not the person), compacting its memory, a cheaper model of the same program, rarer rounds. Not a
    topic: free text that goes into another agent's assignment is the person's to read first."""
    who, key, after = c["who"], c["key"], c.get("after")
    if who == "room":
        now_ = round_of(data)
        return key == "round" and (after is False or (isinstance(after, int) and now_ is not None and after > now_))
    p = next((q for q in data.get("participants", []) if q.get("id") == who), None)
    if p is None:
        return False
    kind = str(p.get("kind") or "")
    if key == "context_trim":
        # Only compacting, and not too soon: «clear» wipes the conversation over and over, «keep» spends more.
        rule = cast("dict[str, Any]", after) if isinstance(after, dict) else {}
        return rule.get("do") == "compact" and isinstance(rule.get("at"), int) and rule["at"] >= 50000
    if key == "reports_to":
        return after not in (None, "person")
    if key == "use_model":
        order = MODEL_ORDER.get(kind, [])
        cur = effective(kind, p, data, "use_model")
        return after in order and cur in order and order.index(after) < order.index(cur)
    return False


def as_list(x: object) -> list[dict[str, Any]]:
    """A list of objects from a record anyone could have written; anything else — none of it."""
    if not isinstance(x, list):
        return []
    return [cast("dict[str, Any]", v) for v in cast("list[object]", x) if isinstance(v, dict)]


def proposal_problem(data: dict[str, Any], prop: dict[str, Any], kinds: list[str]) -> str:
    """Why a proposal can't be made, or "": each change by the checks of /team, each agent to add by those of
    collab add, a team only to a room without one."""
    raw_changes: object = prop.get("changes") or []
    raw_adds: object = prop.get("add") or []
    team: object = prop.get("team")
    changes, adds = as_list(raw_changes), as_list(raw_adds)
    whole = isinstance(raw_changes, list) and isinstance(raw_adds, list)
    if not whole or len(changes) != len(cast("list[object]", raw_changes)) or len(adds) != len(cast("list[object]", raw_adds)):
        return "a proposal needs a change, an agent to add or a team to start"
    if not (changes or adds or team):
        return "a proposal needs a change, an agent to add or a team to start"
    for c in changes:
        who, key = c.get("who"), c.get("key")
        if not isinstance(who, str) or not isinstance(key, str):
            return "a change is who, key and the value after"
        if why := change_problem(data, who, key, c.get("after")):
            return why
    for a in adds:
        if a.get("kind") not in kinds:
            return f"an agent to add needs its program: {', '.join(kinds)}"
        label: object = a.get("label")
        if not isinstance(label, str) or m_text.name_problem(label) or label_taken(data, "", label):
            return f"a name that can't be used: {label!r}"
        for key in SETTINGS:
            if key in a and (why := setting_problem(key, a[key], data)):
                return why
    if team is not None and (not isinstance(team, str) or team not in SCENARIOS or team_of(data).get("scenario")):
        return "a team only from the ready ones, and only to a room without one"
    if len(changes) > PROPOSAL_CHANGES or len(adds) > PROPOSAL_ADDS:
        return f"one proposal holds up to {PROPOSAL_CHANGES} changes and {PROPOSAL_ADDS} agents: split it"
    if len(proposal_words(data, prop, whole=True)) > PROPOSAL_WORDS:
        return "too long to be read whole on its card: split it"
    return ""


def proposal_words(data: dict[str, Any], prop: dict[str, Any], whole: bool = False) -> str:
    """What a proposal would do, for the person: the changes, before → after, and what it starts."""
    parts: list[str] = []
    changes = as_list(prop.get("changes"))
    if changes:
        before = [{**c, "before": explicit_value(data, str(c.get("who")), str(c.get("key")))} for c in changes]
        parts.append(change_words(data, {"changes": before}))
    for a in as_list(prop.get("add")):
        what = [str(a[k]) for k in ("role", "topic", "use_model") if a.get(k)]
        what += [f"memory {memory_words(a['context_trim'])}"] if "context_trim" in a else []
        what += [f"reports to {whom(str(a['reports_to']), data)}"] if a.get("reports_to") else []
        what += [f"in {m_text.tilde(str(a['folder']))}"] if a.get("folder") else []
        parts.append(f"+ {a.get('label')} ({a.get('kind')}{', ' + ' · '.join(what) if what else ''})")
    team = prop.get("team")
    if isinstance(team, str) and team in SCENARIOS:
        parts.append(f"start the {SCENARIOS[team]['title']} team ({len(SCENARIOS[team]['seats'])} agents)")
    words = "; ".join(parts)
    # Never cut: a proposal longer than its card is refused (proposal_problem), not shown in part.
    return words if whole else m_text.clean_field(words, PROPOSAL_WORDS)


def proposal_state() -> dict[str, Any]:
    return read_json(proposals_path())


@room_locked
def set_proposal_state(mid: str, state: str) -> None:
    write_json(proposals_path(), {**read_json(proposals_path()), mid: state})


def pending_proposals(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    states = proposal_state()
    return [m for m in messages if isinstance(m.get("proposal"), dict) and states.get(m["id"]) == "pending"]


def proposal_digest(prop: object) -> str:
    return hashlib.sha256(json.dumps(prop, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def proposal_card(m: dict[str, Any], data: dict[str, Any], request: str = "") -> dict[str, Any]:
    """The chat's question for one proposal: what it does, at whose word, Apply or Decline — one Enter. Bound to what
    was shown: an edit of the log after it is not applied."""
    who = "A command line" if m.get("from") == m_text.ROOM else label_of(data, str(m.get("from")))
    asked = f" (your request: «{m_text.clean_field(request, 120)}»)" if request else ""
    return {
        "step": "proposal",
        "proposal": m["id"],
        "digest": proposal_digest(m.get("proposal")),
        "prompt": f"{who} proposes{asked} — apply?",
        "options": [("Apply", "go"), ("Decline", "decline"), ("Later — /team shows it", "later")],
    }


@room_locked
def record_room_root() -> None:
    """The room's project, written into its .collab.json in the room's home: the home is named only
    by a hash of it, and the check of the copies could not tell where a room's Codex works when the
    room was missing from rooms.json. The home lies outside any Codex
    sandbox, so what is written there may be trusted — never a tmux session's mark, which anyone
    with the tmux server can set. A room not moved yet keeps its file in the project: none there.
    A file without «root» (made before) reads as ever; start writes it the next time."""
    rp = room().paths
    if rp.base == rp.root or not rp.config_path.is_file():
        return
    data = read_config_raw()
    real = os.path.realpath(str(rp.root))
    if isinstance(data, dict) and data.get("root") != real:  # pyright: ignore[reportUnnecessaryIsInstance] — a seam may hand anything
        data["root"] = real
        write_config(data)


@room_locked
def name_asked(pid: str) -> bool:
    """True once for a person whose name came from the computer's account, never chosen: the chat asks
    it then. The mark goes at once — asked one time, kept or not."""
    data = read_config_raw()
    p = next((q for q in data["participants"] if q["id"] == pid), None)
    if p is None or not p.pop("name_from_account", None):
        return False
    write_config(data)
    return True


@room_locked
def rename(pid: str, label: str) -> None:
    """Change a participant's name in .collab.json, keeping everything else as it is."""
    data = read_config_raw()
    for p in data["participants"]:
        if p["id"] == pid:
            p["label"] = label
            p.pop("name_from_account", None)
    write_config(data)


_SHARED_LOCK: dict[str, Any] = {"depth": 0, "fd": None, "thread": threading.RLock()}


def shared_locked(fn: _Fn) -> _Fn:
    """Run fn holding the lock of the person's shared files; re-entrant within a process."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        inst = m_boot.paths()
        with _SHARED_LOCK["thread"]:  # re-entrant within a thread, as the room's
            if _SHARED_LOCK["depth"] == 0:
                import fcntl

                private_dir(inst.shared_lock.parent, strict=False)
                try:
                    fd = os.open(str(inst.shared_lock), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                except OSError as exc:
                    fail(f"refusing the lock {m_text.tilde(str(inst.shared_lock))}: {exc.strerror} (a link there?)")
                fcntl.flock(fd, fcntl.LOCK_EX)
                _SHARED_LOCK["fd"] = fd
            _SHARED_LOCK["depth"] += 1
            try:
                return fn(*args, **kwargs)
            finally:
                _SHARED_LOCK["depth"] -= 1
                if _SHARED_LOCK["depth"] == 0:
                    os.close(_SHARED_LOCK["fd"])  # closing releases the flock
                    _SHARED_LOCK["fd"] = None

    return cast(_Fn, wrapper)


def replace_file(path: Path, data: bytes, mode: int) -> None:
    """Write `path` whole: a new file of a name nobody can guess, never through a link, then renamed.
    A name with the pid in it, written by path, followed a link put there first."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def write_private(path: Path, text: str) -> None:
    """A small file of ours in ~/.collab-room (as PYTHON_FILE)."""
    private_dir(path.parent, strict=False)
    replace_file(path, (text + "\n").encode("utf-8"), 0o600)


def write_config(data: dict[str, Any]) -> None:
    """Replace .collab.json in one step: chat, deliver and join read it concurrently, and a
    half-written file read as empty JSON crashed a reader (found by a flaky test)."""
    rp = room().paths
    # Through the link-refusing writer too: a link at the temporary name was followed.
    replace_state(rp.config_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def note_settings(data: dict[str, Any]) -> None:
    """What the room's own setting writers — the chat's changes, a team started, an agent added with its settings
    — wrote: a setting the file holds and this does not was changed past them. Only they note it: any other
    write of the file (a session bound, a name) would take in a change made past them unseen."""
    if room().paths.state_dir.is_dir():
        replace_state(settings_known_path(), json.dumps(settings_of(data), ensure_ascii=False) + "\n")


def settings_known_path() -> Path:
    return room().paths.state_dir / "settings-known.json"


def settings_of(data: dict[str, Any]) -> dict[str, Any]:
    """The explicit settings in a room's file: the room's team block and each participant's own, by id."""
    out: dict[str, Any] = {"room": {k: v for k, v in team_of(data).items() if k in ROOM_KEYS}}
    for q in cast("list[object]", data.get("participants", [])):
        p = cast("dict[str, Any]", q) if isinstance(q, dict) else {}
        if isinstance(p.get("id"), str):
            out[p["id"]] = {k: p[k] for k in (*SETTINGS, "seat") if k in p}
    return out


def changes_outside(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The settings changed past the program — by hand, or by an agent writing the room's file: each who · key ·
    before → after, against what the program last wrote. Nothing known yet — nothing (a room before this)."""
    known = read_json(settings_known_path())
    if not known:
        return []
    now_ = settings_of(data)
    out: list[dict[str, Any]] = []
    for who, keys in now_.items():
        if who not in known or not isinstance(known[who], dict):
            continue
        before = cast("dict[str, Any]", known[who])
        for key in sorted(set(before) | set(cast("dict[str, Any]", keys))):
            if before.get(key) != keys.get(key):
                out.append({"who": who, "key": key, "before": before.get(key), "after": keys.get(key)})
    return out


@room_locked
def record_outside() -> dict[str, Any] | None:
    """A change past the program goes into the history as made «outside» — it applies, and /team can undo it. The
    room's file read under the lock: the chat's own change, written meanwhile, is not taken for one. Nothing noted
    yet — what is there now is the start."""
    data = read_config_raw()
    known = read_json(settings_known_path())
    if not known:
        note_settings(data)
        return None
    changes = changes_outside(data)
    if not changes:
        if settings_of(data).keys() != known.keys():
            note_settings(data)  # agents joined or left, nothing changed: the start moves on with them
        return None
    record: dict[str, Any] = {"id": uuid.uuid4().hex[:12], "at": now(), "by": "outside", "changes": changes}
    write_state(changes_path(), json.dumps(record, ensure_ascii=False) + "\n", append=True)
    note_settings(data)
    return record


def label_taken(config: dict[str, Any], pid: str, label: str) -> str:
    """Another participant already called so (an agent renamed itself
    after the person and its words looked like the person's)."""
    return next(
        (
            q["id"]
            for q in config["participants"]
            if q["id"] != pid
            and m_text.name_skeleton(label) in (m_text.name_skeleton(q.get("label")), m_text.name_skeleton(q["id"]))
        ),
        "",
    )


def read_json(path: Path) -> dict[str, Any]:
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # ValueError: bad JSON and bad UTF-8 alike
        return {}
    return cast("dict[str, Any]", data) if isinstance(data, dict) else {}


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Whole file at once, through a temporary file of its own. The room's state goes
    the link-refusing way; a file of the person's own (the room list) is written in place."""
    rp = room().paths
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if os.path.commonpath([os.path.abspath(path), os.path.abspath(rp.base)]) == os.path.abspath(rp.base):
        replace_state(path, text)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    out = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(out, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def config_now() -> dict[str, Any]:
    try:
        return load_config()
    except SystemExit:
        return {"participants": []}


def people_ids(config: dict[str, Any]) -> list[str]:
    """The people in the room: those whose seat is a chat, not an agent."""
    return [p["id"] for p in config.get("participants", []) if p["id"] == "owner" or is_chat_command(p.get("command"))]


WORD = re.compile(r"[A-Za-z0-9_-]{1,64}")


def well_formed(m: dict[str, Any]) -> bool:
    """from, every «to» and the id are plain words: they are typed into agents' windows and into
    commands the agent runs."""
    to = m.get("to")
    return (
        isinstance(m.get("id"), str)
        and WORD.fullmatch(m["id"]) is not None
        and isinstance(m.get("from"), str)
        and WORD.fullmatch(m["from"]) is not None
        and isinstance(to, list)
        and bool(cast("list[object]", to))
        and all(isinstance(t, str) and WORD.fullmatch(t) for t in cast("list[object]", to))
        and isinstance(m.get("text"), str)
    )


def use_room(path: str) -> None:
    """Work in the room at `path`: its whole set of paths at once."""
    room().paths = RoomPaths.at(Path(path).expanduser().resolve())
