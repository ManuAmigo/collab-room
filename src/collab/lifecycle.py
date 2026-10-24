from __future__ import annotations
import argparse
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
import collab.boot as m_boot
import collab.text as m_text
import collab.i18n as m_i18n
import collab.term as m_term
import collab.store as m_store
import collab.tmuxio as m_tmuxio
import collab.log as m_log
import collab.screen as m_screen
import collab.installer as m_installer
import collab.deliver as m_deliver
import collab.agents as m_agents


def default_team() -> dict[str, Any]:
    """The owner's seat plus every known agent that is installed on this machine."""
    rp = m_store.room().paths
    team = [
        {
            "id": "owner",
            "label": os.environ.get("USER", "Owner").capitalize(),
            "name_from_account": True,  # nobody chose it: the chat asks at its first opening
            "command": ["python3", m_boot.SELF, "chat", "--as", "owner"],
            "delivery": "pull",
        }
    ]
    for pid, preset in m_screen.PRESETS.items():
        if shutil.which(preset["command"][0]):
            team.append({"id": pid, **json.loads(json.dumps(preset))})
    return {"version": 2, "session": m_boot.room_session_name(rp.root), "participants": team}


def cmd_init(args: argparse.Namespace) -> None:
    rp = m_store.room().paths
    if rp.config_path.exists() and not args.force:
        m_store.fail(m_i18n.tr("{name} already exists (use --force to replace it)", name=rp.config_path.name))
    sample = default_team()
    m_store.write_room(sample, args.ask or (sys.stdin.isatty() and not args.no_ask))
    who = ", ".join(p["id"] for p in sample["participants"])
    print(
        m_i18n.tr(
            "Created the room ({who}) in {base}; the project folder is unchanged. Run `collab start`.", who=who, base=rp.base
        )
    )


def choose_language() -> None:
    """At the first interactive run, before anything else: one screen in both languages, the system's one under the
    cursor; Enter takes it. Saved as the person's default for new rooms. Not asked again, nor without a terminal."""
    if m_store.language_chosen() or not sys.stdin.isatty():
        return
    first = m_store.user_language()
    rows: list[tuple[str, Any]] = [(m_i18n.LANG_NAMES[x], x) for x in sorted(m_i18n.LANGS, key=lambda x: x != first)]
    choice = m_term.pick([m_i18n.words_in("lang.choose"), ""], rows, ["", "↑/↓ · Enter"])
    m_store.set_user_language(choice if choice in m_i18n.LANGS else first)


def cmd_home(args: argparse.Namespace) -> None:
    """`collab` with nothing else («make starting a room simple»). In a folder with
    a room — open it. Elsewhere — choose: a room you worked in, or a new room in a recent
    project; no paths to type, no environment to clear."""
    rp = m_store.room().paths
    choose_language()
    # What comes before a room — this menu, the folders, the name — in the language the person chose.
    m_i18n.speak(m_store.room_language(m_store.read_config_raw()) if rp.config_path.is_file() else m_store.user_language())
    if rp.config_path.is_file():
        cmd_start(args)
        return
    rooms = m_agents.known_rooms()
    # A second checkout of a project (git worktree) is the same project: not offered again (working
    # copies crowded the list).
    fresh = [f for f in m_boot.recent_folders(10) if f not in rooms and not m_agents.linked_worktree(f)]
    # The folder you are in comes first, under the cursor, above your rooms (a
    # newcomer's Enter opened the room listed first). Not home or / — a room there is a slip.
    here = os.path.realpath(str(rp.root))
    rows: list[tuple[str, Any]] = []
    if here not in (os.path.realpath(str(Path.home())), "/") and here not in map(os.path.realpath, rooms):
        fresh = [f for f in fresh if os.path.realpath(f) != here]
        rows += [(m_i18n.tr("THIS FOLDER"), None), (m_i18n.tr("New room — {p0}", p0=m_text.tilde(here)), ("new", here))]
    if rooms:
        rows += [(m_i18n.tr("YOUR ROOMS"), None)]
        rows += [(m_i18n.tr("Open room — {p0}", p0=m_text.tilde(r)), ("open", r)) for r in rooms]
    if fresh:
        rows += [(m_i18n.tr("CREATE A ROOM"), None)]
        rows += [(m_i18n.tr("New room — {p0}", p0=m_text.tilde(f)), ("new", f)) for f in fresh]
    rows += [(m_i18n.tr("Choose another project folder…"), ("other", ""))]
    while True:
        choice = m_term.pick(
            [m_i18n.tr("What would you like to do?"), m_i18n.tr("Choose an existing room, or create one for a project."), ""],
            rows,
            [
                "",
                m_i18n.tr("↑/↓ Move · Enter Select · Esc Cancel"),
                m_i18n.tr("A new room asks only for your name. Add agents later with /add."),
            ],
            erase=lambda v: v is None or v[0] == "other",
        )  # the chosen room stays on screen
        if choice is None:
            if not sys.stdin.isatty():
                print("\n".join(label for label, _ in rows))
                print(m_i18n.tr("Run `collab` in a terminal to choose, or `collab start` in a project folder."))
            return
        kind, folder = choice
        if kind != "other":
            break
        # Browse the folders in the menu; a typed path is the last row. Esc: back here.
        folder = m_agents.browse_folder(Path.cwd())
        if folder is not None:
            break
    m_store.use_room(folder)
    rp = m_store.room().paths
    if rp.config_path.is_file():
        if kind != "open":
            print(m_i18n.tr("A room already exists here. Opening it with its history."))
    else:
        print(m_i18n.tr("Creating a room in {p0}.", p0=m_text.tilde(str(rp.root))))
    cmd_start(args)


