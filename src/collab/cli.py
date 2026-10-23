from __future__ import annotations
import argparse
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
import collab.boot as m_boot
import collab.text as m_text
import collab.store as m_store
import collab.log as m_log
import collab.tasks as m_tasks
import collab.tmuxio as m_tmuxio
import collab.screen as m_screen
import collab.installer as m_installer
import collab.ledger as m_ledger
import collab.deliver as m_deliver
import collab.agents as m_agents
import collab.lifecycle as m_lifecycle
import collab.chat_base as m_chat_base
import collab.chat as m_chat
import collab.dm as m_dm


# One empty line under the input, so it does not sit on the very bottom edge:
# step down to make the line, then back up to type above it.
LIFT = "\n\033[1A"


def cmd_mute(args: argparse.Namespace) -> None:
    m_screen.only_own_seat(m_store.load_config(), args.id, "mutes" if args.func_name == "mute" else "unmutes")
    try:
        was = any(p["id"] == args.id and p.get("muted") for p in m_store.load_config()["participants"])
        label = m_agents.set_muted(args.id, args.func_name == "mute")
    except ValueError as exc:
        m_store.fail(str(exc))
    if args.func_name == "unmute" and not was:
        print(f"{label} was not muted.")  # «unmuted» was said for one never muted
        return
    print(f"{label} {'muted' if args.func_name == 'mute' else 'unmuted'}.")


def cmd_unlimit(args: argparse.Namespace) -> None:
    m_screen.only_own_seat(m_store.load_config(), args.id, "clears the limit of")
    try:
        label, had = m_agents.clear_limit(args.id)
    except ValueError as exc:
        m_store.fail(str(exc))
    print(f"{label}'s usage limit cleared: messages go to it again." if had else f"{label} is not out of its limit.")


def cmd_remove(args: argparse.Namespace) -> None:
    m_screen.only_own_seat(m_store.load_config(), args.id, "removes")
    try:
        label = m_agents.remove_participant(args.id)
    except ValueError as exc:
        m_store.fail(str(exc))
    print(f"{label} disconnected from the room.")


def cmd_add(args: argparse.Namespace) -> None:
    """`collab add`: what /add in the chat does, from a command line — a room set up to the end by a
    script or another agent. The room must be running: the agent gets a window of its session."""
    config = m_store.load_config()
    session = config.get("session", "collab-room")
    if m_tmuxio.tmux("has-session", "-t", f"={session}", check=False).returncode != 0:
        m_store.fail("the room is not running — collab start first, then collab add")
    try:
        folder = Path(args.folder or str(m_store.room().paths.root)).expanduser()
    except RuntimeError:  # «~someone» with no such user
        m_store.fail(f"Folder not found: {args.folder}")
    problem = m_agents.folder_problem(folder)
    if problem:
        m_store.fail(problem)
    base = m_screen.PRESETS[args.kind]["label"]
    label = args.name or next(
        n for n in [base] + [f"{base} {i}" for i in range(2, 100)] if not m_store.label_taken(config, "", n)
    )
    problem = m_text.name_problem(label) or (
        "that is already someone else's name here" if m_store.label_taken(config, "", label) else ""
    )
    if problem:
        m_store.fail(problem)
    pid = m_agents.launch_new_agent(config, args.kind, str(folder.resolve()), label)
    # Not typed in a chat: the people learn who came in, and where it works.
    line = m_text.visible(f"{label} ({base}) was added by collab add, in {m_text.tilde(str(folder.resolve()))}.")
    m_log.append_message(m_text.ROOM, m_store.people_ids(m_store.load_config()), line)
    how = "its window tells its session" if args.kind == "claude" else "it answers the connection check"
    print(f"Starting {label} in room window {pid}. It joins when {how} (collab status).")


