#!/bin/sh
""":"
exec python3 -I -S -c 'import sys;f=sys.argv[1];s=open(f,"rb").read();exec(s[s.index(b"# fast-start"+b":begin"):s.index(b"# fast-start"+b":end")]);_fast_start(f,s)' "$0" "$@"
collab-room: local message room and tmux launcher for teams of people and AI agents.

The room is a local append-only log (.collab/messages.jsonl). Every participant is a
stable `id` from .collab.json; the number of participants is not fixed.

Delivery is explicit and per participant:
  - "pull": the participant reads its inbox itself (Claude Code does it with a
    background watch, a person with `collab chat`);
  - "tmux": `collab deliver` types the message into the participant's tmux pane,
    but only when the pane looks idle (the participant's `idle` regex matches the
    visible screen). It sends the text and one Enter — never Esc, Ctrl-C or any
    other key — so it cannot cancel a running turn or answer an approval prompt.
"""

from __future__ import annotations

# fast-start:begin
import contextlib
import hashlib
import importlib.util
import json
import marshal
import os
import re
import stat
import sys
import tempfile
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# Python compiles a script whole on every run — two thirds of a short command's time — and keeps the
# compiled code only for modules it imports. Started as a command (the `collab` on PATH, Claude's hooks), the
# file's first lines are sh: they run this block alone, which takes the compiled program from the person's
# own cache, or compiles it and keeps it there. `python3 collab` reads those lines as the docstring and
# compiles as before. Only the standard library, nothing of the program: it runs before the program exists,
# on whatever python3 is first on PATH (3.9 too). -I: neither the current folder nor PYTHON* settings reach
# the imports — a json.py where the command is run would otherwise be the json the program imports
# (PYTHONUTF8, PYTHONWARNINGS… are not read either, as in the hand-over to a newer Python).
# The key is the file's content, the interpreter's version and bytecode magic: a changed file is a
# new key, never stale code. The cache is the person's own folder, closed to everyone else and never in a
# temporary folder (a HOME there is one Codex may write); a file there is taken only when it is theirs, no one
# else may write it and its code matches the hash in its head — compiled code runs as it is, unchecked, and
# marshal crashes the interpreter on some damaged data, every run, rather than raise.
# Anything wrong with the cache (missing, broken, foreign) — compiled as without it, never an error.

# Temporary folders: Codex writes there by default (workspace-write); no room, agent or Python lives there.
TEMP_FOLDERS = ("/private/", "/tmp/", "/var/folders/")


def in_temp_folder(real: str) -> bool:
    """A real path in a temporary folder — where Codex writes by default. A fixed list: no setting
    loosens a security check. Case-folded on macOS: its disks ignore case and realpath keeps the case it
    was given — /PRIVATE/TMP/x is /private/tmp/x. No temporary folder to be found: counted as one."""
    fold = (lambda x: x.casefold()) if sys.platform == "darwin" else (lambda x: x)
    try:
        temp = os.path.realpath(tempfile.gettempdir())
    except OSError:
        return True
    return fold(real + "/").startswith(tuple(fold(t) for t in (*TEMP_FOLDERS, temp + "/")))


FAST_KEEP = 16  # compiled versions kept: one per content, a developer's edits make many
FAST_TMP_AGE = 600  # seconds: a temporary file older than that is a write that was killed


def fast_folder() -> str:
    return os.path.join(os.path.expanduser("~"), ".collab-room", "code")


def _fast_safe_folder(folder: str) -> bool:
    """A real folder (not a link), this user's, with no rights for anyone else; its parent this user's
    too and writable by no one else — else the folder could be swapped beneath us."""
    try:
        parent, own = os.lstat(os.path.dirname(folder)), os.lstat(folder)
    except OSError:
        return False
    return (
        stat.S_ISDIR(parent.st_mode)
        and stat.S_ISDIR(own.st_mode)
        and parent.st_uid == own.st_uid == os.getuid()
        and not parent.st_mode & 0o022
        and not own.st_mode & 0o077
        and not in_temp_folder(os.path.realpath(folder))
    )


def _fast_head(key: str, code: bytes) -> bytes:
    return b"collab code " + key.encode() + b" " + hashlib.sha256(code).hexdigest().encode() + b"\n"