def start_deliverer(session: str) -> None:
    """The deliverer lives in the room's own hidden window: no second terminal to keep open, and
    it ends with the room. If the window is there, its program is replaced (respawn) — what was
    delivered is on disk, so nothing is typed twice. --idle-ok: a new room has only the person;
    agents arrive later through /add."""
    rp = m_store.room().paths
    cmd = f"python3 {shlex.quote(m_boot.SELF)} deliver --room {shlex.quote(str(rp.root))} --idle-ok"
    outside = deliverer_outside(session)
    if outside:
        # Started by hand outside the window, it holds the lock: the new one in the window would
        # stop at once, the window would go, and delivery stay on old code — silently.
        # The number is read from a file anyone writing the room's state can write: it is named for
        # «kill» only when that process is this room's deliverer.
        words = (
            subprocess.run(["ps", "-o", "command=", "-p", outside], capture_output=True, text=True).stdout.split()
            if outside.isdigit()
            else []
        )
        its_root = words[words.index("--room") + 1] if "--room" in words[:-1] else ""
        if "deliver" in words and its_root and os.path.realpath(its_root) == os.path.realpath(str(rp.root)):
            print(
                m_i18n.tr(
                    "A deliverer runs outside the room's window (pid {outside}) — stop it (kill {outside_}), then run `collab start` again.",
                    outside=outside,
                    outside_=outside,
                ),
                file=sys.stderr,
            )
        else:
            print(
                m_i18n.tr(
                    "A deliverer runs outside the room's window — find it (ps ax | grep 'collab deliver'), stop it, then run `collab start` again."
                ),
                file=sys.stderr,
            )
        return
    # `=deliver`, not `deliver`: tmux takes a window whose name only begins with it — an agent called
    # «Delivery» was respawned here with the deliverer.
    if m_tmuxio.tmux("list-panes", "-t", f"={session}:=deliver", check=False).returncode == 0:
        m_tmuxio.tmux(
            "respawn-window",
            "-k",
            "-t",
            f"={session}:=deliver",
            "-c",
            m_tmuxio.tmux_dir(str(rp.root)),
            *m_tmuxio.pane_env(),
            m_tmuxio.in_dir(str(rp.root), cmd),
            check=False,
        )
    else:
        m_tmuxio.tmux(
            "new-window",
            "-d",
            "-t",
            f"={session}:",
            "-n",
            "deliver",
            "-c",
            m_tmuxio.tmux_dir(str(rp.root)),
            *m_tmuxio.pane_env(),
            m_tmuxio.in_dir(str(rp.root), cmd),
            check=False,
        )


def deliverer_outside(session: str) -> str:
    """The pid of a deliverer that holds this room's lock from outside the room's deliver window,
    or "" — none runs, or it is the window's own (started with `exec`, it is the pane's process)."""
    rp = m_store.room().paths
    import fcntl

    path = rp.state_dir / "deliver.lock"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return ""
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return ""  # nobody holds it
        except OSError:
            pass
        pid = os.read(fd, 32).decode(errors="replace").strip()
    finally:
        os.close(fd)
    window = m_tmuxio.tmux("display-message", "-p", "-t", f"={session}:=deliver", "#{pane_pid}", check=False)
    here = (
        window.stdout.strip()
        if window.returncode == 0 and m_tmuxio.tmux("list-panes", "-t", f"={session}:=deliver", check=False).returncode == 0
        else ""
    )
    if not pid.isdigit():
        # A deliverer from before it wrote its pid: in the window, it is the window's own.
        return "" if here else "unknown"
    return "" if pid == here else pid


