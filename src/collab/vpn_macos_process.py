"""Bind native Claude hooks to a recorded process birth without a tmux capability."""

from __future__ import annotations

import calendar
import ctypes
from dataclasses import asdict, dataclass
import os
from pathlib import Path
import sys
import time
from typing import Any
import uuid

import collab.store as m_store


@dataclass(frozen=True)
class MacProcessIdentity:
    pid: int
    uid: int
    seconds: int
    microseconds: int

    def __post_init__(self) -> None:
        values = (self.pid, self.uid, self.seconds, self.microseconds)
        if any(type(value) is not int for value in values) or not (
            1 < self.pid < 2**31 and 0 <= self.uid < 2**32 and self.seconds > 0 and 0 <= self.microseconds < 1000000
        ):
            raise ValueError("invalid process birth identity")


class MacBsdInfo(ctypes.Structure):
    # Darwin proc_bsdinfo is an ABI: verify the size and returned byte count before using offsets.
    _fields_ = [
        ("prefix", ctypes.c_uint32 * 12),
        ("comm", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("tail", ctypes.c_uint32 * 6),
        ("seconds", ctypes.c_uint64),
        ("microseconds", ctypes.c_uint64),
    ]


def mac_process_info(pid: int) -> tuple[MacProcessIdentity, int]:
    if sys.platform != "darwin" or type(pid) is not int or not 1 < pid < 2**31 or ctypes.sizeof(MacBsdInfo) != 136:
        raise ValueError("process birth inspection unavailable")
    library = ctypes.CDLL("/usr/lib/libproc.dylib")
    function = library.proc_pidinfo
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    function.restype = ctypes.c_int
    information = MacBsdInfo()
    if function(pid, 3, 0, ctypes.byref(information), ctypes.sizeof(information)) != ctypes.sizeof(information):
        raise ValueError("process birth inspection failed")
    identity = MacProcessIdentity(
        int(information.prefix[3]), int(information.prefix[5]), int(information.seconds), int(information.microseconds)
    )
    if identity.pid != pid:
        raise ValueError("process birth identity changed")
    return identity, int(information.prefix[4])


def mac_vpn_process_fields(config: dict[str, Any], pid: str) -> dict[str, Any]:
    if config.get("macos_vpn") is not True:
        return {}
    if not pid.isdigit() or str(int(pid)) != pid:
        raise ValueError("invalid pane process identity")
    identity, _ = mac_process_info(int(pid))
    if identity.uid != os.geteuid():
        raise ValueError("pane process belongs to another user")
    return {"vpn_process": asdict(identity)}


@m_store.room_locked
def mac_vpn_record_process(participant: str, pid: str) -> None:
    config = m_store.load_config()
    fields = mac_vpn_process_fields(config, pid)
    for entry in config["participants"]:
        if entry["id"] == participant:
            entry.update(fields)
            entry["pane_pid"] = pid
            m_store.write_config(config)
            return
    raise ValueError("VPN participant disappeared")


def mac_hook_ancestors() -> list[MacProcessIdentity]:
    result: list[MacProcessIdentity] = []
    cursor = os.getppid()
    child: MacProcessIdentity | None = None
    for _ in range(20):
        if cursor <= 1 or any(item.pid == cursor for item in result):
            break
        identity, parent = mac_process_info(cursor)
        if identity.uid != os.geteuid():
            break
        if child is None:
            if os.getppid() != cursor:
                break
        else:
            again, current_parent = mac_process_info(child.pid)
            if again != child or current_parent != cursor:
                break
        result.append(identity)
        child, cursor = identity, parent
    return result


def mac_native_session(identity: MacProcessIdentity, session: str, target: str) -> bool:
    home = Path(os.environ.get("COLLAB_CLAUDE_HOME", "~/.claude")).expanduser()
    record = m_store.read_json(home / "sessions" / f"{identity.pid}.json")
    if record.get("sessionId") != session or str(record.get("pid")) != str(identity.pid):
        return False
    if "startedAt" in record:
        # Current native records no longer carry a tmux target or a process-start string.
        # startedAt is session creation (which may follow a trust prompt), not PID birth.
        # Birth/UID and hook ancestry are independently checked from the OS by the caller.
        started_at = record["startedAt"]
        return (
            "procStart" not in record
            and "tmux" not in record
            and record.get("pidDomain") == "darwin"
            and record.get("kind") == "interactive"
            and type(started_at) is int
            and identity.seconds * 1000 <= started_at <= int(time.time() * 1000) + 2000
        )
    if target.startswith("%") and not str(record.get("tmux", "")).endswith("." + target):
        return False
    try:
        started = calendar.timegm(time.strptime(str(record.get("procStart", "")), "%a %b %d %H:%M:%S %Y"))
    except (ValueError, OverflowError):
        return False
    return abs(started - identity.seconds) <= 2


@m_store.room_locked
def mac_vpn_bind_hook(session: str) -> str:
    try:
        if sys.platform != "darwin" or not isinstance(session, str) or str(uuid.UUID(session)) != session:
            return ""
        config = m_store.load_config()
        if config.get("macos_vpn") is not True:
            return ""
        ancestors = mac_hook_ancestors()
        matches: list[tuple[dict[str, Any], MacProcessIdentity]] = []
        for participant in config["participants"]:
            value = participant.get("vpn_process")
            if (
                participant.get("kind") != "claude"
                or not isinstance(value, dict)
                or set(value) != {"pid", "uid", "seconds", "microseconds"}
            ):
                continue
            identity = MacProcessIdentity(**value)
            if (
                identity in ancestors
                and str(identity.pid) == participant.get("pane_pid")
                and mac_native_session(identity, session, participant.get("target", ""))
            ):
                matches.append((participant, identity))
        if len(matches) != 1:
            return ""
        participant, identity = matches[0]
        if identity not in mac_hook_ancestors() or not mac_native_session(identity, session, participant.get("target", "")):
            return ""
        participant["claude_session"] = session
        m_store.write_config(config)
        return participant["id"]
    except (OSError, ValueError, TypeError):
        return ""  # An unavailable or changed identity provides no hook binding.
