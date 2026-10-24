"""The chat's feed thread: what comes in, the states line, the folds into /dm, the people."""

from __future__ import annotations
import contextlib
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import collab.text as m_text
import collab.i18n as m_i18n
import collab.store as m_store
import collab.tmuxio as m_tmuxio
import collab.log as m_log
import collab.screen as m_screen
import collab.installer as m_installer


BAR_FILL = {
    "Ready": 40,
    "Working": 220,
    "Muted": 208,
    "Out of limit": 208,
    "Needs your input": 196,
    "Unsent draft": 196,
    "Disconnected": 196,
}  # anything else: light grey 250


BAR_MARKS = {
    "Needs your input": "!",
    "Unsent draft": "!",
    "Disconnected": "!",
    "Muted": "×",  # Ready with background tasks: the spinner and how many
    "Out of limit": "◷",
    "No update": "?",
    "Unknown": "?",
    "Connecting": "…",
}


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@dataclass(frozen=True)
class ChatFeedContext:
    """The chat's own objects and callbacks the feed thread works with — the same objects, never
    copies; wizard and targets are read through a getter, made after the context."""

    STALE_CHAT: Any
    args: Any
    blocks: Any
    config: Any
    count_mine: Any
    dm_focus: Any
    dm_tabs: Any
    dm_talk: Any
    dm_window_alive: Any
    draw_bottom: Any
    draw_history: Any
    draw_view: Any
    feed: Any
    feed_frame: Any
    feed_log: Any
    fold_into_dm: Any
    folded: Any
    held_alerts: Any
    known: Any
    lock: Any
    me: Any
    names: Any
    old_versions: Any
    printed: Any
    refresh_people: Any
    repaint: Any
    rows: Any
    show: Any
    shown_state: Any
    stale_chat: Any
    states: Any
    status_bar_rows: Any
    stop: Any
    targets: Any
    told_old: Any
    tty: Any
    ui: Any
    wizard: Any


def chat_append(ctx: ChatFeedContext, text: str, remember: bool = True) -> None:
    """Append text to the scrolling feed, above the fixed area."""
    if not ctx.tty:
        print(text, flush=True)
        return
    if remember:
        ctx.feed_log.extend(text.split("\n"))
        ctx.blocks.append(text.split("\n"))
        if ctx.ui.top is None:
            del ctx.feed_log[:-5000]
            del ctx.blocks[:-1000]
    if ctx.ui.top is not None:
        if remember:
            ctx.ui.unseen += 1  # new ones wait at the bottom; the person is not pulled out of history
            ctx.draw_history()
        return
    if ctx.ui.viewing or ctx.ui.reading:
        return  # the agent's screen or a message from /mine is on show; the feed catches up (repaint)
    h, w = ctx.rows(), shutil.get_terminal_size((100, 24)).columns
    sys.stdout.write(ctx.feed_frame(text, h, w))
    sys.stdout.flush()


def chat_old_versions(ctx: ChatFeedContext) -> list[str]:
    """Said once each: this chat runs an older collab — what it starts would get
    yesterday's rules; a Codex runs an older copy — a restart gives it the new one."""
    lines = []
    if ctx.stale_chat() and "chat" not in ctx.told_old:
        ctx.told_old.add("chat")
        lines.append(ctx.STALE_CHAT)
    with contextlib.suppress(OSError):
        now_name = m_installer.current_name()
        for q in ctx.config.get("participants", []):
            cli = str(q.get("cli") or "")
            if q.get("kind") == "codex" and cli and Path(cli).name != now_name and (q["id"], cli) not in ctx.told_old:
                ctx.told_old.add((q["id"], cli))
                who = m_store.label_of(ctx.config, q["id"])
                lines.append(m_i18n.tr("· {who} runs an older collab — /restart {who_} when convenient.", who=who, who_=who))
    return lines


