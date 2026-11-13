from __future__ import annotations
import argparse
import contextlib
import select
import os
import re
import hashlib
import json
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib import request as urllib_request
import uuid
from dataclasses import asdict
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
import collab.usage as m_usage
import collab.installer as m_installer
import collab.vpn_macos_launch as m_vpn_macos_launch
import collab.vpn_macos_room as m_vpn_macos_room
import collab.vpn_macos_process as m_vpn_macos_process


def border_title(text: str) -> None:
    """Name this window on its top border (room_options draws @collab_title there)."""
    if os.environ.get("TMUX_PANE"):
        m_tmuxio.tmux("set-option", "-p", "-t", os.environ["TMUX_PANE"], "@collab_title", m_tmuxio.tmux_text(text), check=False)


@m_store.room_locked
def add_participant(entry: dict[str, Any]) -> None:
    """Add or replace a participant. It starts from now: messages from before it joined — or
    from while it was disconnected — are neither delivered nor shown as unread."""
    rp = m_store.room().paths
    old = {m["id"] for m in m_log.read_messages()}
    m_log.write_set(rp.delivered_dir, entry["id"], old)
    m_log.write_set(rp.receipts_dir, entry["id"], old)
    data = m_store.read_config_raw()
    data["participants"] = [p for p in data["participants"] if p["id"] != entry["id"]] + [entry]
    m_store.write_config(data)


# Whether a codex program's own help names --no-daemon, by its path, its file and the subcommand.
_NO_DAEMON: dict[tuple[str, ...], bool] = {}
CODEX_HELP_WAIT = 3  # seconds a codex has to answer `--help` before it is taken not to know --no-daemon


def codex_knows_no_daemon(program: str, sub: list[str]) -> bool:
    """Does this codex (or its `resume`) take --no-daemon? Its own help says — nothing else is run.
    Not said, or no answer in CODEX_HELP_WAIT seconds: no, and it starts as before rather than not at all."""
    try:
        st = os.stat(program)
    except OSError:
        return False
    key = (program, *sub, str(st.st_mtime_ns), str(st.st_size))
    if key not in _NO_DAEMON:
        try:
            r = subprocess.run(
                [program, *sub, "--help"], capture_output=True, text=True, timeout=CODEX_HELP_WAIT, stdin=subprocess.DEVNULL
            )
            _NO_DAEMON[key] = r.returncode == 0 and any(line.split()[:1] == ["--no-daemon"] for line in r.stdout.splitlines())
        except (OSError, subprocess.SubprocessError):
            _NO_DAEMON[key] = False
    return _NO_DAEMON[key]


class AgentCliError(RuntimeError):
    pass


AGENT_CLI_SOURCES = {
    "claude": ("https://claude.ai/install.sh", "bash"),
    "codex": ("https://chatgpt.com/codex/install.sh", "sh"),
    "kimi": ("https://code.kimi.com/kimi-code/install.sh", "bash"),
}
AGENT_CLI_LAUNCHERS = {
    "claude": ((".local", "bin", "claude"),),
    "codex": (
        (".local", "bin", "codex"),
        (".codex", "packages", "standalone", "current", "bin", "codex"),
        (".codex", "packages", "standalone", "current", "codex"),
    ),
    "kimi": ((".kimi-code", "bin", "kimi"),),
}
AGENT_CLI_VERIFY_TIMEOUT = 15
AGENT_CLI_INSTALL_TIMEOUT = 600
AGENT_CLI_DOWNLOAD_TIMEOUT = 60
AGENT_CLI_SCRIPT_LIMIT = 2_000_000


def agent_user_home() -> Path:
    return Path.home()


def agent_install_root(kind: str) -> Path:
    return agent_user_home() / ".local" / "share" / "collab-room" / "agent-cli" / kind / uuid.uuid4().hex