def cmd_name(args: argparse.Namespace) -> None:
    config = m_store.load_config()
    m_store.participant(config, args.id)
    m_screen.only_own_seat(config, args.id, "renames")
    problem = m_text.name_problem(args.label)
    if problem:
        m_store.fail(problem)
    other = m_store.label_taken(config, args.id, args.label)
    if other:
        m_store.fail(f"«{args.label}» is already {other}'s name in this room — choose another")
    if args.id in m_store.people_ids(config):
        # A person's name is changed only in their own chat (/name): from a command line any
        # agent could rename the person and then take the name.
        m_store.fail(f"{args.id!r} is a person's seat — rename it with /name in their chat")
    m_store.rename(args.id, args.label)
    print(f"{args.id}: {args.label}")


def cmd_say(args: argparse.Namespace) -> None:
    """Post a message. The text from stdin («-») is read first, outside the room's lock: waiting for it
    under the lock held every write of the room — other agents' say and ack, the deliverer — until
    stdin closed."""
    m_deliver.post_message(args, args.text if args.text != "-" else sys.stdin.read())


def cmd_inbox(args: argparse.Namespace) -> None:
    """The participant's messages not yet acknowledged (all of them with --all), the last --limit."""
    rp = m_store.room().paths
    config = m_store.load_config()
    m_store.participant(config, args.participant)
    mine = [m for m in m_log.read_messages() if m_log.is_for(m, args.participant)]
    if not args.all:
        acked = m_log.read_set(rp.receipts_dir, args.participant)
        mine = [m for m in mine if m["id"] not in acked]
    mine = mine[-args.limit :]
    if not mine:
        print(f"No unacknowledged messages for {args.participant}.")
        return
    print("\n\n".join(m_chat_base.fmt(m, config=config) for m in mine))


@m_store.room_locked
def cmd_ack(args: argparse.Namespace) -> None:
    """Mark messages read (--id or --all); --no-reply also closes them as needing no answer."""
    rp = m_store.room().paths
    config = m_store.load_config()
    m_store.participant(config, args.participant)
    if args.all == bool(args.ids):
        m_store.fail("choose either --id <message-id> or --all")
    if args.no_reply and args.all:
        m_store.fail("--no-reply closes named messages only: use --id")
    mine = {m["id"] for m in m_log.read_messages() if m_log.is_for(m, args.participant)}
    ids = mine if args.all else set(args.ids)
    missing = ids - mine
    if missing:
        m_store.fail(f"not addressed to {args.participant}: {', '.join(sorted(missing))}")
    m_log.write_set(rp.receipts_dir, args.participant, m_log.read_set(rp.receipts_dir, args.participant) | ids)
    if args.no_reply:
        # «Read» alone does not end the wait for an answer — otherwise «read it, answered only in
        # my own window» goes unnoticed again. This mark does: no answer is due.
        m_log.write_set(rp.noreply_dir, args.participant, m_log.read_set(rp.noreply_dir, args.participant) | ids)
        print(f"Closed {len(ids)} message(s) for {args.participant} without a reply.")
        return
    print(f"Acknowledged {len(ids)} message(s) for {args.participant}.")


def cmd_task(args: argparse.Namespace) -> None:
    """Which task a participant is on: take, done, or show the current ones."""
    config = m_store.load_config()
    if args.action == "show":
        current = m_tasks.current_tasks()
        pids = [args.participant] if args.participant else [p for p in m_store.ids_of(config) if p in current]
        if args.participant:
            m_store.participant(config, args.participant)
        if not any(p in current for p in pids):
            print("No current tasks.")
        for pid in pids:
            rec = current.get(pid)
            if rec:
                lane = f", lane {rec['lane']}" if rec.get("lane") else ""
                print(
                    f"{m_store.label_of(config, pid)}: {rec['task']} ({rec['kind']}{lane}) since {m_text.local_hm(rec['since'])}"
                )
        return
    if not args.participant or not args.task:
        m_store.fail(f"task {args.action} needs the task's id and --as <participant>")
    m_store.participant(config, args.participant)
    who = m_store.label_of(config, args.participant)
    if args.action == "take":
        if not args.kind:
            m_store.fail(f"--kind is one of: {', '.join(m_tasks.KINDS)}")
        if args.by:
            m_store.participant(config, args.by)
        m_tasks.task_take(args.participant, args.task, args.kind, args.lane or "", args.by or "")
        print(f"{who} is on {args.task} ({args.kind}).")
        return
    was_current = m_tasks.task_done(args.participant, args.task, args.commit or "")
    print(f"{who} finished {args.task}." + ("" if was_current else " (It was not the current task: that one stays.)"))


