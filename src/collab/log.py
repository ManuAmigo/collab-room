from __future__ import annotations
import contextlib
import datetime as dt
import errno
import hashlib
import hmac
import json
import os
import secrets
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, NoReturn, cast
import collab.boot as m_boot
import collab.text as m_text
import collab.i18n as m_i18n
import collab.store as m_store


WARNED_LINES: set[int] = set()


def is_message(m: Any) -> bool:
    """A record every reader can use: `null`, `42` or `[]` in the log made `inbox` fail;
    one without «at», or with a number among its recipients, stopped delivery and the chat's feed."""
    return (
        isinstance(m, dict)
        and all(isinstance(cast("dict[str, Any]", m).get(k), str) for k in ("id", "from", "text", "at"))
        and isinstance(cast("dict[str, Any]", m).get("to"), list)
        and all(isinstance(t, str) for t in cast("list[object]", cast("dict[str, Any]", m)["to"]))
    )


def parse_lines(text: str, first: int = 1) -> list[dict[str, Any]]:
    """The messages of the log's lines, numbered from `first` for the warning about a bad one."""
    out: list[dict[str, Any]] = []
    # errors="replace" when read: one line of bad bytes made every reader fail.
    for n, line in enumerate(text.splitlines(), first):
        if not line.strip():
            continue
        m: Any
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            m = None
        if is_message(m):
            out.append(cast(dict[str, Any], m))
        elif n not in WARNED_LINES:
            # Once per line, and in the chat into its feed: printed to stderr it landed on the input
            # line of the person's chat and came back on every read.
            WARNED_LINES.add(n)
            note = f"warning: skipped malformed message line {n}"
            m_store.warn(note)
    return out


def log_unreadable(exc: OSError) -> NoReturn:
    rp = m_store.room().paths
    # Only a link is called one: a read a sandbox forbade was reported as «a link».
    if exc.errno == errno.ELOOP:
        m_store.fail(
            m_i18n.tr(
                "refusing: {messages_path} is a link — the room's state must be its own files", messages_path=rp.messages_path
            )
        )
    m_store.fail(m_i18n.tr("cannot read {messages_path}: {strerror}", messages_path=rp.messages_path, strerror=exc.strerror))


def read_messages() -> list[dict[str, Any]]:
    """The whole log, read now. Where the past is judged — a signature, /clear, show — this one, not
    the tick's."""
    rp = m_store.room().paths
    try:
        text = m_store.read_state_text(rp.messages_path)
    except FileNotFoundError:
        return []
    except OSError as exc:
        log_unreadable(exc)
    return parse_lines(text)


TAIL = 256  # the bytes before the place read up to that must still be there


@dataclass
class Journal:
    """What this process has read of the room's log: the messages of its whole lines, and how to know
    the file is still the one they came from. The tick reads only what came after (the
    /dm window took 70% of a core re-reading 4 MB on every message)."""

    path: Path | None = None
    file: tuple[int, int] = (-1, -1)  # st_dev, st_ino
    offset: int = 0  # bytes of whole lines read
    size: int = 0
    mtime_ns: int = 0
    tail: bytes = b""  # the TAIL bytes before offset
    lines: int = 0
    messages: list[dict[str, Any]] = field(default_factory=lambda: cast("list[dict[str, Any]]", []))
    # The chat reads from two threads: one tick at a time, or they mix offset and messages.
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def restart(self, path: Path, info: os.stat_result | None = None) -> None:
        self.path, self.offset, self.tail, self.lines, self.messages = path, 0, b"", 0, []
        self.file = (info.st_dev, info.st_ino) if info else (-1, -1)


_JOURNAL = Journal()


def same_file(fh: BinaryIO, info: os.stat_result, j: Journal) -> bool:
    """The file still holds what was read: the same inode, not shorter, the TAIL bytes before the place
    as they were, and at the same size the same mtime (a rewrite in place of the same length)."""
    if (info.st_dev, info.st_ino) != j.file or info.st_size < j.offset:
        return False
    if info.st_size == j.size and info.st_mtime_ns != j.mtime_ns:
        return False
    fh.seek(j.offset - len(j.tail))
    return fh.read(len(j.tail)) == j.tail


def read_log_tick(fresh: bool = False) -> list[dict[str, Any]]:
    """The log for a tick (see tick_read), one thread at a time; each message a copy with its own «to»:
    a caller that changes one never changes the cache under the next tick.
    fresh: read it all again, and keep that for the next ticks. Where a tick judges the past (the /dm
    panel's ⚠ every 30 s), a full read beside the cache let the next tick bring back the text before an
    edit above the tail — the mark went out on the next message."""
    with _JOURNAL.lock:
        if fresh:
            _JOURNAL.restart(m_store.room().paths.messages_path)
        return [{**m, "to": list(m["to"])} for m in tick_read(_JOURNAL)]


