"""Exec the CLI in its own pane, with a separate parent-bound lease monitor."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import select
import signal
import socket
import sys
import subprocess
import time
from typing import Tuple

import collab.vpn_macos as m_vpn_macos
import collab.vpn_macos_daemon as m_vpn_macos_daemon
import collab.vpn_macos_service as m_vpn_macos_service


def mac_close_descriptors(keep: frozenset[int]) -> None:
    for name in os.listdir("/dev/fd"):
        if name.isdigit() and int(name) >= 3 and int(name) not in keep:
            try:
                os.close(int(name))
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise


def mac_group_states(group: int) -> dict[int, str]:
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,pgid=,stat="],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        timeout=2,
    )
    if result.returncode or len(result.stdout) > 2 * 1024 * 1024:
        raise ValueError("agent group inspection failed")
    states: dict[int, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            raise ValueError("agent group inspection rejected")
        if int(parts[1]) == group:
            states[int(parts[0])] = parts[2]
    return states


class MacAgentGroup:
    def __init__(self, group: int, anchor: int) -> None:
        self.group, self.anchor = group, anchor
        self.killed = False
        self.reaped = False

    def stop(self) -> bool:
        if self.reaped:
            return True
        # The unreaped child anchor reserves this group identity even after the CLI exits.
        if not self.killed:
            if os.getpgid(self.anchor) != self.group:
                return False
            os.killpg(self.group, signal.SIGTERM)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                try:
                    states = mac_group_states(self.group)
                except (OSError, ValueError, subprocess.SubprocessError):
                    break  # Inspection failure must not skip the final signal.
                if not any(pid != self.anchor and not state.startswith("Z") for pid, state in states.items()):
                    break
                time.sleep(0.02)
            if os.getpgid(self.anchor) != self.group:
                return False
            os.killpg(self.group, signal.SIGKILL)
            self.killed = True
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                states = mac_group_states(self.group)
            except (OSError, ValueError, subprocess.SubprocessError):
                time.sleep(0.02)
                continue  # Keep the unreaped anchor until a successful observation.
            if self.anchor in states and all(state.startswith("Z") for state in states.values()):
                os.waitpid(self.anchor, 0)
                self.reaped = True
                return True
            time.sleep(0.02)
        return False


def mac_agent_anchor(lifetime: int, ready: int) -> None:
    try:
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, signal.SIG_IGN)
        mac_close_descriptors(frozenset({lifetime, ready}))
        os.write(ready, b"A")
        os.close(ready)
        while os.read(lifetime, 1):
            pass
    finally:
        os._exit(0)


def mac_make_group_anchor(parent: int) -> tuple[MacAgentGroup, int]:
    if os.getpgrp() != parent:
        raise ValueError("agent group identity changed")
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    lifetime, retained = os.pipe()
    read_ready, write_ready = os.pipe()
    anchor = os.fork()
    if anchor == 0:
        os.close(retained)
        os.close(read_ready)
        mac_agent_anchor(lifetime, write_ready)
    os.close(lifetime)
    os.close(write_ready)
    try:
        if not select.select([read_ready], [], [], 3)[0] or os.read(read_ready, 1) != b"A":
            raise ValueError("agent group anchor unavailable")
    except BaseException:
        os.close(retained)
        raise
    finally:
        os.close(read_ready)
    return MacAgentGroup(parent, anchor), retained


def mac_agent_monitor(connection: socket.socket, parent: int, ready: int) -> None:
    retained = -1
    try:
        with open(os.devnull, "r+b", buffering=0) as sink:
            for descriptor in (0, 1, 2):
                os.dup2(sink.fileno(), descriptor)
        group, retained = mac_make_group_anchor(parent)
        os.setsid()
        mac_close_descriptors(frozenset({connection.fileno(), ready, retained}))
        os.write(ready, b"R")
        os.close(ready)
        while os.getppid() == parent:
            readable, _, _ = select.select([connection], [], [], 0.1)
            if readable:
                try:
                    message = m_vpn_macos_daemon.mac_control_read(connection)
                    if message != {"stop": True}:
                        raise ValueError("lease message rejected")
                except (OSError, ValueError):
                    pass  # EOF or malformed control traffic revokes this lease too.
                break
        while not group.stop():
            time.sleep(0.05)
        stopped = True
        try:
            m_vpn_macos_daemon.mac_control_send(connection, {"stopped": stopped})
        except OSError:
            pass
    finally:
        if retained >= 0:
            os.close(retained)
        connection.close()
        os._exit(0)


def mac_vpn_agent_run(folder: Path, kind: str, command: Tuple[str, ...]) -> None:
    if sys.platform != "darwin" or os.getpid() != os.getpgrp():
        raise ValueError("VPN agents require their own macOS pane process group")
    with m_vpn_macos_daemon.mac_control_connect(folder) as connection:
        m_vpn_macos_daemon.mac_control_send(connection, {"action": "lease"})
        # Admission waits behind bounded supervisor work and earlier clients. This is the
        # response budget, as for other control requests; incoming frames keep their short limit.
        response = m_vpn_macos_daemon.mac_control_read(connection, timeout=90)
        port, pid = response.get("port"), response.get("pid")
        if (
            response.get("actual") != "ready"
            or type(port) is not int
            or type(pid) is not int
            or pid <= 0
            or m_vpn_macos_service.mac_proxy_listeners(port) != {pid}
        ):
            raise ValueError("proxy lease identity rejected")
        username, password = response.get("username"), response.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise ValueError("proxy lease credentials rejected")
        endpoint = m_vpn_macos.MacVpnEndpoint(port, username, password)
        arguments = m_vpn_macos.mac_vpn_command(kind, command, endpoint)
        environment = m_vpn_macos.mac_vpn_environment(os.environ, endpoint)
        if select.select([connection], [], [], 0)[0]:
            raise ValueError("proxy lease already revoked")
        parent = os.getpid()
        read_fd, write_fd = os.pipe()
        try:
            monitor = os.fork()
        except BaseException:
            os.close(read_fd)
            os.close(write_fd)
            raise
        if monitor == 0:
            os.close(read_fd)
            mac_agent_monitor(connection, parent, write_fd)
        os.close(write_fd)
        try:
            if not select.select([read_fd], [], [], 3)[0] or os.read(read_fd, 1) != b"R":
                raise ValueError("proxy lease monitor unavailable")
        finally:
            os.close(read_fd)
    # Exec preserves tmux pane PID, terminal and the CLI's session discovery.
    mac_close_descriptors(frozenset())
    os.execvpe(arguments[0], arguments, environment)
