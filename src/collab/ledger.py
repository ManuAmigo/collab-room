from __future__ import annotations
import contextlib
import csv
import io
import json
import math
import os
import sqlite3
import sys
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable, TypeVar, cast
import collab.text as m_text
import collab.store as m_store
import collab.tasks as m_tasks
import collab.screen as m_screen
import collab.usage as m_usage


# The tokens' ledger («statistics to plan by, each portion of spending with the
# task it went to»). Primary are the append-only files — tasks.jsonl, usage.jsonl (here), merge-log.jsonl
# the SQLite base is derived from them, built anew for each report, and asked by queries.
USAGE_FIELDS = ("input", "output", "cache_read", "cache_write")
# Answers of the model in the stretch and the longest context of one (input + cache read + cache write):
# what /clear and a cheaper model change. A line from before them has neither — 0.
ANSWER_FIELDS = ("answers", "ctx_max")


def usage_path() -> Path:
    return m_store.room().paths.state_dir / "usage.jsonl"


def context_path() -> Path:
    return m_store.room().paths.state_dir / "context.json"


def context_of(pid: str) -> tuple[int, str]:
    """The agent's context as its latest answer counted read it (tokens) and that answer's time; (0, "") —
    not counted yet."""
    seen = m_store.read_json(context_path()).get(pid)
    if not isinstance(seen, dict) or not isinstance(seen.get("ctx"), int) or not isinstance(seen.get("at"), str):
        return 0, ""
    return seen["ctx"], seen["at"]


def usage_line(
    pid: str, label: str, kind: str, entry: dict[str, Any], task: dict[str, Any] | None, wakeups: int, split: bool
) -> dict[str, Any]:
    """One line of usage.jsonl: one agent's spending of one model over [from, to], put down to the task it
    was on at «to» (split: a task began or ended inside the stretch). Numbers and labels only."""
    line: dict[str, Any] = {
        "at": m_store.now(),
        "pid": pid,
        "agent": label,
        "agent_kind": kind,
        "model": str(entry.get("model") or ""),
        "effort": entry.get("effort"),
        "from": str(entry.get("from_ts") or ""),
        "to": str(entry.get("to_ts") or ""),
        "wakeups": int(wakeups),
        "task": task["id"] if task else None,
        "task_kind": task.get("kind") if task else None,
        "lane": task.get("lane") if task else None,
        "task_by": task.get("by") if task else None,
        "split": bool(split),
    }
    for name in (*USAGE_FIELDS, *ANSWER_FIELDS):
        line[name] = int(entry.get(name) or 0)
    return line


def append_usage(lines: Iterable[dict[str, Any]]) -> None:
    """Appended through the room's checked state file — never through a link put in its place (store)."""
    text = "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines)
    if text:
        m_store.write_state(usage_path(), text, append=True)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """The records of an append-only file, a broken or half-written line left out; missing — none."""
    try:
        text = m_store.read_state_text(path)
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        with contextlib.suppress(ValueError):
            record: object = json.loads(line)
            if isinstance(record, dict):
                out.append(cast("dict[str, Any]", record))
    return out


SCHEMA = """
CREATE TABLE usage (at TEXT, pid TEXT, agent TEXT, agent_kind TEXT, model TEXT, effort TEXT, t_from TEXT,
    t_to TEXT, input INTEGER, output INTEGER, cache_read INTEGER, cache_write INTEGER, wakeups INTEGER,
    task TEXT, task_kind TEXT, lane TEXT, task_by TEXT, split INTEGER, answers INTEGER, ctx_max INTEGER);
CREATE TABLE task_events (at TEXT, event TEXT, pid TEXT, task TEXT, kind TEXT, lane TEXT, by TEXT, commit_sha TEXT);
CREATE TABLE runs (at TEXT, by TEXT, what TEXT, who TEXT, exit INTEGER, seconds REAL, red INTEGER, said TEXT);
"""


_T = TypeVar("_T")


# The state files are anybody's in the room: what goes into the base is a value SQLite takes, or
# None — a list, an object or a number past its integer once ended the whole report.
INT_LIMIT = 2**63 - 1
# Tokens a line may count: far above any real stretch, and a sum of thousands of lines still fits SQLite's integer.
TOKENS_LIMIT = 10**15


def cell(v: object) -> str | int | float | None:
    """A value from a state file as SQLite takes it: text, a number in range, or None for anything else."""
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v if -INT_LIMIT <= v <= INT_LIMIT else None
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    return None


def counts(u: dict[str, Any]) -> tuple[int, ...] | None:
    """A usage line's numbers, or None: a field that is not a count — not a number, below 0, past the limit —
    leaves the line out, as a broken line is — never the whole report."""
    out: list[int] = []
    for name in (*USAGE_FIELDS, "wakeups", *ANSWER_FIELDS):
        v = u.get(name) or 0
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= TOKENS_LIMIT:
            return None
        out.append(v)
    return tuple(out)