def tick_read(j: Journal) -> list[dict[str, Any]]:
    """The log for a tick of delivery, the chat and /dm: the messages read before and only the whole
    lines that came after. Opened as read_state_text opens it, every time — that the file is ours is
    never remembered. A last line without its line break is a write not done: left for the
    next tick, not taken for a bad line. What it cannot see: an edit in place above the tail, of the
    same length, with its mtime put back — where the past is judged, read_messages()."""
    rp = m_store.room().paths
    if j.path != rp.messages_path:
        j.restart(rp.messages_path)
    try:
        fd, name = m_store.state_dir(rp.messages_path, strict=False, create=False)
        try:
            f = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        finally:
            os.close(fd)
    except FileNotFoundError:
        j.restart(rp.messages_path)
        return []
    except OSError as exc:
        log_unreadable(exc)
    with os.fdopen(f, "rb") as fh:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            log_unreadable(OSError(errno.ELOOP, "a link, not the room's own file"))
        if not same_file(fh, info, j):
            j.restart(rp.messages_path, info)
        if info.st_size > j.offset:
            fh.seek(j.offset)
            data = fh.read(info.st_size - j.offset)
            whole = data[: data.rfind(b"\n") + 1]
            if whole:
                text = whole.decode("utf-8", errors="replace")
                j.messages += parse_lines(text, j.lines + 1)
                j.lines += whole.count(b"\n")
                j.offset += len(whole)
                j.tail = (j.tail + whole)[-TAIL:]
        j.size, j.mtime_ns = info.st_size, info.st_mtime_ns
    return j.messages