def chat_states(ctx: ChatFeedContext) -> list[str]:
    """Calm states to the bar; what needs the person (a question, a lost window, unsent text)
    also goes into the feed, once."""
    out, parts, cells = [], [], []
    out += [m_text.paint(x, "38;5;214") for x in ctx.old_versions()]
    st = m_screen.read_status()
    for pid in ctx.known:
        if pid == ctx.args.as_ or pid not in st:
            continue
        line = m_text.visible(m_screen.describe(pid, st[pid], ctx.args.stale))  # English: what the chat reads
        shown_line = m_text.visible(m_screen.describe(pid, st[pid], ctx.args.stale, m_store.room_language(ctx.config)))
        state = st[pid].get("state", "")
        # The bottom line speaks English; messages in the feed stay as they are.
        # An unknown state from status.json is shown cleaned: it may come from an agent.
        short = m_screen.BAR_WORDS.get(state) or m_text.clean_field(str(state), 20)
        if any(q["id"] == pid and q.get("muted") for q in ctx.config["participants"]):
            short = "Muted"
        if "No update for" in line:
            short = "No update"
        # Every state a filled badge, black bold letters — green Ready, yellow Working,
        # orange Muted (and out of limit), red: act in the agent's window; light grey: no data.
        # Working's mark turns — the eye catches motion; the others carry a sign, not only colour.
        mark = SPINNER[int(time.time() * 4) % len(SPINNER)] if short == "Working" else BAR_MARKS.get(short, " ")
        since = m_text.age(st[pid].get("since", "")) if short == "Working" else ""
        # Cleaned again on show: status.json written before the fix may hold codes already.
        # Out of limit, the cell shows when it is back instead of the model.
        right = st[pid].get("detail", "") if state == "limited" else st[pid].get("model", "")
        bg = re.fullmatch(r"background (\d+)", str(st[pid].get("detail", "")))
        if bg and short == "Ready":
            # Ready for messages, its own commands run in the background: green, and
            # how many by the mark; the model stays where it is («it hid the model»). The
            # spinner too — work goes on — and the clock does not stop: it counts from the earliest
            # background command still running (bg_since), not from when it became Ready.
            mark = SPINNER[int(time.time() * 4) % len(SPINNER)] + " " + bg.group(1)
            since = m_text.age(st[pid].get("bg_since") or st[pid].get("since", ""))
        cells.append(
            (m_store.label_of(ctx.config, pid), m_text.clean_field(right, 40), short, mark, since, BAR_FILL.get(short, 250))
        )
        # «No update for 40 min», «41 min»…: the same news, not a new line in the feed every minute.
        news = re.sub(r"No update for \d+ min", "No update", line)
        if ctx.shown_state.get(pid) != news:
            ctx.shown_state[pid] = news
            if state in ("needs_owner", "lost", "draft"):
                out.append(m_text.paint(shown_line.replace(pid, m_store.label_of(ctx.config, pid), 1), "1;38;5;196"))
    if ctx.stale_chat():
        # Kept in the bar while it lasts: said once in the feed, it scrolled away, and the person took a chat that
        # lacked /team and /telegram for the room's own.
        parts.insert(0, m_text.paint(m_i18n.tr("Old chat — reopen: /exit, then collab"), "1;38;5;214"))
    if m_store.load_config().get("paused"):
        n = m_log.held_count()
        parts.insert(0, m_text.paint(m_i18n.tr("Paused") + (m_i18n.tr(" · {n} waiting", n=n) if n else ""), "1;38;5;214"))
    rows_ = ctx.status_bar_rows(cells, parts, shutil.get_terminal_size((100, 24)))
    ctx.ui.extra_rows = len(rows_) - 1
    ctx.ui.bar = "\n".join(rows_)
    return out