def verify_agent_cli(path: Path) -> None:
    try:
        result = subprocess.run(
            [str(path), "--version"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=AGENT_CLI_VERIFY_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AgentCliError(f"existing {path.name} is not working; it will not be replaced: {exc}") from exc
    if result.returncode != 0:
        detail = m_text.visible((result.stderr or result.stdout).strip()[:300])
        raise AgentCliError(f"existing {path.name} is not working; it will not be replaced: {detail}")


def use_agent_cli(path: Path) -> str:
    verify_agent_cli(path)
    path = path.expanduser().absolute()
    if shutil.which(path.name) != str(path):
        os.environ["PATH"] = str(path.parent) + os.pathsep + os.environ.get("PATH", "")
    return str(path)


def managed_agent_candidates(kind: str) -> list[Path]:
    base = agent_user_home() / ".local" / "share" / "collab-room" / "agent-cli" / kind
    if not base.is_dir() or base.is_symlink():
        return []
    roots = []
    for root in base.iterdir():
        if not root.is_dir() or root.is_symlink():
            continue
        path = installed_agent_path(kind, root)
        if os.path.lexists(path):
            roots.append((root.stat().st_mtime_ns, path))
    return [path for _, path in sorted(roots, reverse=True)]


def existing_agent_cli(kind: str) -> str:
    command = m_screen.PRESETS[kind]["command"][0]
    found = shutil.which(command)
    if found:
        return use_agent_cli(Path(found))
    for launcher in AGENT_CLI_LAUNCHERS[kind]:
        path = agent_user_home().joinpath(*launcher)
        if os.path.lexists(path):
            return use_agent_cli(path)
    for path in managed_agent_candidates(kind):
        return use_agent_cli(path)
    return ""


def sanitized_installer_path() -> str:
    dirs = []
    for folder in os.environ.get("PATH", os.defpath).split(os.pathsep):
        if not folder or folder in dirs:
            continue
        if any(os.path.lexists(Path(folder) / command) for command in AGENT_CLI_SOURCES):
            continue
        dirs.append(folder)
    return os.pathsep.join(dirs) or os.defpath


def installer_environment(kind: str, root: Path) -> dict[str, str]:
    home = root / "home"
    tmp = root / "tmp"
    env = {
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "XDG_CONFIG_HOME": str(root / "xdg" / "config"),
        "XDG_CACHE_HOME": str(root / "xdg" / "cache"),
        "XDG_DATA_HOME": str(root / "xdg" / "data"),
        "PATH": sanitized_installer_path(),
        "SHELL": "/bin/bash",
        "TERM": "dumb",
    }
    if kind == "codex":
        env.update(
            {
                "CODEX_HOME": str(home / ".codex"),
                "CODEX_INSTALL_DIR": str(root / "bin"),
                "CODEX_NON_INTERACTIVE": "1",
            }
        )
        env["PATH"] = str(root / "bin") + os.pathsep + env["PATH"]
    elif kind == "kimi":
        env.update({"KIMI_INSTALL_DIR": str(root / "install"), "KIMI_NO_MODIFY_PATH": "1"})
    for path in (home, tmp, root / "xdg" / "config", root / "xdg" / "cache", root / "xdg" / "data"):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return env


AGENT_DOWNLOAD_BOOTSTRAP = """
import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path
program, kind, root, source = sys.argv[1:]
if os.path.basename(program) in ("agents.py", "boot.py"):
    sys.path.insert(0, os.path.dirname(os.path.dirname(program)))
    from collab.agents import agent_download_worker
else:
    loader = importlib.machinery.SourceFileLoader("_collab_agent_download", program)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    agent_download_worker = module.agent_download_worker
agent_download_worker(kind, Path(root), source)
"""


def agent_download_worker(kind: str, root: Path, source: str) -> None:
    try:
        response = urllib_request.urlopen(source, timeout=20)
        try:
            content = bytearray()
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > AGENT_CLI_SCRIPT_LIMIT:
                    raise AgentCliError(f"the official {kind} installer is too large")
        finally:
            try:
                response.close()
            except (AttributeError, OSError, ValueError):
                pass
        (root / "installer.sh").write_bytes(content)
    except AgentCliError as exc:
        result = ("agent", str(exc))
    except (OSError, ValueError) as exc:
        result = ("download", str(exc))
    except BaseException as exc:
        result = ("internal", f"{type(exc).__name__}: {exc}")
    else:
        result = ("ok", "")
    print(json.dumps(result), flush=True)


def download_agent_script(kind: str, root: Path, cancel: Any = None) -> Path:
    source, _ = AGENT_CLI_SOURCES[kind]
    deadline = time.monotonic() + AGENT_CLI_DOWNLOAD_TIMEOUT

    def parse_result(line: bytes) -> tuple[str, str]:
        try:
            result = json.loads(line)
            if not isinstance(result, list) or len(result) != 2:
                raise ValueError("invalid downloader result")
            status, detail = result
            if status not in ("ok", "agent", "download", "internal") or not isinstance(detail, str):
                raise ValueError("invalid downloader result")
            return status, detail
        except (ValueError, TypeError) as exc:
            raise AgentCliError(f"could not download the official {kind} installer: invalid downloader response") from exc

    try:
        process = start_installer_process(
            [sys.executable, "-I", "-S", "-c", AGENT_DOWNLOAD_BOOTSTRAP, m_boot.PROGRAM_FILE, kind, str(root), source],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise AgentCliError(f"could not start the official {kind} installer download: {exc}") from exc
    stdout = process.stdout
    stderr = process.stderr
    assert stdout is not None and stderr is not None
    os.set_blocking(stdout.fileno(), False)
    os.set_blocking(stderr.fileno(), False)
    buffered = bytearray()
    eof = False

    def drain_stdout() -> None:
        nonlocal eof
        while True:
            try:
                chunk = os.read(stdout.fileno(), 65536)
            except BlockingIOError:
                return
            if not chunk:
                eof = True
                return
            buffered.extend(chunk)
            if len(buffered) > 65536:
                raise AgentCliError(f"could not download the official {kind} installer: invalid downloader response")

    def stderr_detail() -> str:
        content = bytearray()
        while True:
            try:
                chunk = os.read(stderr.fileno(), 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > 65536:
                del content[:-65536]
        return content.decode("utf-8", "replace").strip()

    try:
        while True:
            if cancel and cancel():
                raise AgentCliError("installation canceled")
            if time.monotonic() >= deadline:
                raise AgentCliError(f"{kind} download timed out")
            if select.select([stdout], [], [], 0.05)[0]:
                drain_stdout()
            if b"\n" in buffered:
                line, _, _ = buffered.partition(b"\n")
                result = parse_result(bytes(line))
                break
            if eof or installer_status(process) is not None:
                drain_stdout()
                if b"\n" in buffered:
                    line, _, _ = buffered.partition(b"\n")
                    result = parse_result(bytes(line))
                    break
                if buffered:
                    result = parse_result(bytes(buffered))
                    break
                if installer_status(process) is None:
                    raise AgentCliError(
                        f"could not download the official {kind} installer: downloader closed output without a result"
                    )
                detail = stderr_detail()
                raise AgentCliError(
                    f"could not download the official {kind} installer: downloader exited with status {installer_status(process)}: {m_text.visible(detail[-500:])}"
                )
        if cancel and cancel():
            raise AgentCliError("installation canceled")
        if time.monotonic() >= deadline:
            raise AgentCliError(f"{kind} download timed out")
        status, detail = result
        if status == "agent":
            raise AgentCliError(detail)
        if status != "ok":
            raise AgentCliError(f"could not download the official {kind} installer: {detail}")
        return root / "installer.sh"
    finally:
        stop_installer(process)
        stdout.close()
        stderr.close()


AGENT_PROCESS_ANCHOR = """
import json,os,signal,subprocess,sys
status = int(sys.argv[1])
signal.signal(signal.SIGTERM, lambda *_: None)
signal.signal(signal.SIGINT, lambda *_: None)
signal.signal(signal.SIGHUP, lambda *_: None)
try:
    child = subprocess.Popen(json.loads(sys.argv[2]), close_fds=True)
except OSError as error:
    os.write(status, ("E"+str(error.errno)+"\\n").encode())
    os.close(status)
    while True: signal.pause()
for descriptor in (0,1,2):
    try: os.close(descriptor)
    except OSError: pass
code = child.wait()
os.write(status, (str(code)+"\\n").encode())
os.close(status)
while True: signal.pause()
"""


def start_installer_process(command: list[str], **options: Any) -> subprocess.Popen[Any]:
    # A live, directly owned leader reserves the group even after the command exits.
    read_anchor_status, write_anchor_status = os.pipe()
    try:
        process: Any = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", AGENT_PROCESS_ANCHOR, str(write_anchor_status), json.dumps(command)],
            pass_fds=(write_anchor_status,),
            start_new_session=True,
            **options,
        )
    except BaseException:
        os.close(read_anchor_status)
        raise
    finally:
        os.close(write_anchor_status)
    os.set_blocking(read_anchor_status, False)
    process._collab_status_fd = read_anchor_status
    process._collab_status_bytes = b""
    process._collab_command_status = None
    return process


def installer_status(process: Any) -> int | None:
    if process._collab_command_status is not None:
        return process._collab_command_status
    try:
        chunk = os.read(process._collab_status_fd, 32)
    except BlockingIOError:
        return None
    if not chunk:
        raise AgentCliError("command anchor closed without a result")
    process._collab_status_bytes += chunk
    data = process._collab_status_bytes
    if len(data) > 16:
        raise AgentCliError("command anchor result rejected")
    if b"\n" not in data:
        return None
    if re.fullmatch(rb"E[0-9]{1,3}\n", data):
        error = int(data[1:])
        raise OSError(error, os.strerror(error))
    if not re.fullmatch(rb"-?[0-9]{1,3}\n", data):
        raise AgentCliError("command anchor result rejected")
    process._collab_command_status = int(data)
    return process._collab_command_status


def stop_installer(process: Any) -> None:
    if process.returncode is not None:
        return  # An already reaped PID must never authorize a group signal.
    try:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                states = m_vpn_macos_launch.mac_group_states(process.pid)
            except (OSError, ValueError, subprocess.SubprocessError):
                break  # Inspection is best effort; the final signal is mandatory.
            if not any(pid != process.pid and not state.startswith("Z") for pid, state in states.items()):
                break
            time.sleep(0.02)
    finally:
        try:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        finally:
            try:
                process.wait()
            finally:
                descriptor = getattr(process, "_collab_status_fd", None)
                if descriptor is not None:
                    os.close(descriptor)
                    process._collab_status_fd = None


def run_agent_installer(kind: str, root: Path, cancel: Any = None) -> None:
    script = download_agent_script(kind, root, cancel)
    _, shell = AGENT_CLI_SOURCES[kind]
    env = installer_environment(kind, root)
    stdout = root / "installer.out"
    stderr = root / "installer.err"
    with stdout.open("w", encoding="utf-8") as out, stderr.open("w", encoding="utf-8") as err:
        try:
            process = start_installer_process(
                [shell, str(script)],
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                text=True,
            )
        except OSError as exc:
            raise AgentCliError(f"could not start the official {kind} installer: {exc}") from exc
        deadline = time.monotonic() + AGENT_CLI_INSTALL_TIMEOUT
        status = None
        try:
            while (status := installer_status(process)) is None:
                if cancel and cancel():
                    raise AgentCliError("installation canceled")
                if time.monotonic() >= deadline:
                    raise AgentCliError(f"{kind} installation timed out")
                time.sleep(0.05)
        except (AgentCliError, KeyboardInterrupt) as exc:
            if isinstance(exc, KeyboardInterrupt):
                raise AgentCliError("installation canceled") from exc
            raise
        finally:
            stop_installer(process)
    if status != 0:
        detail = stderr.read_bytes()[-4000:].decode("utf-8", "replace").strip()
        raise AgentCliError(f"the official {kind} installer failed: {m_text.visible(detail[-500:])}")


def installed_agent_path(kind: str, root: Path) -> Path:
    if kind == "claude":
        return root / "home" / ".local" / "bin" / "claude"
    if kind == "codex":
        return root / "bin" / "codex"
    return root / "install" / "bin" / "kimi"


def ensure_agent_cli(kind: str, progress: Any = None, cancel: Any = None) -> str:
    """Find a working CLI or install only a missing one in an isolated root, never in the owner's profile."""
    existing = existing_agent_cli(kind)
    if existing:
        return existing
    root = agent_install_root(kind)
    m_store.private_dir(root)
    lang = m_i18n.SPOKEN["lang"]
    if progress:
        progress(m_i18n.words_in("add.download", lang, kind=kind))
    run_agent_installer(kind, root, cancel)
    if progress:
        progress(m_i18n.words_in("add.verify", lang, kind=kind))
    return use_agent_cli(installed_agent_path(kind, root))


def agent_command(kind: str, resume: list[str] | None = None, model: str = "", yolo: bool = False) -> str:
    """The same preset arguments for /add, relaunch and /restart, including resume, the model its
    settings give it and, for a Kimi that explicitly opted in, Ask When Needed."""
    preset = m_screen.PRESETS[kind]["command"]
    program = shutil.which(preset[0]) or preset[0]
    chosen = ["--model", model] if model and m_store.MODEL_NAME.fullmatch(model) else []
    if yolo and kind != "kimi":
        raise ValueError("--yolo is only for Kimi")
    if kind == "codex":
        # Its own process, not the shared background server (the person's Codex app keeps one): through
        # it a room's Codex did not take the rule written for it just before its start, and the person
        # approved every say by hand. Sandbox and approvals stay as they are.
        flags = ["--no-daemon"] if codex_knows_no_daemon(program, (resume or [])[:1]) else []
        if resume:
            return shlex.join([program, resume[0], *preset[1:], *flags, *chosen, *resume[1:]])
        return shlex.join([program, *preset[1:], *flags, *chosen])
    return shlex.join([program, *preset[1:], *chosen, *(["--yolo"] if yolo else []), *(resume or [])])


def model_for(p: dict[str, Any]) -> str:
    """The model an agent the room starts again runs on: by its settings' layers, as they are now. The room's
    file as it is, like the rest of a restart: each value is checked on its own (store.effective), and a
    restart is not stopped by someone else's seat."""
    return str(m_store.effective(m_screen.agent_kind(p), p, m_store.read_config_raw(), "use_model") or "")


@m_store.room_locked
def launch_new_agent(
    config: dict[str, Any],
    kind: str,
    folder: str,
    label: str,
    own: dict[str, Any] | None = None,
    seat: str = "",
    yolo: bool = False,
) -> str:
    """Start an agent in its own window of the room's tmux session and register it. `own`: the settings
    given it explicitly (store.SETTINGS) — only these are written into its seat; the rest go on by the
    layers below. `seat`: its seat in the room's scenario, whose settings it takes. `yolo`: its explicit
    Ask When Needed choice, preserved for later launches; only a Kimi may have one."""
    own = own or {}
    # The id and the name by the room's file as it is now, under its lock — not the chat's copy: two
    # /add at once (two chats, or /add and a join) got one id, and add_participant replaced the first
    # The session too: `start --session` writes it there, a chat started before kept the
    # old one, and the window went to a session that was not the room's.
    fresh = m_store.load_config()
    session = fresh.get("session", "collab-room")
    if m_store.label_taken(fresh, "", label):
        raise SystemExit(m_i18n.tr("that is already someone else's name here"))
    pid = m_text.slug(label, m_store.ids_of(fresh), kind)
    model = str(m_store.effective(kind, {"id": pid, **({"seat": seat} if seat else {}), **own}, fresh, "use_model") or "")
    cli = (
        m_installer.codex_room_rule(pid, folder, label) if kind == "codex" and not m_vpn_macos_room.mac_vpn_enabled(fresh) else ""
    )
    command = m_vpn_macos_room.mac_vpn_wrap(kind, agent_command(kind, model=model, yolo=yolo), fresh)
    pane = ""
    code = uuid.uuid4().hex[:4]
    token = m_usage.prepare_kimi_launch(pid, code, folder, m_usage.kimi_usage_adapter(command)) if kind == "kimi" else ""
    if token:
        command = m_usage.kimi_launch_command(command, pid, token)
    try:
        out = m_tmuxio.tmux(
            "new-window",
            "-d",
            "-P",
            "-F",
            "#{pane_id} #{pane_pid}",
            "-t",
            f"={session}:",
            "-n",
            pid,
            "-c",
            m_tmuxio.tmux_dir(folder),
            *m_tmuxio.pane_env(),
            m_tmuxio.in_dir(folder, command),
            check=False,
        )
        if out.returncode != 0:
            raise SystemExit(out.stderr.strip() or "tmux could not open a window")
        pane, _, pane_pid = out.stdout.strip().partition(" ")
        entry = {
            "id": pid,
            "label": label,
            **{k: v for k, v in m_screen.PRESETS[kind].items() if k not in ("label", "command")},
            "target": pane,
            "pane_pid": pane_pid,
            **m_vpn_macos_process.mac_vpn_process_fields(fresh, pane_pid),
            "delivery": "tmux",
            "joined": "pending",
            "joined_at": m_store.now(),
            "join_code": code,
            "started_by_room": True,
            "kind": kind,
            "folder": folder,
            **({"cli": cli} if cli else {}),
            **{k: v for k, v in own.items() if k in m_store.SETTINGS},
            **({"seat": seat} if seat else {}),
            **({"yolo": True} if yolo else {}),
        }
        if token:
            entry["kimi_usage_generation"] = token
        add_participant(entry)
        if token:
            m_usage.finish_kimi_launch(entry, token)
    except BaseException:
        # Half-started is undone: a rule for an id no one holds, or an agent running in
        # a window the room does not know of. The window is the one just opened, by its fresh id.
        if pane:
            m_tmuxio.tmux("kill-pane", "-t", pane, check=False)
        if cli:
            for spelling in m_installer.room_spellings():
                m_installer.set_agent_rule(spelling, pid, "")
        raise
    m_screen.write_status(pid, "joining", "", "self")
    if kind != "claude":
        m_log.append_message(m_text.ROOM, [pid], m_screen.ping_text(code))
    return pid


def room_folders(config: dict[str, Any]) -> list[str]:
    """Where the room's agents already work (their tmux panes' folders) — of a window that is still
    the agent's: a bare «%N» after a tmux restart is anyone's, and its folder was offered."""
    out = []
    for p in config.get("participants", []):
        if p.get("target") and p.get("pane", True) is not False and not m_screen.pane_guard(p, p["target"], idle=False):
            r = m_tmuxio.tmux("display-message", "-p", "-t", p["target"], "#{pane_current_path}", check=False)
            path = r.stdout.strip() if r.returncode == 0 else ""
            if path and path not in out:
                out.append(path)
    return out


def folder_rows(cur: Path) -> list[tuple[str, str]]:
    """One screen of the folder browser («browse the folders right in the menu; typing
    a path is the last resort»): use this one, go up, go into a subfolder, or type a path. Values:
    "use", "up", "in:<name>", "type", "denied:<name>". A folder that can't be opened says so
    (chosen anyway, its agent died at once with no reason given)."""
    rows = [(m_i18n.tr("✓ Use this folder — {p0}", p0=m_text.tilde(str(cur))), "use")]
    if cur.parent != cur:
        rows.append((m_i18n.tr("↑ Parent folder"), "up"))
    try:
        subs = sorted((d.name for d in cur.iterdir() if not d.name.startswith(".") and d.is_dir()), key=str.lower)
    except OSError:
        subs = []
    shown = {name: m_text.visible(name).replace("\n", " ") for name in subs}  # as in tilde()
    rows += [
        (f"  {shown[name]}/", "in:" + name)
        if os.access(cur / name, os.R_OK | os.X_OK)
        else (m_i18n.tr("  {p0} — no access", p0=shown[name]), "denied:" + name)
        for name in subs
    ]
    return rows + [(m_i18n.tr("Enter path manually…"), "type")]


def folder_problem(folder: Path) -> str:
    """Why an agent can't start in this folder, or "". Not only this process: the agent is started
    by the tmux server, and on macOS it may lack access this terminal has."""
    # The real path: tmux runs its check in its server's folder, not ours — a relative one was checked
    # somewhere else.
    folder = folder.resolve()
    if not folder.is_dir():
        return f"Folder not found: {m_text.tilde(str(folder))}"
    if not os.access(folder, os.R_OK | os.X_OK) or not m_tmuxio.tmux_can_read(folder):
        return f"Cannot open this folder: {m_text.tilde(str(folder))}. Choose another folder."
    return ""


def mark_welcomed() -> None:
    inst = m_boot.paths()
    try:
        m_store.private_dir(inst.welcomed_path.parent, strict=False)
        inst.welcomed_path.write_text(m_store.now() + "\n", encoding="utf-8")
    except OSError:
        pass


@m_store.room_locked
def remove_participant(pid: str) -> str:
    """Disconnect someone: the room stops delivering to them and forgets their state. The
    conversation and their window stay (an unused branch closed it by a bare %N)."""
    rp = m_store.room().paths
    data = m_store.read_config_raw()
    entry = next((p for p in data["participants"] if p["id"] == pid), None)
    if entry is None:
        raise ValueError(f"No participant {pid!r}")
    if pid == "owner" or m_store.is_chat_command(entry.get("command")):
        raise ValueError("The owner's seat can't be removed")
    label = entry.get("label") or pid
    data["participants"] = [p for p in data["participants"] if p["id"] != pid]
    m_store.write_config(data)
    if entry.get("cli"):
        for spelling in m_installer.room_spellings():
            m_installer.set_agent_rule(spelling, pid, "")  # its rule goes with it
    # status.json under the hooks' own lock too: removal held only the room's, and a hook writing
    # in between put the removed agent back or lost its own update.
    with m_screen.status_lock():
        for path in (rp.status_path, rp.await_path):
            state = m_store.read_json(path)
            if pid in state:
                state.pop(pid)
                m_store.write_json(path, state)
    m_log.room_line(["all"], "{label} disconnected from the room.", label=label)
    return label


@m_store.room_locked  # limits.json is the deliverer's too (hold_for_limit)
def clear_limit(pid: str) -> tuple[str, bool]:
    """«Clear the limit» by hand: its limit was raised, or ended early, and the agent sits
    idle and silent — the room would hold its messages until the recorded time. The time goes, the notice
    stays known: the same line still on its screen is not a new limit until something is typed and answered
    with it again (hold_for_limit). The people are told. Returns its name and whether it had a limit."""
    rp = m_store.room().paths
    config = m_store.load_config()
    entry = next((p for p in config["participants"] if p["id"] == pid), None)
    if entry is None:
        raise ValueError(f"No participant {pid!r}")
    limits = m_store.read_json(rp.limits_path)
    rec = m_screen.limit_of(limits, pid)
    label = entry.get("label") or pid
    if not rec.get("until") and not (rec.get("quota_code") == m_screen.KIMI_QUOTA_CODE and not rec.get("over")):
        return label, False
    rec.pop("until", None)
    rec["over"] = time.time()
    limits[pid] = rec
    m_store.write_json(rp.limits_path, limits)
    m_log.room_line(m_store.people_ids(config), "{label}'s usage limit was cleared by hand.", label=label)
    return label, True


@m_store.room_locked
def set_muted(pid: str, muted: bool) -> str:
    """Mute: the room stops delivering room-wide messages to this participant — messages addressed
    to it by name still arrive; it stays in the room and its state is shown as Muted. Unmute: it hears new messages again — what was said to everyone meanwhile is
    not re-sent; it stays in the room history for anyone to read."""
    rp = m_store.room().paths
    data = m_store.read_config_raw()
    entry = next((p for p in data["participants"] if p["id"] == pid), None)
    if entry is None:
        raise ValueError(f"No participant {pid!r}")
    if pid == "owner" or m_store.is_chat_command(entry.get("command")):
        raise ValueError("The owner's seat can't be muted")
    if muted:
        entry["muted"] = True
    elif entry.pop("muted", None):
        # Only what was said to everyone meanwhile is skipped: messages to it by name kept coming
        # and are its to read — unmute had marked them read and dropped their waits.
        seen = {m["id"] for m in m_log.read_messages() if m["from"] != pid and pid not in m["to"]}
        m_log.write_set(rp.delivered_dir, pid, m_log.read_set(rp.delivered_dir, pid) | seen)
        m_log.write_set(rp.receipts_dir, pid, m_log.read_set(rp.receipts_dir, pid) | seen)
        awaiting = m_store.read_json(rp.await_path)
        waits = awaiting.get(pid)
        waits = [waits] if isinstance(waits, dict) else waits or []
        keep = [w for w in waits if isinstance(w, dict) and w.get("id") not in seen]
        if keep != waits:
            if keep:
                awaiting[pid] = keep
            else:
                awaiting.pop(pid, None)
            m_store.write_json(rp.await_path, awaiting)
    m_store.write_config(data)
    return entry.get("label") or pid


def leave_the_room_window() -> None:
    """The chat is closing: whoever looks at its window leaves the room's session — back to the
    session they came from, or out of tmux. After Close chat tmux showed the next
    window, an agent's, and what the person typed next would have gone to the agent."""
    own = os.environ.get("TMUX_PANE")
    if not own:
        return
    session = m_tmuxio.tmux("display-message", "-p", "-t", own, "#{session_name}", check=False).stdout.strip()
    listed = m_tmuxio.tmux("list-clients", "-F", "#{client_name}\t#{pane_id}\t#{client_last_session}", check=False).stdout
    for row in listed.splitlines():
        client, pane, last = (row.split("\t") + ["", "", ""])[:3]
        if pane != own:
            continue
        if last and last != session:
            m_tmuxio.tmux("switch-client", "-c", client, "-t", f"={last}", check=False)
        else:
            m_tmuxio.tmux("detach-client", "-t", client, check=False)


def room_agent_pane(root: Path, pid: str) -> str | None:
    """The live tmux pane of `pid` in the room at `root` — None if it is not there or not in tmux."""
    p = next((q for q in m_store.read_json(m_boot.room_config(root)).get("participants", []) if q.get("id") == pid), None)
    if not p:
        return None
    if "target" in p:
        pane = p["target"] if p["target"].startswith("%") else m_tmuxio.exact(p["target"])
    else:
        pane = (m_store.read_json(m_boot.room_base(root) / ".collab" / "panes.json").get("panes") or {}).get(pid)
    # The same look as before typing: /answer, /mine and open went to a bare
    # «%N» another session had after a tmux restart.
    if not pane or m_screen.pane_guard(p, pane, idle=False, root=root):
        return None
    return pane


def rooms_with_agent(pid: str) -> list[Path]:
    # One room may be listed twice (as started, and among recent project folders): once here.
    return list(dict.fromkeys(Path(r).resolve() for r in known_rooms() if room_agent_pane(Path(r), pid)))


def open_command(pid: str) -> str:
    """What the person pastes into a new terminal tab. Short when only this room has the agent;
    with the room's folder when another room has the same name, or this room is not listed —
    the new tab opens in whatever folder the IDE likes."""
    rp = m_store.room().paths
    cli = "collab" if shutil.which("collab") else f"python3 {shlex.quote(m_boot.SELF)}"
    if rooms_with_agent(pid) == [rp.root.resolve()]:
        return f"{cli} open {pid}"
    return f"{cli} open {pid} --room {shlex.quote(str(rp.root.resolve()))}"


def agent_program(p: dict[str, Any]) -> str:
    """What runs in an agent's window, for the tab's name («Claude Code» / «Codex» with
    the agent's name): its kind when the room started it, else what its prompt looks like."""
    return {"claude": "Claude Code", "codex": "Codex"}.get(m_screen.agent_kind(p), "CollabRoom")


CODEX_VIEW_MARK = "@collab_codex_view"
CODEX_CLIPBOARD = "@collab_codex_clipboard"
CODEX_COPY_CONDITION = f"#{{==:#{{{CODEX_VIEW_MARK}}},1}}"
CODEX_COPY_PREFIX = f'if-shell -F "{CODEX_COPY_CONDITION}"'


def codex_clipboard_command() -> str:
    custom = os.environ.get("COLLAB_CLIPBOARD")
    try:
        candidates = [shlex.split(custom)] if custom else [["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]]
    except ValueError:
        return ""
    for command in candidates:
        if command and shutil.which(command[0]):
            return shlex.join(command)
    return ""


def codex_compound_lines(action: str) -> list[str]:
    if "\n" in action or "\r" in action:
        return []
    lines, start, index, quote = [], 0, 0, ""
    while index < len(action):
        if quote:
            if action[index] == "\\" and quote == '"':
                index += 2
                continue
            if action[index] == quote:
                quote = ""
        elif action[index] in "'\"":
            quote = action[index]
        elif action.startswith(r"\;\;", index):
            lines.append(action[start:index])
            index += 4
            start = index
            continue
        elif action[index] == "\\":
            index += 2
            continue
        index += 1
    if quote:
        return []
    lines.append(action[start:])
    return lines if all(line.strip() for line in lines) else []


def install_codex_copy_binding(table: str) -> None:
    result = m_tmuxio.tmux("list-keys", "-T", m_tmuxio.tmux_text(table), "MouseDragEnd1Pane", check=False)
    if result.returncode != 0 or len(result.stdout.splitlines()) != 1:
        return
    prefix = f"bind-key -T {table} MouseDragEnd1Pane "
    line = result.stdout.rstrip("\n")
    if not line.startswith(prefix):
        return
    action = line[len(prefix) :]
    if not action or action.startswith(CODEX_COPY_PREFIX):
        return
    lines = codex_compound_lines(action)
    if not lines:
        return
    source = (
        f'bind-key -T {table} MouseDragEnd1Pane if-shell -F "{CODEX_COPY_CONDITION}" {{\n'
        f' send-keys -X copy-pipe-and-cancel "#{{{CODEX_CLIPBOARD}}}"\n'
        "} {\n" + "\n".join(lines) + "\n}\n"
    )
    descriptor, name = tempfile.mkstemp(prefix="collab-codex-copy-", suffix=".conf")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as config:
            config.write(source)
        m_tmuxio.tmux("source-file", name, check=False)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(name)


def view_options(view: str, entry: dict[str, Any], label: str, back: str) -> list[str]:
    """The options of a view of an agent's window, as one tmux chain: the bottom line in the room's
    style — quiet grey, no list of windows, the agent's name and how to get back on
    the left, the time on the right; a narrow window cuts the hint before the time."""
    right = "#{?#{e|<:#{client_width},36},1,#{?#{e|<:#{client_width},60},8,19}}"
    # A long name gives way before the way back: cut at the end, the hint went first.
    name = label if m_text.text_cols(label) <= 40 else m_text.cut_cols(label, 39) + "…"
    codex = m_screen.agent_kind(entry) == "codex"
    chain: list[str] = []
    for option in (
        ("mouse", "on" if codex else "off"),
        ("status", "on"),
        ("status-left-length", "100"),
        ("set-titles", "on"),
        ("set-titles-string", f"{agent_program(entry)} · {m_tmuxio.tmux_text(label, clock=True)}"),
        ("status-left", f" {m_tmuxio.tmux_text(name, clock=True)} · {back} "),
        ("status-style", "bg=colour236,fg=colour250"),
        ("status-right", "#{?#{e|<:#{client_width},36},,#{?#{e|<:#{client_width},60},%H:%M ,%d.%m.%Y %H:%M }}"),
        (
            "status-format[0]",
            f"#[align=left]#{{T;=/#{{e|-:#{{client_width}},{right}}}/…:status-left}}#[align=right]#{{T:status-right}}",
        ),
    ):
        chain += ["set-option", "-t", f"={view}:", *option, ";"]
    if codex:
        # An alternate-screen Codex does not request mouse events; tmux's default wheel binding
        # forwards them as input-history arrows. Only its view captures the mouse, even for a
        # Codex started before inline mode was added. The room keeps native terminal selection.
        for table in ("copy-mode", "copy-mode-vi"):
            install_codex_copy_binding(table)
        chain += [
            "set-option",
            "-t",
            f"={view}:",
            CODEX_VIEW_MARK,
            "1",
            ";",
            "set-option",
            "-t",
            f"={view}:",
            CODEX_CLIPBOARD,
            codex_clipboard_command(),
            ";",
            "set-option",
            "-t",
            f"={view}:",
            "key-table",
            CODEX_VIEW_KEYS,
            ";",
            "bind-key",
            "-T",
            CODEX_VIEW_KEYS,
            "MouseDown1Pane",
            "select-pane",
            "-t",
            "=",
            ";",
            "bind-key",
            "-T",
            CODEX_VIEW_KEYS,
            "MouseDrag1Pane",
            "copy-mode",
            "-M",
            ";",
            "bind-key",
            "-T",
            CODEX_VIEW_KEYS,
            "WheelUpPane",
            "copy-mode",
            "-eu",
            ";",
            "bind-key",
            "-T",
            CODEX_VIEW_KEYS,
            "WheelDownPane",
            "display-message",
            "",
            ";",
            "bind-key",
            "-T",
            CODEX_VIEW_KEYS,
            "C-]",
            "detach-client",
            ";",
        ]
    return chain


CODEX_VIEW_KEYS = "collab-codex-view"
POPUP_KEYS = "collab-popup"  # a key table of its own: Ctrl+] there, the person's other sessions untouched


# The room's session of a window: the one a view marks, else its group (views made by older versions), else
# its own. tmux names a window by the session last looking at it — often a view.
ROOM_SESSION = f"#{{?{m_tmuxio.VIEW_OF},#{{{m_tmuxio.VIEW_OF}}},#{{?session_group,#{{session_group}},#{{session_name}}}}}}"
VIEW_HOLD = "collab-hold"


def make_view(view: str, window: str, session: str) -> list[str]:
    """The head of a tmux command that makes `view`, a session showing only `window` (linked, not
    grouped). A grouped view (`new-session -t`) gets a window of its own first, closed at once: with no
    command, a login shell, and one closed while it starts stays for good, stuck opening its closed
    terminal — every /open left one now and then (the stuck «-zsh»). Here the first window
    runs `sleep` itself, no shell, and goes once the agent's is in. The window is asked for in the same
    command: a failed command ends it there, nothing is made for a window that is gone."""
    return [
        "has-session", "-t", window, ";",
        "new-session", "-d", "-s", view, "-n", VIEW_HOLD, "sleep", "30", ";",  # two words: no shell
        "set-option", "-t", f"={view}:", m_tmuxio.VIEW_OF, session, ";",
        "link-window", "-s", window, "-t", f"={view}:", ";",
        "kill-window", "-t", f"={view}:={VIEW_HOLD}", ";",
    ]  # fmt: skip


def drop_stale_views(pane: str) -> None:
    """Views of this room's windows that nobody looks at: one is told to end with its last client only
    once attached, and a chat ended between making it and showing it left it for good — such views
    piled up. Older than a few seconds only: another chat may be opening its own."""
    origin = m_tmuxio.tmux("display-message", "-p", "-t", pane, ROOM_SESSION, check=False).stdout.strip()
    if not origin:
        return
    listed = m_tmuxio.tmux(
        "list-sessions", "-F", f"#{{session_name}}\t#{{session_attached}}\t#{{session_created}}\t{ROOM_SESSION}", check=False
    ).stdout.splitlines()
    for line in listed:
        name, attached, created, of = (line.split("\t") + ["", "", "", ""])[:4]
        if (
            name.startswith("collab-open-")
            and attached == "0"
            and of == origin
            and created.isdigit()
            and time.time() - int(created) > 10
        ):
            m_tmuxio.tmux("kill-session", "-t", f"={name}", check=False)


def open_popup(pid: str) -> str:
    """The agent's own window over the chat, at once (/open should open it, not hand
    out a command for a new tab): a view of its session in a tmux popup; typing there answers the
    agent; Ctrl+] closes it and the chat is where it was. Returns "" once the person closed it, or
    why it could not open — then the chat offers the command for a new tab, as before."""
    rp = m_store.room().paths
    if not os.environ.get("TMUX"):
        return "the chat is not inside tmux"
    if not m_tmuxio.tmux_at_least(3, 2):
        return "a window over the chat needs tmux 3.2 or newer"
    pane = room_agent_pane(rp.root, pid)
    if not pane:
        return "this agent has no window in tmux"
    drop_stale_views(pane)
    found = (
        m_tmuxio.tmux(
            "display-message", "-p", "-t", pane, f"{ROOM_SESSION}\t#{{window_id}}\t#{{pane_id}}\t#{{socket_path}}", check=False
        )
        .stdout.strip()
        .split("\t")
    )
    if len(found) != 4:
        return "its window could not be found"
    session, window, pane_id, socket = found
    entry = next((q for q in m_store.load_config().get("participants", []) if q.get("id") == pid), {})
    label = entry.get("label") or pid
    view = f"collab-open-{pid}-{uuid.uuid4().hex[:6]}"
    chain = make_view(view, window, session)
    chain += view_options(view, entry, label, "Ctrl+] — back to the room")
    chain += [
        "set-option",
        "-t",
        f"={view}:",
        "key-table",
        CODEX_VIEW_KEYS if m_screen.agent_kind(entry) == "codex" else POPUP_KEYS,
        ";",
        "bind-key",
        "-T",
        POPUP_KEYS,
        "C-]",
        "detach-client",
        ";",
        "select-window",
        "-t",
        f"={view}:{window}",
        ";",
        "select-pane",
        "-t",
        pane_id,
    ]
    made = m_tmuxio.tmux(*chain, check=False)
    if made.returncode != 0:
        return made.stderr.strip() or "tmux refused the view"
    # Its own client inside the popup, on the same server (-S), never through a shell: the person's
    # zsh read «=view» as the path of /usr/bin/view. Only attached, it is told to end with its last
    # client — a view not yet attached was ended at once. Waits until the popup is closed: no
    # timeout here, the person reads and answers as long as they need.
    shown = subprocess.run(
        [
            m_tmuxio.TMUX,
            "display-popup",
            "-E",
            "-w",
            "95%",
            "-h",
            "90%",
            "-T",
            f" {m_tmuxio.tmux_text(label)} ",
            "env",
            "-u",
            "TMUX",
            m_tmuxio.TMUX,
            "-S",
            socket,
            "attach",
            "-t",
            f"={view}",
            ";",
            "set-option",
            "-t",
            f"={view}:",
            "destroy-unattached",
            "on",
        ],
        text=True,
        capture_output=True,
    )
    if shown.returncode != 0:
        m_tmuxio.tmux("kill-session", "-t", f"={view}", check=False)
        return shown.stderr.strip() or "tmux could not show a window over the chat"
    m_tmuxio.tmux("kill-session", "-t", f"={view}", check=False)  # closed some other way: no view left behind
    return ""


def cmd_open(args: argparse.Namespace) -> None:
    """`collab open codex`: the agent's own window, whole, in this terminal tab — to answer it by hand
    (/open, «a full window with the agent», in a new WebStorm tab). The tab gets its own
    view of the agent's tmux session, so the chat's tab stays where it is; closing the tab ends only
    that view, the agent keeps running."""
    rp = m_store.room().paths
    if args.room:
        rooms = [rp.root] if room_agent_pane(rp.root, args.id) else []
    else:
        found = rooms_with_agent(args.id) + ([rp.root.resolve()] if room_agent_pane(rp.root, args.id) else [])
        rooms = list(dict.fromkeys(found))
    if not rooms:
        m_store.fail(
            m_i18n.tr("{id}: no window of this agent in a running room (an agent outside tmux cannot be opened)", id=args.id)
        )
    if len(rooms) > 1:
        m_store.fail(
            f"{args.id} is in several rooms; choose one:\n"
            + "\n".join(f"  collab open {args.id} --room {shlex.quote(str(r))}" for r in rooms)
        )
    root = rooms[0]
    pane = room_agent_pane(root, args.id) or ""
    found = (
        m_tmuxio.tmux("display-message", "-p", "-t", pane, f"{ROOM_SESSION}\t#{{window_id}}\t#{{pane_id}}", check=False)
        .stdout.strip()
        .split("\t")
    )
    if not pane or len(found) != 3:
        # Closed between finding it and now: said, not a traceback.
        m_store.fail(m_i18n.tr("{id}: its window is gone — nothing to open", id=args.id))
    session, window, pane_id = found
    entry = next((q for q in m_store.read_json(m_boot.room_config(root)).get("participants", []) if q.get("id") == args.id), {})
    label = entry.get("label") or args.id
    view = f"collab-open-{args.id}-{uuid.uuid4().hex[:6]}"
    if not (sys.stdin.isatty() and sys.stdout.isatty()) or m_tmuxio.args_no_attach():
        m_store.fail(m_i18n.tr("run it in a terminal tab: collab open {id}", id=args.id))
    # One tmux command, in this order: the view is made, the tab attaches to it, and only then it is
    # told to end with its last client — a view not yet attached was ended at once.
    chain = make_view(view, window, session)
    # Already inside tmux this client switches to it: a second one is not nested.
    chain += ["switch-client" if os.environ.get("TMUX") else "attach-session", "-t", f"={view}", ";"]
    # The tab's bottom line in the room's style: quiet grey, no list of windows, the
    # agent's name and how to get back on the left, the time on the right; a narrow window cuts the
    # hint before the time, and the time below 36 columns.
    chain += view_options(view, entry, label, "close this tab to go back to the room")
    chain += [
        "select-window",
        "-t",
        f"={view}:{window}",
        ";",
        "select-pane",
        "-t",
        pane_id,
        ";",
        "set-option",
        "-t",
        f"={view}:",
        "destroy-unattached",
        "on",
    ]
    os.execvp(m_tmuxio.TMUX, [m_tmuxio.TMUX, *chain])


@m_store.shared_locked
def remember_room() -> None:
    """Every started room is listed for `collab` with no arguments — newest first. Temporary
    folders are not: a test room in a scratch folder topped the person's menu."""
    inst = m_boot.paths()
    rp = m_store.room().paths
    if (str(rp.root) + "/").startswith(m_boot.TEMP_PREFIXES):
        return
    rooms = m_store.read_json(inst.rooms_path)
    rooms[str(rp.root)] = m_store.now()
    m_store.private_dir(inst.rooms_path.parent, strict=False)
    m_store.write_json(inst.rooms_path, rooms)


def known_rooms() -> list[str]:
    """Rooms that still exist: the ones started before, then project folders that have a room."""
    inst = m_boot.paths()
    rooms = m_store.read_json(inst.rooms_path)
    out = [
        p
        for p, _ in sorted(rooms.items(), key=lambda x: x[1], reverse=True)
        if m_boot.room_config(Path(p)).is_file() and not (p + "/").startswith(m_boot.TEMP_PREFIXES)
    ]
    # A folder an agent worked in counts as a room only by a room's own home: a .collab.json in the
    # project itself is anybody's — a Codex's folder with one it wrote was offered in the menu as «Open
    # room», and the first start ran what it held. Started by the person, it is above.
    out += [f for f in m_boot.recent_folders(12) if f not in out and (m_boot.room_home(Path(f)) / ".collab.json").is_file()]
    return out


def browse_folder(start: Path) -> str | None:
    """The launch menu's «Choose another project folder…»: the same browser as /add — into a
    folder, up, use this one; a typed path only as the last row. None: Esc."""
    cur = start
    while True:
        choice = m_term.pick(
            [m_i18n.tr("Folder: {p0}", p0=m_text.tilde(str(cur))), ""],
            folder_rows(cur),
            ["", m_i18n.tr("↑/↓ Move · Enter Open or select · Esc Back")],
            erase=True,
        )
        if choice is None:
            return None
        if choice == "use":
            return str(cur)
        if choice == "up":
            cur = cur.parent
        elif choice.startswith("in:"):
            cur = cur / choice[3:]
        elif choice.startswith("denied:"):
            print(m_i18n.tr("Cannot open this folder: {p0}. Choose another folder.", p0=m_text.tilde(str(cur / choice[7:]))))
        elif choice == "type":
            try:
                typed = m_term.ask_line(m_i18n.tr("Project folder: ")).strip()
            except EOFError:
                return None
            if Path(typed).expanduser().is_dir():
                return typed
            print(m_i18n.tr("Folder not found: {typed}", typed=typed))


def linked_worktree(folder: str) -> bool:
    """A second checkout of a repository (`git worktree add`): its .git is a file naming
    <repo>/.git/worktrees/<name>."""
    try:
        head = (Path(folder) / ".git").read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:  # no .git, or a folder (the main checkout)
        return False
    return head.startswith("gitdir:") and "/worktrees/" in head


@m_store.room_locked
def relaunch_agent(session: str, p: dict[str, Any]) -> str:
    """Start again an agent the room had started (its window is gone, e.g. after /stop), in its
    folder, as a fresh session: it joins anew through a connection check."""
    if "container" in p:
        m_store.fail(m_i18n.tr("Container VPN is frozen; remove legacy container settings before starting the room."))
    rp = m_store.room().paths
    kind = p.get("kind") or ("claude" if "❯" in p.get("idle", "") else "codex")
    folder = p.get("folder") or str(rp.root)
    launch_config = m_store.read_config_raw()
    cli = (
        m_installer.codex_room_rule(p["id"], folder)
        if kind == "codex" and not m_vpn_macos_room.mac_vpn_enabled(launch_config)
        else ""
    )
    code = uuid.uuid4().hex[:4]
    command = m_vpn_macos_room.mac_vpn_wrap(
        kind, agent_command(kind, model=model_for(p), yolo=bool(p.get("yolo"))), launch_config
    )
    token = m_usage.prepare_kimi_launch(p["id"], code, folder, m_usage.kimi_usage_adapter(command)) if kind == "kimi" else ""
    if token:
        command = m_usage.kimi_launch_command(command, p["id"], token)
    out = m_tmuxio.tmux(
        "new-window",
        "-d",
        "-P",
        "-F",
        "#{pane_id} #{pane_pid}",
        "-t",
        f"={session}:",
        "-n",
        p["id"],
        "-c",
        m_tmuxio.tmux_dir(folder),
        *m_tmuxio.pane_env(),
        m_tmuxio.in_dir(folder, command),
        check=False,
    )
    if out.returncode != 0:
        m_store.fail(m_i18n.tr("{p0}: could not start it again: {p1}", p0=p["id"], p1=out.stderr.strip()))
    pane, _, pane_pid = out.stdout.strip().partition(" ")
    data = m_store.read_config_raw()
    for q in data["participants"]:
        if q["id"] == p["id"]:
            q.update(target=pane, pane_pid=pane_pid, joined="pending", joined_at=m_store.now(), join_code=code)
            q.pop("confirmed_session", None)
            q.pop("window_session", None)
            q.update(m_vpn_macos_process.mac_vpn_process_fields(data, pane_pid))
            if token:
                q["kimi_usage_generation"] = token
                m_usage.finish_kimi_launch(q, token)
            q.pop("cli", None)
            q.update({"cli": cli} if cli else {})
    m_store.write_config(data)
    m_screen.write_status(p["id"], "joining", "", "self")
    if kind != "claude":  # a Claude tells its session itself (confirm_joins); a Codex is asked
        m_log.append_message(m_text.ROOM, [p["id"]], m_screen.ping_text(code))
    return pane


def scenario_unavailable(scenario: dict[str, Any]) -> list[str]:
    """The seats whose program is not on this machine, named: the team is not started without them, and no
    other program is put in their place unasked."""
    return [
        f"{seat['label']}: {m_screen.PRESETS[seat['kind']]['label']} is not installed"
        for seat in scenario["seats"]
        if not shutil.which(m_screen.PRESETS[seat["kind"]]["command"][0])
    ]


def start_scenario(name: str, folder: str, seats: list[dict[str, Any]] | None = None) -> tuple[list[str], list[str]]:
    """A ready team: the scenario is copied into the room's file, then each seat's agent is started the way /add
    starts one, with only its seat recorded — its settings go by the seat. `seats`: the team as the person changed it
    before the launch; checked like the scenario's own. (started ids, «name: why» of those that did not start). One
    that failed does not stop the rest."""
    scenario = m_store.scenario_of(name, list(m_screen.PRESETS))
    if scenario is None:
        raise ValueError(f"no ready team {name}")
    team_seats: list[dict[str, Any]] = json.loads(json.dumps(seats if seats is not None else scenario["seats"]))
    problems = m_store.scenario_problems({**scenario, "seats": team_seats}, list(m_screen.PRESETS))
    if problems:
        raise ValueError("; ".join(problems))
    team = {"scenario": name, "title": scenario["title"], "seats": team_seats, "output": scenario["output"], "without_person": []}
    m_store.set_team(team)
    started: list[str] = []
    failed: list[str] = []
    for seat in team_seats:
        config = m_store.load_config()
        label = next(
            n for n in [seat["label"]] + [f"{seat['label']} {i}" for i in range(2, 100)] if not m_store.label_taken(config, "", n)
        )
        try:
            pid = launch_new_agent(config, seat["kind"], folder, label, seat=seat["seat"])
        except SystemExit as exc:
            failed.append(f"{label}: {exc.code if isinstance(exc.code, str) else 'it did not start'}")
            continue
        started.append(pid)
        if seat["role"] == "lead":
            m_store.set_team({**m_store.team_of(m_store.load_config()), "lead": pid})
    return started, failed


def team_started(config: dict[str, Any], started: list[str], failed: list[str]) -> str:
    """One line for a team started: how many of how many, who leads — and each that did not start, with why.
    Not «all started» when some did not."""
    lead, lang = m_store.lead_of(config), m_store.room_language(config)
    total = m_i18n.words_in("n.agents", lang, n=len(started) + len(failed))
    head = m_i18n.words_in("team.started", lang, started=len(started), total=total) + (
        m_i18n.words_in("team.started.lead", lang, lead=m_store.label_of(config, lead))
        if lead
        else m_i18n.words_in("team.started.nolead", lang)
    )
    return "\n".join([head, *(f"  ! {m_text.visible(why)}" for why in failed)])


def apply_proposal(mid: str, by: str, digest: str = "", cancel: Any = None, progress: Any = None) -> list[str]:
    """The person's Enter on a proposal: its changes as one record of the history, its agents started the way /add
    starts them, its team started — what each did, in words. Checked again first: the room may have changed."""
    m = next((x for x in m_log.read_messages() if x.get("id") == mid and isinstance(x.get("proposal"), dict)), None)
    if m is None or m_store.proposal_state().get(mid) != "pending":
        raise ValueError("no such proposal waiting")
    if digest and m_store.proposal_digest(m["proposal"]) != digest:
        raise ValueError("it changed after it was shown — look at it again in /team")
    prop, data = m["proposal"], m_store.load_config()
    problem = m_store.proposal_problem(data, prop, list(m_screen.PRESETS))
    if problem:
        m_store.set_proposal_state(mid, "refused")  # no longer applies: it does not wait on for ever
        raise ValueError(problem)
    root = str(m_store.room().paths.root)
    lines = []
    if prop.get("changes"):
        rec, _ = m_store.apply_changes(prop["changes"], str(m.get("from")), str(m.get("re") or ""))
        lines.append(f"Saved: {m_store.change_words(m_store.load_config(), rec)}.")
    for a in prop.get("add") or []:
        folder = str(a.get("folder") or root)
        if why := folder_problem(Path(folder)):
            lines.append(f"! {a['label']}: {why}")
            continue
        own = {k: a[k] for k in m_store.SETTINGS if k in a}
        ensure_agent_cli(a["kind"], progress=progress, cancel=cancel)
        pid = launch_new_agent(
            m_store.load_config(), a["kind"], str(Path(folder).resolve()), a["label"], own, yolo=bool(a.get("yolo"))
        )
        lines.append(f"Starting {a['label']} in room window {pid}.")
    if prop.get("team"):
        started, failed = start_scenario(prop["team"], root)
        lines.append(team_started(m_store.load_config(), started, failed))
    m_store.set_proposal_state(mid, "applied")
    return lines


def agent_window_alive(q: dict[str, Any]) -> bool:
    """Its window is there and holds the process the room started in it."""
    target = q.get("target") or ""
    return bool(target) and not m_screen.pane_guard(q, target, idle=False)


def restart_agent(session: str, p: dict[str, Any], fresh: bool = False, sid: str = "") -> str:
    """Start an agent the room had started again, in its own window, continuing the same
    conversation (a Codex reads its rules only at start — after a new rule it went on
    asking the person for every answer, and a fresh agent lost what it was doing). The room
    remembers the new process of the window and checks the connection anew."""
    if "container" in p:
        m_store.fail(m_i18n.tr("Container VPN is frozen; remove legacy container settings before starting the room."))
    kind = m_screen.agent_kind(p)
    if not p.get("started_by_room") or kind not in ("claude", "codex", "kimi"):
        m_store.fail(m_i18n.tr("{p0}: only an agent the room started can be restarted", p0=p["id"]))
    # Only this agent's own conversation, by its exact id — never «the latest in the folder»: five
    # Claudes work in one folder. Not found, it is said; a new one only when asked for.
    # Looked for before the room's lock: up to 50 Codex logs of 20 MB were read holding it, and
    # delivery to every agent, say and ack all waited.
    if kind == "kimi":
        binding = m_usage.kimi_resume_binding(p, sid)
        if fresh or not binding:
            m_store.fail(m_i18n.tr("{p0}: no confirmed managed session and cursor; restart refused", p0=p["id"]))
        sid = binding["session_id"]
    else:
        sid = "" if fresh else (sid or m_screen.agent_session(p))
    if not sid and not fresh:
        m_store.fail(m_i18n.tr("{p0}: its conversation was not found", p0=p["id"]))
    return restart_in_window(session, p, kind, sid)


def _kimi_adoption_eligible(report: dict[str, Any]) -> bool:
    gates = report.get("gates", {})
    required = ("participant", "session_id", "metadata", "process", "hook", "checkpoint")
    return all(gates.get(gate) == "PASS" for gate in required) and gates.get("writer") != "FAIL"


def adopt_kimi_session(session: str, p: dict[str, Any], sid: str, execute: bool = False) -> dict[str, Any]:
    """Review an owner-selected existing session; only explicit consent reaches the managed restart."""
    report = m_installer.kimi_adoption_preflight(p, sid)
    report["adoption_eligible"] = _kimi_adoption_eligible(report)
    report["recovery_command"] = f"kimi --session {sid}" if m_usage.KIMI_PART.fullmatch(sid) else ""
    if not execute or not report["adoption_eligible"]:
        report["execution"] = "dry-run" if not execute else "refused"
        return report
    try:
        pane = restart_in_window(session, p, "kimi", sid, adoption=report)
    except (m_store.OperationFailed, SystemExit) as exc:
        report["execution"] = "failed"
        report["error"] = str(exc)
    else:
        report["execution"] = "started"
        report["pane"] = pane
    return report


def kimi_adoption_poststop(agent: str, token: str, old_pid: str, sid: str, dev: int, ino: int) -> bool:
    rp = m_store.room().paths
    proof = m_usage.obj(m_usage.kimi_state(rp.state_dir / "kimi-launches.json").get(token))
    adoption = m_usage.obj(proof.get("adoption"))
    resume = m_usage.obj(proof.get("resume_binding"))
    target = str(adoption.get("target") or "")
    old_start = str(adoption.get("old_start") or "")
    if (
        proof.get("id") != agent
        or proof.get("token") != token
        or not proof.get("generation")
        or proof.get("generation") == resume.get("generation")
        or resume.get("pane_pid") != old_pid
        or resume.get("target") != target
        or resume.get("session_id") != sid
        or adoption.get("old_pid") != old_pid
        or adoption.get("session_id") != sid
        or adoption.get("dev") != dev
        or adoption.get("ino") != ino
        or not target
        or not old_start
    ):
        m_store.fail(m_i18n.tr("{p0}: no confirmed managed session and cursor; restart refused", p0=agent))
    current_pid = str(os.getppid())
    shown = m_tmuxio.tmux("display-message", "-p", "-t", target, "#{pane_pid}", check=False)
    new_start = m_usage.kimi_process_start(current_pid)
    if shown.returncode or shown.stdout.strip() != current_pid or current_pid == old_pid or not new_start:
        m_store.fail(m_i18n.tr("{p0}: no confirmed managed session and cursor; restart refused", p0=agent))
    p = {
        "id": agent,
        "kind": "kimi",
        "pane_pid": old_pid,
        "target": target,
        "join_code": resume.get("generation"),
        "folder": proof.get("folder"),
        "started_by_room": True,
    }
    report = m_installer.kimi_adoption_preflight(p, sid, poststop=True)
    wire = report.get("snapshot", {}).get("wire", {})
    if not _kimi_adoption_eligible(report) or wire.get("dev") != dev or wire.get("ino") != ino:
        m_store.fail(m_i18n.tr("{p0}: no confirmed managed session and cursor; restart refused", p0=agent))
    return True


def _kimi_adoption_guard_command(command: str, p: dict[str, Any], sid: str, latest: dict[str, Any], token: str) -> str:
    wire = latest["snapshot"]["wire"]
    guard = shlex.join(
        [
            "env",
            "COLLAB_KIMI_ROOM=" + str(m_store.room().paths.root),
            "COLLAB_KIMI_AGENT=" + p["id"],
            "COLLAB_KIMI_GENERATION=" + token,
            "KIMI_CODE_HOME=" + str(m_usage.kimi_home().parent),
            sys.executable,
            str(m_boot.SELF),
            "usage",
            "kimi-adoption-guard",
            "--agent",
            p["id"],
            "--pid",
            str(p["pane_pid"]),
            "--session",
            sid,
            "--dev",
            str(wire["dev"]),
            "--ino",
            str(wire["ino"]),
            "--room",
            str(m_store.room().paths.root),
        ]
    )
    return shlex.join(["/bin/sh", "-c", guard + " && exec " + command])


@m_store.room_locked
def restart_in_window(session: str, p: dict[str, Any], kind: str, sid: str, adoption: dict[str, Any] | None = None) -> str:
    """restart_agent's part under the room's lock: the window, the room's file, the check."""
    rp = m_store.room().paths
    if adoption is not None and kind != "kimi":
        m_store.fail(m_i18n.tr("{p0}: no confirmed managed session and cursor; restart refused", p0=p["id"]))
    resume_binding: dict[str, Any] = {}
    latest: dict[str, Any] = {}
    adoption_cursor: dict[str, Any] = {}
    if kind == "kimi":
        current = next((q for q in m_store.read_config_raw().get("participants", []) if q.get("id") == p["id"]), {})
        if adoption is not None:
            latest = m_installer.kimi_adoption_preflight(current, sid)
            first_snapshot = adoption.get("snapshot", {})
            latest_snapshot = latest.get("snapshot", {})
            first_process = first_snapshot.get("process", {})
            latest_process = latest_snapshot.get("process", {})
            first_wire = first_snapshot.get("wire", {})
            latest_wire = latest_snapshot.get("wire", {})
            same_process = first_process == latest_process and bool(latest_process)
            same_wire = (
                first_wire.get("dev") == latest_wire.get("dev")
                and first_wire.get("ino") == latest_wire.get("ino")
                and bool(latest_wire)
            )
            if not _kimi_adoption_eligible(adoption) or not _kimi_adoption_eligible(latest) or not same_process or not same_wire:
                m_store.fail(m_i18n.tr("{p0}: no confirmed managed session and cursor; restart refused", p0=p["id"]))
            resume_binding = {
                "generation": current.get("join_code"),
                "pane_pid": current.get("pane_pid"),
                "target": current.get("target"),
                "sessions_root": str(m_usage.kimi_home()),
                "workdir_key": m_usage.kimi_workdir_key(os.path.realpath(str(current.get("folder") or rp.root))),
                "session_id": sid,
                "agent_id": "main",
                "adapter": m_usage.KIMI_NORMALIZED,
            }
        else:
            resume_binding = m_usage.kimi_resume_binding(current, sid)
        if (
            not sid
            or not resume_binding
            or any(current.get(k) != p.get(k) for k in ("target", "pane_pid", "join_code", "folder", "kimi_usage_generation"))
        ):
            m_store.fail(m_i18n.tr("{p0}: no confirmed managed session and cursor; restart refused", p0=p["id"]))
        p = current
    folder = p.get("folder") or str(rp.root)
    launch_config = m_store.read_config_raw()
    cli = (
        m_installer.codex_room_rule(p["id"], folder)
        if kind == "codex" and not m_vpn_macos_room.mac_vpn_enabled(launch_config)
        else ""
    )  # the newest rule, read at its start
    resume = (["--resume" if kind == "claude" else "--session", sid] if kind != "codex" else ["resume", sid]) if sid else []
    command = m_vpn_macos_room.mac_vpn_wrap(
        kind, agent_command(kind, resume, model_for(p), yolo=bool(p.get("yolo"))), launch_config
    )
    code = uuid.uuid4().hex[:4]
    token = ""
    if kind == "kimi":
        adapter = m_usage.kimi_usage_adapter(command)
        if adapter != resume_binding.get("adapter"):
            m_store.fail(m_i18n.tr("{p0}: no confirmed managed session and cursor; restart refused", p0=p["id"]))
        if adoption is not None and p["id"] in m_usage.kimi_state(rp.state_dir / "kimi-usage-cursor.json"):
            m_store.fail(m_i18n.tr("{p0}: no confirmed managed session and cursor; restart refused", p0=p["id"]))
        token = m_usage.prepare_kimi_launch(p["id"], code, folder, adapter, resume=resume_binding)
        if adoption is not None:
            wire = latest["snapshot"]["wire"]
            baseline = asdict(m_usage.Cursor(m_store.now()))
            baseline["files"] = {"wire": {"ino": wire["ino"], "off": wire["off"], "mid": 0}}
            baseline["binding"] = hashlib.sha256(json.dumps(resume_binding, sort_keys=True).encode()).hexdigest()
            baseline["observed"] = False
            cursor_path = rp.state_dir / "kimi-usage-cursor.json"
            state = m_usage.kimi_state(cursor_path)
            state[p["id"]] = baseline
            proofs_path = rp.state_dir / "kimi-launches.json"
            proofs = m_usage.kimi_state(proofs_path)
            proof = m_usage.obj(proofs.get(token))
            proof["adoption"] = {
                "old_pid": str(p["pane_pid"]),
                "old_start": latest["snapshot"]["process"]["start"],
                "target": p["target"],
                "session_id": sid,
                "dev": wire["dev"],
                "ino": wire["ino"],
            }
            proofs[token] = proof
            try:
                m_store.write_json(proofs_path, proofs)
                m_store.write_json(cursor_path, state)
            except (OSError, SystemExit):
                proofs = m_usage.kimi_state(proofs_path)
                proofs.pop(token, None)
                m_store.write_json(proofs_path, proofs)
                raise
            adoption_cursor = baseline
        command = m_usage.kimi_launch_command(command, p["id"], token)
        if adoption is not None:
            command = _kimi_adoption_guard_command(command, p, sid, latest, token)
    pane = p.get("target", "")
    # Its own window only: «respawn-pane -k» into a number tmux gave out again would kill someone
    # else's program.
    if adoption is not None and not agent_window_alive(p):
        if token:
            path = rp.state_dir / "kimi-launches.json"
            proofs = m_usage.kimi_state(path)
            proofs.pop(token, None)
            m_store.write_json(path, proofs)
        if adoption_cursor:
            cursor_path = rp.state_dir / "kimi-usage-cursor.json"
            state = m_usage.kimi_state(cursor_path)
            if state.get(p["id"]) == adoption_cursor:
                state.pop(p["id"], None)
                m_store.write_json(cursor_path, state)
        m_store.fail(m_i18n.tr("{p0}: no confirmed managed session and cursor; restart refused", p0=p["id"]))
    if agent_window_alive(p):
        out = m_tmuxio.tmux(
            "respawn-pane",
            "-k",
            "-t",
            pane,
            "-c",
            m_tmuxio.tmux_dir(folder),
            *m_tmuxio.pane_env(),
            m_tmuxio.in_dir(folder, command),
            check=False,
        )
    else:
        out = m_tmuxio.tmux(
            "new-window",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-t",
            f"={session}:",
            "-n",
            p["id"],
            "-c",
            m_tmuxio.tmux_dir(folder),
            *m_tmuxio.pane_env(),
            m_tmuxio.in_dir(folder, command),
            check=False,
        )
        pane = out.stdout.strip()
    if out.returncode != 0:
        if token:
            path = rp.state_dir / "kimi-launches.json"
            proofs = m_usage.kimi_state(path)
            proofs.pop(token, None)
            m_store.write_json(path, proofs)
        if adoption_cursor:
            cursor_path = rp.state_dir / "kimi-usage-cursor.json"
            state = m_usage.kimi_state(cursor_path)
            if state.get(p["id"]) == adoption_cursor:
                state.pop(p["id"], None)
                m_store.write_json(cursor_path, state)
        m_store.fail(m_i18n.tr("{p0}: could not restart it: {p1}", p0=p["id"], p1=out.stderr.strip()))
    pane_pid = m_tmuxio.tmux("display-message", "-p", "-t", pane, "#{pane_pid}", check=False).stdout.strip()
    data = m_store.read_config_raw()
    for q in data["participants"]:
        if q["id"] == p["id"]:
            q.update(target=pane, pane_pid=pane_pid, joined="pending", joined_at=m_store.now(), join_code=code)
            q.pop("confirmed_session", None)
            q.pop("window_session", None)
            q.update(m_vpn_macos_process.mac_vpn_process_fields(data, pane_pid))
            if token:
                q["kimi_usage_generation"] = token
                m_usage.finish_kimi_launch(q, token)
            q.pop("cli", None)
            q.update({"cli": cli} if cli else {})
    m_store.write_config(data)
    m_screen.write_status(p["id"], "joining", "", "self")
    if kind != "claude":  # a Claude tells its session itself (confirm_joins); a Codex is asked
        m_log.append_message(m_text.ROOM, [p["id"]], m_screen.ping_text(code))
    return pane