def append_message(
    sender: str,
    to: list[str],
    text: str,
    re_: str = "",
    sign: bool = False,
    quiet: bool = False,
    silent: bool = False,
    service: bool = False,
    brief: bool = False,
    proposal: dict[str, Any] | None = None,
    said: dict[str, Any] | None = None,
    via: str = "",
    tg: str = "",
    files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rp = m_store.room().paths
    record: dict[str, Any] = {"id": uuid.uuid4().hex[:12], "at": m_store.now(), "from": sender, "to": to, "text": text}
    if re_:
        record["re"] = re_
    if quiet:
        record["quiet"] = True  # a note that wakes no one (is_quiet)
    if silent:
        record["silent"] = True  # the room's own note: never typed to an agent (is_silent)
    if service:
        record["service"] = True  # the merge queue's note in an agent's name: not its work (where_it_was)
    if brief:
        record["brief"] = True  # an agent's assignment (deliver.send_briefs): told by this, never by its words
    if proposal is not None:
        record["proposal"] = proposal  # a change of the team the person applies (store.proposal_card)
    if said is not None:
        record["said"] = said  # the room's own line: its English template and parameters, retold in the chat's language
    if via:
        record["via"] = via  # where the person wrote it (telegram): under the signature, so no one adds or drops it
    if tg:
        record["tg"] = tg  # the bot server's id of it: taken twice, written once
    if files:
        record["files"] = files  # an agent's files for the person (say --file): copies made when it was said
    if sign:
        record["sig"] = signature(owner_key(create=True), record)
        mark_signing(record["id"])
    m_store.write_state(rp.messages_path, json.dumps(record, ensure_ascii=False) + "\n", append=True)
    return record


def room_line(to: list[str], en: str, silent: bool = False, **params: Any) -> dict[str, Any]:
    """The room's own line to people, as text in English (agents and old chats read that) and as its template with
    its parameters — a chat in another language retells it (m_i18n.retell). Each template has its Russian one in
    the catalog (a test reads every call)."""
    shown = {k: str(v) for k, v in params.items()}
    return append_message(m_text.ROOM, to, en.format(**shown) if shown else en, silent=silent, said={"t": en, "p": shown})


UNREADABLE_SAID: set[str] = set()


def read_set(directory: Path, pid: str) -> set[str]:
    """One agent's ids (delivered, read). An unreadable file stopped delivery to every agent for good:
    it is put aside under another name and said so once. What was delivered then
    counts as everything up to the file's last change — an empty set would type the whole history in
    again, and «up to now» lost what was still waiting («fourth» never arrived)."""
    rp = m_store.room().paths
    path = directory / f"{pid}.json"
    try:
        # No exists() first: it follows a link, and a dangling one read as «nothing delivered yet»
        # typed the whole history in again.
        data: object = json.loads(m_store.read_state_text(path, "strict"))  # a link is put aside too
        if not isinstance(data, list) or not all(isinstance(x, str) for x in cast("list[object]", data)):
            raise ValueError("not a list of ids")
        return set(cast("list[str]", data))
    except FileNotFoundError:
        return set()
    except (OSError, ValueError) as exc:  # ValueError: bad JSON and bad UTF-8 too
        aside = f"{path.name}.unreadable-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            moved, ids = put_aside(directory, pid, aside)
        except (OSError, SystemExit, m_store.OperationFailed):  # no lock for a reader that cannot write
            moved = False
            ids: set[str] = (
                {m["id"] for m in read_messages() if m_text.at_time(m["at"]) < time.time() - 1}
                if directory == rp.delivered_dir
                else set()
            )
        # Said as it is: «kept as» only when it was. Not put aside, it is left as it was —
        # writing the new set there destroyed the very file the warning was about — and
        # said once, not every second.
        where = f"kept as {aside}" if moved else "it could not be put aside and is left as it is"
        note = f"warning: {path.name} for {pid} could not be read ({exc}); {where}"
        if not moved:
            if str(path) in UNREADABLE_SAID:
                return ids
            UNREADABLE_SAID.add(str(path))
        m_store.warn(note)
        return ids


@m_store.room_locked
def put_aside(directory: Path, pid: str, aside: str) -> tuple[bool, set[str]]:
    """read_set's unreadable file put aside and the set written anew — under the room's lock, as the
    deliverer writes these sets: inbox and watch did it unlocked, and a delivered mark could be lost and
    the message typed again. Whether it was moved, and the ids that count as done."""
    rp = m_store.room().paths
    path = directory / f"{pid}.json"
    last, moved = time.time(), False
    # Put aside inside the room's state folder, opened refusing links like every other state write
    # — a plain os.replace followed whatever the path led to.
    try:
        fd, name = m_store.state_dir(path, strict=False)
        try:
            last = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mtime
            os.replace(name, aside, src_dir_fd=fd, dst_dir_fd=fd)
            moved = True
        finally:
            os.close(fd)
    except (OSError, SystemExit, m_store.OperationFailed):  # OperationFailed: the same, in the chat
        pass
    ids: set[str] = (
        {m["id"] for m in read_messages() if m_text.at_time(m["at"]) < last - 1} if directory == rp.delivered_dir else set()
    )
    if moved:
        with contextlib.suppress(OSError, SystemExit, m_store.OperationFailed):
            write_set(directory, pid, ids)
    return moved, ids


def write_set(directory: Path, pid: str, values: set[str]) -> None:
    m_store.write_state(directory / f"{pid}.json", json.dumps(sorted(values), indent=2) + "\n")


def mine_since(pid: str, start: bool = False) -> float:
    """From when «for you» counts. The first time it is now: before /mine a person had no marks of
    reading, and a reopened chat said «● 717 for you» — two days of history."""
    rp = m_store.room().paths
    if start:
        start_mine_since(pid)
    data = m_store.read_json(rp.state_dir / "mine-since.json")
    return m_text.at_time(str(data.get(pid, ""))) if isinstance(data.get(pid), str) else time.time()


# Read-change-write of one file for every person: two people in one room lost each other's mark
# without the room's lock.
@m_store.room_locked
def start_mine_since(pid: str) -> None:
    rp = m_store.room().paths
    path = rp.state_dir / "mine-since.json"
    data = m_store.read_json(path)
    if not isinstance(data.get(pid), str):
        data[pid] = m_store.now()
        m_store.write_json(path, data)


@m_store.room_locked
def mark_all_mine_read(pid: str) -> None:
    rp = m_store.room().paths
    path = rp.state_dir / "mine-since.json"
    data = m_store.read_json(path)
    # A second on: times are in seconds, and what came in this same second was in the list just read.
    data[pid] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)).isoformat(timespec="seconds")
    m_store.write_json(path, data)


