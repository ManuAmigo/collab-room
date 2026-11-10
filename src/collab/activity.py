"""Bounded, explicit queue activity; it never changes input readiness or delivery guards."""

from __future__ import annotations
import contextlib
import json
import math
import os
import re
import stat
import threading
import time
from typing import Any
import collab.text as m_text
import collab.i18n as m_i18n
import collab.store as m_store

ACTIVITY_LIMIT = 131072
ACTIVITY_LEASE = 30
ACTIVITY_PHASES = {"waiting", "checking", "finishing", "done", "failed"}
ACTIVITY_RUN = re.compile(r"[0-9a-f]{32}")


def activity_file(name: str = "activity.json") -> dict[str, Any]:
    if name not in ("activity.json", "merge-queue.json"):
        return {}
    try:
        directory, leaf = m_store.state_dir(m_store.room().paths.state_dir / name, strict=False, create=False)
        try:
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1 or info.st_size > ACTIVITY_LIMIT:
                return {}
            raw = source.read(ACTIVITY_LIMIT + 1)
        value = json.loads(raw) if len(raw) <= ACTIVITY_LIMIT else None
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def activity_records() -> dict[str, dict[str, Any]]:
    data = activity_file()
    entries = data.get("entries")
    if data.get("v") != 1 or data.get("room") != str(m_store.room().paths.root) or not isinstance(entries, dict):
        return {}
    if len(entries) > 128:
        return {}
    result = {}
    for pid, rec in entries.items():
        if not isinstance(pid, str) or not m_store.WORD.fullmatch(pid) or not isinstance(rec, dict):
            continue
        stamp = rec.get("updated")
        if (
            not isinstance(rec.get("run"), str)
            or not ACTIVITY_RUN.fullmatch(rec["run"])
            or not isinstance(rec.get("phase"), str)
            or rec["phase"] not in ACTIVITY_PHASES
            or not isinstance(stamp, (int, float))
            or isinstance(stamp, bool)
            or not math.isfinite(stamp)
            or rec.get("lease") != ACTIVITY_LEASE
        ):
            continue
        result[pid] = {k: rec[k] for k in ("run", "phase", "updated", "lease")}
    return result


@m_store.room_locked
def publish_activity(pid: str, run: str, phase: str = "", *, begin: bool = False, root: str = "") -> bool:
    """Begin replaces this participant's slot; every later write must still own that run."""
    if root and root != str(m_store.room().paths.root):
        return False
    if (
        not isinstance(pid, str)
        or not isinstance(run, str)
        or not m_store.WORD.fullmatch(pid)
        or not ACTIVITY_RUN.fullmatch(run)
        or phase not in ("", *ACTIVITY_PHASES)
    ):
        return False
    known = set(m_store.ids_of(m_store.read_config_raw()))
    if pid not in known:
        return False
    entries = {k: v for k, v in activity_records().items() if k in known}
    if pid not in entries and len(entries) >= 128:
        return False
    old = entries.get(pid, {})
    if not begin and old.get("run") != run:
        return False
    if begin and phase != "waiting":
        return False
    entries[pid] = {"run": run, "phase": phase or old["phase"], "updated": time.time(), "lease": ACTIVITY_LEASE}
    m_store.write_json(
        m_store.room().paths.state_dir / "activity.json",
        {
            "v": 1,
            "room": str(m_store.room().paths.root),
            "entries": entries,
        },
    )
    return True


def activity_projection(config: dict[str, Any]) -> dict[str, str]:
    """A fresh publisher proves a phase; a queue slot without one is unknown, never checking."""
    known = set(m_store.ids_of(config))
    records = activity_records()
    result = {}
    for pid, rec in records.items():
        if pid not in known:
            continue
        age = time.time() - rec["updated"]
        if rec["phase"] in ("done", "failed"):
            if 0 <= age <= ACTIVITY_LEASE:
                result[pid] = rec["phase"]
        else:
            result[pid] = rec["phase"] if 0 <= age <= ACTIVITY_LEASE else "stale"
    queue = activity_file("merge-queue.json").get("queue")
    queued = {}
    for entry in queue if isinstance(queue, list) and len(queue) <= 128 else []:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("id"), str)
            or entry["id"] not in known
            or type(entry.get("pid")) is not int
            or entry["pid"] <= 0
            or not isinstance(entry.get("start"), str)
            or not entry["start"]
            or "landed" in entry
            or "stopped" in entry
        ):
            continue
        queued[entry["id"]] = entry.get("activity_run")
    for pid, run in queued.items():
        if not isinstance(run, str) or run != records.get(pid, {}).get("run") or pid not in result:
            result[pid] = "stale"
    return result


def activity_words(phase: str, lang: str = "") -> str:
    return m_i18n.words_in("activity." + phase, lang or m_i18n.SPOKEN["lang"]) if phase in (*ACTIVITY_PHASES, "stale") else ""


class ActivityLease:
    """Only a known publisher renews its lease, including while its child writes no output."""

    def __init__(self, pid: str, run: str, interval: float = 5.0):
        self.pid, self.run, self.interval = pid, run, interval
        self.root = str(m_store.room().paths.root)
        self.peers: dict[str, str] = {}
        self.event = threading.Event()
        self.thread: threading.Thread | None = None

    def phase(self, phase: str = "") -> bool:
        with contextlib.suppress(OSError, ValueError, SystemExit):
            return publish_activity(self.pid, self.run, phase, root=self.root)
        return False

    def pulse(self) -> None:
        while not self.event.wait(self.interval):
            self.phase()
            for pid, run in self.peers.copy().items():
                with contextlib.suppress(OSError, ValueError, SystemExit):
                    publish_activity(pid, run, root=self.root)

    def start(self) -> None:
        with contextlib.suppress(OSError, ValueError, SystemExit):
            publish_activity(self.pid, self.run, "waiting", begin=True, root=self.root)
        self.thread = threading.Thread(target=self.pulse, daemon=True)
        self.thread.start()

    def finish(self, phase: str) -> None:
        self.event.set()
        if self.thread:
            self.thread.join(timeout=1)
        self.phase(phase)


def activity_lines(config: dict[str, Any], width: int) -> list[str]:
    lines = []
    for pid, phase in activity_projection(config).items():
        name = m_text.clean_field(m_store.label_of(config, pid), 20)
        text = f"{activity_words(phase, m_store.room_language(config))} · {name}"
        lines.extend(m_text.wrap_cols(text, max(1, width)))
    return lines
