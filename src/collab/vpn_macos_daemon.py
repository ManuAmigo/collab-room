"""Per-user proxy supervisor with bounded control frames and revocable agent leases."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import select
import signal
import socket
import stat
import subprocess
import threading
import time
from typing import Any, Callable

import collab.boot as m_boot
import collab.store as m_store
import collab.vpn_config as m_vpn_config
import collab.vpn_macos_install as m_vpn_macos_install
import collab.vpn_macos_state as m_vpn_macos_state
import collab.vpn_macos_service as m_vpn_macos_service


def mac_vpn_folder() -> Path:
    root = m_boot.paths().python_file.parent
    m_store.private_dir(root, strict=False)
    directory, name = m_vpn_config.vpn_private_parent(root / "vpn-macos")
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=directory)
        except FileExistsError:
            pass
        inner = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        try:
            info = os.fstat(inner)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError("proxy state directory rejected")
        finally:
            os.close(inner)
    finally:
        os.close(directory)
    return root / "vpn-macos"


def mac_control_read(connection: socket.socket, timeout: float = 3, tick: Callable[[], None] | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    data = bytearray()
    while len(data) <= 4096:
        if tick is not None:
            tick()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("proxy control frame deadline exceeded")
        connection.settimeout(min(remaining, 0.1) if tick is not None else remaining)
        try:
            part = connection.recv(1)
        except socket.timeout:
            continue
        if not part:
            raise ValueError("proxy control connection closed")
        if part == b"\n":
            try:
                document = json.loads(data, object_pairs_hook=m_vpn_config.vpn_unique_fields)
            except (ValueError, UnicodeError):
                raise ValueError("proxy control frame rejected") from None
            if not isinstance(document, dict):
                raise ValueError("proxy control frame rejected")
            return document
        data.extend(part)
    raise ValueError("proxy control frame too large")


def mac_control_send(connection: socket.socket, document: dict[str, Any]) -> None:
    payload = json.dumps(document, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
    if len(payload) > 4096:
        raise ValueError("proxy control frame too large")
    connection.sendall(payload)


def mac_control_connect(folder: Path) -> socket.socket:
    directory, _ = m_vpn_config.vpn_private_parent(folder / "control.sock")
    try:
        info = os.stat("control.sock", dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("proxy control socket rejected")
    finally:
        os.close(directory)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(90)
        connection.connect(str(folder / "control.sock"))
        return connection
    except BaseException:
        connection.close()
        raise


def mac_control_request(folder: Path, action: str) -> dict[str, Any]:
    with mac_control_connect(folder) as connection:
        mac_control_send(connection, {"action": action})
        return mac_control_read(connection, timeout=90)


class MacProxySupervisor:
    def __init__(self, runtime: m_vpn_macos_service.MacProxyProcess, binary: Path, account: m_vpn_config.VpnAccount) -> None:
        self.runtime, self.binary, self.account = runtime, binary, account
        self.state = m_vpn_macos_state.MacProxyState()
        self.leases: list[socket.socket] = []
        self.pending_stops: list[socket.socket] = []
        self.exit_ip = ""

    def revoke(self) -> None:
        leases = self.leases + self.pending_stops
        self.leases, self.pending_stops = [], []
        deadline = time.monotonic() + 3
        for connection in leases:
            try:
                connection.settimeout(0.1)
                mac_control_send(connection, {"stop": True})
            except OSError:
                pass
        for connection in leases:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or mac_control_read(connection, timeout=remaining) != {"stopped": True}:
                    raise ValueError("agent stop unconfirmed")
            except (OSError, ValueError):
                self.pending_stops.append(connection)
            else:
                connection.close()
        if self.pending_stops:
            raise m_vpn_macos_service.MacProxyError("agent stop unconfirmed")

    def apply(self, event: str) -> None:
        if event == "on" and self.account.expires is not None and self.account.expires <= time.time():
            raise ValueError("proxy account expired")
        pending = [event]
        while pending:
            decision = m_vpn_macos_state.mac_proxy_transition(self.state, pending.pop(0), self.state.generation)
            self.state = decision.state
            for action in decision.actions:
                if action == "stop_agents":
                    try:
                        self.revoke()
                    except (OSError, ValueError):
                        self.state = m_vpn_macos_state.MacProxyState(
                            self.state.generation, self.state.desired, "blocked", self.state.restarts
                        )
                        self.runtime.stop()
                        raise
                elif action == "stop_proxy":
                    try:
                        self.runtime.stop()
                    except (OSError, ValueError, subprocess.SubprocessError):
                        self.state = m_vpn_macos_state.MacProxyState(
                            self.state.generation, self.state.desired, "blocked", self.state.restarts
                        )
                        raise
                    self.exit_ip = ""
                elif action == "start_proxy":
                    try:
                        self.runtime.start(self.binary, self.account, self.state.generation)
                    except m_vpn_macos_service.MacProxyForeign:
                        pending.append("foreign_listener")
                    except (OSError, ValueError, subprocess.SubprocessError):
                        pending.append("died")
                    else:
                        pending.append("started")
                elif action == "probe":
                    try:
                        self.exit_ip = self.runtime.probe()
                    except (OSError, ValueError, subprocess.SubprocessError):
                        pending.append("probe_failed")
                    else:
                        pending.append("probe_ok")

    def tick(self) -> None:
        if self.account.expires is not None and self.account.expires <= time.time():
            if self.state.desired != "off":
                self.apply("off")
            return
        if self.state.actual == "ready":
            try:
                owned = self.runtime.owned()
            except (OSError, ValueError, subprocess.SubprocessError):
                owned = False
            if not owned:
                child = self.runtime.process
                self.apply("foreign_listener" if child is not None and child.poll() is None else "died")

    def status(self) -> dict[str, Any]:
        identity = self.runtime.identity
        return {
            "supervisor_pid": os.getpid(),
            "actual": self.state.actual,
            "desired": self.state.desired,
            "generation": self.state.generation,
            "restarts": self.state.restarts,
            "pid": identity.pid if identity else 0,
            "port": identity.endpoint.port if identity else 0,
            "exit_ip": self.exit_ip,
        }

    def admit(self, connection: socket.socket) -> None:
        if self.state.actual != "ready" or not self.runtime.owned():
            raise ValueError("proxy not ready")
        document = self.status()
        identity = self.runtime.identity
        if identity is None:
            raise ValueError("proxy identity unavailable")
        document.update(username=identity.endpoint.username, password=identity.endpoint.password)
        mac_control_send(connection, document)
        connection.settimeout(None)
        self.leases.append(connection)


def mac_proxy_daemon(folder: Path, binary: Path, account_path: Path) -> None:
    directory, _ = m_vpn_config.vpn_private_parent(folder / "supervisor.lock")
    try:
        lock = os.open("supervisor.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        info = os.fstat(lock)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("proxy lock rejected")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.path.lexists(folder / "unconfirmed.json"):
            raise m_vpn_macos_service.MacProxyError("previous supervisor stop is unconfirmed")
        mac_proxy_daemon_locked(folder, binary, account_path)
    finally:
        os.close(lock)


def mac_proxy_daemon_locked(folder: Path, binary: Path, account_path: Path) -> None:
    account = m_vpn_config.vpn_account(account_path, time.time())
    supervisor = MacProxySupervisor(m_vpn_macos_service.MacProxyProcess(), binary, account)
    address = folder / "control.sock"
    if len(os.fsencode(address)) >= 104:
        raise ValueError("proxy control socket path too long")
    mac_proxy_marker(folder, {"kind": "mac-vpn-supervisor", "actual": "unconfirmed", "phase": "running"})
    if os.path.lexists(address):
        info = os.lstat(address)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("proxy control socket occupied")
        os.unlink(address)
    stop = threading.Event()
    previous = {int(sig): signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGTERM, signal.SIGINT)}
    bound_identity = None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(address))
            bound = os.lstat(address)
            bound_identity = (bound.st_dev, bound.st_ino)
            os.chmod(address, 0o600)
            server.listen(16)
            while not stop.is_set():
                supervisor.tick()
                ready, _, _ = select.select([server, *supervisor.leases], [], [], 0.2)
                for connection in ready:
                    if connection is server:
                        client, _ = server.accept()
                        mac_proxy_control_client(supervisor, client)
                    elif connection in supervisor.leases:
                        supervisor.leases.remove(connection)
                        try:
                            confirmed = mac_control_read(connection) == {"stopped": True}
                        except (OSError, ValueError):
                            confirmed = False
                        if confirmed:
                            connection.close()
                        else:
                            supervisor.pending_stops.append(connection)
                            supervisor.state = m_vpn_macos_state.MacProxyState(
                                supervisor.state.generation, supervisor.state.desired, "blocked", supervisor.state.restarts
                            )
                            supervisor.runtime.stop()
    finally:
        mac_proxy_cleanup(supervisor, folder, address, bound_identity, previous)


def mac_proxy_marker(folder: Path, document: dict[str, Any]) -> None:
    directory, name = m_vpn_config.vpn_private_parent(folder / "unconfirmed.json")
    try:
        m_vpn_macos_install.mac_proxy_write(directory, name, json.dumps(document, ensure_ascii=True).encode(), 0o600)
    finally:
        os.close(directory)


def mac_proxy_cleanup(
    supervisor: MacProxySupervisor,
    folder: Path,
    address: Path,
    bound_identity: tuple[int, int] | None,
    previous: dict[int, Any],
) -> None:
    failures: list[str] = []
    # Cleanup is an outer boundary: every independent cleanup must run even after another fails.
    for name, cleanup in (("agents", supervisor.revoke), ("proxy", supervisor.runtime.stop)):
        try:
            cleanup()
        except BaseException:
            failures.append(name)
    for connection in supervisor.leases + supervisor.pending_stops:
        try:
            connection.close()
        except BaseException:
            failures.append("lease_close")
    supervisor.leases, supervisor.pending_stops = [], []
    for sig, handler in previous.items():
        try:
            signal.signal(sig, handler)
        except BaseException:
            failures.append("signal_restore")
    try:
        if bound_identity is not None:
            current = os.lstat(address)
            if (current.st_dev, current.st_ino) == bound_identity:
                address.unlink()
            else:
                failures.append("socket_identity")
    except FileNotFoundError:
        pass
    except BaseException:
        failures.append("socket_cleanup")
    if failures:
        # The startup marker already exists if replacing it fails (for example, a full disk).
        mac_proxy_marker(folder, {"kind": "mac-vpn-supervisor", "actual": "blocked", "failures": failures})
        raise m_vpn_macos_service.MacProxyError("supervisor cleanup unconfirmed")
    directory, name = m_vpn_config.vpn_private_parent(folder / "unconfirmed.json")
    try:
        payload = m_vpn_macos_install.mac_proxy_cache_read(directory, name)
        if payload is not None:
            if json.loads(payload).get("kind") != "mac-vpn-supervisor":
                raise ValueError("supervisor marker identity rejected")
            os.unlink(name, dir_fd=directory)
            os.fsync(directory)
    finally:
        os.close(directory)


def mac_proxy_control_client(supervisor: MacProxySupervisor, connection: socket.socket) -> None:
    retained = False
    try:
        connection.settimeout(3)
        request = mac_control_read(connection, tick=supervisor.tick)
        if set(request) != {"action"} or request["action"] not in ("on", "off", "status", "lease"):
            raise ValueError("proxy control request rejected")
        action = request["action"]
        if action in ("on", "off"):
            supervisor.apply(action)
        if action == "lease":
            supervisor.admit(connection)
            retained = True
        else:
            mac_control_send(connection, supervisor.status())
    except (OSError, ValueError, subprocess.SubprocessError):
        pass  # Failed control clients never acquire an agent lease.
    finally:
        if not retained:
            connection.close()
