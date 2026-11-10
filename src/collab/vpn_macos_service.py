"""Own and inspect a proxy child; no agent is admitted before a verified probe."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import sys
from pathlib import Path
import socket
import subprocess
import time

import collab.vpn_config as m_vpn_config
import collab.vpn_macos as m_vpn_macos


class MacProxyError(ValueError):
    pass


class MacProxyForeign(MacProxyError):
    pass


@dataclass(frozen=True)
class MacProxyIdentity:
    pid: int
    generation: int
    endpoint: m_vpn_macos.MacVpnEndpoint


def mac_proxy_listeners(port: int) -> frozenset[int]:
    result = subprocess.run(
        ["/usr/sbin/lsof", "-nP", "-a", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        close_fds=True,
        timeout=2,
    )
    if result.returncode not in (0, 1) or len(result.stdout) > 8192:
        raise MacProxyError("proxy listener inspection failed")
    owners: set[int] = set()
    for line in result.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit() and int(line[1:]) > 0:
            owners.add(int(line[1:]))
        elif line.startswith("f") and line[1:].isdigit() and owners:
            continue  # lsof emits descriptor markers even with the PID-only field selection.
        else:
            raise MacProxyError("proxy listener inspection rejected")
    return frozenset(owners)


def mac_vpn_resolve_peer(server: str) -> str:
    """Bound bootstrap DNS in a disposable child; never give it an agent target or credentials."""
    try:
        server = m_vpn_config.vpn_server_name(server)
        try:
            return m_vpn_config.vpn_routed_ipv4(server)
        except ValueError:
            pass
        code = (
            "import json,socket,sys;"
            "print(json.dumps(sorted({r[4][0] for r in socket.getaddrinfo(sys.argv[1],None,socket.AF_INET,socket.SOCK_STREAM)})))"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", code, server],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            timeout=3,
        )
        if result.returncode != 0 or len(result.stdout) > 4096:
            raise ValueError()
        answers = json.loads(result.stdout)
        if not isinstance(answers, list) or not 1 <= len(answers) <= 16:
            raise ValueError()
        addresses = [m_vpn_config.vpn_routed_ipv4(answer) for answer in answers]
        return addresses[0]
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        raise MacProxyError("VPN server bootstrap DNS failed") from None


class MacProxyProcess:
    def __init__(self) -> None:
        self.process: subprocess.Popen[bytes] | None = None
        self.identity: MacProxyIdentity | None = None

    def start(self, binary: Path, account: m_vpn_config.VpnAccount, generation: int) -> MacProxyIdentity:
        if self.process is not None:
            raise MacProxyError("proxy child already exists")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            endpoint = m_vpn_macos.MacVpnEndpoint(reservation.getsockname()[1])
        peer = mac_vpn_resolve_peer(account.peer)
        payload = m_vpn_macos.mac_vpn_config(account, endpoint, peer).payload
        process = subprocess.Popen(
            [str(binary), "run", "-c", "/dev/stdin"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        self.process = process
        self.identity = MacProxyIdentity(process.pid, generation, endpoint)
        try:
            if process.stdin is None:
                raise MacProxyError("proxy configuration pipe unavailable")
            process.stdin.write(payload)
            process.stdin.close()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise MacProxyError("proxy child exited before listening")
                owners = mac_proxy_listeners(endpoint.port)
                if owners - {process.pid}:
                    raise MacProxyForeign("foreign proxy listener")
                if owners == {process.pid}:
                    return self.identity
                time.sleep(0.05)
            raise MacProxyError("proxy listener did not appear")
        except BaseException:
            self.stop()
            raise

    def owned(self) -> bool:
        process, identity = self.process, self.identity
        if process is None or identity is None or process.poll() is not None:
            return False
        owners = mac_proxy_listeners(identity.endpoint.port)
        return owners == {process.pid} and process.poll() is None

    def probe(self) -> str:
        if not self.owned() or self.identity is None:
            raise MacProxyError("proxy listener ownership lost")
        endpoint = self.identity.endpoint
        result = subprocess.run(
            [
                "/usr/bin/curl",
                "--silent",
                "--show-error",
                "--max-time",
                "10",
                "--max-filesize",
                "128",
                "--config",
                "-",
                "--proxy",
                f"http://127.0.0.1:{endpoint.port}",
                "--noproxy",
                "",
                "https://api.ipify.org",
            ],
            input=f'proxy-user = "{endpoint.username}:{endpoint.password}"\n'.encode(),
            capture_output=True,
            close_fds=True,
            timeout=12,
        )
        if result.returncode != 0 or len(result.stdout) > 128 or not self.owned():
            raise MacProxyError("proxy transport probe failed")
        try:
            address = ipaddress.ip_address(result.stdout.decode("ascii").strip())
            if not address.is_global:
                raise ValueError()
        except (ValueError, UnicodeError):
            raise MacProxyError("proxy exit address rejected") from None
        return str(address)

    def stop(self) -> None:
        process = self.process
        if process is None:
            return
        if process.stdin is not None and not process.stdin.closed:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
        # Popen retains the child relationship; no PID loaded from a state file is signalled.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        else:
            process.wait()
        self.process = None
        self.identity = None
