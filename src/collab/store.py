from __future__ import annotations
import contextlib
import datetime as dt
import errno
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
