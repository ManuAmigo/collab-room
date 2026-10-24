from __future__ import annotations
import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
import collab.boot as m_boot
import collab.text as m_text
import collab.i18n as m_i18n
import collab.term as m_term
import collab.store as m_store
import collab.tmuxio as m_tmuxio
import collab.installer as m_installer
import collab.telegram as m_telegram
import collab.agents as m_agents
import collab.lifecycle as m_lifecycle

# `collab doctor`: what runs where, as text to paste for someone looking at another machine — versions, each room's
# chat, delivery, panel and Telegram and the copy each runs, the update's last lines. Never a secret, a key or a
# message's words; the home folder as ~. --fix offers, one yes/no each, what a stale room needs; agents untouched.
VERSION_LINE = re.compile(rb'^VERSION = "([0-9][0-9.]*)"$', re.M)
WINDOW_ROLES = ("chat", "deliver", "dm", "telegram")


def file_version(path: str) -> str:
    """The VERSION a collab file says it is; "" — not one."""
    try:
        with open(path, "rb") as f:
            head = f.read(1 << 20)
    except OSError:
        return ""
    m = VERSION_LINE.search(head)
    return m.group(1).decode() if m else ""


def quiet(argv: list[str]) -> str:
    """A program's own first line (its --version), or "" — not there, or no answer in 10 s."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return m_text.clean_field(((done.stdout or done.stderr).strip().splitlines() or [""])[0], 80)


def plugin_version() -> str:
    claude = shutil.which("claude")
    if not claude:
        return ""
    try:
        return m_installer.claude_plugin_version(claude) or ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def runs_in(pane_pid: str) -> list[tuple[str, str]]:
    """What collab runs under a window's process: (role, the collab file) — the chat, the deliverer, the panel, the
    room's Telegram."""
    if not pane_pid.isdigit():
        return []
    pids = [pane_pid, *subprocess.run(["pgrep", "-P", pane_pid], capture_output=True, text=True).stdout.split()]
    out: list[tuple[str, str]] = []
    for pid in pids:
        words = subprocess.run(["ps", "-ww", "-o", "command=", "-p", pid], capture_output=True, text=True).stdout.split()
        for i, word in enumerate(words[:-1]):
            if re.fullmatch(r"collab(-[0-9a-f]{16})?", Path(word).name) and words[i + 1] in WINDOW_ROLES:
                out.append((words[i + 1], word))
                break
    return out


def chat_file(cmd: Any) -> str:
    """The collab file a room's chat command starts."""
    if not m_store.is_chat_command(cmd):
        return ""
    at = 1 if re.fullmatch(r"python[0-9.]*", Path(cmd[0]).name) else 0
    return str(cmd[at])


