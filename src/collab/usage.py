"""Token spend of the room's agents, from their own session logs — numbers only.

What an agent spent since the last look: input, output, cache read and cache write, by model and effort.
Read only, never a word of the conversation: a line is parsed for its numbers and its model's name, and
nothing else of it leaves this module.
"""

from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
import re
import stat
import shlex
import subprocess
import uuid
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast
import collab.store as m_store
import collab.screen as m_screen
import collab.text as m_text

FIELDS = ("input", "output", "cache_read", "cache_write")
SEEN_MAX = 50_000  # answers remembered per agent; past it the oldest go, and «since» moves past them
CTX_MAX = 64  # Codex turns whose model and effort are kept for the records that follow them
CHUNK = 8 << 20  # bytes read from one log in one look; the rest waits for the next
BUDGET = 8 << 20  # and from all of an agent's logs in one call: the room's lock is held meanwhile
CONTEXT_TAIL = 2 << 20  # how far back a first look at a Codex log seeks its turn's model and effort
MARKS = {"claude": b'"usage"', "codex": b'"payload"'}
# A Codex compacted its context: its log's own record of that, at the head of a line (a quote of these words in
# a message is escaped inside a string). Its assignment is sent again (deliver.send_briefs).
COMPACTED = b'"type":"compacted"'  # a line without it has no numbers: not even parsed
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}")  # a model's or an effort's name, as shown
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")  # an answer's id, as kept in the cursor


def cursor_path() -> Path:
    return m_store.room().paths.state_dir / "usage-cursor.json"


def clean(name: Any) -> str | None:
    """A name from a log, shown only if it looks like one: the log is the agent's to write."""
    return name if isinstance(name, str) and NAME.fullmatch(name) else None


def stamp(ts: object) -> str:
    """A log's time as the room writes its own (m_store.now: UTC, seconds, «+00:00»): Claude writes
    «…T12:34:56.789Z», Codex may write otherwise, and the report compares them as strings. One
    that cannot be read — "", and that answer is not counted."""
    try:
        return dt.datetime.fromisoformat(str(ts)).astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError):  # a date at the edge of the calendar overflows on the shift
        return ""


def count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def claude_home() -> Path:
    return Path(os.environ.get("COLLAB_CLAUDE_HOME", "~/.claude")).expanduser() / "projects"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions"


def claude_files(p: dict[str, Any]) -> list[Path]:
    """The agent's Claude session log and its subagents' (their spend is the agent's too). The session is
    the one delivery keeps for its window (window_session): no tmux call of its own.
    After a resume the session id changes, and the new file copies the old answers: they are told apart by
    their ids and times (read_claude)."""
    sid = str(p.get("window_session") or "")
    if not m_screen.SESSION_ID.fullmatch(sid):
        return []
    # /add writes the agent's folder; a seat `start` opens works in its «cwd» under the project.
    raw: object = p.get("cwd")
    cwd = raw if isinstance(raw, str) else "."  # the settings are the person's to edit
    folder = os.path.realpath(p.get("folder") or str(m_store.room().paths.root / cwd))
    base = claude_home() / re.sub(r"[^A-Za-z0-9]", "-", folder)
    subs = base / sid / "subagents"
    try:
        extra = sorted(subs / n for n in os.listdir(subs) if n.endswith(".jsonl"))
    except OSError:
        extra = []
    return [base / f"{sid}.jsonl", *extra]


