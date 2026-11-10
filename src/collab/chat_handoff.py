"""Private, durable chat handoff snapshots and token-only update control metadata."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import collab.store as m_store
import collab.installer as m_installer

HANDOFF_VERSION = 1
HANDOFF_LIMIT = 1024 * 1024
TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}")


class HandoffError(RuntimeError):
    pass


class _ProcessGone(RuntimeError):
    pass


def _process_start(pid: int) -> str:
    _number(pid, "claimant pid", 1)
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "pid=", "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=2,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HandoffError("cannot verify claimant process") from exc
    if result.returncode:
        if result.returncode == 1 and not result.stdout.strip():
            raise _ProcessGone
        raise HandoffError("cannot verify claimant process")
    parts = result.stdout.strip().split(maxsplit=1)
    if len(parts) != 2 or not parts[0].isdigit() or int(parts[0]) != pid or not parts[1]:
        raise HandoffError("ambiguous claimant process identity")
    return parts[1]


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start: str

    def __post_init__(self) -> None:
        _number(self.pid, "claimant pid", 1)
        _identifier(self.start, "process start")

    @classmethod
    def current(cls) -> ProcessIdentity:
        return cls(os.getpid(), _process_start(os.getpid()))

    def as_dict(self) -> dict[str, Any]:
        return {"pid": self.pid, "start": self.start}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ProcessIdentity:
        _keys(value, {"pid", "start"}, "claimant")
        return cls(**value)


def _current_claimant(value: ProcessIdentity | None) -> ProcessIdentity:
    current = ProcessIdentity.current()
    if value is not None and value != current:
        raise HandoffError("claimant identity does not match the calling process")
    return current


def _claimant_alive(identity: ProcessIdentity) -> bool:
    try:
        return _process_start(identity.pid) == identity.start
    except _ProcessGone:
        return False


def _identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise HandoffError(f"invalid handoff {field}")
    return value


def _number(value: int, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise HandoffError(f"invalid handoff {field}")
    return value


def _keys(value: dict[str, Any], expected: set[str], field: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise HandoffError(f"invalid handoff {field}")


def _check_directory(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise HandoffError("unsafe handoff directory")


@dataclass(frozen=True)
class Seat:
    room_id: str
    participant: str
    pane: str
    source_pid: int
    generation: int

    def __post_init__(self) -> None:
        _identifier(self.room_id, "room")
        _identifier(self.participant, "participant")
        _identifier(self.pane, "pane")
        _number(self.source_pid, "source pid", 1)
        _number(self.generation, "generation", 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "room": self.room_id,
            "participant": self.participant,
            "pane": self.pane,
            "source_pid": self.source_pid,
            "generation": self.generation,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Seat:
        _keys(value, {"room", "participant", "pane", "source_pid", "generation"}, "seat")
        return cls(value["room"], value["participant"], value["pane"], value["source_pid"], value["generation"])


@dataclass(frozen=True)
class DmPayload:
    text: str
    cursor: int
    top: int | None
    tabs: tuple[str, ...]
    active: str
    focus: bool
    history_position: int

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise HandoffError("invalid DM text")
        _number(self.cursor, "DM cursor")
        if self.cursor > len(self.text):
            raise HandoffError("DM cursor is outside the draft")
        if self.top is not None:
            _number(self.top, "DM top")
        if not isinstance(self.tabs, tuple) or any(not isinstance(tab, str) or not tab for tab in self.tabs):
            raise HandoffError("invalid DM tabs")
        if len(set(self.tabs)) != len(self.tabs):
            raise HandoffError("duplicate DM tabs")
        if not isinstance(self.active, str) or (self.tabs and self.active not in self.tabs) or (not self.tabs and self.active):
            raise HandoffError("invalid active DM tab")
        if not isinstance(self.focus, bool):
            raise HandoffError("invalid DM focus")
        _number(self.history_position, "DM history position")

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "cursor": self.cursor,
            "top": self.top,
            "tabs": list(self.tabs),
            "active": self.active,
            "focus": self.focus,
            "history_position": self.history_position,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DmPayload:
        _keys(value, {"text", "cursor", "top", "tabs", "active", "focus", "history_position"}, "DM payload")
        if not isinstance(value["tabs"], list):
            raise HandoffError("invalid DM tabs")
        return cls(**{**value, "tabs": tuple(value["tabs"])})


@dataclass(frozen=True)
class HandoffPayload:
    text: str
    cursor: int
    recipient: str | None
    dm: DmPayload | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise HandoffError("invalid handoff text")
        _number(self.cursor, "cursor")
        if self.cursor > len(self.text):
            raise HandoffError("cursor is outside the draft")
        if self.recipient is not None:
            _identifier(self.recipient, "recipient")
        if self.dm is not None and not isinstance(self.dm, DmPayload):
            raise HandoffError("invalid DM payload")

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "cursor": self.cursor,
            "recipient": self.recipient,
            "dm": self.dm.as_dict() if self.dm is not None else None,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> HandoffPayload:
        _keys(value, {"text", "cursor", "recipient", "dm"}, "payload")
        dm = value["dm"]
        if dm is not None:
            dm = DmPayload.from_dict(dm)
        return cls(value["text"], value["cursor"], value["recipient"], dm)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise HandoffError("duplicate handoff JSON key")
        result[key] = value
    return result


class HandoffStore:
    def __init__(self, root: Path, lock_timeout: float = 2.0):
        if (
            isinstance(lock_timeout, bool)
            or not isinstance(lock_timeout, (int, float))
            or not math.isfinite(lock_timeout)
            or lock_timeout <= 0
        ):
            raise HandoffError("invalid claim lock timeout")
        self.root = Path(root)
        self.lock_timeout = float(lock_timeout)

    def _path(self, token: str) -> Path:
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
            raise HandoffError("invalid handoff token")
        return self.root / f"{token}.json"

    def _write(self, path: Path, snapshot: dict[str, Any]) -> None:
        encoded = (json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > HANDOFF_LIMIT:
            raise HandoffError("handoff snapshot is too large")
        directory = None
        temporary = None
        try:
            directory, name = m_store.state_dir(path)
            _check_directory(directory)
            temporary = f".{name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(out, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            temporary = None
            os.fsync(directory)
        except OSError as exc:
            raise HandoffError("cannot write handoff snapshot") from exc
        finally:
            if directory is not None:
                if temporary is not None:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except OSError:
                        pass
                os.close(directory)

    def _read(self, path: Path) -> dict[str, Any]:
        directory = None
        try:
            directory, name = m_store.state_dir(path, strict=False, create=False)
            _check_directory(directory)
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > HANDOFF_LIMIT
                ):
                    raise HandoffError("unsafe handoff snapshot")
                encoded = stream.read(HANDOFF_LIMIT + 1)
            if len(encoded) > HANDOFF_LIMIT:
                raise HandoffError("handoff snapshot is too large")
            value = json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_object)
            if not isinstance(value, dict):
                raise HandoffError("invalid handoff snapshot")
            return value
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HandoffError("cannot read handoff snapshot") from exc
        finally:
            if directory is not None:
                os.close(directory)

    def _load(self, token: str, seat: Seat) -> tuple[Path, dict[str, Any], HandoffPayload]:
        path = self._path(token)
        snapshot = self._read(path)
        _keys(snapshot, {"v", "token", "seat", "payload", "claim"}, "snapshot")
        if snapshot["v"] != HANDOFF_VERSION or snapshot["token"] != token:
            raise HandoffError("handoff snapshot identity mismatch")
        if Seat.from_dict(snapshot["seat"]) != seat:
            raise HandoffError("handoff seat or generation mismatch")
        claim = snapshot["claim"]
        if claim is not None:
            ProcessIdentity.from_dict(claim)
        return path, snapshot, HandoffPayload.from_dict(snapshot["payload"])

    @contextmanager
    def _claim_lock(self, token: str):
        path = self._path(token).with_suffix(".lock")
        directory = None
        fd = None
        try:
            directory, name = m_store.state_dir(path, strict=False, create=False)
            _check_directory(directory)
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise HandoffError("unsafe handoff claim lock")
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise HandoffError("handoff claim is busy") from None
                    time.sleep(min(0.01, remaining))
            yield
        except OSError as exc:
            raise HandoffError("cannot lock handoff claim") from exc
        finally:
            if fd is not None:
                os.close(fd)
            if directory is not None:
                os.close(directory)

    def save(self, seat: Seat, payload: HandoffPayload) -> str:
        token = secrets.token_hex(16)
        snapshot = {"v": HANDOFF_VERSION, "token": token, "seat": seat.as_dict(), "payload": payload.as_dict(), "claim": None}
        self._write(self._path(token), snapshot)
        return token

    def save_claimed(self, seat: Seat, payload: HandoffPayload) -> str:
        token = self.save(seat, payload)
        self.claim(token, seat)
        return token

    def claim(self, token: str, seat: Seat, claimant: ProcessIdentity | None = None) -> HandoffPayload:
        current = _current_claimant(claimant)
        with self._claim_lock(token):
            path, snapshot, payload = self._load(token, seat)
            previous = snapshot["claim"]
            if previous is not None:
                previous_identity = ProcessIdentity.from_dict(previous)
                if previous_identity == current:
                    return payload
                if _claimant_alive(previous_identity):
                    raise HandoffError("previous claimant is still alive")
            snapshot["claim"] = current.as_dict()
            self._write(path, snapshot)
            return payload

    def complete(self, token: str, seat: Seat, claimant: ProcessIdentity | None = None) -> None:
        current = _current_claimant(claimant)
        with self._claim_lock(token):
            path, snapshot, _ = self._load(token, seat)
            claim = snapshot["claim"]
            if claim is None:
                if seat.source_pid != os.getpid():
                    raise HandoffError("only the snapshot creator can discard an unclaimed snapshot")
            elif ProcessIdentity.from_dict(claim) != current:
                raise HandoffError("only the current claimant can remove the snapshot")
            directory = None
            try:
                directory, name = m_store.state_dir(path, strict=False, create=False)
                _check_directory(directory)
                os.unlink(name, dir_fd=directory)
                os.fsync(directory)
            except OSError as exc:
                raise HandoffError("cannot remove handoff snapshot") from exc
            finally:
                if directory is not None:
                    os.close(directory)

    def discard(self, token: str, seat: Seat, claimant: ProcessIdentity | None = None) -> None:
        self.complete(token, seat, claimant)


CONTROL_KINDS = {"request", "dm-request", "dm-ack", "ready"}


def control_path(kind: str, participant: str, pane: str) -> Path:
    if kind not in CONTROL_KINDS:
        raise HandoffError("invalid handoff control kind")
    _identifier(participant, "participant")
    _identifier(pane, "pane")
    digest = hashlib.sha256(f"{participant}\x00{pane}".encode()).hexdigest()[:24]
    return m_store.room().paths.state_dir / "handoff" / f"{digest}.{kind}.json"


def write_control(path: Path, value: dict[str, Any]) -> None:
    HandoffStore(path.parent)._write(path, value)


def read_control(path: Path) -> dict[str, Any] | None:
    try:
        return HandoffStore(path.parent)._read(path)
    except HandoffError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            return None
        raise


def delete_control(path: Path) -> None:
    directory = None
    try:
        directory, name = m_store.state_dir(path, strict=False, create=False)
        _check_directory(directory)
        os.unlink(name, dir_fd=directory)
        os.fsync(directory)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise HandoffError("cannot remove handoff control") from exc
    finally:
        if directory is not None:
            os.close(directory)


def request_update(participant: str) -> dict[str, Any]:
    pane = m_store.read_panes().get("panes", {}).get(participant)
    if not isinstance(pane, str) or not pane:
        raise HandoffError("chat seat is not registered")
    path = control_path("request", participant, pane)
    if read_control(path) is not None:
        raise HandoffError("a chat update is already pending")
    request = {
        "v": HANDOFF_VERSION,
        "token": secrets.token_hex(16),
        "generation": time.time_ns(),
        "target": str(m_installer.command_copy()),
        "participant": participant,
        "pane": pane,
    }
    write_control(path, request)
    return request


def read_request(participant: str, pane: str, kind: str = "request") -> dict[str, Any] | None:
    request = read_control(control_path(kind, participant, pane))
    if request is None:
        return None
    required = {"v", "token", "generation", "target", "participant", "pane"}
    if not isinstance(request, dict) or not required <= set(request) or set(request) - required - {"source_pid"}:
        raise HandoffError("invalid update request")
    if (
        request["v"] != HANDOFF_VERSION
        or not isinstance(request["token"], str)
        or not TOKEN_PATTERN.fullmatch(request["token"])
        or request["participant"] != participant
        or request["pane"] != pane
        or (kind == "request" and not isinstance(request["target"], str))
        or (kind == "dm-request" and request["target"] != "dm")
    ):
        raise HandoffError("invalid chat update request")
    _number(request["generation"], "generation", 1)
    if "source_pid" in request:
        _number(request["source_pid"], "source pid", 1)
    return request


def write_dm_request(request: dict[str, Any], dm_pane: str) -> None:
    value = {**request, "target": "dm", "pane": dm_pane}
    write_control(control_path("dm-request", request["participant"], dm_pane), value)


def write_dm_ack(
    request: dict[str, Any], snapshot_token: str, source_pid: int, tabs: list[str], active: str, focus: bool
) -> None:
    if not TOKEN_PATTERN.fullmatch(snapshot_token):
        raise HandoffError("invalid DM snapshot token")
    value = {
        "v": HANDOFF_VERSION,
        "token": request["token"],
        "snapshot_token": snapshot_token,
        "generation": request["generation"],
        "participant": request["participant"],
        "pane": request["pane"],
        "source_pid": source_pid,
        "tabs": tabs,
        "active": active,
        "focus": focus,
    }
    write_control(control_path("dm-ack", request["participant"], request["pane"]), value)


def read_dm_ack(request: dict[str, Any], dm_pane: str) -> dict[str, Any] | None:
    ack = read_control(control_path("dm-ack", request["participant"], dm_pane))
    if ack is None:
        return None
    _keys(
        ack,
        {"v", "token", "snapshot_token", "generation", "participant", "pane", "source_pid", "tabs", "active", "focus"},
        "DM acknowledgement",
    )
    if (
        ack["v"] != HANDOFF_VERSION
        or ack["token"] != request["token"]
        or ack["generation"] != request["generation"]
        or ack["participant"] != request["participant"]
        or ack["pane"] != dm_pane
    ):
        return None
    if (
        not isinstance(ack["snapshot_token"], str)
        or not TOKEN_PATTERN.fullmatch(ack["snapshot_token"])
        or not isinstance(ack["tabs"], list)
        or any(not isinstance(tab, str) or not tab for tab in ack["tabs"])
        or not isinstance(ack["active"], str)
        or not isinstance(ack["focus"], bool)
    ):
        raise HandoffError("invalid DM acknowledgement")
    _number(ack["source_pid"], "DM source pid", 1)
    return ack


def write_ready(request: dict[str, Any], status: str, dm_pane: str | None = None) -> None:
    if status not in ("ready", "failed"):
        raise HandoffError("invalid handoff ready state")
    pane = dm_pane or request["pane"]
    value = {
        "v": HANDOFF_VERSION,
        "token": request["token"],
        "generation": request["generation"],
        "participant": request["participant"],
        "pane": pane,
        "status": status,
    }
    write_control(control_path("ready", request["participant"], pane), value)


def read_ready(request: dict[str, Any], allow_superseded: bool = False, dm_pane: str | None = None) -> dict[str, Any] | None:
    pane = dm_pane or request["pane"]
    ready = read_control(control_path("ready", request["participant"], pane))
    if ready is None:
        return None
    _keys(ready, {"v", "token", "generation", "participant", "pane", "status"}, "ready state")
    if (
        ready["v"] != HANDOFF_VERSION
        or ready["token"] != request["token"]
        or ready["generation"] != request["generation"]
        or ready["participant"] != request["participant"]
        or ready["pane"] != pane
    ):
        return {"status": "superseded"} if allow_superseded else None
    if ready["status"] not in ("ready", "failed"):
        raise HandoffError("invalid handoff ready state")
    return ready


def current_dm_request(request: dict[str, Any], dm_pane: str) -> bool:
    current = read_request(request["participant"], dm_pane, "dm-request")
    return bool(current and current["token"] == request["token"] and current["generation"] == request["generation"])