def cmd_watch(args: argparse.Namespace) -> None:
    """One line block per new message. `--for all` shows the whole room (for people)."""
    rp = m_store.room().paths
    config = m_store.load_config()
    everyone = args.participant == "all"
    if not everyone:
        m_store.participant(config, args.participant)
    # A participant's watch starts from what it has not acknowledged, not from "now": a watcher
    # restarted after a crash or an expired timer would otherwise silently skip what arrived
    # while it was down. The room view (`all`) has no acks, so it starts from now unless --backlog.
    if everyone:
        seen = {m["id"] for m in m_log.read_messages()} if not args.backlog else set()
    else:
        acked = m_log.read_set(rp.receipts_dir, args.participant)
        seen = {m["id"] for m in m_log.read_messages() if m["id"] in acked or not m_log.is_for(m, args.participant)}
    if not args.quiet:
        print(f"Watching {'the room' if everyone else args.participant}; Ctrl-C to stop.", flush=True)
    try:
        while True:
            acked = set() if everyone else m_log.read_set(rp.receipts_dir, args.participant)
            if not everyone and m_store.load_config().get("paused"):
                # Paused: an agent reading for itself hears nothing either; what arrives waits and
                # comes on resume.
                if args.once:
                    return
                time.sleep(args.interval)
                continue
            if not everyone and any(
                p["id"] == args.participant and p.get("muted") for p in m_store.load_config()["participants"]
            ):
                # Muted: room-wide talk is not for its ears (and is not re-sent after unmute);
                # a message addressed to it by name still comes through.
                seen.update(m["id"] for m in m_log.read_messages() if args.participant not in m.get("to", []))
            for m in m_log.read_messages():
                if m["id"] in seen:
                    continue
                seen.add(m["id"])
                if everyone or (m_log.is_for(m, args.participant) and m["id"] not in acked):
                    print(m_chat_base.fmt(m, with_id=not everyone, config=config), flush=True)
            if args.once:
                return
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


def cmd_show(args: argparse.Namespace) -> None:
    """One message in full, by its id — where a cut one in the chat points."""
    config = m_store.load_config()
    for m in m_log.read_messages():
        if m["id"] == args.id:
            # With the room's config: without it a fake «from the person» showed no ⚠.
            print(m_chat_base.fmt(m, config=config))
            return
    m_store.fail(f"no message {args.id}")


def hook_participant(config: dict[str, Any], pid: str, hook: dict[str, Any]) -> str | None:
    """Whom a Claude Code hook speaks for. Hooks live in a folder's settings, so every Claude
    started in that folder runs them with the same `--as`. A second Claude in the participant's
    folder reported «idle» for it while it worked, and a second Claude that joined
    as claude2 stayed «Connecting»: its hooks spoke as `claude` and were dropped.
    The participant bound to the hook's session is the one; `--as` only when none is bound to it,
    and never for a participant bound to another session. Two bound to one session: nobody."""
    sid = hook.get("session_id") if isinstance(hook, dict) else None
    if sid:
        mine = [p["id"] for p in config.get("participants", []) if p.get("claude_session") == sid]
        if mine:
            return mine[0] if len(mine) == 1 else None
    bound = next((p.get("claude_session") for p in config.get("participants", []) if p["id"] == pid), None)
    return None if bound and sid else pid


