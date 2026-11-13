"""Opt-in room launch routing to one user's macOS network-only supervisor."""

from __future__ import annotations

from pathlib import Path
import os
import fcntl
import stat
import json
import platform
import shlex
import subprocess
import sys
import time
from typing import Any, Mapping

import collab.boot as m_boot
import collab.store as m_store
import collab.vpn_config as m_vpn_config
import collab.vpn_macos_daemon as m_vpn_macos_daemon
import collab.vpn_macos_install as m_vpn_macos_install


def mac_vpn_enabled(config: Mapping[str, Any]) -> bool:
    enabled = config.get("macos_vpn", False)
    if type(enabled) is not bool:
        raise ValueError("macos_vpn must be a boolean")
    return enabled


def mac_vpn_wrap(kind: str, command: str, config: Mapping[str, Any]) -> str:
    if not mac_vpn_enabled(config):
        return command
    if sys.platform != "darwin" or kind not in ("codex", "claude"):
        raise ValueError("network-only VPN supports macOS Codex and Claude only")
    arguments = shlex.split(command)
    if not arguments:
        raise ValueError("VPN agent command is empty")
    return shlex.join([sys.executable, "-I", "-S", m_boot.SELF, "vpn-agent", "--kind", kind, "--", *arguments])


def mac_vpn_ensure(folder: Path, binary: Path, account: Path) -> None:
    try:
        m_vpn_macos_daemon.mac_control_request(folder, "status")
        return
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-S",
            m_boot.SELF,
            "vpn-daemon",
            "--folder",
            str(folder),
            "--binary",
            str(binary),
            "--account",
            str(account),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            m_vpn_macos_daemon.mac_control_request(folder, "status")
            return
        except (FileNotFoundError, ConnectionRefusedError):
            process.poll()  # Reap a losing concurrent singleton startup.
            time.sleep(0.05)
    raise ValueError("proxy supervisor did not become available")


@m_store.room_locked
def mac_vpn_record_enabled(enabled: bool) -> None:
    config = m_store.load_config()
    config["macos_vpn"] = enabled
    m_store.write_config(config)


def mac_vpn_room_action(action: str) -> dict[str, Any]:
    if sys.platform != "darwin":
        raise ValueError("network-only VPN requires macOS")
    config = m_store.load_config()
    enabled = mac_vpn_enabled(config)
    folder = m_vpn_macos_daemon.mac_vpn_folder()
    if action == "status":
        try:
            status = m_vpn_macos_daemon.mac_control_request(folder, "status")
        except (FileNotFoundError, ConnectionRefusedError):
            status = {"actual": "blocked" if (folder / "unconfirmed.json").exists() else "off"}
        return {**status, "room_enabled": enabled}
    if action not in ("setup", "on", "off") or (action == "on" and not enabled):
        raise ValueError("run VPN setup for this room first")
    if action in ("setup", "on"):
        account = Path.home() / ".config" / "collab-vpn" / "agents.json"
        m_vpn_config.vpn_account(account, time.time())
        binary = m_vpn_macos_install.mac_vpn_install(folder, platform.machine())
        mac_vpn_ensure(folder, binary, account)
    try:
        status = m_vpn_macos_daemon.mac_control_request(folder, "on" if action == "setup" else action)
    except (FileNotFoundError, ConnectionRefusedError):
        if action != "off" or (folder / "unconfirmed.json").exists():
            raise
        status = {"actual": "off"}
    if action == "off":
        if status.get("actual") != "off":
            raise ValueError("proxy did not stop")
        if status.get("off_exits") is True:
            mac_vpn_wait_stopped(folder)
        mac_vpn_record_enabled(False)
        enabled = False
    if action == "setup":
        if status.get("actual") != "ready":
            raise ValueError("proxy did not become ready")
        mac_vpn_record_enabled(True)
    return {**status, "room_enabled": enabled or action == "setup", "restart_required": action == "setup"}


def mac_vpn_save_link(link: str) -> None:
    """Keep credentials outside the room; held directory descriptors refuse symlink parents."""
    account = m_vpn_config.vpn_link(link)
    home = Path.home().resolve()
    directory = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for name in (".config", "collab-vpn"):
            try:
                os.mkdir(name, 0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            os.close(directory)
            directory = child
        path = home / ".config" / "collab-vpn" / "agents.json"
        checked, name = m_vpn_config.vpn_private_parent(path)
        try:
            held, reopened = os.fstat(directory), os.fstat(checked)
            if (held.st_dev, held.st_ino) != (reopened.st_dev, reopened.st_ino):
                raise ValueError("private directory changed")
            document = {
                "status": "active",
                "expire": None,
                "proxies": {"vless": {"id": account.credentials.uuid}},
                "links": [link],
            }
            m_vpn_macos_install.mac_proxy_write(checked, name, json.dumps(document).encode(), 0o600)
        finally:
            os.close(checked)
    except (OSError, ValueError):
        raise m_vpn_config.VpnSecretError("Could not save private VPN account.") from None
    finally:
        os.close(directory)


def mac_vpn_wait_stopped(folder: Path) -> None:
    """The daemon releases its lock only after socket and blocked-marker cleanup."""
    directory, name = m_vpn_config.vpn_private_parent(folder / "supervisor.lock")
    lock = None
    try:
        lock = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(lock)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("proxy lock rejected")
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("VPN supervisor stop unconfirmed") from None
                time.sleep(0.05)
        for entry in ("control.sock", "unconfirmed.json"):
            try:
                os.stat(entry, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise ValueError("VPN supervisor cleanup unconfirmed")
    finally:
        if lock is not None:
            os.close(lock)
        os.close(directory)