def restore_owner_pane(session: str, config: dict[str, Any]) -> None:
    """Bring the person's chat back into a running room when it was closed (/exit)."""
    rp = m_store.room().paths
    owner = next((p for p in config["participants"] if m_store.is_chat_command(p.get("command"))), None)
    if owner is None:
        return
    data = m_store.read_panes()
    if not data and rp.panes_path.exists():
        # Unreadable, not missing: which window the chat is in is not known, and a second chat would be
        # opened beside the person's own. Said once by read_panes; nothing done.
        return
    data = data or {"session": session, "panes": {}}
    pane = data.get("panes", {}).get(owner["id"])
    if pane and m_tmuxio.tmux("list-panes", "-t", pane, check=False).returncode == 0:
        return
    cmd = m_store.current_chat_command(owner["command"])
    cmd[0] = shutil.which(cmd[0]) or cmd[0]
    out = m_tmuxio.tmux(
        "new-window",
        "-P",
        "-F",
        "#{pane_id}",
        "-t",
        f"={session}:",
        "-n",
        "room",
        "-c",
        m_tmuxio.tmux_dir(str(rp.root)),
        *m_tmuxio.pane_env(),
        m_tmuxio.in_dir(str(rp.root), " ".join(shlex.quote(x) for x in cmd)),
        check=False,
    )
    if out.returncode == 0:
        data.setdefault("panes", {})[owner["id"]] = out.stdout.strip()
        m_store.replace_state(rp.panes_path, json.dumps(data, indent=2) + "\n")


def not_running(config: dict[str, Any]) -> None:
    """A running room is only returned to: an agent written into its settings since (by hand) has no
    window, and the start said nothing about it. Said in the terminal and to the room's people."""
    data = m_store.read_panes() or {}
    for p in config["participants"]:
        if "target" in p or not p.get("pane", True) or m_store.is_chat_command(p.get("command")):
            continue
        if p["id"] in m_store.people_ids(config):
            continue
        pane = data.get("panes", {}).get(p["id"])
        if pane and m_tmuxio.tmux("list-panes", "-t", pane, check=False).returncode == 0:
            continue
        rec = m_log.room_line(
            m_store.people_ids(config),
            "{name} is in the room's settings but not running: the room was running already, and start only returned to "
            "it — collab stop, then collab start starts it (history stays).",
            name=m_text.visible(m_store.label_of(config, p["id"])),
        )
        print(rec["text"])


@m_store.room_locked
def set_paused(paused: bool) -> None:
    """Pause: agents get no messages until resume; they finish what they are doing. Without it
    five agents talked on and on, and only a hand-closed window stopped them."""
    rp = m_store.room().paths
    data = m_store.read_config_raw()
    if paused:
        data["paused"] = True
    else:
        data.pop("paused", None)
    m_store.write_config(data)
    # The bottom line of the room shows the pause (room_options).
    m_tmuxio.tmux(
        "set-option", "-t", f"={data.get('session', 'collab-room')}:", "@collab_paused", "1" if paused else "0", check=False
    )
    if not paused and rp.held_path.exists():
        # Each line on its own, the file gone only after: a «42» in it once lost every message after
        # it, the file already deleted.
        for line in rp.held_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                h = json.loads(line)
            except ValueError:
                continue
            if (
                isinstance(h, dict)
                and all(isinstance(h.get(k), str) for k in ("from", "text"))
                and isinstance(h.get("to"), list)
                and all(isinstance(t, str) for t in h["to"])
            ):
                m_log.append_message(
                    h["from"],
                    h["to"],
                    h["text"],
                    h.get("re", "") if isinstance(h.get("re"), str) else "",
                    quiet=h.get("quiet") is True,
                    silent=h.get("silent") is True,
                )
        rp.held_path.unlink()
    if not paused:
        m_deliver.talk_reset()  # the person let the talk go on: counted anew, or the next word paused again


def cmd_pause(args: argparse.Namespace) -> None:
    set_paused(args.func_name == "pause")
    print(
        "The room is paused: agents get no messages until `collab resume`."
        if args.func_name == "pause"
        else "The room is resumed: waiting messages are delivered."
    )


def cmd_clear(args: argparse.Namespace) -> None:
    if not args.yes:
        if not sys.stdin.isatty():
            m_store.fail(m_i18n.tr("add --yes to clear the history without a question"))
        if not m_term.confirm(m_i18n.tr("Clear the room's history? It is moved to an archive, not deleted.")):
            print(m_i18n.tr("Canceled."))
            return
    print(m_i18n.tr("History moved to {p0}.", p0=m_log.archive_history()))


