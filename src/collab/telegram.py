from __future__ import annotations
import argparse
import contextlib
import http.client
import json
import re
import ssl
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any
import collab.boot as m_boot
import collab.text as m_text
import collab.i18n as m_i18n
import collab.store as m_store
import collab.tmuxio as m_tmuxio
import collab.log as m_log
import collab.ledger as m_ledger
import collab.screen as m_screen

# The room's Telegram: the person writes to the room's lead from Telegram, through the bot's server — the room asks it
# (HTTPS, a long poll), the computer opens no port. What comes from Telegram is the person's message, signed here with
# their key and marked «via telegram» under the signature: read, answered, never applied (log.request_of).
SERVER = "https://bot.mytrener.com"
BOT = "@Callaba_room_bot"
WAIT = 50  # a long poll's wait, under the server's
CALL_WAIT = 70  # a call's whole time, the long poll's included
TEXT_MAX = 4000
START = "^"  # the cursor of what was sent, at the log's beginning
# What the server gives back, by its shape only: it reaches a terminal and the room's file — nothing else passes.
CODE = re.compile(r"[A-Z0-9]{4,32}")  # also Telegram's start parameter: A-Za-z0-9_-, up to 64
BOT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
ROOM_ID = re.compile(r"[0-9a-f]{32}")
TOKEN = re.compile(r"[A-Za-z0-9_-]{20,128}")


class Refused(Exception):
    """The server's answer was not 200: its status and its word."""

    def __init__(self, status: int, word: str) -> None:
        super().__init__(f"{status} {word}")
        self.status, self.word = status, word


def state_path() -> Path:
    return m_store.room().paths.state_dir / "telegram.json"


def load() -> dict[str, Any]:
    """The room's link to the bot: {server, room_id, token, after, sent, state}; {} — none. 0600, in the room's home:
    the token is never in the log, a brief, an agent's window or its environment."""
    data = m_store.read_json(state_path())
    return data if isinstance(data.get("room_id"), str) and isinstance(data.get("token"), str) else {}


def save(state: dict[str, Any]) -> None:
    m_store.private_dir(state_path().parent, strict=False)
    m_store.write_private(state_path(), json.dumps(state))


@m_store.room_locked  # read and written in one step: the chat's «off» and the run's cursor never cross
def update(**fields: Any) -> dict[str, Any]:
    """Only these fields changed, over the file as it is now: the run's loop held what it read before a long poll,
    and wrote it back over an «off» the chat had set meanwhile."""
    state = {**load(), **fields}
    save(state)
    return state


