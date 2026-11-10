"""Opt-in room launch routing to one user's macOS network-only supervisor."""

from __future__ import annotations

from pathlib import Path
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
def mac_vpn_record_setup() -> None:
    config = m_store.load_config()
    config["macos_vpn"] = True
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
    if action not in ("setup", "on", "off") or (action != "setup" and not enabled):
        raise ValueError("run VPN setup for this room first")
    if action in ("setup", "on"):
        account = Path.home() / ".config" / "collab-vpn" / "agents.json"
        m_vpn_config.vpn_account(account, time.time())
        binary = m_vpn_macos_install.mac_vpn_install(folder, platform.machine())
        mac_vpn_ensure(folder, binary, account)
    status = m_vpn_macos_daemon.mac_control_request(folder, "on" if action == "setup" else action)
    if action == "setup":
        if status.get("actual") != "ready":
            raise ValueError("proxy did not become ready")
        mac_vpn_record_setup()
    return {**status, "room_enabled": enabled or action == "setup", "restart_required": action == "setup"}