def for_person(messages: list[dict[str, Any]], pid: str, all_time: bool = False) -> list[dict[str, Any]]:
    """What a person should not miss («when I come back, the messages for me, without
    reading everything»): addressed to them by name, and answers to their own messages, even when
    sent to everyone. Their own are not."""
    # An answer counts only from someone the message was for (--re is the agent's own word —
    # anyone could otherwise get into the person's list by naming one of their messages).
    since = 0.0 if all_time else mine_since(pid)
    mine = {m["id"]: m["to"] for m in messages if m["from"] == pid}
    # And only an answer the person is meant to read — to them or to everyone: A's letter to B naming
    # the person's message is A's and B's talk.
    return [
        m
        for m in messages
        if m["from"] != pid
        and m_text.at_time(m["at"]) >= since
        and (
            pid in m["to"]
            or ("all" in m["to"] and m.get("re") in mine and ("all" in mine[m["re"]] or m["from"] in mine[m["re"]]))
        )
    ]


def is_for(message: dict[str, Any], pid: str) -> bool:
    """Addressed to pid and not written by pid: your own words are not your mail."""
    if message.get("from") == pid:
        return False
    to: list[str] = message.get("to") or []
    return pid in to or "all" in to


def owner_key(create: bool = False) -> bytes:
    inst = m_boot.paths()
    try:
        return bytes.fromhex(inst.owner_key.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        if not create:
            return b""
    m_store.private_dir(inst.owner_key.parent, strict=False)
    try:
        fd = os.open(str(inst.owner_key), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(secrets.token_hex(32) + "\n")
    except FileExistsError:
        pass
    return bytes.fromhex(inst.owner_key.read_text(encoding="utf-8").strip())


def signature(key: bytes, m: dict[str, Any]) -> str:
    fields: list[Any] = [m["id"], m["at"], m["from"], m["to"], m["text"], m.get("re", "")]
    if m.get("via"):
        fields.append(["via", m["via"]])  # only then: what was signed before stays signed
    data = json.dumps(fields, ensure_ascii=False)
    return hmac.new(key, data.encode("utf-8"), hashlib.sha256).hexdigest()


_SIGNING: dict[str, Any] = {}


def signing_from() -> tuple[int, dict[str, int]]:
    """Where signing began in this room's feed: the place of the first signed message from a
    person, and each message's (last) place. By place in the file, not by time or by the key's
    age: a fake could carry an old «at», and a deleted key had switched
    the check off."""
    rp = m_store.room().paths
    try:
        info = rp.messages_path.stat()
    except OSError:
        return -1, {}
    stamp = (str(rp.messages_path), info.st_size, info.st_mtime_ns, signing_mark())
    if _SIGNING.get("stamp") != stamp:
        where: dict[str, int]
        first, first_id, where = -1, "", {}
        for n, m in enumerate(read_messages()):
            where[m.get("id", "")] = n
            if first < 0 and m.get("sig"):
                first, first_id = n, m.get("id", "")
        # Begun once, judged for good: /clear starts an empty feed, and a fake written before the
        # person's first new message passed unmarked. The mark outlives /clear.
        # The mark keeps the first signed message's id: while it is in the feed, judging starts
        # there; once /clear took it away, the whole new feed is judged — a later signed message
        # never moves the start past a fake written before it.
        begun = signing_mark()
        if begun is None and first >= 0:
            mark_signing(first_id)
        elif begun is not None and begun.startswith("after:"):
            # Begun when the person's chat opened, before they wrote anything: what came later is
            # judged (a new room's fake passed until the first message).
            n = where.get(begun[len("after:") :])
            first = 0 if n is None else n + 1
        elif begun is not None:
            first = where.get(begun, 0)
        _SIGNING.update(stamp=stamp, first=first, where=where)
    return _SIGNING["first"], _SIGNING["where"]


def signing_mark() -> str | None:
    rp = m_store.room().paths
    try:
        # From the checked file itself: a link at its name was followed.
        return m_store.read_state_text(rp.state_dir / "signing").strip()
    except (OSError, ValueError):
        return None


# Both «if there is no mark, write one» under the room's lock: two chats (a second one, /dm) wrote
# them at once, the later write won and judging began later — a fake «from the person» written
# between them passed without ⚠.
@m_store.room_locked
def begin_signing() -> None:
    """The person's chat is open: it signs from now on. Older talk keeps its place unjudged."""
    signing_from()
    if signing_mark() is None:
        messages = read_messages()
        mark_signing("after:" + messages[-1]["id"] if messages else "")


def mark_signing(first_id: str) -> None:
    """This room's chat signs: from now on every message «from a person» is judged."""
    try:
        write_signing_mark(first_id)
    except (m_store.OperationFailed, SystemExit):
        pass  # a reader that cannot write (a sandboxed Codex's inbox: not even the lock) still judges by the feed


@m_store.room_locked
def write_signing_mark(first_id: str) -> None:
    rp = m_store.room().paths
    if signing_mark() is None:
        m_store.replace_state(rp.state_dir / "signing", first_id)


def unsigned_person(m: dict[str, Any], config: dict[str, Any]) -> bool:
    """«From a person», but not signed by that person's chat. Judged from the first signed message
    on: before it this room's chat did not sign yet. Once signing began, a missing key leaves
    every such message unsigned — not unjudged."""
    if m.get("from") not in m_store.people_ids(config):
        return False
    first, where = signing_from()
    if first < 0 or where.get(m.get("id", ""), first) < first:
        return False
    key = owner_key()
    return not key or not hmac.compare_digest(str(m.get("sig", "")), signature(key, m))


# A request the lead may answer with a narrowing applied at once: young, and used once.
REQUEST_AGE = 6 * 3600


def request_of(m: dict[str, Any], config: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The person's own request a proposal answers: the message it names with --re — from a person, signed by that
    person's chat (checked here, also in a room that never signed before), to the proposer alone, not older than
    REQUEST_AGE, not itself an answer to a proposal (a «Declined»), and not used by another proposal. Anything else
    is no request: the proposal waits for Enter."""
    req = next((x for x in messages if x.get("id") == m.get("re")), None)
    if req is None or req.get("from") not in m_store.people_ids(config) or req.get("to") != [m.get("from")]:
        return None
    if req.get("via"):
        return None  # written from Telegram: read and answered, never a request a change applies by (nor shown as one)
    key = owner_key()
    if not key or not hmac.compare_digest(str(req.get("sig", "")), signature(key, req)):
        return None
    if time.time() - m_text.at_time(str(req.get("at") or "")) > REQUEST_AGE:
        return None
    if any(isinstance(x.get("proposal"), dict) and x.get("id") == req.get("re") for x in messages):
        return None
    if any(x.get("re") == req["id"] and isinstance(x.get("proposal"), dict) and x["id"] != m["id"] for x in messages):
        return None  # used by another proposal
    return req


def held_count() -> int:
    rp = m_store.room().paths
    try:
        return len(rp.held_path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


@m_store.room_locked
def archive_history() -> Path:
    """Clear the room's conversation without destroying it: the messages move to
    .collab/archive/, and the per-message state (delivered, read, waits) starts afresh."""
    rp = m_store.room().paths
    archive = rp.state_dir / "archive"
    # A name of its own for every clear: two in one second wrote the second over the first and the
    # history was gone. Folders are opened refusing links.
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = archive / f"messages-{stamp}-{uuid.uuid4().hex[:6]}.jsonl"
    if rp.messages_path.exists() and not rp.messages_path.is_symlink():
        src_fd, src_name = m_store.state_dir(rp.messages_path)
        dst_fd, dst_name = m_store.state_dir(target)
        try:
            os.replace(src_name, dst_name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
        finally:
            os.close(src_fd)
            os.close(dst_fd)
    else:
        m_store.replace_state(target, "")
    for directory in (rp.delivered_dir, rp.receipts_dir, rp.noreply_dir):
        # Opened refusing links, emptied by names inside it: «delivered» made a link to another
        # folder had that folder's *.json deleted by /clear.
        fd, _ = m_store.state_dir(directory / "x")
        try:
            for name in os.listdir(fd):
                if name.endswith(".json"):
                    os.unlink(name, dir_fd=fd)
        finally:
            os.close(fd)
    rp.await_path.unlink(missing_ok=True)
    return target


def answered(w: dict[str, Any], pid: str, messages: list[dict[str, Any]]) -> bool:
    """Every delivered message is closed by the agent — an answer that names it
    (`say --re ID`), or an explicit «no reply needed» (`ack --no-reply --id ID`). A plain answer
    to the sender without --re still counts, so older habits do not raise alarms."""
    rp = m_store.room().paths
    if w["id"] in read_set(rp.noreply_dir, pid):
        return True
    for m in messages:
        if m.get("from") != pid or m.get("at", "") < w["at"]:
            continue
        if m.get("re") == w["id"] or (not m.get("re") and (w["from"] in m.get("to", []) or "all" in m.get("to", []))):
            return True
    return False


def closed_by(pid: str, messages: list[dict[str, Any]]) -> set[str]:
    """What the agent has itself read (ack), closed (ack --no-reply) or answered (say --re)."""
    rp = m_store.room().paths
    return (
        read_set(rp.receipts_dir, pid)
        | read_set(rp.noreply_dir, pid)
        | {m["re"] for m in messages if m.get("from") == pid and m.get("re")}
    )