def codex_file(p: dict[str, Any], known: str, code: str = "") -> tuple[list[Path], str]:
    """The agent's Codex session logs and the connection check they were found by: the one found before while the
    agent is in the same run (`code` is its join code then); after a /restart — a new check, a new conversation in a
    new file, the old one left on disk — found again, and the old one read to its end beside it. A scan of the
    sessions folder, so not on every look."""
    logs = codex_home()
    # The cursor is a file in the room's state, anyone's to edit: its path counts only inside Codex's own
    # sessions folder — nothing outside ~/.claude and ~/.codex is opened.
    had = bool(known) and os.path.isfile(known) and Path(os.path.realpath(known)).is_relative_to(os.path.realpath(logs))
    run = str(p.get("join_code") or "")
    if had and code == run:
        return [Path(known)], run
    sid = m_screen.agent_session(p)
    try:
        found = sorted(logs.rglob(f"rollout-*{sid}.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True) if sid else []
    except OSError:
        found = []
    if not found:
        return ([Path(known)] if had else []), ""  # the new one not there yet: looked for again next time
    old = [Path(known)] if had and os.path.realpath(known) != os.path.realpath(found[0]) else []
    return [found[0], *old], run


def unread(p: dict[str, Any]) -> str:
    """Why the agent's spending is not read — its log not found; "" when it is. For the report: a table
    without an agent looked complete."""
    kind = m_screen.agent_kind(p)
    if kind == "claude":
        files = claude_files(p)
        if not files:
            return (
                "the session of its window is not known: delivery reads it from Claude's own record once a minute while it runs"
            )
        if not files[0].is_file():
            return f"no log of its session {p.get('window_session')} in {m_text.tilde(str(files[0].parent))}"
        return ""
    if kind == "codex":
        known = next(iter(obj(obj(m_store.read_json(cursor_path())).get(p["id"])).get("files") or {}), "")
        if codex_file(p, str(known))[0]:
            return ""
        return f"no log of its session in {m_text.tilde(str(codex_home()))} (found by its connection check)"
    if kind == "kimi":
        b = kimi_binding(p)
        if not b:
            proof = obj(
                kimi_state(m_store.room().paths.state_dir / "kimi-launches.json").get(str(p.get("kimi_usage_generation") or ""))
            )
            reason = proof.get("reason")
            if (
                proof.get("id") == p["id"]
                and isinstance(reason, str)
                and reason
                in (
                    "native adapter has not been verified; no token data",
                    "session changed or resumed without a new managed launch",
                    "managed resume cursor unavailable; no token data",
                )
            ):
                return reason
            return "no confirmed Kimi session binding (automatic collection is not installed)"
        fd = open_under(Path("/"), kimi_file(b))
        if fd is None:
            return "bound Kimi usage log is unavailable or unsafe"
        os.close(fd)
        cursor = obj(kimi_state(m_store.room().paths.state_dir / "kimi-usage-cursor.json").get(p["id"]))
        signature = hashlib.sha256(json.dumps(b, sort_keys=True).encode()).hexdigest()
        return (
            ""
            if cursor.get("binding") == signature and cursor.get("observed") is True
            else "no validated Kimi usage records since the binding baseline"
        )
    return "not a Claude or a Codex: its spending is not read"


def open_under(base: Path, path: Path) -> int | None:
    """The log opened from its home folder down, every step refusing a link, as the room's own state is
    (store.state_dir): a session log, or a folder on the way to it, made a link elsewhere is not read.
    None — not there, not under `base`, or a link."""
    try:
        parts = path.relative_to(base).parts
        fd = os.open(str(base), os.O_RDONLY | os.O_DIRECTORY)
    except (ValueError, OSError):
        return None
    try:
        for part in parts[:-1]:
            inner = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = inner
        # Refuse special files after open without waiting for a FIFO writer first.
        f = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except (OSError, IndexError):
        return None
    finally:
        os.close(fd)
    if not stat.S_ISREG(os.fstat(f).st_mode):
        os.close(f)
        return None
    return f


def read_new(
    base: Path, path: Path, at: dict[str, int], to_end: bool = False, limit: int = CHUNK
) -> tuple[list[bytes], dict[str, int]]:
    """The whole lines added to a log since `at` (its inode, offset, and whether it stopped inside a line),
    and where reading stopped; `to_end` — nothing read, the end noted (a first look). A new inode or a
    shorter file is a new log: read from its start. A line longer than one look's read (a tool's result,
    an image) is skipped to its end, never read again and again: numbers are in short lines. Only a
    whole look's read can tell such a line: with less of the call's budget left, a line is waited for."""
    f = open_under(base, path)
    if f is None:
        return [], at
    with os.fdopen(f, "rb") as fh:
        info = os.fstat(fh.fileno())
        if to_end:
            return [], {"ino": info.st_ino, "off": info.st_size, "mid": 0}
        same = at.get("ino") == info.st_ino and at.get("off", 0) <= info.st_size
        offset, mid = (at.get("off", 0), at.get("mid", 0)) if same else (0, 0)
        fh.seek(offset)
        data = fh.read(min(limit, CHUNK))
    start = 0
    if mid:
        first = data.find(b"\n")
        if first < 0:
            return [], {"ino": info.st_ino, "off": offset + len(data), "mid": 1}
        start = first + 1
    end = data.rfind(b"\n") + 1  # a line still being written waits for the next look
    if end <= start and len(data) == CHUNK:
        return [], {"ino": info.st_ino, "off": offset + len(data), "mid": 1}
    lines = data[start:end].splitlines() if end > start else []
    return lines, {"ino": info.st_ino, "off": offset + max(end, start), "mid": 0}


def last_context(base: Path, path: Path, ctx: dict[str, list[str | None]]) -> None:
    """A first look reads nothing — and the turn it fell in had its model and effort written before it:
    its records went out with no model. The log's last CONTEXT_TAIL bytes are looked at for
    turn_context lines only; no numbers are taken from them."""
    f = open_under(base, path)
    if f is None:
        return
    with os.fdopen(f, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        fh.seek(max(0, size - CONTEXT_TAIL))
        data = fh.read(CONTEXT_TAIL)
    for raw in data.splitlines()[1:] if size > CONTEXT_TAIL else data.splitlines():  # the first may be cut
        if b'"turn_context"' in raw:
            read_codex(raw.decode("utf-8", errors="replace"), ctx)


def obj(x: object) -> dict[str, Any]:
    """A JSON object as a dict; anything else as an empty one — a log line is the agent's to write."""
    return cast("dict[str, Any]", x) if isinstance(x, dict) else {}


def parse(line: str) -> dict[str, Any]:
    try:
        return obj(json.loads(line))
    except ValueError:
        return {}


def claude_usage(u: dict[str, Any]) -> dict[str, int]:
    """One answer's numbers. «iterations» when it has any — the top fields are sometimes zeros while the
    numbers are there (token-sources.md, trap 3)."""
    raw: object = u.get("iterations")
    items = cast("list[object]", raw) if isinstance(raw, list) else []
    steps = [obj(x) for x in items] or [u]
    out: dict[str, int] = dict.fromkeys(FIELDS, 0)
    for step in steps:
        out["input"] += count(step.get("input_tokens"))
        out["output"] += count(step.get("output_tokens"))
        out["cache_read"] += count(step.get("cache_read_input_tokens"))
        out["cache_write"] += count(step.get("cache_creation_input_tokens"))
    return out


Answer = tuple[str, str, dict[str, int], "str | None", "str | None"]  # id, time, numbers, model, effort


def read_claude(line: str) -> Answer | None:
    """An answer line's numbers; None for any other line."""
    r = parse(line)
    msg = obj(r.get("message")) if r.get("type") == "assistant" else {}
    mid: object = msg.get("id")
    if not isinstance(mid, str) or not isinstance(msg.get("usage"), dict):
        return None
    return mid, stamp(r.get("timestamp")), claude_usage(obj(msg.get("usage"))), clean(msg.get("model")), None


def read_codex(line: str, ctx: dict[str, list[str | None]]) -> Answer | None:
    """A token_usage_record's numbers; a turn_context line only updates `ctx` (turn id -> model, effort).
    Codex counts cached input inside its input, and reasoning inside its output (token-sources.md): input
    here is what was not read from the cache."""
    r = parse(line)
    pay = obj(r.get("payload"))
    turn = pay.get("turn_id")
    if r.get("type") == "turn_context" and isinstance(turn, str):
        ctx[turn] = [clean(pay.get("model")), clean(pay.get("effort"))]
        while len(ctx) > CTX_MAX:
            ctx.pop(next(iter(ctx)))
        return None
    if r.get("type") != "token_usage_record" or not isinstance(pay.get("usage"), dict):
        return None
    u = obj(pay.get("usage"))
    cached = count(u.get("cached_input_tokens"))
    nums = {
        "input": max(0, count(u.get("input_tokens")) - cached),
        "output": count(u.get("output_tokens")),
        "cache_read": cached,
        "cache_write": count(u.get("cache_write_input_tokens")),
    }
    model, effort = ctx.get(str(turn), [None, None])
    thread, resp = pay.get("thread_id"), pay.get("response_id")
    # Both parts or no key: «None:None» would make every record without them one answer.
    key = f"{thread}:{resp}" if isinstance(thread, str) and isinstance(resp, str) else ""
    return key, stamp(r.get("timestamp")), nums, model, effort


@dataclass
class Cursor:
    """Where one agent's counting stands: since when, how far into each log, the answers already counted
    (id -> time), a Codex log's recent turns (turn id -> model, effort) and the run its log is of."""

    since: str
    files: dict[str, dict[str, int]] = field(default_factory=lambda: {})
    seen: dict[str, str] = field(default_factory=lambda: {})
    ctx: dict[str, list[str | None]] = field(default_factory=lambda: {})
    code: str = ""  # a Codex's: the join code of the run its log was found for

    @classmethod
    def load(cls, raw: object) -> Cursor | None:
        """The stored cursor, its parts that are not of their shape dropped; None — none stored."""
        r = obj(raw)
        if not isinstance(r.get("since"), str):
            return None
        files = {k: {f: count(obj(v).get(f)) for f in ("ino", "off", "mid")} for k, v in obj(r.get("files")).items()}
        seen = {k: v for k, v in obj(r.get("seen")).items() if isinstance(v, str)}
        ctx = {
            k: [clean(x) for x in cast("list[object]", v)][:2]
            for k, v in obj(r.get("ctx")).items()
            if isinstance(v, list) and len(cast("list[object]", v)) == 2
        }
        return cls(r["since"], files, seen, ctx, r["code"] if isinstance(r.get("code"), str) else "")


# This adapter's persisted zeros cannot distinguish missing usage from measured zero.
KIMI_ADAPTER = "openai-compatible-2.4.1"
KIMI_NORMALIZED = "kimi-code-2.4.1-normalized"
KIMI_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
# Native workspace slugs retain dots, including hidden directory basenames.
# Keep the previous simple-key contract without broadening session/agent IDs.
KIMI_WORKDIR = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9_-]{0,127}|wd_[a-z0-9._-]{1,40}_[0-9a-f]{12})")


def kimi_state(path: Path) -> dict[str, Any]:
    """Only fixed state names, bounded regular files, no followed components or links."""
    if path.parent != m_store.room().paths.state_dir or path.name not in (
        "kimi-launches.json",
        "kimi-usage-bindings.json",
        "kimi-usage-cursor.json",
    ):
        return {}
    fd = open_under(Path("/"), path)
    if fd is None:
        return {}
    try:
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if info.st_nlink > 1 or info.st_size > CHUNK:
                return {}
            data = source.read(CHUNK + 1)
            return obj(json.loads(data)) if len(data) <= CHUNK else {}
    except (OSError, ValueError):
        return {}


def kimi_home() -> Path:
    return Path(os.environ.get("KIMI_CODE_HOME", "~/.kimi-code")).expanduser().absolute() / "sessions"


def kimi_binding(p: dict[str, Any]) -> dict[str, Any]:
    b = obj(kimi_state(m_store.room().paths.state_dir / "kimi-usage-bindings.json").get(p["id"]))
    if not p.get("join_code") or any(
        b.get(k) != str(p.get(v) or "") for k, v in (("generation", "join_code"), ("pane_pid", "pane_pid"), ("target", "target"))
    ):
        return {}
    if b.get("adapter") not in (KIMI_ADAPTER, KIMI_NORMALIZED) or b.get("sessions_root") != str(kimi_home()):
        return {}
    if not isinstance(b.get("workdir_key"), str) or not KIMI_WORKDIR.fullmatch(str(b["workdir_key"])):
        return {}
    if any(not isinstance(b.get(k), str) or not KIMI_PART.fullmatch(str(b[k])) for k in ("session_id", "agent_id")):
        return {}
    if p.get("kimi_usage_generation") and b.get("launch_token") != p["kimi_usage_generation"]:
        return {}
    # Subagents and other usage scopes have not been demonstrated by this adapter.
    return b if b["agent_id"] == "main" else {}


def register_kimi_binding(p: dict[str, Any], launch: dict[str, Any], event: dict[str, Any]) -> bool:
    """Internal handoff from a managed launch, not a hook receiver. `launch` must be the
    launcher-owned proof, not fields copied from `event`. No live producer is installed here.
    The caller must have verified pane identity and the SessionStart parent process independently."""
    if (
        m_screen.agent_kind(p) != "kimi"
        or event.get("hook_event_name") != "SessionStart"
        or event.get("source") != "startup"
        or event.get("client_type") != "kimi_code_cli"
    ):
        return False
    if str(event.get("parent_pid") or "") != str(launch.get("pane_pid") or ""):
        return False
    b = {k: launch.get(k) for k in ("generation", "pane_pid", "target", "sessions_root", "workdir_key", "adapter")}
    b.update(session_id=event.get("session_id"), agent_id="main")
    if isinstance(launch.get("token"), str):
        b["launch_token"] = launch["token"]
    if not p.get("pane_pid") or not p.get("join_code"):
        return False
    if any(
        b.get(k) != str(p.get(v) or "") for k, v in (("generation", "join_code"), ("pane_pid", "pane_pid"), ("target", "target"))
    ):
        return False
    if b["sessions_root"] != str(kimi_home()) or b["adapter"] not in (KIMI_ADAPTER, KIMI_NORMALIZED):
        return False
    if not isinstance(b.get("workdir_key"), str) or not KIMI_WORKDIR.fullmatch(str(b["workdir_key"])):
        return False
    if not isinstance(b.get("session_id"), str) or not KIMI_PART.fullmatch(str(b["session_id"])):
        return False
    path = m_store.room().paths.state_dir / "kimi-usage-bindings.json"
    m_store.write_json(path, {**kimi_state(path), p["id"]: b})
    return True


def kimi_resume_binding(p: dict[str, Any], sid: str = "") -> dict[str, Any]:
    """Only the exact managed session with a durable cursor can survive a new process."""
    if not p.get("id") or m_screen.agent_kind(p) != "kimi":
        return {}
    b = kimi_binding(p)
    proof = obj(kimi_state(m_store.room().paths.state_dir / "kimi-launches.json").get(str(p.get("kimi_usage_generation") or "")))
    if not b or not p.get("started_by_room") or (sid and sid != b["session_id"]) or proof.get("reason"):
        return {}
    if any(
        proof.get(k) != b.get(v)
        for k, v in (
            ("token", "launch_token"),
            ("generation", "generation"),
            ("pane_pid", "pane_pid"),
            ("target", "target"),
            ("session_id", "session_id"),
            ("adapter", "adapter"),
            ("sessions_root", "sessions_root"),
            ("workdir_key", "workdir_key"),
        )
    ):
        return {}
    identity = kimi_process_start(str(p.get("pane_pid") or ""))
    if (
        proof.get("id") != p["id"]
        or proof.get("folder") != os.path.realpath(str(p.get("folder") or ""))
        or not proof.get("start_identity")
        or (identity and identity != proof["start_identity"])
    ):
        return {}
    return b if kimi_resume_cursor(p["id"], b) else {}


def kimi_resume_cursor(pid: str, b: dict[str, Any]) -> dict[str, Any]:
    """Validate the old signature and append-only source without copying its contents."""
    if (
        b.get("sessions_root") != str(kimi_home())
        or b.get("adapter") not in (KIMI_ADAPTER, KIMI_NORMALIZED)
        or b.get("agent_id") != "main"
    ):
        return {}
    if (
        not isinstance(b.get("workdir_key"), str)
        or not KIMI_WORKDIR.fullmatch(b["workdir_key"])
        or not isinstance(b.get("session_id"), str)
        or not KIMI_PART.fullmatch(b["session_id"])
    ):
        return {}
    cursor = obj(kimi_state(m_store.room().paths.state_dir / "kimi-usage-cursor.json").get(pid))
    signature = hashlib.sha256(json.dumps(b, sort_keys=True).encode()).hexdigest()
    if cursor.get("binding") != signature or not stamp(cursor.get("since")) or not isinstance(cursor.get("seen"), dict):
        return {}
    at = obj(obj(cursor.get("files")).get("wire"))
    if any(type(at.get(k)) is not int for k in ("ino", "off", "mid")) or at["off"] < 0 or at["mid"] not in (0, 1):
        return {}
    fd = open_under(Path("/"), kimi_file(b))
    if fd is None:
        return {}
    try:
        info = os.fstat(fd)
        return cursor if info.st_ino == at["ino"] and info.st_size >= at["off"] else {}
    finally:
        os.close(fd)


def kimi_file(b: dict[str, Any]) -> Path:
    return Path(b["sessions_root"]) / b["workdir_key"] / b["session_id"] / "agents" / b["agent_id"] / "wire.jsonl"


def kimi_usage_adapter(command: str) -> str:
    """A launcher-owned compatibility check. No config, credentials, model alias or hook
    payload chooses a format. Unknown versions are no-data, never a guessed fallback."""
    try:
        argv = shlex.split(command)
        if not argv:
            return ""
        result = subprocess.run([argv[0], "--version"], capture_output=True, text=True, timeout=2)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    return KIMI_NORMALIZED if result.returncode == 0 and result.stdout.strip() == "2.4.1" else ""


def kimi_process_start(pid: str) -> str:
    """OS observation of the launch process, never a field from hook JSON."""
    if not pid.isdecimal() or int(pid) <= 1:
        return ""
    try:
        got = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", pid], capture_output=True, text=True, timeout=1)
    except (OSError, subprocess.SubprocessError):
        return ""
    return got.stdout.strip() if got.returncode == 0 else ""


def kimi_process_status(pid: str) -> str:
    if not pid.isdecimal() or int(pid) <= 1:
        return "unknown"
    try:
        got = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", pid], capture_output=True, text=True, timeout=1)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if got.returncode == 0:
        return "alive" if got.stdout.strip() else "unknown"
    return "gone" if got.returncode == 1 and not got.stdout.strip() and not got.stderr.strip() else "unknown"


def kimi_workdir_key(folder: str) -> str:
    normalized = folder.replace("\\", "/").rstrip("/")
    slug = re.sub(r"[^a-z0-9._-]+", "-", normalized.split("/")[-1].lower()).strip("-")[:40].strip("-")
    if slug in ("", ".", ".."):
        slug = "workspace"
    return "wd_" + slug + "_" + hashlib.sha256(normalized.encode()).hexdigest()[:12]


def prepare_kimi_launch(pid: str, generation: str, folder: str, adapter: str = "", resume: dict[str, Any] | None = None) -> str:
    """Launcher-owned metadata. Adapter proof belongs to the caller; normal native launches
    are unknown until their protocol has been verified, not inferred from hook model names."""
    token = uuid.uuid4().hex
    path = m_store.room().paths.state_dir / "kimi-launches.json"
    proofs = kimi_state(path)
    # One active launch per participant: old tokens never authorize the next generation.
    proofs = {k: v for k, v in proofs.items() if resume or obj(v).get("id") != pid}
    proofs[token] = {
        "id": pid,
        "token": token,
        "generation": generation,
        "folder": os.path.realpath(folder),
        "at": m_store.now(),
        "sessions_root": str(kimi_home()),
        "workdir_key": kimi_workdir_key(os.path.realpath(folder)),
        "adapter": adapter,
    }
    if resume:
        proofs[token]["resume_binding"] = resume
    m_store.write_json(path, proofs)
    return token


def kimi_launch_command(command: str, pid: str, token: str) -> str:
    # Fixed variable names and quoted values; no credentials or hook payload in the command.
    return (
        shlex.join(
            [
                "env",
                "COLLAB_KIMI_ROOM=" + str(m_store.room().paths.root),
                "COLLAB_KIMI_AGENT=" + pid,
                "COLLAB_KIMI_GENERATION=" + token,
                "KIMI_CODE_HOME=" + str(kimi_home().parent),
            ]
        )
        + " "
        + command
    )


def finish_kimi_launch(p: dict[str, Any], token: str) -> None:
    path = m_store.room().paths.state_dir / "kimi-launches.json"
    proofs = kimi_state(path)
    proof = obj(proofs.get(token))
    if proof.get("id") != p["id"] or proof.get("generation") != p.get("join_code"):
        return
    proof.update(
        pane_pid=str(p.get("pane_pid") or ""),
        target=str(p.get("target") or ""),
        start_identity=kimi_process_start(str(p.get("pane_pid") or "")),
    )
    proofs[token] = proof
    m_store.write_json(path, proofs)


@m_store.room_locked
def collect_kimi_start(event: dict[str, Any]) -> bool:
    """The trusted hook process observes its own parent. Environment only selects a room's
    checked state: all authority comes from the current participant and the launch manifest."""
    token, pid = os.environ.get("COLLAB_KIMI_GENERATION", ""), os.environ.get("COLLAB_KIMI_AGENT", "")
    if not re.fullmatch(r"[0-9a-f]{32}", token) or not pid:
        return False
    p = obj(next((q for q in m_store.read_config_raw().get("participants", []) if q.get("id") == pid), None))
    proof_path = m_store.room().paths.state_dir / "kimi-launches.json"
    proofs = kimi_state(proof_path)
    proof = obj(proofs.get(token))
    parent = str(os.getppid())
    if (
        m_screen.agent_kind(p) != "kimi"
        or p.get("started_by_room") is not True
        or p.get("kimi_usage_generation") != token
        or proof.get("id") != pid
        or proof.get("token") != token
        or not proof.get("start_identity")
        or proof.get("start_identity") != kimi_process_start(parent)
        or proof.get("sessions_root") != str(kimi_home())
        or any(
            str(proof.get(k) or "") != str(p.get(v) or "")
            for k, v in (("pane_pid", "pane_pid"), ("target", "target"), ("generation", "join_code"))
        )
        or parent != str(p.get("pane_pid") or "")
        or proof.get("folder") != os.path.realpath(str(p.get("folder") or ""))
    ):
        return False
    if event.get("hook_event_name") != "SessionStart" or event.get("client_type") != "kimi_code_cli":
        return False
    sid = event.get("session_id")
    if not isinstance(sid, str) or not KIMI_PART.fullmatch(sid):
        return False
    if not isinstance(event.get("cwd"), str) or os.path.realpath(event["cwd"]) != proof["folder"]:
        return False
    bindings_path = m_store.room().paths.state_dir / "kimi-usage-bindings.json"

    def unbind(reason: str) -> bool:
        proof["reason"] = reason
        proofs[token] = proof
        m_store.write_json(proof_path, proofs)
        bindings = kimi_state(bindings_path)
        bindings.pop(pid, None)
        m_store.write_json(bindings_path, bindings)
        return False

    if event.get("source") not in ("startup", "resume"):
        return False
    resume = obj(proof.get("resume_binding"))
    managed_resume = bool(resume) and event.get("source") == "resume" and resume.get("session_id") == sid
    if (
        (event.get("source") == "resume" and not managed_resume)
        or (resume and not managed_resume)
        or (proof.get("session_id") and proof["session_id"] != sid)
    ):
        return unbind("session changed or resumed without a new managed launch")
    if proof.get("reason"):
        return False  # invalidated generations cannot be revived by replaying an old startup
    if proof.get("adapter") not in (KIMI_ADAPTER, KIMI_NORMALIZED):
        return unbind("native adapter has not been verified; no token data")
    next_binding = {k: proof.get(k) for k in ("generation", "pane_pid", "target", "sessions_root", "workdir_key", "adapter")}
    next_binding.update(session_id=sid, agent_id="main", launch_token=token)
    carried = (kimi_resume_cursor(pid, resume) or kimi_resume_cursor(pid, next_binding)) if managed_resume else {}
    if managed_resume and not proof.get("session_id") and not carried:
        return unbind("managed resume cursor unavailable; no token data")
    if not register_kimi_binding(p, proof, {**event, "source": "startup", "parent_pid": int(parent)}):
        return False
    # The manifest is created before exec. Start at zero even when the async hook arrives
    # after the first usage; never baseline at that late EOF. Repeated hooks do not rewind.
    b = kimi_binding(p)
    signature = hashlib.sha256(json.dumps(b, sort_keys=True).encode()).hexdigest()
    cursor = m_store.room().paths.state_dir / "kimi-usage-cursor.json"
    state = kimi_state(cursor)
    if not proof.get("session_id") and obj(state.get(pid)).get("binding") != signature:
        state[pid] = (
            {**carried, "binding": signature}
            if managed_resume
            else {**asdict(Cursor(str(proof["at"]))), "binding": signature, "observed": False}
        )
        m_store.write_json(cursor, state)
    proof["session_id"] = sid
    proofs = {k: v for k, v in proofs.items() if obj(v).get("id") != pid or k == token}
    proofs[token] = proof
    m_store.write_json(proof_path, proofs)
    return True


def _read_records(path: Path, at: dict[str, Any]) -> dict[str, Any] | None:
    dev, ino, off, mid = at.get("dev"), at.get("ino"), at.get("off"), at.get("mid")
    if (
        not isinstance(dev, int)
        or not isinstance(ino, int)
        or not isinstance(off, int)
        or not isinstance(mid, int)
        or ino < 0
        or off < 0
        or mid not in (0, 1)
    ):
        return None
    fd = open_under(Path("/"), path)
    if fd is None:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if info.st_dev != dev or info.st_ino != ino or info.st_size < off:
            return None
        stream.seek(off)
        data = stream.read(CHUNK)
    start = 0
    if mid:
        first = data.find(b"\n")
        if first < 0:
            return {"records": [], "cursor": {"dev": dev, "ino": ino, "off": off + len(data), "mid": 1}, "eof": False}
        start = first + 1
    end = data.rfind(b"\n") + 1
    if end <= start:
        if start == len(data):
            return {
                "records": [],
                "cursor": {"dev": dev, "ino": ino, "off": off + start, "mid": 0},
                "eof": len(data) < CHUNK,
            }
        if len(data) == CHUNK:
            return {"records": [], "cursor": {"dev": dev, "ino": ino, "off": off + len(data), "mid": 1}, "eof": False}
        return {"records": [], "cursor": {"dev": dev, "ino": ino, "off": off + start, "mid": 0}, "eof": False}
    cursor = {"dev": dev, "ino": ino, "off": off + end, "mid": 0}
    try:
        records: list[tuple[dict[str, Any], int]] = []
        position = off + start
        for raw in data[start:end].splitlines(keepends=True):
            position += len(raw)
            record: Any = json.loads(raw)
            if not isinstance(record, dict):
                return None
            records.append((cast(dict[str, Any], record), position))
    except ValueError:
        return None
    return {"records": records, "cursor": cursor, "eof": len(data) < CHUNK and end == len(data)}


def _terminal_event(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    record = cast(dict[str, Any], raw)
    if record.get("type") == "context.append_loop_event":
        if record.get("agentId") != "main":
            return {}
        event = obj(record.get("event"))
        return event if event.get("type") in ("step.end", "turn.step.interrupted") else {}
    if record.get("agentId") != "main" or record.get("type") not in ("step.end", "turn.step.interrupted", "turn.ended"):
        return {}
    return record


def kimi_quota_boundary(p: dict[str, Any]) -> dict[str, Any]:
    b = kimi_binding(p)
    if not b:
        return {}
    path = kimi_file(b)
    fd = open_under(Path("/"), path)
    if fd is None:
        return {}
    try:
        info = os.fstat(fd)
    finally:
        os.close(fd)
    start = max(0, info.st_size - CHUNK)
    at = {"dev": info.st_dev, "ino": info.st_ino, "off": start, "mid": int(start > 0)}
    latest: tuple[dict[str, Any], int] | None = None
    step: dict[str, Any] = {}
    deadline = time.monotonic() + 2
    for _ in range(32):
        if time.monotonic() >= deadline:
            return {}
        progress = _read_records(path, at)
        if progress is None:
            return {}
        for raw, offset in progress["records"]:
            event = _terminal_event(raw)
            if event.get("type") == "step.end" and event.get("finishReason") == "error":
                step = event
            if event.get("type") != "turn.ended" or event.get("reason") != "failed":
                continue
            error = obj(event.get("error"))
            if (
                error.get("name") == "APIProviderQuotaExhaustedError"
                and obj(error.get("details")).get("statusCode") == 429
                and isinstance(event.get("turnId"), int)
            ):
                latest = (event, offset)
        if progress["eof"] or not progress["records"]:
            break
        at = progress["cursor"]
    else:
        return {}
    if latest is None:
        return {}
    event, offset = latest
    turn = event.get("turnId")
    if not isinstance(turn, int):
        return {}
    return {
        "binding": hashlib.sha256(json.dumps(b, sort_keys=True).encode()).hexdigest(),
        "dev": info.st_dev,
        "ino": info.st_ino,
        "off": offset,
        "mid": 0,
        "turn": turn,
        "step_uuid": step.get("uuid") if step.get("turnId") == turn else "",
        "trace_id": event.get("traceId") if isinstance(event.get("traceId"), str) else "",
        "fsm": {"completed_steps": [], "latest_completed": None},
    }


def kimi_quota_recovery_state(p: dict[str, Any], boundary: dict[str, Any]) -> dict[str, Any]:
    b = kimi_binding(p)
    stored_cursor = boundary.get("cursor", boundary)
    at = cast(dict[str, Any], stored_cursor).copy() if isinstance(stored_cursor, dict) else {}
    empty = {"done": False, "recovered": False, "cursor": at}
    if not b:
        return empty
    signature = hashlib.sha256(json.dumps(b, sort_keys=True).encode()).hexdigest()
    if boundary.get("binding") != signature or not isinstance(boundary.get("turn"), int):
        return empty
    stored_fsm = obj(at.get("fsm", boundary.get("fsm", {"completed_steps": [], "latest_completed": None})))
    stored_steps = stored_fsm.get("completed_steps")
    latest_completed = stored_fsm.get("latest_completed")
    if not isinstance(stored_steps, list):
        return empty
    steps = cast(list[Any], stored_steps)
    if (
        any(isinstance(turn, bool) or not isinstance(turn, int) or turn <= boundary["turn"] for turn in steps)
        or (latest_completed is not None and (isinstance(latest_completed, bool) or not isinstance(latest_completed, int)))
        or (latest_completed is not None and latest_completed not in steps)
    ):
        return empty
    progress = _read_records(kimi_file(b), at)
    if progress is None:
        return empty
    completed_steps: set[int] = set(steps)
    for raw, _ in progress["records"]:
        event = _terminal_event(raw)
        turn = event.get("turnId")
        if not isinstance(turn, int):
            continue
        kind = event.get("type")
        if kind == "step.end":
            if (
                event.get("finishReason") == "end_turn"
                and event.get("providerFinishReason") == "completed"
                and event.get("rawFinishReason") == "stop"
                and turn > boundary["turn"]
            ):
                completed_steps.add(turn)
            elif event.get("finishReason") == "error":
                latest_completed = None
        elif kind == "turn.step.interrupted":
            latest_completed = None
        elif kind == "turn.ended":
            if event.get("reason") == "failed":
                latest_completed = None
            elif event.get("reason") == "completed" and turn in completed_steps and turn > boundary["turn"]:
                latest_completed = turn
    fsm = {"completed_steps": sorted(completed_steps), "latest_completed": latest_completed}
    cursor = {key: value for key, value in boundary.items() if key not in ("cursor", "fsm")}
    return {
        "done": progress["eof"],
        "recovered": latest_completed in completed_steps,
        "cursor": {**cursor, **progress["cursor"], "fsm": fsm},
        "fsm": fsm,
    }


def kimi_quota_recovered(p: dict[str, Any], boundary: dict[str, Any]) -> bool:
    progress = kimi_quota_recovery_state(p, boundary)
    return progress["done"] and progress["recovered"]


def save_usage_cursor(path: Path, state: dict[str, Any], updates: dict[str, Any] | None, pid: str, before: Any) -> None:
    if updates is None:
        m_store.write_json(path, state)
    else:
        updates[path.name] = {"pid": pid, "before": before, "after": state[pid]}


def kimi_increment(p: dict[str, Any], *, updates: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    b = kimi_binding(p)
    if not b:
        return []
    path = kimi_file(b)
    # Opening from / also refuses a link substituted for the sessions root itself.
    fd = open_under(Path("/"), path)
    if fd is None:
        return []
    os.close(fd)
    cp = m_store.room().paths.state_dir / "kimi-usage-cursor.json"
    state = kimi_state(cp)
    before = state.get(p["id"])
    signature = hashlib.sha256(json.dumps(b, sort_keys=True).encode()).hexdigest()
    old = obj(state.get(p["id"]))
    stored = Cursor.load(old) if old.get("binding") == signature else None
    fresh = stored is None
    cur = stored or Cursor(m_store.now())
    at = cur.files.get("wire", {})
    lines, after = read_new(Path("/"), path, at, to_end=fresh)
    out: list[dict[str, Any]] = []
    for raw in lines:
        r = parse(raw.decode("utf-8", errors="replace"))
        if r.get("type") != "usage.record" or r.get("agentId") != b["agent_id"] or r.get("usageScope") != "turn":
            continue
        model, ms, u = clean(r.get("model")), r.get("time"), obj(r.get("usage"))
        if model is None or isinstance(ms, bool) or not isinstance(ms, int):
            continue
        try:
            ts = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat(timespec="milliseconds")
        except (ValueError, OverflowError, OSError):
            continue
        names = ("inputOther", "output", "inputCacheRead", "inputCacheCreation")
        nums = [u.get(k) for k in names]
        if any(v is not None and (isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 10**15) for v in nums):
            continue
        # A durable record has no request id: canonical numeric identity survives copied/reformatted lines.
        key = hashlib.sha256(json.dumps([ms, model, *nums]).encode()).hexdigest()
        if key in cur.seen or ts < cur.since:
            continue
        cur.seen[key] = ts
        known = any(isinstance(v, int) and v > 0 for v in nums)
        vals = dict(zip(FIELDS, nums)) if known else dict.fromkeys(FIELDS)
        vals["cache_write"] = None
        out.append(
            {
                "model": model,
                "effort": None,
                **vals,
                "thinking": None,
                "coverage": "partial" if known else "unknown",
                "reason": "reported-cli 2.4.1; cache-write/thinking unknown" if known else "reported-cli 2.4.1; zero-or-missing",
                "answers": 1,
                "ctx_max": None,
                "ctx_last": None,
                "from_ts": ts,
                "to_ts": ts,
            }
        )
    cur.files = {"wire": after}
    if len(cur.seen) > SEEN_MAX:
        keep = sorted(cur.seen.items(), key=lambda kv: kv[1])[-SEEN_MAX:]
        cur.seen, cur.since = dict(keep), keep[0][1]
    state[p["id"]] = {**asdict(cur), "binding": signature, "observed": bool(out) or (not fresh and old.get("observed") is True)}
    save_usage_cursor(cp, state, updates, p["id"], before)
    return out


def increment(p: dict[str, Any], *, updates: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """What the agent spent since the last call: one record per model and effort — {model, effort, input,
    output, cache_read, cache_write, answers, ctx_max, ctx_last, from_ts, to_ts}: the answers counted, the
    longest context of one (input + cache read + cache write) and that of the latest, times of the first
    and last answer. Nothing
    new — []. The first call for an agent starts from now: its past is not counted against the task it is
    on. Called under the room's lock by its one writer (delivery's tick); the cursor is in the room's state,
    so a restart of delivery neither counts again nor loses a piece.
    With updates supplied, stage cursor writes there for the journal transaction."""
    kind = m_screen.agent_kind(p)
    if kind == "kimi":
        return kimi_increment(p, updates=updates)
    if kind not in ("claude", "codex"):
        return []
    state = m_store.read_json(cursor_path())
    before = state.get(p["id"])
    stored = Cursor.load(state.get(p["id"]))
    fresh = stored is None
    cur = stored or Cursor(m_store.now())
    base = claude_home() if kind == "claude" else codex_home()
    if kind == "claude":
        files = claude_files(p)
    else:
        files, cur.code = codex_file(p, next(iter(cur.files), ""), cur.code)
    groups: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    read_to: dict[str, dict[str, int]] = {}
    compacted: list[float] = []
    left = BUDGET
    for path in files:
        at = cur.files.get(str(path), {})
        if left <= 0:
            # The call's budget spent: this log waits where it was, for the next call (a day's backlog of
            # 400 MB after a long stop — say, ack and delivery waited for the room's lock all that while).
            read_to[str(path)] = at
            continue
        # A first look starts from now: what is in the log already is not this task's. A log that appears
        # later (a resumed session, a new subagent) is read from its start: its copies of old answers are
        # told by their ids and times below.
        lines, read_to[str(path)] = read_new(base, path, at, to_end=fresh, limit=left)
        if fresh and kind == "codex":
            last_context(base, path, cur.ctx)
        if fresh:
            pass  # a first look notes the end and reads nothing: no budget spent
        elif read_to[str(path)].get("ino") == at.get("ino"):
            left -= read_to[str(path)].get("off", 0) - at.get("off", 0)
        else:
            left -= read_to[str(path)].get("off", 0)
        for raw in lines:
            if kind == "codex" and COMPACTED in raw[:200]:
                compacted.append(time.time())
            if MARKS[kind] not in raw:
                continue
            line = raw.decode("utf-8", errors="replace")
            got = read_claude(line) if kind == "claude" else read_codex(line, cur.ctx)
            if not got:
                continue
            key, ts, nums, model, effort = got
            # A copy of an answer (a resumed session's new file repeats the old ones, a reply is written
            # in several lines with one id): counted once. Older than the cursor: not this cursor's.
            if not ts or not KEY.fullmatch(key) or key in cur.seen or ts < cur.since:
                continue
            cur.seen[key] = ts
            g = groups.setdefault(
                (model, effort), {"model": model, "effort": effort, **dict.fromkeys(FIELDS, 0), "answers": 0, "ctx_max": 0}
            )
            for f in FIELDS:
                g[f] += nums[f]
            g["answers"] += 1
            size = nums["input"] + nums["cache_read"] + nums["cache_write"]
            g["ctx_max"] = max(g["ctx_max"], size)
            if ts >= (g.get("to_ts") or ""):
                g["ctx_last"] = size  # the context as it is now: what the latest answer read
            g["from_ts"] = min(g.get("from_ts") or ts, ts)
            g["to_ts"] = max(g.get("to_ts") or ts, ts)
    cur.files = read_to  # a log no longer the agent's (its session ended) is not looked at again
    if len(cur.seen) > SEEN_MAX:
        keep = sorted(cur.seen.items(), key=lambda kv: kv[1])[-SEEN_MAX:]
        cur.seen, cur.since = dict(keep), keep[0][1]
    state[p["id"]] = asdict(cur)
    save_usage_cursor(cursor_path(), state, updates, p["id"], before)
    if compacted:
        seen = m_store.read_json(m_store.compacted_path())
        save_usage_cursor(m_store.compacted_path(), {**seen, p["id"]: compacted[-1]}, updates, p["id"], seen.get(p["id"]))
    return [g for g in groups.values() if any(g[f] for f in FIELDS)]