def call(server: str, method: str, path: str, body: dict[str, Any] | None = None, token: str = "") -> dict[str, Any]:
    """One request to the bot's server. Only https — plain http to this very machine for tests — with the certificate
    checked; the token in its header, never in a URL."""
    url = urllib.parse.urlsplit(server)
    local = url.hostname in ("127.0.0.1", "localhost")
    if url.scheme != "https" and not (url.scheme == "http" and local):
        raise Refused(0, "the server must be https")
    conn: http.client.HTTPConnection
    if url.scheme == "https":
        conn = http.client.HTTPSConnection(
            url.hostname or "", url.port or 443, timeout=CALL_WAIT, context=ssl.create_default_context()
        )
    else:
        conn = http.client.HTTPConnection(url.hostname or "", url.port or 80, timeout=CALL_WAIT)
    headers = {"X-Collab-Version": m_boot.VERSION, "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        resp = conn.getresponse()
        raw = resp.read(1 << 20)
    finally:
        conn.close()
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if resp.status != 200:
        raise Refused(resp.status, m_text.clean_field(m_text.visible(str(data.get("error") or "")), 80))
    return data


def spend_today() -> dict[str, int]:
    """Today's tokens (UTC) by agent, from the room's ledger — what the person's limit is held against."""
    today = m_store.now()[:10]
    out: dict[str, int] = {}
    for row in m_ledger.read_jsonl(m_ledger.usage_path()):
        if str(row.get("at") or "")[:10] != today:
            continue
        n = sum(int(row.get(k) or 0) for k in ("input", "output", "cache_read", "cache_write") if isinstance(row.get(k), int))
        name = m_text.clean_field(str(row.get("agent") or row.get("pid") or "?"), 40)
        out[name] = out.get(name, 0) + n
    return out


def waiting_agents(config: dict[str, Any]) -> list[str]:
    """The agents whose program waits for the person in its window (a red state) — by id."""
    status = m_screen.read_status()
    return [p["id"] for p in config["participants"] if status.get(p["id"], {}).get("state") == "needs_owner"]


def room_state(config: dict[str, Any]) -> dict[str, Any]:
    """What the bot shows of the room: its name (and its project folder's parent, to tell a twin apart), its lead's,
    its language, whether there is a lead to write to; its agents' states, today's spend, how many wait for the person."""
    lead = m_store.lead_of(config)
    rp = m_store.room().paths
    status = m_screen.read_status()
    people = set(m_store.people_ids(config))
    agents = [
        {
            "name": m_text.clean_field(m_store.label_of(config, p["id"]), 40),
            "state": str(status.get(p["id"], {}).get("state") or "unknown")[:40],
        }
        for p in config["participants"]
        if p["id"] not in people
    ][:20]
    pending = len(m_store.pending_proposals(m_log.read_messages()[-500:]))
    return {
        "name": m_text.clean_field(rp.root.name.lstrip(".") or rp.root.name, 80),
        "hint": m_text.clean_field(rp.root.parent.name, 80),
        "lead_name": m_text.clean_field(m_store.label_of(config, lead), 80) if lead else "",
        "lang": m_store.room_language(config),
        "has_lead": bool(lead),
        "agents": agents,
        "spend_today": sum(spend_today().values()),
        "needs_you": len(waiting_agents(config)) + pending,
    }


def enable(server: str = "") -> tuple[str, float, str]:
    """Telegram on: the room registered with the bot once, its state given, a new code to link the person's account.
    (code, its end as a time, the link that opens the bot with the code — "" when the server names no bot)."""
    config = m_store.load_config()
    state = load()
    if not state:
        made = call(
            server or SERVER, "POST", "/v1/rooms", {"name": room_state(config)["name"], "lang": m_store.room_language(config)}
        )
        rid, token = str(made.get("room_id") or ""), str(made.get("token") or "")
        if not ROOM_ID.fullmatch(rid) or not TOKEN.fullmatch(token):
            raise Refused(0, "the server's answer is not a room")
        state = {"server": server or SERVER, "room_id": rid, "token": token, "after": 0, "sent": ""}
    state["state"] = "on"
    save(state)
    call(state["server"], "PUT", f"/v1/rooms/{state['room_id']}/state", room_state(config), state["token"])
    got = call(state["server"], "POST", f"/v1/rooms/{state['room_id']}/code", {}, state["token"])
    code = str(got.get("code") or "")
    if not CODE.fullmatch(code) or not isinstance(got.get("expires"), (int, float)):
        raise Refused(0, "the server's answer is not a code")
    bot = str(got.get("bot") or "")
    link = f"https://t.me/{bot}?start={code}" if BOT_NAME.fullmatch(bot) else ""
    return code, float(got["expires"]), link


def disable() -> None:
    if load():
        update(state="off")


def unlink() -> int:
    """The account unlinked on the server too — what waited there is cancelled; the room keeps its registration."""
    state = load()
    if not state:
        return 0
    got = call(state["server"], "DELETE", f"/v1/rooms/{state['room_id']}/link", None, state["token"])
    update(state="off")
    return int(got.get("cancelled") or 0)


@m_store.room_locked
def take(msg: dict[str, Any], config: dict[str, Any]) -> bool:
    """One message from Telegram into the room: from the person, to the lead, signed with the person's key and marked
    via telegram under the signature, its server id kept — taken twice, written once. False — no lead to write to."""
    lead = m_store.lead_of(config)
    people = m_store.people_ids(config)
    mid = str(msg.get("id") or "")
    if not lead or not people or not mid:
        return False
    if any(m.get("tg") == mid for m in m_log.read_messages()[-500:]):
        return True
    text = m_text.visible(str(msg.get("text") or ""))[:TEXT_MAX]
    re_ = str(msg.get("re") or "")
    known = re_ and any(m.get("id") == re_ for m in m_log.read_messages()[-2000:])
    m_log.append_message(people[0], [lead], text, re_=re_ if known else "", sign=True, via="telegram", tg=mid)
    return True


def outgoing(config: dict[str, Any], messages: list[dict[str, Any]], after: str) -> list[tuple[str, str, str]]:
    """What goes to Telegram since the message `after`: the lead's messages to the person, its proposals (to apply on
    the computer), the goal reached. (room message id, kind, text)."""
    lead = m_store.lead_of(config)
    people = set(m_store.people_ids(config))
    # START — from the log's beginning (it was empty at the first look); an id gone (the history cleared) — from it too.
    start = 0 if after == START else next((i + 1 for i, m in enumerate(messages) if m.get("id") == after), 0)
    by_id = {str(m.get("id")): m for m in messages}
    out: list[tuple[str, str, str]] = []
    for m in messages[start:]:
        to = set(m.get("to") or [])
        if m.get("from") == lead and lead and to & people and not m.get("quiet"):
            if isinstance(m.get("proposal"), dict):
                out.append((m["id"], "proposal", m_store.proposal_words(config, m["proposal"])))
            else:
                # By what it is, not by its words: an answer to the person's message is a reply; one the lead begins
                # itself asks the person something — a question.
                answered = by_id.get(str(m.get("re") or ""), {}).get("from") in people
                out.append((m["id"], "reply" if answered else "question", str(m.get("text") or "")))
        elif (
            m.get("from") == m_text.ROOM
            and to & people
            and str((m.get("said") or {}).get("t", "")).startswith("{lead} declared the goal reached")
        ):
            out.append((m["id"], "goal", str(m.get("text") or "")))
        elif (
            m.get("from") == m_text.ROOM
            and to & people
            and str((m.get("said") or {}).get("t", "")).startswith("Agents wrote {n}")
        ):
            out.append((m["id"], "paused", str(((m.get("said") or {}).get("p") or {}).get("n") or "")))
    return out


def send_out(state: dict[str, Any]) -> dict[str, Any]:
    """Each new message for Telegram, once, in order; the cursor moves past one only when the server took it."""
    config = m_store.load_config()
    messages = m_log.read_messages()
    if not state.get("sent"):
        state = update(sent=messages[-1]["id"] if messages else START)  # from now: the past is not sent
    for mid, kind, text in outgoing(config, messages, str(state.get("sent") or "")):
        if text.strip():
            call(
                state["server"],
                "POST",
                f"/v1/rooms/{state['room_id']}/messages",
                {"room_msg": mid, "kind": kind, "text": text},
                state["token"],
            )
        state = update(sent=mid)
    return notices(state, config)


def notices(state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """What the room's state says, not a message: an agent that now waits for the person in its window — once each
    time it begins to; today's spend past the person's limit — once a day, with the spend by agent."""
    post = lambda room_msg, kind, text: call(
        state["server"],
        "POST",
        f"/v1/rooms/{state['room_id']}/messages",
        {"room_msg": room_msg, "kind": kind, "text": text},
        state["token"],
    )
    waiting = waiting_agents(config)
    before = [str(x) for x in state.get("waiting") or []]
    for pid in waiting:
        if pid not in before:
            post(f"att-{pid}-{uuid.uuid4().hex[:12]}", "attention", m_store.label_of(config, pid))  # each time its own
    if waiting != before:
        state = update(waiting=waiting)
    limit, today = int(state.get("limit") or 0), m_store.now()[:10]
    if limit and state.get("limit_told") != today:
        spent = spend_today()
        total = sum(spent.values())
        if total > limit:
            num = lambda n: f"{n:,}".replace(",", " ")
            lines = [m_i18n.tr("Spent today: {spent} tokens of {limit}.", spent=num(total), limit=num(limit))]
            lines += [f"{name}: {num(n)}" for name, n in sorted(spent.items(), key=lambda x: -x[1])[:10]]
            post(f"limit-{today}", "limit", "\n".join(lines))
            state = update(limit_told=today)
    return state


def take_in(state: dict[str, Any], wait: float) -> dict[str, Any]:
    """One long poll: the events first (unlinked — off at once), then each message written into the room and
    acknowledged after it is."""
    config = m_store.load_config()
    got = call(
        state["server"],
        "GET",
        f"/v1/rooms/{state['room_id']}/updates?after={int(state.get('after') or 0)}&wait={wait}",
        None,
        state["token"],
    )
    if any(e.get("kind") == "unlinked" for e in got.get("events") or []):
        state = update(state="off", after=int(got.get("after") or 0))
        m_log.room_line(m_store.people_ids(config), "Telegram is unlinked: the account was unlinked in the bot.", silent=True)
        return state
    taken = [str(m["id"]) for m in got.get("messages") or [] if isinstance(m, dict) and take(m, config)]
    if taken:
        call(state["server"], "POST", f"/v1/rooms/{state['room_id']}/ack", {"messages": taken}, state["token"])
    return update(after=int(got.get("after") or 0))


def run(wait: float = WAIT) -> None:
    """`collab telegram run`: the room's Telegram, in its own window while the room runs. Out every two seconds, in by
    a long poll; the network gone (a sleeping Mac) — again, more slowly; a token refused — off, and the person told."""
    import fcntl

    rp = m_store.room().paths
    m_store.private_dir(rp.state_dir)
    with open(rp.state_dir / "telegram.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return  # another runs
        stop = threading.Event()

        def out_loop() -> None:
            while not stop.is_set():
                state = load()
                if state.get("state") == "on":
                    with contextlib.suppress(Exception):
                        send_out(state)
                stop.wait(2)

        threading.Thread(target=out_loop, daemon=True).start()
        pause, said = 1.0, ""
        last_state: dict[str, Any] = {}
        while True:
            state = load()
            if state.get("state") != "on":
                stop.set()
                return
            try:
                current = room_state(m_store.load_config())
                if current != last_state:
                    call(state["server"], "PUT", f"/v1/rooms/{state['room_id']}/state", current, state["token"])
                    last_state = current
                take_in(state, wait)
                pause, said = 1.0, ""
            except Refused as exc:
                if exc.status == 401:
                    update(state="off")
                    m_log.room_line(
                        m_store.people_ids(m_store.load_config()),
                        "Telegram is off: the bot refused this room's key.",
                        silent=True,
                    )
                    stop.set()
                    return
                why = str(exc)
            except (OSError, ValueError, http.client.HTTPException) as exc:
                why = type(exc).__name__
            else:
                continue
            if why != said:
                print(m_i18n.tr("telegram: no connection to the bot ({why}); trying again", why=why), file=sys.stderr)
                said = why
            time.sleep(pause)
            pause = min(60.0, pause * 2)


def in_own_window() -> bool:
    """This process runs in the room's own «telegram» window — under the process tmux started there, not one an agent
    started with the same words."""
    session = m_store.load_config().get("session", "collab-room")
    shown = m_tmuxio.tmux("display-message", "-p", "-t", f"={session}:=telegram", "#{pane_pid}", check=False)
    root = shown.stdout.strip() if shown.returncode == 0 else ""
    if not root.isdigit():
        return False
    import os
    import subprocess

    pid = os.getpid()
    for _ in range(30):
        if str(pid) == root:
            return True
        r = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        pid = int(r.stdout.strip() or 0)
        if pid <= 1:
            return False
    return False


def cmd_telegram(args: argparse.Namespace) -> None:
    """`collab telegram on|off|unlink|status|run`. Setting it up is the person's: a Claude running this is refused (its
    own words would come back signed as the person's); `run` only in the room's own window, which `collab start` opens."""
    action = args.action
    if action == "run":
        if not in_own_window():
            m_store.fail(m_i18n.tr("collab telegram run runs only in the room's own window «telegram»: `collab start` opens it."))
        run()
        return
    if action in ("on", "unlink"):
        # Linking a room to an account is the person's: in their chat, where no agent's command reaches.
        m_store.fail(m_i18n.tr("Telegram is set up in the room's chat: /telegram there connects or unlinks it."))
    if action == "off":
        if m_screen.caller_session_or_refuse():
            # Off from an agent would cut the person off the room without their knowing: theirs, in the chat or a terminal.
            m_store.fail(m_i18n.tr("Telegram is the person's to turn off: /telegram in their chat. An agent does not."))
        disable()
        print(m_i18n.tr("Telegram is off for this room. Its link stays: `collab telegram on` turns it back on."))
        return
    if action == "limit":
        if m_screen.caller_session_or_refuse():
            m_store.fail(m_i18n.tr("The spending limit is the person's to set. An agent does not change it."))
        if not load():
            m_store.fail(m_i18n.tr("Telegram is not set up for this room: /telegram in the room's chat."))
        n = max(0, int(args.tokens or 0))
        update(limit=n, limit_told="")
        print(
            m_i18n.tr("A day's limit: {n} tokens — past it, Telegram tells you once a day.", n=f"{n:,}".replace(",", " "))
            if n
            else m_i18n.tr("No day's limit: Telegram does not tell you of spending.")
        )
        return
    state = load()
    if not state:
        print(m_i18n.tr("Telegram is not set up for this room: `collab telegram on`."))
        return
    print(m_i18n.tr("Telegram: {state} · server {server}", state=state.get("state", "off"), server=state.get("server", "")))