def build_base(
    target: Path,
    usage: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    ask: Callable[[sqlite3.Connection], _T],
) -> _T:
    """The derived base, built anew in a file of its own made beside `target` (O_EXCL, 600), asked through the
    very connection that built it, then put in its place by rename: an existing path — a link planted there
    too — is never opened, and no path is opened again by name — a file URI with «#» or «?» in a folder's
    name cut off «mode=ro» and made an empty file outside the state folder."""
    fd, tmp = tempfile.mkstemp(prefix=".usage-base-", suffix=".tmp", dir=str(target.parent))
    os.close(fd)
    try:
        with contextlib.closing(sqlite3.connect(tmp)) as db:
            db.executescript(SCHEMA)
            db.executemany(
                "INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        *(cell(u.get(k)) for k in ("at", "pid", "agent", "agent_kind", "model", "effort", "from", "to")),
                        *n[:5], *(cell(u.get(k)) for k in ("task", "task_kind", "lane", "task_by")), int(bool(u.get("split"))),
                        *n[5:],
                    )
                    for u in usage
                    if (n := counts(u)) is not None
                ],
            )  # fmt: skip
            db.executemany(
                "INSERT INTO task_events VALUES (?,?,?,?,?,?,?,?)",
                [tuple(cell(e.get(k)) for k in ("at", "event", "pid", "task", "kind", "lane", "by", "commit")) for e in tasks],
            )
            db.executemany(
                "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)",
                [
                    (
                        cell(r.get("at")), cell(r.get("by")), cell(r.get("what")), json.dumps(r.get("who"), ensure_ascii=False),
                        cell(r.get("exit")), cell(r.get("seconds")), len(red) if isinstance(red := r.get("red"), list) else 0,
                        cell(r.get("said")),
                    )
                    for r in runs
                ],
            )  # fmt: skip
            db.commit()
            answer = ask(db)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
    return answer


# What a report groups by: the column, and the name shown for «no task» (spending outside any task is a number too).
GROUPS = {"kind": "task_kind", "agent": "agent", "model": "model", "lane": "lane", "task": "task"}


def report_rows(db: sqlite3.Connection, by: str, since: str = "") -> list[tuple[Any, ...]]:
    """Per group: tasks, tokens in, out, cache read, cache write, cache share of what was read, wakeups,
    tokens per task — input + output + cache write; cache reads, cheap, are not in it; the model's answers,
    the mean and the longest context of one — the mean over the lines that counted answers. «(no task)» where
    nothing was taken. `since` is compared as text with the journal's UTC times (2026-10-19, or with «+00:00»)."""
    column = GROUPS[by]
    query = f"""
        SELECT COALESCE({column}, '(no task)') AS g, COUNT(DISTINCT task), SUM(input), SUM(output), SUM(cache_read),
               SUM(cache_write), SUM(wakeups), SUM(answers), MAX(ctx_max),
               SUM(CASE WHEN answers > 0 THEN input + cache_read + cache_write ELSE 0 END)
        FROM usage WHERE t_to >= ? GROUP BY g ORDER BY SUM(input + output + cache_write) DESC
    """
    rows: list[tuple[Any, ...]] = []
    for g, tasks, inp, out, cread, cwrite, wakeups, answers, ctx_max, ctx_sum in db.execute(query, (since,)):
        read = inp + cread + cwrite
        share = round(100 * cread / read) if read else 0
        spent = inp + out + cwrite
        per_task = round(spent / tasks) if tasks else spent
        mean = round(ctx_sum / answers) if answers else 0
        rows.append((g, tasks, inp, out, cread, cwrite, f"{share}%", wakeups, per_task, answers, mean, ctx_max))
    return rows


HEADER = (
    "group", "tasks", "input", "output", "cache read", "cache write", "cache share", "wakeups", "per task",
    "answers", "mean context", "max context",
)  # fmt: skip


def shown(v: object, fmt: str) -> str:
    """A cell as the person's terminal or spreadsheet gets it: text through the white list
    — no control codes, no invisible format characters, one line, 80 columns at most; in markdown no «|»
    to break the table; in csv a leading = + - @ made text, not a formula."""
    if not isinstance(v, str):
        return str(v)
    text = m_text.clean_field(v, 80)
    if fmt == "csv":
        return "'" + text if text[:1] in ("=", "+", "-", "@") else text
    return text.replace("|", "¦")


def render(rows: list[tuple[Any, ...]], fmt: str) -> str:
    cells = [[shown(c, fmt) for c in r] for r in rows]
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(HEADER)
        writer.writerows(cells)
        return buf.getvalue()
    lines = ["| " + " | ".join(HEADER) + " |", "|" + "---|" * len(HEADER)]
    lines += ["| " + " | ".join(r) + " |" for r in cells]
    return "\n".join(lines) + "\n"


