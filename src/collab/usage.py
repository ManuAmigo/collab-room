"""Token spend of the room's agents, from their own session logs — numbers only.

What an agent spent since the last look: input, output, cache read and cache write, by model and effort.
Read only, never a word of the conversation: a line is parsed for its numbers and its model's name, and
nothing else of it leaves this module.
"""

from __future__ import annotations
import datetime as dt
import json
import os
import re
import stat
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


def codex_file(p: dict[str, Any], known: str) -> list[Path]:
    """The agent's Codex session log: the one found before while it is there; else found again —
    a scan of the sessions folder, so not on every look."""
    logs = codex_home()
    # The cursor is a file in the room's state, anyone's to edit: its path counts only inside Codex's own
    # sessions folder — nothing outside ~/.claude and ~/.codex is opened.
    if known and os.path.isfile(known) and Path(os.path.realpath(known)).is_relative_to(os.path.realpath(logs)):
        return [Path(known)]
    sid = m_screen.agent_session(p)
    if not sid:
        return []
    try:
        found = sorted(logs.rglob(f"rollout-*{sid}.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return []
    return found[:1]


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
        if codex_file(p, str(known)):
            return ""
        return f"no log of its session in {m_text.tilde(str(codex_home()))} (found by its connection check)"
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
        f = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
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
    (id -> time), and a Codex log's recent turns (turn id -> model, effort)."""

    since: str
    files: dict[str, dict[str, int]] = field(default_factory=lambda: {})
    seen: dict[str, str] = field(default_factory=lambda: {})
    ctx: dict[str, list[str | None]] = field(default_factory=lambda: {})

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
        return cls(r["since"], files, seen, ctx)


def increment(p: dict[str, Any]) -> list[dict[str, Any]]:
    """What the agent spent since the last call: one record per model and effort — {model, effort, input,
    output, cache_read, cache_write, answers, ctx_max, ctx_last, from_ts, to_ts}: the answers counted, the
    longest context of one (input + cache read + cache write) and that of the latest, times of the first
    and last answer. Nothing
    new — []. The first call for an agent starts from now: its past is not counted against the task it is
    on. Called under the room's lock by its one writer (delivery's tick); the cursor is in the room's state,
    so a restart of delivery neither counts again nor loses a piece."""
    kind = m_screen.agent_kind(p)
    if kind not in ("claude", "codex"):
        return []
    state = m_store.read_json(cursor_path())
    stored = Cursor.load(state.get(p["id"]))
    fresh = stored is None
    cur = stored or Cursor(m_store.now())
    base = claude_home() if kind == "claude" else codex_home()
    files = claude_files(p) if kind == "claude" else codex_file(p, next(iter(cur.files), ""))
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
    m_store.write_json(cursor_path(), state)
    if compacted:
        seen = m_store.read_json(m_store.compacted_path())
        m_store.write_json(m_store.compacted_path(), {**seen, p["id"]: compacted[-1]})
    return [g for g in groups.values() if any(g[f] for f in FIELDS)]