def room_report(root: str, installed: str) -> tuple[list[str], dict[str, Any]]:
    """One room's lines, and what --fix could do for it."""
    m_store.use_room(root)
    rp = m_store.room().paths
    lines = [m_i18n.tr("Room {room}", room=m_text.tilde(root))]
    fix: dict[str, Any] = {"root": root, "chat_command": None, "stale": []}
    try:
        config = json.loads(rp.config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [*lines, m_i18n.tr("  its .collab.json cannot be read")], fix
    session = str(config.get("session") or "collab-room")
    lines.append(m_i18n.tr("  language: {code}", code=m_store.room_language(config)))
    owner = next((p for p in config.get("participants", []) if m_store.is_chat_command(p.get("command"))), None)
    if owner is not None:
        path = chat_file(owner["command"])
        version = file_version(path)
        if not m_store.trusted_chat_command(owner["command"]):
            # Not this collab nor its copy: started as written — from the folder the room was made in, for good.
            lines.append(
                m_i18n.tr(
                    "  ! its chat starts from {path}, version {v} (installed {installed}) — collab doctor --fix starts it from the installed collab",
                    path=m_text.tilde(path),
                    v=version or "?",
                    installed=installed,
                )
            )
            fix["chat_command"] = owner["id"]
    running = m_tmuxio.tmux("has-session", "-t", f"={session}", check=False).returncode == 0
    lines.append(m_i18n.tr("  running: {yes}", yes=m_i18n.tr("yes") if running else m_i18n.tr("no")))
    if running:
        panes = m_tmuxio.tmux("list-panes", "-s", "-t", f"={session}", "-F", "#{pane_id} #{pane_pid}", check=False).stdout
        for line in panes.splitlines():
            pane, _, pid = line.partition(" ")
            for role, path in runs_in(pid):
                version = file_version(path)
                old = bool(version) and version != installed
                lines.append(
                    m_i18n.tr(
                        "  {role}: {path}, version {v}{old}",
                        role=role,
                        path=m_text.tilde(path),
                        v=version or "?",
                        old=m_i18n.tr(" — older than the installed one") if old else "",
                    )  # fmt: skip
                )
                if old and role in ("chat", "deliver"):
                    fix["stale"].append((role, pane, session))
    tg = m_telegram.load()
    lines.append(
        m_i18n.tr("  Telegram: {state}", state=tg.get("state", "off") if tg else m_i18n.tr("not set up"))
    )  # the room's id and key are never shown
    return lines, fix


def doctor_report() -> tuple[str, list[dict[str, Any]]]:
    inst = m_boot.paths()
    installed_path = str(m_installer.installed_collab())
    installed = file_version(installed_path) or m_boot.VERSION
    lines = [
        m_i18n.tr("collab {v} — this one: {self}", v=m_boot.VERSION, self=m_text.tilde(m_boot.SELF)),
        m_i18n.tr("installed: {path}, version {v}", path=m_text.tilde(installed_path), v=installed),
        m_i18n.tr(
            "Claude Code: {v} · its collab-room plugin: {plugin}",
            v=quiet(["claude", "--version"]) or "—",
            plugin=plugin_version() or "—",
        ),
        m_i18n.tr("Codex: {v}", v=quiet(["codex", "--version"]) or "—"),
        "",
    ]
    fixes: list[dict[str, Any]] = []
    for root in m_agents.known_rooms():
        part, fix = room_report(root, installed)
        lines += [*part, ""]
        fixes.append(fix)
    log = inst.update_state.with_suffix(".log")
    try:
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
    except OSError:
        tail = []
    lines.append(m_i18n.tr("update.log, its last lines:") if tail else m_i18n.tr("update.log: none"))
    lines += ["  " + m_text.clean_field(m_text.tilde(x), 200) for x in tail]
    home = str(Path.home())
    text = "\n".join(m_text.visible(x).replace(home, "~") for x in lines)
    return text, fixes


def fix_room(fix: dict[str, Any]) -> list[str]:
    """What a stale room needs, each with the person's yes: its chat started from the installed collab, a stale chat
    or delivery restarted. Agents are never touched."""
    said: list[str] = []
    m_store.use_room(fix["root"])
    installed = str(m_installer.installed_collab())
    name = m_text.tilde(fix["root"])
    if fix["chat_command"] and m_term.confirm(
        m_i18n.tr("Room {room}: start its chat from the installed collab ({path})?", room=name, path=m_text.tilde(installed))
    ):
        data = m_store.read_config_raw()
        for p in data["participants"]:
            if p.get("id") == fix["chat_command"] and m_store.is_chat_command(p.get("command")):
                at = 1 if re.fullmatch(r"python[0-9.]*", Path(p["command"][0]).name) else 0
                p["command"] = [sys.executable, installed, *p["command"][at + 1 :]]
        m_store.write_config(data)
        said.append(m_i18n.tr("Room {room}: its chat starts from the installed collab now (from its next start).", room=name))
    for role, pane, session in fix["stale"]:
        if not m_term.confirm(m_i18n.tr("Room {room}: its {role} runs an older collab — restart it?", room=name, role=role)):
            continue
        if role == "deliver":
            m_lifecycle.start_deliverer(session)
        else:
            config = m_store.load_config()
            owner = next((p for p in config["participants"] if m_store.is_chat_command(p.get("command"))), None)
            if owner is None:
                continue
            cmd = m_store.current_chat_command(owner["command"])
            program = " ".join(shlex.quote(x) for x in [shutil.which(cmd[0]) or cmd[0], *cmd[1:]])
            m_tmuxio.tmux("respawn-pane", "-k", "-t", pane, m_tmuxio.in_dir(str(m_store.room().paths.root), program), check=False)
        said.append(m_i18n.tr("Room {room}: its {role} restarted.", room=name, role=role))
    return said


def cmd_doctor(args: argparse.Namespace) -> None:
    text, fixes = doctor_report()
    print(text)
    if not args.fix:
        return
    if not sys.stdin.isatty():
        m_store.fail(m_i18n.tr("collab doctor --fix asks before each change: run it in a terminal."))
    for fix in fixes:
        for line in fix_room(fix):
            print(line)