def read_hook_stdin() -> dict[str, Any]:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def cmd_status(args: argparse.Namespace) -> None:
    """Show everyone's state, or (`--as id STATE`) report your own — for participants without a
    watched pane, e.g. a Claude session outside tmux."""
    rp = m_store.room().paths
    if args.if_room and not rp.config_path.is_file():
        return  # called from an agent hook in a project without a room: nothing to report, no noise
    config = m_store.load_config()
    if args.as_:
        if args.hook:
            args.as_ = hook_participant(config, args.as_, read_hook_stdin())
            if args.as_ is None:
                return
        if args.if_room and args.as_ not in m_store.ids_of(config):
            return
        m_store.participant(config, args.as_)
        if args.state not in ("working", "idle", "needs_owner"):
            m_store.fail("state must be working, idle or needs_owner")
        m_screen.write_status(args.as_, args.state, args.note or "", "self", args.model or "")
        print(f"{args.as_}: {args.state}")
        return
    st = m_screen.read_status()
    for pid in m_store.ids_of(config):
        print(m_screen.describe(pid, st[pid], args.stale) if pid in st else f"· {pid}: No status yet")


def cmd_hook_model(args: argparse.Namespace) -> None:
    """For Claude Code hooks: read the hook's JSON on stdin (SessionStart has `model`,
    PostModelSwitch has `to_model`) and put the model next to this participant's name.
    Silent and harmless outside a room — hooks must never get in the agent's way."""
    rp = m_store.room().paths
    if not rp.config_path.is_file():
        return
    data = read_hook_stdin()
    args.as_ = hook_participant(m_store.load_config(), args.as_, data)
    if args.as_ is None:
        return
    raw = data.get("to_model") or data.get("model") or ""
    if not raw and isinstance(data.get("transcript_path"), str):
        # SessionStart often comes without `model` (never in `claude -p`), but the
        # session transcript records the model of every answer: take the latest one.
        try:
            with open(data["transcript_path"], "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 400_000))
                tail = f.read().decode("utf-8", "ignore")
            found = re.findall(r'"model":"(claude-[^"]+)"', tail)
            raw = found[-1] if found else ""
        except OSError:
            raw = ""
    if not isinstance(raw, str) or not raw or args.as_ not in m_store.ids_of(m_store.load_config()):
        return
    m_screen.set_model(args.as_, m_screen.pretty_model(raw))


# Local git answers in well under a second; one that hangs (a stale lock, a slow network disk)
# must not hang the agent asking for its report.
GIT_TIMEOUT = 30


def cmd_usage_report(args: argparse.Namespace) -> None:
    """The spending to plan by, from the room's own files — by task kind, agent, model, lane, task."""
    print(m_ledger.report(args.by, args.since or "", args.format), end="")
    if args.format == "csv":  # the table stays a table; who has no data — beside it
        for note in m_ledger.unread_notes():
            print(note, file=sys.stderr)


def cmd_report(args: argparse.Namespace) -> None:
    """Commit summary for the room: hash, subject, files, push state. Checks are the sender's to add."""
    rp = m_store.room().paths
    repo = args.repo or str(rp.root)

    def run_git(*a: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(["git", "-C", repo, *a], text=True, capture_output=True, timeout=GIT_TIMEOUT)
        except FileNotFoundError:
            m_store.fail("git is required for report: install it (macOS: xcode-select --install) and run report again")
        except subprocess.TimeoutExpired:
            m_store.fail(
                f"git {a[0]} did not answer in {GIT_TIMEOUT} s in {m_text.tilde(repo)}; nothing was sent — "
                "check that repository and run report again"
            )

    def git(*a: str) -> str:
        r = run_git(*a)
        if r.returncode:
            m_store.fail(f"git {a[0]} failed in {m_text.tilde(repo)}: {m_text.visible(r.stderr.strip()) or 'no message'}")
        return r.stdout.strip()

    # Outside a repository git said only «fatal: not a git repository» — no folder, no way out.
    if run_git("rev-parse", "--git-dir").returncode:
        m_store.fail(f"{m_text.tilde(repo)} is not a git repository: give the checkout with --repo <folder>; nothing was sent")
    head = git("log", "-1", "--format=%h %s")
    files = git("show", "--stat", "--format=", "HEAD").splitlines()
    upstream = run_git("rev-parse", "--abbrev-ref", "@{u}")
    if upstream.returncode:
        pushed = "no upstream configured"
    else:
        ahead = git("rev-list", "--count", f"{upstream.stdout.strip()}..HEAD")
        pushed = (
            f"pushed to {upstream.stdout.strip()}"
            if ahead == "0"
            else f"NOT pushed: {ahead} commit(s) ahead of {upstream.stdout.strip()}"
        )
    dirty = git("status", "--short")
    lines = [
        f"Commit {head}",
        *(f"  {f.strip()}" for f in files),
        f"Push: {pushed}",
        f"Working tree: {'clean' if not dirty else 'has uncommitted changes'}",
    ]
    text = "\n".join(lines)
    if args.send_from:
        # Through say, with all of its checks: recipients, the sender's own name, the pause, the
        # room's lock — report wrote past them.
        cmd_say(
            argparse.Namespace(sender=args.send_from, to=args.to, re=None, text=text + ("\n" + args.note if args.note else ""))
        )
        print(text)
    else:
        print(text)


# ── cli ────────────────────────────────────────────────────────────────────────


def positive_int(text: str) -> int:
    """--limit: 0 showed every message and -1 all but the first."""
    if not text.isdigit() or int(text) < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number of 1 or more")
    return int(text)


def interval_seconds(text: str) -> float:
    """--interval: -1 ended watch in a traceback, 0 read the whole log in a busy loop."""
    try:
        value = float(text)
    except ValueError:
        value = -1.0
    if not value >= 0.1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds, 0.1 or more")
    return value


def build_parser() -> argparse.ArgumentParser:
    """Every command and its options; --room is added to each one that lacks it."""
    ap = argparse.ArgumentParser(prog="collab", description="Local room for people and AI agents; any number of participants.")
    ap.add_argument("--version", action="version", version=f"collab {m_boot.VERSION}")
    sub = ap.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="create .collab.json with a sample team")
    s.add_argument("--force", action="store_true")
    s.add_argument("--ask", action="store_true", help="ask for participants' names even without a terminal")
    s.add_argument("--no-ask", action="store_true", help="do not ask for names")
    s.set_defaults(func=m_lifecycle.cmd_init)

    s = sub.add_parser("install", help="one-time setup: collab command, Claude Code plugin, Codex skill")
    s.add_argument(
        "--bin-dir",
        help="where to put the collab command (default: ~/.local/bin, made if missing; install says how to add it to PATH)",
    )
    s.add_argument("--dry-run", action="store_true", help="show what would be done")
    s.add_argument(
        "--dev",
        action="store_true",
        help="the collab command is a link to this repository's file, not a read-only copy (for working on collab)",
    )
    s.add_argument(
        "--codex-room-commands",
        action="store_true",
        help="let Codex agents a room starts answer in that room without asking (only say/ack/inbox, own room and name)",
    )
    s.add_argument(
        "--no-codex-room-commands", action="store_true", help="withdraw that: forget the consent and remove every rule it gave"
    )
    s.set_defaults(func=m_installer.cmd_install)

    s = sub.add_parser("update", help="install a newer collab now, from the clone it was installed from")
    s.add_argument("--off", action="store_true", help="turn off the update at collab start (it fetches the clone's origin)")
    s.add_argument("--on", action="store_true", help="turn the update at collab start back on")
    s.add_argument("--auto", action="store_true", help=argparse.SUPPRESS)  # what collab start runs beside the room
    s.add_argument("--after", type=int, default=0, help=argparse.SUPPRESS)  # the start that runs it
    s.set_defaults(func=m_installer.cmd_update)

    s = sub.add_parser("start", help="first time: create the room here; then open it (agents, delivery, chat)")
    s.add_argument("--session")
    s.set_defaults(func=m_lifecycle.cmd_start)

    s = sub.add_parser("say", aliases=["send"], help="post a message to the room")
    s.add_argument("--from", dest="sender", required=True)
    s.add_argument("--to", action="append", help="recipient id; repeat; default all")
    s.add_argument("--re", help="id of the message this answers — closes the wait for it")
    s.add_argument(
        "--quiet", action="store_true", help="a note that wakes no agent («Taking: …», «done»): it goes with their next message"
    )
    s.add_argument("text", help="message text, or - to read stdin")
    s.set_defaults(func=cmd_say)

    s = sub.add_parser("inbox", help="unacknowledged messages for a participant")
    s.add_argument("--for", dest="participant", required=True)
    s.add_argument("--limit", type=positive_int, default=50)
    s.add_argument("--all", action="store_true", help="include acknowledged")
    s.set_defaults(func=cmd_inbox)

    s = sub.add_parser("ack", help="mark messages as read and handled")
    s.add_argument("--for", dest="participant", required=True)
    s.add_argument("--id", dest="ids", action="append")
    s.add_argument("--all", action="store_true")
    s.add_argument("--no-reply", action="store_true", help="the named messages need no answer (closes the wait)")
    s.set_defaults(func=cmd_ack)

    s = sub.add_parser("task", help="the task a participant is on: take, done, show (for counting tokens by task)")
    s.add_argument("action", choices=["take", "done", "show"])
    s.add_argument("task", nargs="?", help="the task's id, e.g. 4.2 or T1")
    s.add_argument("--as", dest="participant", help="who takes or finishes it")
    s.add_argument("--kind", choices=m_tasks.KINDS, help="take: the kind of work")
    s.add_argument("--lane", choices=m_tasks.LANES, help="take: the lane, A/B/C")
    s.add_argument("--by", help="take: who gave the task (a participant's id)")
    s.add_argument("--commit", help="done: the commit it ended in")
    s.set_defaults(func=cmd_task)

    s = sub.add_parser("watch", help="print new messages (for a participant, or `all` for the whole room)")
    s.add_argument("--for", dest="participant", required=True)
    s.add_argument("--interval", type=interval_seconds, default=1.0)
    s.add_argument(
        "--backlog",
        action="store_true",
        help="room view (`--for all`): also print earlier messages; a participant always gets its unacknowledged ones",
    )
    s.add_argument("--once", action="store_true", help="print and exit (for tests and scripts)")
    s.add_argument("--quiet", action="store_true", help="no banner line (for agents that turn each line into an event)")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser(
        "dm",
        help="the direct-conversation panel beside the chat (open it with /dm in the chat; not secret: the room log holds it)",
    )
    s.add_argument("--as", dest="as_", required=True)
    s.add_argument("--interval", type=interval_seconds, default=0.5)
    s.set_defaults(func=m_dm.cmd_dm)

    s = sub.add_parser("chat", help="a person's seat: read the room and write `@id text`")
    s.add_argument("--as", dest="as_", required=True)
    s.add_argument("--interval", type=interval_seconds, default=1.0)
    s.add_argument("--stale", type=float, default=30.0, help="minutes after which a self-reported state is shown as stale")
    s.add_argument("--max-lines", type=int, default=12, help="cut longer messages; 0 = never")
    s.add_argument("--plain", action="store_true", help="old one-line format, no colours")
    s.set_defaults(func=m_chat.cmd_chat)

    for verb, text in (
        ("pause", "pause the room: agents get no messages until resume"),
        ("resume", "resume the room: waiting messages are delivered"),
    ):
        s = sub.add_parser(verb, help=text)
        s.set_defaults(func=m_lifecycle.cmd_pause, func_name=verb)

    s = sub.add_parser("clear", help="clear the room's history (it is moved to .collab/archive/)")
    s.add_argument("--yes", action="store_true", help="do not ask")
    s.set_defaults(func=m_lifecycle.cmd_clear)

    s = sub.add_parser("stop", help="stop the room: close its windows and the agents it started (history stays)")
    s.add_argument("--yes", action="store_true", help="do not ask")
    s.set_defaults(func=m_lifecycle.cmd_stop)

    s = sub.add_parser("remove", help="disconnect a participant (the conversation stays)")
    s.add_argument("id")
    s.set_defaults(func=cmd_remove)

    for verb, text in (
        ("mute", "deliver to a participant only what is addressed to it by name; it stays in the room"),
        ("unmute", "deliver new messages to a participant again (nothing missed is re-sent)"),
    ):
        s = sub.add_parser(verb, help=text)
        s.add_argument("id")
        s.set_defaults(func=cmd_mute, func_name=verb)

    s = sub.add_parser("unlimit", help="clear an agent's usage limit by hand: its messages are delivered again")
    s.add_argument("id")
    s.set_defaults(func=cmd_unlimit)

    s = sub.add_parser("add", help="start an agent in the running room — what /add does in the chat")
    s.add_argument("kind", choices=sorted(m_screen.PRESETS))
    s.add_argument("--folder", help="the project folder it works in (default: the room's)")
    s.add_argument("--name", help="its name in the room (default: Claude, Codex… — the next one free)")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("name", help="give a participant a name shown in the room")
    s.add_argument("id")
    s.add_argument("label")
    s.set_defaults(func=cmd_name)

    s = sub.add_parser("open", help="open an agent's window in this terminal tab (from the chat: /open)")
    s.add_argument("id")
    s.set_defaults(func=m_agents.cmd_open)

    s = sub.add_parser("show", help="print one message in full")
    s.add_argument("id")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("status", help="everyone's state (working / idle / needs you / lost), or set yours with --as")
    s.add_argument("--as", dest="as_")
    s.add_argument("state", nargs="?")
    s.add_argument("--note")
    s.add_argument("--stale", type=float, default=30.0)
    s.add_argument("--model", help="the model you run on, shown next to your name (e.g. «Opus 5.5»)")
    s.add_argument(
        "--if-room",
        action="store_true",
        help="for agent hooks: do nothing (exit 0) when there is no room here or the id is not in it",
    )
    s.add_argument(
        "--hook",
        action="store_true",
        help="read the Claude hook's JSON on stdin; ignore other sessions than the participant's own",
    )
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("deliver", help="type messages into idle tmux panes of participants with delivery 'tmux'")
    s.add_argument("--interval", type=interval_seconds, default=2.0)
    s.add_argument("--once", action="store_true")
    s.add_argument("--from-now", action="store_true", help="do not deliver messages that existed before start")
    s.add_argument("--idle-ok", action="store_true", help="keep running even if nobody needs tmux delivery yet")
    s.set_defaults(func=m_deliver.cmd_deliver)

    s = sub.add_parser("hook-model", help="for Claude Code hooks: take the model from the hook's JSON on stdin")
    s.add_argument("--as", dest="as_", default=os.environ.get("COLLAB_AS", "claude"))
    s.set_defaults(func=cmd_hook_model)

    s = sub.add_parser("usage", help="tokens spent, put down to tasks: usage report")
    usage = s.add_subparsers(dest="usage_cmd", required=True)
    u = usage.add_parser(
        "report",
        help="a table to plan by: spending per task kind, agent, model, lane or task (per task: in + out + cache write;"
        " answers: the model's; context: of one answer, mean and max)",
    )
    u.add_argument("--by", choices=sorted(m_ledger.GROUPS), default="kind")
    u.add_argument(
        "--since", help="UTC, as the journal writes it (2026-10-19, 2026-10-19T15:00:00+00:00): only spending ended after it"
    )
    u.add_argument("--format", choices=["md", "csv"], default="md")
    u.set_defaults(func=cmd_usage_report)
    add_room_option(usage)
    s = sub.add_parser("report", help="commit summary: hash, files, push state")
    s.add_argument("--repo")
    s.add_argument("--send-from", help="also post the summary to the room as this participant")
    s.add_argument("--to", action="append")
    s.add_argument("--note", help="extra line, e.g. checks and what they do not prove")
    s.set_defaults(func=cmd_report)
    add_room_option(sub)
    return ap


def add_room_option(sub: Any) -> None:
    """Every command can name its room with --room, after the command: an agent's
    reply with a «COLLAB_ROOT=… python3 …» prefix did not match a permission rule for
    «python3 …/collab say …», and the agent's safety filter refused it."""
    seen_parsers = set()
    for parser in sub.choices.values():
        if id(parser) in seen_parsers or "--room" in parser._option_string_actions:
            continue
        seen_parsers.add(id(parser))
        parser.add_argument("--room", help="the room's folder (default: COLLAB_ROOT, then here)")


def repeated_identity(argv: list[str]) -> str:
    """The first of --room / --from / --for given twice in one command, counting `--x=…` and
    abbreviations argparse would accept."""
    seen: dict[str, int] = {}
    for token in argv[1:]:
        name = token.split("=", 1)[0]
        if not name.startswith("--") or len(name) < 3:
            continue
        for full in ("--room", "--from", "--for"):
            if full.startswith(name):
                seen[full] = seen.get(full, 0) + 1
                if seen[full] > 1:
                    return full
    return ""


IDENTITY_FLAG = {"say": ("--from", "sender"), "ack": ("--for", "participant"), "inbox": ("--for", "participant")}


def rule_shape_differs(argv: list[str], args: argparse.Namespace) -> str:
    """A rule allows «<copy> say --room R --from P …» by its first words. What argparse made of the
    whole line must be exactly those R and P (a pre-check of our own could
    disagree with argparse — then the rule saw one room and the command used another)."""
    if len(argv) < 5 or argv[0] not in IDENTITY_FLAG or argv[1] != "--room":
        return ""
    flag, attr = IDENTITY_FLAG[argv[0]]
    if Path(args.room).expanduser().resolve() != Path(argv[2]).expanduser().resolve():
        return "--room"
    if argv[3] == flag and getattr(args, attr, None) != argv[4]:
        return flag
    return ""


def guard_room_state() -> None:
    """Run from the read-only copy a Codex rule names (outside the sandbox): refuse a room whose
    .collab.json or state is reached through a link — reads included."""
    rp = m_store.room().paths
    guarded = [rp.config_path, rp.state_dir]
    if rp.state_dir.is_dir() and not rp.state_dir.is_symlink():
        for base, dirs, files in os.walk(rp.state_dir):
            guarded += [Path(base) / x for x in dirs + files]
    for path in guarded:
        try:
            info = path.lstat()
        except OSError:
            continue
        # More than one name, not «not one»: a file caught in os.replace has none (see read_state_text).
        if stat.S_ISLNK(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1):
            m_store.fail(f"refusing: {path} is a link — the room's state must be its own files")


def main() -> None:
    """Bare `collab`: the home menu. Otherwise the command, after the checks a Codex rule relies on —
    no identity given twice, the line as argparse read it, the copy answering only say/ack/inbox."""
    if len(sys.argv) == 1:
        m_lifecycle.cmd_home(argparse.Namespace(session=None))
        return
    repeated = repeated_identity(sys.argv[1:])
    if repeated:
        # An allow rule matches «say --room R --from P» by its beginning: a second --room (or an
        # abbreviation, or --room=) later in the line would quietly win and send elsewhere.
        m_store.fail(f"{repeated} is given more than once")
    args = build_parser().parse_args()
    if getattr(args, "room", None):
        differs = rule_shape_differs(sys.argv[1:], args)
        if differs:
            m_store.fail(f"{differs}: the command line is ambiguous; give each of --room, --from/--for once, plainly")
        m_store.use_room(args.room)
    # The copy knows itself by its own file, not by the environment (a changed
    # COLLAB_SAFE_DIR switched the check off). And the check of links runs for every answer, copy
    # or not: for a plain room it costs nothing.
    if re.fullmatch(r"collab-[0-9a-f]{16}", Path(m_boot.SELF).name) and sys.argv[1:2] and sys.argv[1] not in IDENTITY_FLAG:
        m_store.fail("this copy of collab only answers in a room: say, ack, inbox")
    if sys.argv[1:2] and sys.argv[1] in IDENTITY_FLAG:
        guard_room_state()
    if args.func is m_chat.cmd_chat:
        m_chat.run_chat(args)
        return
    args.func(args)


if __name__ == "__main__":
    main()