def unread_notes() -> list[str]:
    """«<agent> (<kind>): no data — <why>» for each of the room's agents whose spending is not read."""
    try:
        config = m_store.read_config_raw()
    except SystemExit:  # no room's settings: no agents to name
        return []
    notes: list[str] = []
    for p in config.get("participants") or []:
        if not isinstance(p, dict) or not any(k in p for k in ("idle", "busy", "attention")):
            continue  # a person spends no tokens
        p = cast("dict[str, Any]", p)
        why = m_usage.unread(p)
        if why:
            who = m_text.clean_field(str(p.get("label") or p.get("id")), 80)
            notes.append(f"{who} ({m_screen.agent_kind(p) or 'agent'}): no data — {why}")
    return notes


def report(by: str, since: str = "", fmt: str = "md") -> str:
    """The base built anew from the room's files, asked, and the table returned; in markdown, the agents
    whose spending is not read under it (the csv stays a table: unread_notes gives them)."""
    rp = m_store.room().paths
    m_store.private_dir(rp.state_dir)
    rows = build_base(
        rp.state_dir / "usage.db",
        read_jsonl(usage_path()),
        read_jsonl(rp.state_dir / "tasks.jsonl"),
        read_jsonl(rp.state_dir / "merge-log.jsonl"),
        lambda db: report_rows(db, by, since),
    )
    notes = unread_notes() if fmt != "csv" else []
    return render(rows, fmt) + "".join(f"\n{n}" for n in notes) + ("\n" if notes else "")


def task_at(events: list[dict[str, Any]], pid: str, ts: str) -> dict[str, Any] | None:
    """The task `pid` was on at `ts`, from tasks.jsonl (oldest first, the room's UTC times): its last take at
    or before `ts`, unless a done or a leave of that task came between."""
    current: dict[str, Any] | None = None
    for e in events:
        if e.get("pid") != pid:
            continue
        if str(e.get("at") or "") > ts:
            continue  # not «break»: one line with a time to come, put first, left all the rest «no task»
        if e.get("event") == "take":
            current = {"id": e.get("task"), "kind": e.get("kind"), "lane": e.get("lane"), "by": e.get("by")}
        elif e.get("event") in ("done", "left") and current and e.get("task") == current["id"]:
            current = None
    return current


# Each time the deliverer types into an agent's window — a wake: the agent reads its context again;
# the room's own typings (the joining check, «back from its limit») too. Kept in the deliverer's memory: a restart
# loses the wakes not yet written — good enough for an estimate.
WAKEUPS: dict[str, int] = {}


def woke(pid: str) -> None:
    WAKEUPS[pid] = WAKEUPS.get(pid, 0) + 1


@m_store.room_locked  # usage.increment moves its cursor in the room's state
def record(p: dict[str, Any]) -> int:
    """The agent's spending since the last look as lines of usage.jsonl, each put down to the task at its end
    (split: the task changed inside the stretch); the wakes counted since go with the first. Nothing spent —
    nothing written, and the wakes wait for the spending they cause."""
    entries = m_usage.increment(p)
    if not entries:
        return 0
    pid = p["id"]
    wakes, events = WAKEUPS.pop(pid, 0), m_tasks.task_events()
    lines: list[dict[str, Any]] = []
    for i, e in enumerate(entries):
        task = task_at(events, pid, str(e.get("to_ts") or ""))
        began = task_at(events, pid, str(e.get("from_ts") or ""))
        split = (began or {}).get("id") != (task or {}).get("id")
        lines.append(usage_line(pid, p.get("label") or pid, m_screen.agent_kind(p), e, task, wakes if i == 0 else 0, split))
    append_usage(lines)
    latest = max(entries, key=lambda e: str(e.get("to_ts") or ""))
    contexts = m_store.read_json(context_path())
    contexts[pid] = {"ctx": int(latest.get("ctx_last") or 0), "at": str(latest.get("to_ts") or "")}
    m_store.write_json(context_path(), contexts)
    return len(lines)


EVERY = 60.0  # seconds between two looks at one agent's session log: often enough for a report, light on the tick
_LOOKED: dict[str, float] = {}
_SAID: dict[str, str] = {}  # the last counting error said for each agent: said once, not every minute


def tick(config: dict[str, Any]) -> None:
    """From the deliverer's tick, the one writer of usage.jsonl: each agent at most once in EVERY seconds.
    Counting is never delivery's trouble: an error is said and the tick goes on."""
    moment = time.monotonic()
    for p in config["participants"]:
        if not any(k in p for k in ("idle", "busy", "attention")) or moment - _LOOKED.get(p["id"], -EVERY) < EVERY:
            continue
        _LOOKED[p["id"]] = moment
        try:
            record(p)
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            if _SAID.get(p["id"]) != str(exc):
                _SAID[p["id"]] = str(exc)
                print(f"usage of {p['id']} not counted: {m_text.clean_field(str(exc), 300)}", file=sys.stderr)
        else:
            _SAID.pop(p["id"], None)