def _fast_cached(folder: str, key: str, path: str) -> Any:
    """The compiled program kept under `key`, or None."""
    if not _fast_safe_folder(folder):
        return None
    try:
        fd = os.open(os.path.join(folder, key), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        try:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022 or st.st_nlink != 1:
                return None
            head, _, data = f.read().partition(b"\n")
        except OSError:  # a disk that fails the read: compiled as without the cache
            return None
        if head + b"\n" != _fast_head(key, data):
            return None
        try:
            code = marshal.loads(data)
        except Exception:  # whatever marshal raises on data that passed the hash
            return None
        if not isinstance(code, types.CodeType):
            return None
        with contextlib.suppress(OSError):
            os.utime(f.fileno())  # in use: the last to be cleaned away
    # The same content at another path (a clone and its installed copy): tracebacks name the file run.
    return code if code.co_filename == path else _fast_named(code, path)


def _fast_named(code: Any, path: str) -> Any:
    consts = tuple(_fast_named(c, path) if isinstance(c, types.CodeType) else c for c in code.co_consts)
    return code.replace(co_filename=path, co_consts=consts)


def _fast_keep(folder: str, key: str, code: Any) -> None:
    """Kept for the next run — written whole under a temporary name, then put in place; the oldest
    beyond FAST_KEEP go."""
    with contextlib.suppress(FileExistsError, OSError):
        os.mkdir(os.path.dirname(folder), 0o700)
    with contextlib.suppress(FileExistsError, OSError):
        os.mkdir(folder, 0o700)
    if not _fast_safe_folder(folder):
        return
    tmp = os.path.join(folder, f".{key}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as f:
            data = marshal.dumps(code)
            f.write(_fast_head(key, data) + data)
        os.replace(tmp, os.path.join(folder, key))
        names = os.listdir(folder)
        kept = [os.path.join(folder, n) for n in names if len(n) == 64 and not n.startswith(".")]
        kept.sort(key=lambda n: os.lstat(n).st_mtime)
        left = [os.path.join(folder, n) for n in names if n.startswith(".") and n.endswith(".tmp")]
        for old in kept[:-FAST_KEEP] + [n for n in left if os.lstat(n).st_mtime < time.time() - FAST_TMP_AGE]:
            os.unlink(old)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def _fast_start(path: str, source: bytes) -> None:
    """Run the program in `source` (the file at `path`) as `python3 path` would."""
    key = hashlib.sha256(
        importlib.util.MAGIC_NUMBER + sys.version.encode() + bytes([sys.flags.optimize]) + b"\0" + source
    ).hexdigest()
    folder = fast_folder()
    code = _fast_cached(folder, key, path)
    if code is None:
        code = compile(source, path, "exec", dont_inherit=True)
        _fast_keep(folder, key, code)
    sys.argv[:] = sys.argv[1:]
    main = sys.modules["__main__"].__dict__
    for name in [n for n in main if not n.startswith("__")]:
        del main[name]  # the start's own names: the program finds a module as fresh as a script's
    main["__file__"] = path
    exec(code, main)


# fast-start:end


@dataclass(frozen=True)
class InstallPaths:
    """Where collab keeps what is the person's own, not one room's: they follow HOME and the
    environment. One set, swapped whole: nothing reassigns a path one by one."""

    # Codex must not have to ask before every reply — its rule needs Codex's config read as
    # TOML (tomllib, Python 3.11+); on macOS's own python3 (3.9) it could not be, and no rule was given.
    # `collab install` finds or installs a 3.11+ and records it here; collab started by
    # an older one — a «python3» shebang, Claude's hooks, an agent's «python3 …/collab say» — runs itself
    # again with that one. Nothing in rooms or hooks has to change.
    # A fixed place, not one an environment variable could move.
    python_file: Path
    # Folders that are collab's own, by their default places only: these are closed when found open to
    # reading. Not by the environment — COLLAB_INVITES=~/x/invites must not make collab chmod the person's ~/x.
    own_dirs: tuple[Path, ...]
    codex_home: Path
    agent_rules: Path
    codex_consent: Path
    # The files every room of the person shares — the room list and the Codex rules — were changed
    # read-modify-write with no lock: two rooms at once lost an entry, and a rule one
    # room had just dropped could be written back by another.
    shared_lock: Path
    safe_copies: Path
    # Where install put the `collab` command, and the clone it was installed from. Fixed
    # places, as python_file: what runs now is what that command leads to — a running chat's own file
    # is a copy that never changes, and comparing with it, the chat never knew it was old.
    installed_file: Path
    source_file: Path
    # The update at start: the switch that turns it off, and what the last one found (told once).
    update_off: Path
    update_state: Path
    invites: Path
    welcomed_path: Path
    owner_key: Path  # the person's signing key — see UNSIGNED
    rooms_path: Path

    @classmethod
    def now(cls) -> "InstallPaths":
        """The paths for HOME and the environment as they are now."""
        home = Path("~").expanduser()
        codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
        invites = Path(os.environ.get("COLLAB_INVITES", "~/.collab-room/invites")).expanduser()
        consent = invites.parent / "codex-room-replies"
        return cls(
            python_file=home / ".collab-room" / "python",
            own_dirs=(home / ".collab-room", home / ".collab-room" / "rooms", home / ".local" / "share" / "collab-room"),
            codex_home=codex_home,
            agent_rules=codex_home / "rules" / "collab-room-agents.rules",
            codex_consent=consent,
            shared_lock=consent.parent / "shared.lock",
            safe_copies=Path(os.environ.get("COLLAB_SAFE_DIR", "~/.local/share/collab-room")).expanduser(),
            installed_file=home / ".collab-room" / "installed",
            source_file=home / ".collab-room" / "source",
            update_off=home / ".collab-room" / "no-auto-update",
            update_state=home / ".collab-room" / "update.json",
            invites=invites,
            welcomed_path=invites.parent / "welcomed",
            owner_key=invites.parent / "owner.key",
            rooms_path=Path(os.environ.get("COLLAB_ROOMS", "~/.collab-room/rooms.json")).expanduser(),
        )


class Install:
    """The person's paths for this process; changed only by one assignment of a whole set."""

    def __init__(self, install_paths: InstallPaths) -> None:
        self.paths = install_paths


_INSTALL = Install(InstallPaths.now())


def install() -> Install:
    return _INSTALL


def paths() -> InstallPaths:
    """Read once per operation: `p = paths()`, then p.… to its end."""
    return _INSTALL.paths


# The version of this collab: each public release carries it as its tag, v<VERSION>. A new release
# raises it — the publishing refuses a version that is already released.
VERSION = "0.1.24"


def _safe_mode(st: os.stat_result, group_may_write: bool = False) -> bool:
    """Owned by this user (or root, as python.org's and the system's are); writable by no one else.
    The one exception, for an interpreter only: root-owned with group admin (gid 80) writing, as
    python.org installs it — admin members can sudo anyway, so it gives no one new power. Any other
    group that may write: refused; for our own folder and pointer: no group at all."""
    if st.st_uid not in (os.getuid(), 0) or st.st_mode & stat.S_IWOTH:
        return False
    return not st.st_mode & stat.S_IWGRP or (group_may_write and sys.platform == "darwin" and st.st_uid == 0 and st.st_gid == 80)


def _inside(path: str, folder: str) -> bool:
    """Is `path` inside `folder` — by file identity, not by spelling: on macOS «/Users/a» and
    «/users/A» are one folder."""
    try:
        target = os.stat(folder)
    except OSError:
        return False
    for parent in Path(path).parents:
        with contextlib.suppress(OSError):
            st = os.stat(parent)
            if (st.st_dev, st.st_ino) == (target.st_dev, target.st_ino):
                return True
    return False


def _folder_kept(folder: str) -> bool:
    """A folder the person's or root's that no one else writes — group admin aside (Homebrew's folders
    are the person's with admin writing; admin members can sudo anyway)."""
    try:
        fs = os.lstat(folder)
    except OSError:
        return False
    if fs.st_uid not in (os.getuid(), 0) or fs.st_mode & stat.S_IWOTH:
        return False
    return not fs.st_mode & stat.S_IWGRP or (sys.platform == "darwin" and fs.st_gid in (0, 80))  # macOS wheel/admin


def _link_folder_kept(folder: str) -> bool:
    """A folder holding a link on the way to a Python: kept, and — like the Python itself — not in
    home or a temporary folder, where an agent working as the person writes."""
    return _folder_kept(folder) and not in_temp_folder(folder) and not _inside(folder, str(Path.home()))


def trusted_interpreter(path: str) -> bool:
    """A Python we may run as the person: its real file, not inside a virtual environment (a project's
    .venv is where an agent writes), regular, safe mode. Checked before it is ever run."""
    return not interpreter_refused(path)


def _mode_refused(path: str, st: os.stat_result, group_may_write: bool) -> str:
    """Why _safe_mode refuses it, and the command that fixes it; "" — it does not."""
    if _safe_mode(st, group_may_write):
        return ""
    if st.st_uid not in (os.getuid(), 0):
        return f"it belongs to another user (uid {st.st_uid})"
    if st.st_mode & stat.S_IWOTH:
        return f"anyone may change it; fix: sudo chmod o-w {path}"
    return f"its group may change it; fix: sudo chmod g-w {path}"


def interpreter_refused(path: str) -> str:
    """Why a Python is not one we may run as the person ("" — it is), in words to show: install said
    «not found» of three it refused for their group (root:wheel, writable by the group)."""
    real = os.path.realpath(path)
    # A Python by name: the pointer lies in home, and an agent allowed to write there could point it
    # at any program outside home — /bin/sh, osascript — run with the person's arguments.
    # A second line, not the border: a Codex that may write home already owns ~/.zshrc, ~/.claude and
    # the rest. The border is not letting Codex write home — and then no room rule is given.
    if not re.fullmatch(r"python3(\.\d+)?", os.path.basename(real)):
        return f"its real file {real} is not named python3"
    # Only one installed outside the home folder — the system's, python.org's, Homebrew's. What lies
    # in home (uv's, pyenv's, a project's .venv) an agent allowed to write there could change, and the
    # person's next collab would run it.
    if _inside(real, str(Path.home())):
        return "it is in your home folder, where an agent may write"
    if any(os.path.exists(os.path.join(p, "pyvenv.cfg")) for p in Path(real).parents):
        return "it is in a virtual environment"
    # Nor in a temporary folder: Codex writes /tmp and $TMPDIR by default, and no real Python lives there.
    if in_temp_folder(real):
        return "it is in a temporary folder"
    try:
        st = os.lstat(real)
    except OSError:
        return "it cannot be read"
    # Every folder above it, and every folder holding a link on the way to it: kept.
    for folder in Path(real).parents:
        if not _folder_kept(str(folder)):
            return f"its folder {folder} may be changed by others"
    for folder in link_folders(path):
        if not _link_folder_kept(folder):
            return f"the folder {folder} of a link on the way to it may be changed by others, or is in home"
    if not stat.S_ISREG(st.st_mode):
        return "it is not a regular file"
    # The one exception, python.org's: root:admin with the group writing (_safe_mode); root:wheel is not.
    why = _mode_refused(real, st, group_may_write=True)
    if why:
        return why
    return "" if os.access(real, os.X_OK) else "it cannot be run"


def link_folders(path: str) -> list[str]:
    """Every folder that holds a link on the way from `path` to its real file — whoever may write one of
    them may point the path elsewhere (a recorded link, /opt/homebrew/opt/…)."""
    out: list[str] = []
    head, rest, hops = "/", os.path.abspath(path).strip("/").split("/"), 0
    while rest and hops < 40:
        nxt = os.path.join(head, rest.pop(0))
        if os.path.islink(nxt):
            hops += 1
            out.append(head)
            # head holds no links (built from «/» of plain folders): «..» in the target is plain text here.
            rest = os.path.normpath(os.path.join(head, os.readlink(nxt))).strip("/").split("/") + rest
            head = "/"
        else:
            head = nxt
    return out


def read_private(path: Path) -> str:
    """A small file of ours in ~/.collab-room, or "" — read through one descriptor that refuses links,
    from a folder and a file nobody else can write (the installed and source files too)."""
    try:
        folder = os.lstat(path.parent)
        if not stat.S_ISDIR(folder.st_mode) or not _safe_mode(folder):
            return ""
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return ""
    with os.fdopen(fd, encoding="utf-8") as f:
        st = os.fstat(f.fileno())
        # More than one name — a second name someone made — is refused. None (0) is the old file
        # install has just replaced while we read it: still what we wrote.
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1 or not _safe_mode(st):
            return ""
        try:
            return f.read(4096).strip()
        except (OSError, UnicodeDecodeError):
            return ""


def recorded_python(as_written: bool = False) -> str:
    """The real path of the Python `collab install` recorded (as_written: the path as recorded, which
    may be a link), or "" (read_private)."""
    inst = paths()
    path = read_private(inst.python_file)
    if not os.path.isabs(path):
        return ""
    # Resolved once: what is checked is what is returned and run. The links on
    # the way are checked by their folders (trusted_interpreter, agent_folders below).
    real = os.path.realpath(path)
    if not trusted_interpreter(real) or not all(_link_folder_kept(folder) for folder in link_folders(path)):
        return ""
    # Not inside a room's project or an agent's folder — an agent added after install could work
    # where the recorded Python lies, and every collab on an older python3 runs it.
    # The recorded path may be a link that outlives an upgrade: every folder holding a link counts too.
    if any(_inside(where, folder) for where in [real, *link_folders(path)] for folder in agent_folders()):
        return ""
    return path if as_written else real


def _start_dir() -> Path:
    """The room's folder: COLLAB_ROOT, else where we are. os.getcwd() is not asked when the room is
    named — inside a tmux server started from a restricted program it fails even for a folder that
    exists, and every room pane crashed at start."""
    if os.environ.get("COLLAB_ROOT"):
        return Path(os.environ["COLLAB_ROOT"])
    try:
        return Path.cwd()
    except OSError:
        return Path(os.environ.get("PWD") or ".")


def room_home(root: Path) -> Path:
    """Where a room keeps its files: ~/.collab-room/rooms/<folder>-<hash of its real path>, outside
    the project. In the project a sandboxed Codex working there could change .collab.json — the
    commands that `collab start`, run by the person, executes. The
    room is still named by its project folder; the real path (no links, /tmp = /private/tmp)
    keeps one folder one room."""
    base = os.environ.get("COLLAB_STATE_HOME") or str(
        Path(os.environ.get("COLLAB_INVITES", "~/.collab-room/invites")).expanduser().parent / "rooms"
    )
    real = os.path.realpath(str(Path(root).expanduser()))
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(real).name.lstrip(".") or "room")[:40] or "room"
    return Path(base).expanduser() / f"{name}-{hashlib.sha256(real.encode('utf-8')).hexdigest()[:12]}"


def room_session_name(root: Path) -> str:
    """The room's tmux session: its folder's name and a short hash of its real path — two projects
    named «app» shared one session, and `collab stop` in one closed the other's agents."""
    real = os.path.realpath(str(Path(root).expanduser()))
    name = re.sub(r"[^A-Za-z0-9_-]", "-", Path(real).name)[:24] or "room"
    return f"collab-{name}-{hashlib.sha256(real.encode('utf-8')).hexdigest()[:6]}"


def room_base(root: Path) -> Path:
    """The folder holding .collab.json and .collab/: the room's home; the project itself only for
    a room made by an older version and not moved yet (`collab start` moves it)."""
    home = room_home(root)
    if (home / ".collab.json").exists() or not (Path(root) / ".collab.json").is_file():
        return home
    return Path(root)


def room_config(root: Path) -> Path:
    return room_base(root) / ".collab.json"


def _plain_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def agent_workplaces(agent: Callable[[dict[str, Any]], bool] | None = None) -> list[tuple[str, str]]:
    """Every folder an agent may write as its own, as (whose, folder) — whose is "" for a room's project.
    One list for every check (gathered three ways, the Python checks missed a room known
    only by its home and an agent's cwd). `agent` picks whose folders count (all — None); a room's
    project counts when any of its agents does, or always with None. Plain JSON and nothing defined
    further down: it runs before the hand-over too, on an older Python."""
    sources: dict[str, tuple[str, Path]] = {}
    # Every room there is, not the menu's list: known_rooms() hides rooms in temporary
    # folders by COLLAB_TEMP_PREFIXES — a setting must not loosen a safety check. So: every room of
    # rooms.json as it is (one not moved yet keeps its .collab.json in the project itself)…
    rooms = _plain_json(Path(os.environ.get("COLLAB_ROOMS", "~/.collab-room/rooms.json")).expanduser())
    for room_dir in rooms:
        if isinstance(room_dir, str) and os.path.isabs(room_dir):
            cfg = room_config(Path(room_dir))
            sources.setdefault(os.path.realpath(cfg), (room_dir, cfg))
    # …the agents' recent folders that hold a room — an extra source (a room not moved and not in
    # rooms.json); defined further down, so not read before the hand-over…
    recent = globals().get("recent_folders")
    for folder in recent() if recent else []:
        cfg = room_config(Path(folder))
        if cfg.is_file():
            sources.setdefault(os.path.realpath(cfg), (folder, cfg))
    # …and every room's home on disk: it knows its project from «root» (written by start/init in
    # newer versions). One made before and not started since has none: its agents' folders are checked, an
    # agent with no folder of its own is not, until its next start.
    for cfg in room_home(Path("/")).parent.glob("*/.collab.json"):
        root = _plain_json(cfg).get("root")
        sources.setdefault(os.path.realpath(cfg), (root if isinstance(root, str) and os.path.isabs(root) else "", cfg))
    out: list[tuple[str, str]] = []
    for room_dir, cfg in sources.values():
        listed = [p for p in _plain_json(cfg).get("participants", []) if isinstance(p, dict) and isinstance(p.get("id"), str)]
        chosen = [p for p in listed if agent is None or agent(p)]
        if room_dir and (agent is None or chosen):
            out.append(("", room_dir))
        for p in chosen:
            who = str(p.get("label") or p["id"])
            if isinstance(p.get("folder"), str) and os.path.isabs(p["folder"]):
                out.append((who, p["folder"]))
            elif room_dir and isinstance(p.get("cwd"), str):
                # start runs it in ROOT / cwd — «../..» is above the project.
                out.append((who, os.path.normpath(Path(room_dir) / p["cwd"])))
    return out


def agent_folders() -> list[str]:
    """Every room's project folder and every agent's folder this computer knows of."""
    return [folder for _, folder in agent_workplaces()]


if sys.version_info < (3, 11):
    _py = recorded_python()
    if _py and os.environ.get("COLLAB_REEXEC") != _py:
        os.environ["COLLAB_REEXEC"] = _py  # once: a recorded old Python must not loop
        # -I -S: no PYTHON* settings, no user or site packages, not this folder on the path — only
        # the interpreter's own standard library (collab needs nothing else).
        os.execv(_py, [_py, "-I", "-S", os.path.realpath(__file__), *sys.argv[1:]])
    if sys.argv[1:2] != ["install"]:  # install still runs: it is what finds a newer one
        sys.exit(
            f"collab needs Python 3.11 or newer (this is {sys.version.split()[0]}, {sys.executable}).\n"
            "Run: collab install — it finds or installs one."
        )
else:
    # Handed over: the guard is for that one step. Left in the environment, a tmux server this collab
    # starts kept it, and every room window's «python3 collab» (3.9 again) took it for a loop and quit —
    # a new room without windows.
    os.environ.pop("COLLAB_REEXEC", None)


# ── commands ───────────────────────────────────────────────────────────────────

SELF = str(Path(__file__).resolve())


TEMP_PREFIXES: tuple[str, ...] = tuple(os.environ.get("COLLAB_TEMP_PREFIXES", ":".join(TEMP_FOLDERS)).split(":"))


def recent_folders(limit: int = 8) -> list[str]:
    """Folders where Claude Code or Codex was recently started — newest first, existing only,
    temporary ones left out. Read from the agents' own history, so /add offers a choice instead
    of a typed path. COLLAB_CLAUDE_HOME / COLLAB_CODEX_HOME point tests elsewhere."""
    seen: list[tuple[float, str]] = []
    claude_home = Path(os.environ.get("COLLAB_CLAUDE_HOME", "~/.claude")).expanduser() / "projects"
    try:
        projects = sorted(claude_home.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True)[:25]
    except OSError:
        projects = []
    for proj in projects:
        try:
            newest = max(proj.glob("*.jsonl"), key=lambda f: f.stat().st_mtime)
        except (ValueError, OSError):
            continue
        try:
            with newest.open(encoding="utf-8", errors="replace") as fh:
                for _, raw in zip(range(300), fh):
                    m = re.search(r'"cwd":"((?:[^"\\]|\\.)*)"', raw)
                    if m:
                        seen.append((newest.stat().st_mtime, json.loads(f'"{m.group(1)}"')))
                        break
        except (OSError, ValueError):  # an escape JSON does not know: that file is skipped
            continue
    codex_home = Path(os.environ.get("COLLAB_CODEX_HOME", os.environ.get("CODEX_HOME", "~/.codex"))).expanduser()
    files: list[Path] = []
    for day in sorted((codex_home / "sessions").glob("*/*/*"), reverse=True):
        files += sorted(day.glob("*.jsonl"), reverse=True)
        if len(files) >= 40:
            break
    for f in files[:40]:
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                cwd = json.loads(fh.readline()).get("payload", {}).get("cwd")
            # Another program's file, so any JSON: only a path is a folder (a number raised).
            if isinstance(cwd, str) and cwd and "\0" not in cwd:
                seen.append((f.stat().st_mtime, cwd))
        except (OSError, ValueError, AttributeError):
            continue
    out: list[str] = []
    for _, cwd in sorted(seen, key=lambda x: x[0], reverse=True):
        if "\0" in cwd or cwd in out or (cwd + "/").startswith(TEMP_PREFIXES) or not Path(cwd).is_dir():
            continue
        out.append(cwd)
        if len(out) >= limit:
            break
    return out