def stop_plan(config: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    """Who closes with the room (their windows are in its tmux session) and who keeps running
    (agents in windows of their own, e.g. a Codex in session «codex»). The session is the one the
    room actually runs in — `start --session` may have named it differently."""
    rp = m_store.room().paths
    started = m_store.read_json(rp.panes_path).get("session") if rp.panes_path.exists() else None
    session = started or config.get("session", "collab-room")
    inside = set(m_tmuxio.tmux("list-panes", "-s", "-t", f"={session}", "-F", "#{pane_id}", check=False).stdout.split())
    panes = m_deliver.current_panes(config)
    closing, staying = [], []
    for p in config["participants"]:
        pane = panes.get(p["id"], "")
        if pane in inside:
            closing.append(m_store.label_of(config, p["id"]))
        elif pane:
            staying.append(m_store.label_of(config, p["id"]))
    return session, closing, staying


def describe_stop(closing: list[str], staying: list[str]) -> str:
    # «the delivery process»: an agent named Delivery made «Closes: Owner, Delivery, and delivery.»
    text = "Closes: " + (", ".join(closing) if closing else "nothing but the room's windows") + ", and the delivery process."
    if staying:
        text += " Keep running in their own windows: " + ", ".join(staying) + "."
    return text + " The conversation and settings stay; `collab` opens the room again."


def cmd_stop(args: argparse.Namespace) -> None:
    """Stop a room on purpose («how do I stop the room?»): say what closes, ask,
    then end its tmux session. History and settings stay in the folder."""
    rp = m_store.room().paths
    config = m_store.load_config()
    session, closing, staying = stop_plan(config)
    if m_tmuxio.tmux("has-session", "-t", f"={session}", check=False).returncode != 0:
        print(m_i18n.tr("The room is not running."))
        return
    owner_root = m_tmuxio.session_room(session)
    if owner_root != os.path.realpath(str(rp.root)) and (owner_root or rp.base == rp.root):
        # Not this room's: `stop` in one «app» closed the other «app» with its agents. A
        # session without the room's mark is taken for this room's only by a name the room wrote itself,
        # in its home (rooms made by older versions run unmarked): from a room's files still in the
        # project — written there by an agent, maybe — it named the person's own, closed whole.
        whose = f"another room's: {m_text.tilde(owner_root)}" if owner_root else "not a room's"
        print(m_text.visible(f"The room is not running. The tmux session {session} is {whose}."))
        return
    print(describe_stop(closing, staying))
    if not args.yes:
        if not sys.stdin.isatty():
            m_store.fail(m_i18n.tr("add --yes to stop the room without a question"))
        if not m_term.confirm(m_i18n.tr("Stop the room? Agents started by the room will be closed.")):
            print(m_i18n.tr("Canceled."))
            return
    m_tmuxio.tmux("kill-session", "-t", f"={session}", check=False)
    print(m_i18n.tr("The room is stopped."))


def room_options(session: str) -> None:
    # Whose session this is: start and stop of another room with the same session name check it.
    rp = m_store.room().paths
    m_tmuxio.tmux("set-option", "-t", f"={session}:", "@collab_root", os.path.realpath(str(rp.root)), check=False)
    # Window options, set on the chat's own window: `={session}:` is the session's current window,
    # and after a restart of the deliverer that was «deliver» — the border over the chat stayed
    # tmux's own.
    m_tmuxio.tmux("set-option", "-w", "-t", f"={session}:=room", "pane-border-status", "top", check=False)
    # The border names the window in our words (tmux's own «0 "MacBook-Air.local"»
    # said nothing): the chat and its /dm panel set @collab_title, cleaned by tmux_text; a window
    # without one has a plain line — not the title a program inside chose for itself.
    m_tmuxio.tmux(
        "set-option", "-w", "-t", f"={session}:=room", "pane-border-format", "#{?@collab_title, #{@collab_title} ,}", check=False
    )
    # Mouse off for the room's session only: with the person's global `mouse on` tmux took the
    # drag, and text from the feed could not be selected and copied in WebStorm.
    m_tmuxio.tmux("set-option", "-t", f"={session}:", "mouse", "off", check=False)
    # The terminal tab is named after the room («Room (name)» or «CollabRoom»):
    # tmux passes this title to the terminal, and the status line starts with it.
    name = f"CollabRoom · {rp.root.name.lstrip('.') or rp.root.name}"
    m_tmuxio.tmux("set-option", "-t", f"={session}:", "set-titles", "on", check=False)
    m_tmuxio.tmux("set-option", "-t", f"={session}:", "set-titles-string", m_tmuxio.tmux_text(name, clock=True), check=False)
    # The line itself: only the room's name — «CollabRoom · collab-room» said the name twice; the
    # brand stays in the tab's title.
    # Cut only once, to the window's width (status-format below): cutting to 28 here as well gave
    # «……» from two cuts.
    title = rp.root.name.lstrip(".") or rp.root.name
    # «Paused» first, before the name: a narrow window cuts the end of the name, never the pause —
    # the only signal of the line (on the right, then after a long name, it was lost).
    m_tmuxio.tmux(
        "set-option",
        "-t",
        f"={session}:",
        "status-left",
        f"#{{?#{{==:#{{@collab_paused}},1}}, #[fg=colour214]⏸ Paused#[fg=colour250] ·,}} {m_tmuxio.tmux_text(title, clock=True)} ",
        check=False,
    )
    m_tmuxio.tmux("set-option", "-t", f"={session}:", "status-left-length", "60", check=False)
    # The bottom line, minimal: the room's name on the left; on the right «Paused»
    # while paused, then the date and time. No list of windows — «0:deliver 1:claude-3 2:room*» was
    # the room's kitchen, and «deliver» read like an agent. Quiet grey: green already means Ready.
    # Set for this session only — the person's other tmux sessions keep their own line.
    paused = "1" if m_store.config_now().get("paused") else "0"
    m_tmuxio.tmux("set-option", "-t", f"={session}:", "@collab_paused", paused, check=False)
    m_tmuxio.tmux("set-option", "-t", f"={session}:", "status-style", "bg=colour236,fg=colour250", check=False)
    # Date and time on the right; a narrow window gives up the date first (below 60 columns),
    # then the time (below 36).
    m_tmuxio.tmux(
        "set-option",
        "-t",
        f"={session}:",
        "status-right",
        "#{?#{e|<:#{client_width},36},,#{?#{e|<:#{client_width},60},%H:%M ,%d.%m.%Y %H:%M }}",
        check=False,
    )
    m_tmuxio.tmux("set-option", "-t", f"={session}:", "status-right-length", "40", check=False)
    # The left side gets what the right one leaves: window width minus «01.01.2026 22:09 » (18),
    # «22:09 » (7) or nothing — a fixed cut ran into the time at 40–45 columns.
    right = "#{?#{e|<:#{client_width},36},1,#{?#{e|<:#{client_width},60},8,19}}"
    m_tmuxio.tmux(
        "set-option",
        "-t",
        f"={session}:",
        "status-format[0]",
        f"#[align=left]#{{T;=/#{{e|-:#{{client_width}},{right}}}/…:status-left}}#[align=right]#{{T:status-right}}",
        check=False,
    )


def move_room_home(args: argparse.Namespace) -> None:
    """Move a room that an older version kept inside its project out of it. Its running deliverer and the
    person's chat are stopped first — they would go on writing into the old place and split the
    log; the start below brings both back. Nothing is executed on the way: the old
    .collab.json may have been changed by an agent, so start commands it holds other than the
    person's chat are shown, and asked about."""
    rp = m_store.room().paths
    old_config, old_state = rp.root / ".collab.json", rp.root / ".collab"
    home = m_boot.room_home(rp.root)
    if old_config.is_symlink() or old_state.is_symlink():
        m_store.fail(
            m_i18n.tr(
                "refusing to move the room: {old_config} or {old_state} is a link", old_config=old_config, old_state=old_state
            )
        )
    if (home / ".collab.json").exists() or (home / ".collab").exists():
        m_store.fail(m_i18n.tr("refusing to move the room: {home} is already there — nothing was changed", home=home))
    # Read once, from the checked file itself, and what is shown is what is written to the new home:
    # a second name of .collab.json made in the project moved along with the file and let an agent
    # edit the moved room for good; a link put in place while the person answered was
    # moved instead of the file shown.
    raw = project_file_bytes(old_config)
    try:
        data = json.loads(raw.decode("utf-8")) if raw is not None else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    # Its shape before anything reads it: «participants»: [1] fell over with a traceback.
    if not isinstance(data.get("participants", []), list) or not all(isinstance(p, dict) for p in data.get("participants", [])):
        m_store.fail(m_i18n.tr("not moved: .collab.json in the project is not a room's file — check it and run `collab` again"))
    # Everyone but the person's own chat, as this program writes it. One without a command still
    # gets a window from start — a bare shell — and with tmux delivery the room typed a message into
    # it, run by the shell. Only a seat that opens nothing (pane: false) is not asked about.
    unusual = [
        p
        for p in data.get("participants", [])
        if "target" in p or (not m_store.trusted_chat_command(p.get("command")) and (p.get("command") or p.get("pane", True)))
    ]
    if unusual:
        # A target is a window delivery types into: one pointed at the person's chat would make
        # an agent's words look typed there.
        print(m_i18n.tr("This room's file holds start commands besides your chat, or windows to type into:"))
        for p in unusual:
            if "target" in p:
                what = f"types into window {p['target']}" + (f" ({' '.join(map(str, p['command']))})" if p.get("command") else "")
            elif p.get("command"):
                what = " ".join(map(str, p["command"]))
            else:
                what = "opens a shell window" + (" and types messages into it" if p.get("delivery") == "tmux" else "")
            # Anybody's text, shown right before the yes/no: no control codes.
            print("  " + m_text.visible(f"{p.get('id')}: {what}").replace("\n", " "))
        if not sys.stdin.isatty() or not m_term.confirm(m_i18n.tr("Keep them and start them?")):
            m_store.fail(m_i18n.tr("not moved; check .collab.json in the project and run `collab` again"))
    session = data.get("session", "collab-room")
    if m_tmuxio.tmux("has-session", "-t", f"={session}", check=False).returncode == 0:
        # The session and the windows come from the project's files, which an agent may write: they
        # named the person's own session, and its window was closed. Only a session
        # this room marked as its own is touched, and in it only its own windows.
        if m_tmuxio.session_room(session) != os.path.realpath(str(rp.root)):
            # No command to close it in this text: the name comes from a file an agent may have written,
            # and «close it» would have the person close their own session.
            m_store.fail(
                m_text.visible(
                    f"not moved: .collab.json in the project names the tmux session {session}, which runs and "
                    "is not this room's — nothing was changed. Check that file; if that session is this room "
                    "started by an older collab, close the room's windows yourself, then run `collab` again"
                )
            )
        inside = set(m_tmuxio.tmux("list-panes", "-s", "-t", f"={session}", "-F", "#{pane_id}", check=False).stdout.split())
        m_tmuxio.tmux("kill-window", "-t", f"={session}:=deliver", check=False)
        owner_pane = m_store.read_json(old_state / "panes.json").get("panes") or {}
        for p in data.get("participants", []):
            if m_store.is_chat_command(p.get("command")) and owner_pane.get(p["id"]) in inside:
                m_tmuxio.tmux("kill-pane", "-t", owner_pane[p["id"]], check=False)
        time.sleep(0.5)
    m_store.private_dir(home, strict=False)
    skipped: list[str] = []
    if old_state.exists():
        try:
            src = os.open(str(old_state), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            m_store.fail(m_i18n.tr("refusing to move the room: {old_state} is a link", old_state=old_state))
        try:
            os.mkdir(home / ".collab", 0o700)
            copy_own_files(src, home / ".collab", skipped)
        finally:
            os.close(src)
        if not skipped:  # what was not moved stays where it was, as said below
            try:
                shutil.rmtree(old_state)  # link-safe here (shutil.rmtree.avoids_symlink_attacks)
            except OSError as exc:
                print(m_i18n.tr("The old {old_state} was not removed: {strerror}", old_state=old_state, strerror=exc.strerror))
    if raw is not None:
        fd = os.open(str(home / ".collab.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        old_config.unlink()
    m_store.use_room(str(rp.root))
    print(m_i18n.tr("Room files moved out of the project: {p0}", p0=m_text.tilde(str(home))))
    if skipped:
        print(
            f"Left in {m_text.tilde(str(old_state))} — links or files with a second name, not the room's own: "
            + ", ".join(skipped)
        )
        if ".collab/messages.jsonl" in skipped:
            print(m_i18n.tr("The room's history was among them: the moved room starts without it."))


def project_file_bytes(path: Path) -> bytes | None:
    """A file in the project, read through one descriptor: not a link, a regular file with one name.
    None when there is no such file."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        m_store.fail(m_i18n.tr("refusing to move the room: {path} is a link", path=path))
    with os.fdopen(fd, "rb") as fh:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            m_store.fail(m_i18n.tr("refusing to move the room: {path} is a link or has a second name", path=path))
        return fh.read()


def copy_own_files(src_fd: int, dst: Path, skipped: list[str], where: str = ".collab") -> None:
    """The room's state, copied file by file from an open folder: links and files with a second name
    stay behind and are named — os.replace moved them along."""
    for name in sorted(os.listdir(src_fd)):
        info = os.stat(name, dir_fd=src_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            inner = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=src_fd)
            try:
                os.mkdir(dst / name, 0o700)
                copy_own_files(inner, dst / name, skipped, f"{where}/{name}")
            finally:
                os.close(inner)
            continue
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=src_fd)
            with os.fdopen(fd, "rb") as fh:
                if os.fstat(fh.fileno()).st_nlink == 1:
                    out = os.open(str(dst / name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(out, "wb") as oh:
                        oh.write(fh.read())
                    continue
        skipped.append(f"{where}/{name}")


@m_store.room_locked
def refresh_codex_rules() -> None:
    """A Codex the room started answers through a read-only copy of collab named in its rule and
    hints. After collab changed (or the room moved out of the project) that copy is old — it
    looked for the room in the project. Each start gives such agents the current copy and rule; a
    running Codex reads rules at its start, so it needs a restart to use them."""
    rp = m_store.room().paths
    if not rp.config_path.is_file():
        return
    data = m_store.read_config_raw()
    changed = False
    for q in data.get("participants", []):
        if q.get("kind") == "codex" and q.get("started_by_room") and q.get("cli"):
            if m_agents.agent_window_alive(q):
                # Running, it keeps the copy its rule names: a new rule reaches it only at its next
                # start, and new hints meanwhile matched no rule — every answer asked the person
                # The chat says it runs an old version; /restart gives it the new.
                continue
            cli = m_installer.codex_room_rule(q["id"], q.get("folder") or str(rp.root))
            if cli != q["cli"]:
                if cli:
                    q["cli"] = cli
                else:
                    q.pop("cli", None)
                changed = True
    if changed:
        m_store.write_config(data)


def open_room_windows(
    session: str,
    config: dict[str, Any],
    own: list[dict[str, Any]],
    relaunch: list[dict[str, Any]],
    gone: list[tuple[dict[str, Any], str]],
    panes: dict[str, str],
) -> list[str]:
    """A new room's windows: the session with its own seats, the agents it starts again, panes.json.
    Returns the agents delivered to in windows of their own."""
    rp = m_store.room().paths
    # «"layout": "tiled"» in the room's settings: every seat a pane of the chat's window, as rooms opened before.
    tiled = config.get("layout") == "tiled"
    for i, p in enumerate(own):
        cwd = str((rp.root / p.get("cwd", ".")).resolve())
        # The program is the pane's own process, not a line typed into a login shell: a login
        # shell rewrites PATH from the person's rc files, and the room would start a different
        # `codex` than the one it found (a test launched the real Codex that way).
        program = ""
        if p.get("command"):
            cmd = m_store.current_chat_command(p["command"])
            cmd[0] = shutil.which(cmd[0]) or cmd[0]
            program = " ".join(shlex.quote(x) for x in cmd)
        run_cmd = [m_tmuxio.in_dir(cwd, program)]
        if i == 0:
            out = m_tmuxio.tmux(
                "new-session",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-s",
                session,
                "-n",
                "room",
                "-c",
                m_tmuxio.tmux_dir(cwd),
                *m_tmuxio.pane_env(),
                *run_cmd,
            )
        # People's chats stay together in the chat's window: the clients of one session share its
        # current window, and a second person's chat in a window of its own was not where they were.
        elif tiled or m_store.is_chat_command(p.get("command")):
            out = m_tmuxio.tmux(
                "split-window",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                f"{session}:room",
                "-c",
                m_tmuxio.tmux_dir(cwd),
                *m_tmuxio.pane_env(),
                *run_cmd,
            )
            m_tmuxio.tmux("select-layout", "-t", f"{session}:room", "tiled")
        else:
            # Each agent in a window of its own, as /add opens one: four panes in one window were a maze for a
            # person who knew no tmux keys, and the chat is seen whole. The name never a room window's own.
            name = p["id"] if p["id"] not in (m_text.ROOM, "deliver") else f"{p['id']}-agent"
            out = m_tmuxio.tmux(
                "new-window",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                f"={session}:",
                "-n",
                name,
                "-c",
                m_tmuxio.tmux_dir(cwd),
                *m_tmuxio.pane_env(),
                *run_cmd,
            )
        pane = out.stdout.strip()
        panes[p["id"]] = pane
        # -T is a tmux format: an agent's own name «#(…)» ran a command at start on tmux 3.4.
        m_tmuxio.tmux("select-pane", "-t", pane, "-T", m_tmuxio.tmux_text(p.get("label", p["id"])), check=False)
        if i == 0:
            # `new-session -e` also writes COLLAB_ROOT into the whole tmux session: a terminal
            # opened there later inherited this room, and starting another room needed
            # `env -u COLLAB_ROOT`. Each room pane gets it through pane_env().
            m_tmuxio.tmux("set-environment", "-u", "-t", f"={session}", "COLLAB_ROOT", check=False)

    # Named apart: an agent started again in a new window was told as one «delivered to» where it was.
    kept = [p["id"] for p in config["participants"] if "target" in p and p not in relaunch and all(p is not q for q, _ in gone)]
    for p in relaunch:
        panes[p["id"]] = m_agents.relaunch_agent(session, p)
    room_options(session)
    m_store.private_dir(rp.state_dir)
    m_store.replace_state(rp.panes_path, json.dumps({"session": session, "panes": panes}, indent=2) + "\n")
    os.chmod(rp.panes_path, 0o600)
    return kept


def cmd_start(args: argparse.Namespace) -> None:
    rp = m_store.room().paths
    if shutil.which(m_tmuxio.TMUX) is None:
        m_store.fail(m_i18n.tr("tmux is required: {p0}, and run `collab start` again", p0=m_tmuxio.tmux_install_hint()))
    venv = m_tmuxio.leave_venv()
    if venv:
        print(m_i18n.tr("The active venv {p0} is left out of the room: its windows run without it.", p0=m_text.tilde(venv)))
    m_log.owner_key(create=True)  # installed before this feature or removed: the room makes its own
    if not all(c.isprintable() for c in str(rp.root)):
        # The room's path is in every hint typed into an agent's window: a «\r» in a folder's name pressed
        # Enter there mid-message, an ESC was a key.
        m_store.fail(
            m_i18n.tr("the project folder's path has control characters: {p0} — rename the folder", p0=m_text.tilde(str(rp.root)))
        )
    if not rp.config_path.is_file():
        # First start in this project: create the room right here — with the person only.
        # It used to seat and start every installed agent («maybe I won't have
        # Codex there at all») and ask names for «codex» and «claude», not for real agents.
        # Agents come in through /add: which one, which folder, what name.
        choose_language()
        team = default_team()
        team["participants"] = team["participants"][:1]
        team["language"] = m_store.user_language()  # the person's default; a room made before speaks English
        m_store.write_room(team, sys.stdin.isatty())
        m_store.replace_state(m_store.offer_team_path(), m_store.now() + "\n")  # «What will we do?» at its first chat
        print(
            m_i18n.tr("Room created for {p0}. Pick a ready team or add agents in the chat.", p0=team["participants"][0]["label"])
        )
    if rp.base == rp.root:
        move_room_home(args)
        rp = m_store.room().paths
    m_store.record_room_root()
    refresh_codex_rules()
    m_installer.update_in_background(str(rp.root))
    config = m_store.load_config()
    if not m_tmuxio.tmux_can_read(rp.root):
        m_store.fail(
            m_i18n.tr(
                "tmux cannot open {p0}: macOS keeps this folder private from the program that started tmux (Documents, Desktop and Downloads are protected), so every window of the room would close at once — the «[exited]» you saw. Use a project folder outside them, or allow access in System Settings → Privacy & Security → Files and Folders.",
                p0=m_text.tilde(str(rp.root)),
            )
        )
    m_agents.remember_room()
    session = getattr(args, "session", None) or config.get("session", "collab-room")
    if session != config.get("session", "collab-room"):
        # `start --session X` ran the room as X, but /add and /restart read the room's file and opened
        # their windows in the session named there — not this one. The name is the room's now.
        if not m_store.WORD.fullmatch(session):
            m_store.fail(m_i18n.tr("--session must be 1 to 64 letters, digits, _ and -"))
        config["session"] = session
        m_store.write_config(config)
    owner_root = (
        m_tmuxio.session_room(session) if m_tmuxio.tmux("has-session", "-t", f"={session}", check=False).returncode == 0 else ""
    )
    if owner_root and owner_root != os.path.realpath(str(rp.root)):
        # Another room's session under the same name (two folders «app»): this room gets a name of
        # its own instead of walking into the other's windows.
        session = m_boot.room_session_name(rp.root)
        config["session"] = session
        m_store.write_config(config)
        print(
            m_i18n.tr(
                "The tmux session name was another room's ({p0}); this room now uses {session}.",
                p0=m_text.tilde(owner_root),
                session=session,
            )
        )
    # `=name` is the exact name: without it tmux finds a session by its beginning ("room" would match "room-old").
    if m_tmuxio.tmux("has-session", "-t", f"={session}", check=False).returncode == 0:
        # Running already: return to it. Agents and the queue stay as they are; the deliverer is
        # restarted in place (it was left on old code — new hints never reached a running room), and the
        # person's chat comes back if /exit closed it.
        if not os.environ.get("COLLAB_NO_AUTODELIVER"):
            start_deliverer(session)
        room_options(session)
        restore_owner_pane(session, config)
        not_running(config)
        # The person lands on the chat: restarting the deliverer made its window the current one,
        # and every terminal of the room showed an empty screen.
        m_tmuxio.tmux("select-window", "-t", f"={session}:=room", check=False)
        m_tmuxio.attach(session)
        return
    panes: dict[str, str] = {}
    own = [p for p in config["participants"] if "target" not in p and p.get("pane", True)]
    relaunch = []
    gone: list[tuple[dict[str, Any], str]] = []
    for p in config["participants"]:
        if "target" in p:
            why = (
                "does not exist"
                if m_tmuxio.tmux("list-panes", "-t", m_tmuxio.exact(p["target"]), check=False).returncode != 0
                else m_screen.pane_guard(p, m_tmuxio.exact(p["target"]), idle=False)
            )
            if why:
                if p.get("started_by_room"):
                    # The room started it, so the room starts it again: after /stop its window
                    # is gone, and the room did not open at all.
                    relaunch.append(p)
                else:
                    # A window the person runs on their own (e.g. a Codex in its session): gone, the room still
                    # opens — it did not at all, chat and delivery to the others included; now it opens
                    # always, the agent disconnected, with one line saying what to do.
                    gone.append((p, why))
                continue
            panes[p["id"]] = m_tmuxio.exact(p["target"])
    if not own:
        m_store.fail(m_i18n.tr("nobody needs a pane here; add a participant without `target` (e.g. the owner's chat)"))
    try:
        kept = open_room_windows(session, config, own, relaunch, gone, panes)
    except BaseException:
        # Half started — a session with the chat, but no panes.json and no deliverer: the next start
        # took it for a running room, started nobody again and opened a second chat.
        # The session did not exist before this start: what is there now is this start's own.
        m_tmuxio.tmux("kill-session", "-t", f"={session}", check=False)
        raise
    print(
        f"Started {session!r}: {', '.join(p['id'] for p in own)}."
        + (f" Delivering to existing panes: {', '.join(kept)}." if kept else "")
        + (f" Started again: {', '.join(p['id'] for p in relaunch)}." if relaunch else "")
    )
    for p, why in gone:
        name = m_text.visible(m_store.label_of(config, p["id"]))
        m_screen.write_status(p["id"], "lost", why, "screen")
        rec = m_log.room_line(
            m_store.people_ids(config),
            "{name}: its window {target} — {why}. It gets no messages until its window is there again; /remove {name} "
            "takes it out of the room.",
            name=name,
            target=m_text.visible(str(p["target"])),
            why=m_text.visible(why.rstrip(".")),
        )
        print(rec["text"])
    if True:  # always: agents added later through /add need the deliverer too
        if os.environ.get("COLLAB_NO_AUTODELIVER"):
            print(m_i18n.tr("Run `collab deliver` (any terminal) so tmux participants receive their messages."))
        else:
            # The deliverer lives in the room's own hidden window: no second terminal to keep open,
            # and it ends with the room.
            start_deliverer(session)
    m_tmuxio.tmux("select-pane", "-t", panes[own[0]["id"]], check=False)
    m_tmuxio.attach(session)