def chat_fold_into_dm(ctx: ChatFeedContext, m: dict[str, Any]) -> bool:
    """While an agent's /dm tab is open, its strictly private letters to the person go there; the
    feed keeps one line that is updated, not repeated: «· Ann ⇄ Bob in /dm · 3 new».
    Never a letter to others too, never one «from the person» the chat did
    not sign — that one is shown whole with its ⚠."""
    rp = m_store.room().paths
    if (
        not ctx.dm_focus[0]
        or m["to"] != [ctx.args.as_]
        or m["from"] in (ctx.args.as_, m_text.ROOM)
        or m_log.unsigned_person(m, ctx.config)
    ):
        return False
    agent = m["from"]
    if agent not in ctx.dm_tabs(ctx.args.as_)["tabs"]:
        return False
    read = m_log.read_set(rp.receipts_dir, ctx.args.as_)
    new = sum(1 for x in ctx.dm_talk(m_log.read_messages(), ctx.args.as_, agent) if x["from"] == agent and x["id"] not in read)
    text = m_text.paint(
        m_i18n.tr("· {me} ⇄ {p1} in /dm · {new} new", me=ctx.me, p1=m_store.label_of(ctx.config, agent), new=new)
        if new
        else m_i18n.tr("· {me} ⇄ {p1} in /dm", me=ctx.me, p1=m_store.label_of(ctx.config, agent)),
        "2",
    )
    if ctx.folded[0] == agent and ctx.blocks and ctx.blocks[-1] is ctx.folded[1]:
        # The same talk again, nothing in between: that line is updated, not another one added.
        old = len(ctx.folded[1])
        ctx.folded[1][:] = [text]
        del ctx.feed_log[-old:]
        ctx.feed_log.append(text)
        ctx.repaint()
    else:
        ctx.feed(text)
        ctx.folded[0], ctx.folded[1] = agent, ctx.blocks[-1] if ctx.blocks else None
    return True


def chat_pump(ctx: ChatFeedContext) -> None:
    last_bar, last_rows = None, ctx.ui.laid
    said: set[str] = set()  # each trouble once, not every tick

    def trouble(text: str) -> None:
        if text not in said:
            said.add(text)
            ctx.feed(m_text.paint(text, "1;38;5;196"))

    while not ctx.stop.wait(ctx.args.interval):
        with ctx.lock:
            if ctx.refresh_people() and ctx.tty:
                ctx.ui.redraw_input()
            with contextlib.suppress(Exception):
                seen_focus = list(ctx.dm_focus)
                ctx.dm_focus[0] = ctx.dm_window_alive(ctx.args.as_)
                ctx.dm_focus[1] = (
                    m_tmuxio.tmux(
                        "display-message", "-p", "-t", os.environ.get("TMUX_PANE", ""), "#{pane_active}", check=False
                    ).stdout.strip()
                    != "0"
                )
                if ctx.dm_focus != seen_focus and ctx.tty and not ctx.wizard():
                    ctx.ui.redraw_input()
            # The log, once a tick: for «● N for you» and for the feed both. Only what came since is read
            # (the tick's reader): the whole 4 MB log every tick was the chat's CPU. The feed
            # judges each message once, when it first shows it; the past is read whole where it is judged
            # again — history, /mine, a reply by id.
            try:
                tick_log: list[dict[str, Any]] | None = m_log.read_log_tick()
            except m_store.OperationFailed as exc:  # a link in the log: said once, the feed goes on
                trouble(m_i18n.tr("! {exc}", exc=exc))
                tick_log = None
            with contextlib.suppress(Exception):
                before = ctx.ui.mine_count
                if tick_log is not None:
                    ctx.count_mine(tick_log)
                if ctx.ui.mine_count != before and ctx.tty and not ctx.wizard():
                    ctx.ui.redraw_input()
            # The window was resized — its width too: a /dm panel beside the chat took a third of it,
            # and the old draft hint stayed over the input — rebuild the fixed area.
            if ctx.tty and (ctx.rows(), shutil.get_terminal_size((100, 24)).columns) != last_rows:
                last_rows = (ctx.rows(), shutil.get_terminal_size((100, 24)).columns)
                ctx.repaint()
                last_bar = None
            # A record or a status no part of the chat can show is said once, and the feed goes on:
            # it used to end the feed thread in silence.
            # An open menu sits over the bottom of the feed: what comes in now would scroll it up
            # with the feed. It waits — the states line still updates — and is
            # printed on the first tick after the menu closes.
            hold = bool(ctx.ui.popup)
            try:
                ctx.held_alerts.extend("\n" + alert for alert in ctx.states())
            except Exception as exc:  # noqa: BLE001
                trouble(m_i18n.tr("! Could not read the agents' states: {exc}", exc=repr(exc)))
            while ctx.held_alerts and not hold:
                ctx.feed(ctx.held_alerts.pop(0))
            log = [] if hold or tick_log is None else tick_log
            for m in log:
                if m["id"] not in ctx.printed:
                    ctx.printed.add(m["id"])
                    # Your own words were shown as you typed them; one in your name that
                    # this chat did not send is shown, marked.
                    try:
                        if ctx.fold_into_dm(m):
                            continue
                        if m["from"] != ctx.args.as_ or m_log.unsigned_person(m, ctx.config):
                            ctx.feed(ctx.show(m))
                    except Exception as exc:  # noqa: BLE001
                        trouble(m_i18n.tr("! Could not show message {p0}: {exc}", p0=m_text.visible(m["id"]), exc=repr(exc)))
            # A proposal for the team waiting for the person: its card opens by itself when no question is open —
            # one Enter applies it. Each once: «Later» leaves it to /team. By the room's record of proposals, which
            # delivery fills — not by the tick's messages, which may come before it is judged.
            if ctx.tty and not ctx.wizard() and not ctx.ui.popup and ctx.args.as_ in m_store.people_ids(ctx.config):
                try:
                    shown = ctx.ui.proposals_shown
                    waiting = [k for k, v in m_store.proposal_state().items() if v == "pending" and k not in shown]
                    everything = m_log.read_messages() if waiting else []
                    at = next((i for i, x in enumerate(everything) if x.get("id") == waiting[0]), -1) if waiting else -1
                    # What came after the proposal and is not on the screen yet goes first, on the next tick: a card
                    # opened over it held it back, and it poured out after the person's answer, over the answer's
                    # own line.
                    if waiting and not any(x.get("id") not in ctx.printed for x in everything[at + 1 :]):
                        shown.add(waiting[0])  # the rest open one by one, each when no question is open
                        m = everything[at] if at >= 0 and isinstance(everything[at].get("proposal"), dict) else None
                        if m is not None:
                            cfg = m_store.load_config()
                            req = m_log.request_of(m, cfg, everything) or {}  # only the person's own request is shown as one
                            ctx.feed(m_text.paint(m_store.proposal_words(cfg, m["proposal"]), "38;5;252") + "\n" * 5)
                            ctx.wizard().update(m_store.proposal_card(m, cfg, str(req.get("text") or "")))
                            ctx.ui.redraw_input()
                except Exception as exc:  # noqa: BLE001
                    trouble(m_i18n.tr("! Could not show a proposal: {exc}", exc=repr(exc)))
            while sink := m_store.chat_mode().sink:
                ctx.feed(m_text.paint(sink.pop(0), "38;5;245"))
            if ctx.ui.viewing:
                ctx.draw_view()
            if ctx.ui.bar != last_bar:
                last_bar = ctx.ui.bar
                ctx.draw_bottom()


