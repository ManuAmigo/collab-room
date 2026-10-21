"""Tasks as data: which task each participant is on, so what an agent
spends can be counted against the task it was spent on — by kind, lane, role, and «no task» too.

Two files in the room's state: tasks.jsonl, only appended — every take, every leave (a take of another
task while one is on), every done, with its time; and tasks.json — each participant's current task.
Only ids and labels, never a message's words. One current task each: a review taken in the middle of
one's own work leaves it, and the log says when it was left and taken up again."""

from __future__ import annotations
import datetime as dt
import json
import re
from typing import Any
import collab.store as m_store

# The kinds of work a plan names: a report sums by them, so they are a fixed set.
KINDS = ("refactor", "fix", "review", "diagnosis", "research", "tests", "docs", "coordination")
LANES = ("A", "B", "C")
# A task's id as the plan and the room write it — 4.2, T1, fix-12: letters (any script), digits,
# «.», «-», «_». No spaces, no control codes: the id goes into a report's table as it is.
TASK_ID = re.compile(r"[^\W_][\w.\-]{0,39}")
COMMIT = re.compile(r"[0-9a-f]{7,40}")


def check_task_id(task: str) -> str:
    if not TASK_ID.fullmatch(task):
        m_store.fail(f"a task id is letters, digits, «.», «-» or «_», at most 40, starting with a letter or digit: {task!r}")
    return task


def is_task_record(rec: Any) -> bool:
    """A current task as take() writes it. tasks.json is a file in the room's state anyone there can
    edit: a «task» holding ESC codes went to the person's terminal by `task show`, and one without a
    kind made it fail — so every field is checked on reading, not only on writing."""
    if not isinstance(rec, dict):
        return False
    task, kind, lane, by, since = (rec.get(k) for k in ("task", "kind", "lane", "by", "since"))
    if not (isinstance(task, str) and TASK_ID.fullmatch(task) and kind in KINDS and lane in ("", *LANES)):
        return False
    if not (by == "" or (isinstance(by, str) and TASK_ID.fullmatch(by))):
        return False
    if not isinstance(since, str):
        return False
    try:
        dt.datetime.fromisoformat(since)  # the time m_store.now() wrote, nothing else
    except ValueError:
        return False
    return True


def current_tasks() -> dict[str, dict[str, Any]]:
    """Every participant's current task: {pid: {"task", "kind", "lane", "by", "since"}} — only records
    that are what take() writes; anything else in the file is skipped."""
    rp = m_store.room().paths
    data = m_store.read_json(rp.state_dir / "tasks.json")
    return {pid: rec for pid, rec in data.items() if is_task_record(rec)}


def current_task(pid: str) -> dict[str, Any] | None:
    """`pid`'s current task for the usage journal: {"id", "kind", "lane", "by", "since"}, or
    None — its spending then goes to «no task». A done's own time is in its record in tasks.jsonl."""
    rec = current_tasks().get(pid)
    if not rec:
        return None
    return {
        "id": rec["task"],
        "kind": rec.get("kind", ""),
        "lane": rec.get("lane", ""),
        "by": rec.get("by", ""),
        "since": rec.get("since", ""),
    }


def task_events() -> list[dict[str, Any]]:
    """The log, oldest first; a line that is not a record is skipped (a torn last line of a crash)."""
    rp = m_store.room().paths
    path = rp.state_dir / "tasks.jsonl"
    try:
        text = m_store.read_state_text(path)
    except FileNotFoundError:
        return []
    out = []
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def log_task_event(rec: dict[str, Any]) -> None:
    rp = m_store.room().paths
    m_store.write_state(rp.state_dir / "tasks.jsonl", json.dumps(rec, ensure_ascii=False) + "\n", append=True)


@m_store.room_locked
def task_take(pid: str, task: str, kind: str, lane: str = "", by: str = "") -> dict[str, Any]:
    """`pid` is on `task` from now. The one it was on, if another, is left — not done."""
    check_task_id(task)
    if kind not in KINDS:
        m_store.fail(f"kind is one of: {', '.join(KINDS)}")
    if lane and lane not in LANES:
        m_store.fail(f"lane is one of: {', '.join(LANES)}")
    rp = m_store.room().paths
    at = m_store.now()
    data = current_tasks()
    was = data.get(pid)
    if was and was["task"] != task:
        log_task_event({"at": at, "event": "left", "pid": pid, "task": was["task"]})
    rec = {"task": task, "kind": kind, "lane": lane, "by": by, "since": at}
    log_task_event({"at": at, "event": "take", "pid": pid, "task": task, "kind": kind, "lane": lane, "by": by})
    data[pid] = rec
    m_store.write_json(rp.state_dir / "tasks.json", data)
    return rec


@m_store.room_locked
def task_done(pid: str, task: str, commit: str = "") -> bool:
    """`pid` finished `task`. Returns whether it was the current one (then nothing is current). A task
    it never took is refused: a done without its take counts no time."""
    check_task_id(task)
    if commit and not COMMIT.fullmatch(commit):
        m_store.fail(f"a commit is its hex id, 7 to 40 characters: {commit!r}")
    if not any(e.get("event") == "take" and e.get("pid") == pid and e.get("task") == task for e in task_events()):
        m_store.fail(f"{pid} never took {task}: take it first (collab task take)")
    rp = m_store.room().paths
    log_task_event({"at": m_store.now(), "event": "done", "pid": pid, "task": task, "commit": commit})
    data = current_tasks()
    was_current = data.get(pid, {}).get("task") == task
    if was_current:
        data.pop(pid)
        m_store.write_json(rp.state_dir / "tasks.json", data)
    return was_current