def chat_refresh_people(ctx: ChatFeedContext) -> bool:
    """Pick up participants added while the seat is open (/add, join) without a restart."""
    rp = m_store.room().paths
    try:
        m = rp.config_path.stat().st_mtime
    except OSError:
        return False
    if m == ctx.ui.cfg_mtime:
        return False
    ctx.ui.cfg_mtime = m
    try:
        fresh = m_store.load_config()
    except (Exception, SystemExit) as exc:  # noqa: BLE001
        # One bad .collab.json (a participant with an unknown `delivery`) ended the feed's thread for
        # good, while the input still sent — the person wrote without seeing answers. Said
        # once per version of the file; the chat goes on with whom it had; a fixed file is read again.
        ctx.feed(
            m_text.paint(
                m_i18n.tr(
                    "! The room's settings can't be read: {p0} — the chat goes on with the participants it had; fix .collab.json and it reads them again.",
                    p0=m_text.clean_field(str(exc), 200),
                ),
                "1;38;5;196",
            )
        )
        return False
    ctx.config.clear()
    ctx.config.update(fresh)
    m_i18n.speak(m_store.room_language(ctx.config))  # a /language from another chat: this one follows
    ctx.known[:] = m_store.ids_of(ctx.config)
    ctx.names.clear()
    ctx.names.update(m_store.aliases(ctx.config))
    ctx.targets()[:] = ["all"] + [pid for pid in ctx.known if pid != ctx.args.as_]
    if ctx.ui.selected is not None and ctx.ui.selected not in ctx.targets():
        # The chosen recipient left. Do NOT fall back to Everyone: a private draft would leak
        # to the whole room on Enter. Ask for a new choice instead.
        ctx.ui.selected = None
        ctx.feed(
            m_text.paint(m_i18n.tr("! The recipient left the room — press Tab to choose who gets your message."), "1;38;5;196")
        )
    return True
