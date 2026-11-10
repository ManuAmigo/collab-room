"""Network-only macOS launch policy and pinned proxy release description."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import io
import json
import secrets
import re
import tarfile
from typing import Any, Mapping, Tuple

import collab.vpn_config as m_vpn_config


@dataclass(frozen=True)
class MacVpnEndpoint:
    port: int
    username: str = field(default_factory=lambda: secrets.token_hex(16), repr=False)
    password: str = field(default_factory=lambda: secrets.token_hex(32), repr=False)

    def __post_init__(self) -> None:
        if type(self.port) is not int or not 1024 <= self.port <= 65535:
            raise ValueError("invalid local proxy port")
        if not all(
            isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32,64}", value) for value in (self.username, self.password)
        ):
            raise ValueError("invalid local proxy credentials")

    @property
    def url(self) -> str:
        return f"http://{self.username}:{self.password}@127.0.0.1:{self.port}"


@dataclass(frozen=True)
class MacVpnRelease:
    architecture: str
    sha256: str
    version: str = "1.14.1"

    @property
    def archive(self) -> str:
        return f"sing-box-{self.version}-darwin-{self.architecture}.tar.gz"

    @property
    def url(self) -> str:
        return f"https://github.com/SagerNet/sing-box/releases/download/v{self.version}/{self.archive}"


def mac_vpn_release(machine: str) -> MacVpnRelease:
    if machine == "x86_64":
        return MacVpnRelease("amd64", "b34381b047106fe84895df14f7aaae06f3182130b728006944deb0d59d8590c3")
    if machine == "arm64":
        return MacVpnRelease("arm64", "b9024642ef7b4848252df5469b7f60ef3c18bb5e217a16a0934f0174f8ad11b4")
    raise ValueError("unsupported macOS architecture")


def mac_vpn_profile(endpoint: MacVpnEndpoint) -> str:
    # File access is deliberately unchanged; this is a socket policy, not file isolation.
    return (
        "(version 1)\n(allow default)\n(deny network*)\n"
        f'(allow network-outbound (remote tcp "localhost:{endpoint.port}"))\n'
        '(deny mach-lookup (global-name "com.apple.dnssd.service"))\n'
        '(deny network-outbound (remote unix-socket (literal "/var/run/mDNSResponder")))\n'
    )


def mac_vpn_config(
    account: m_vpn_config.VpnAccount, endpoint: MacVpnEndpoint, resolved_peer: str | None = None
) -> m_vpn_config.VpnPrivateConfig:
    # Only the VPN server is resolved at startup; agent CONNECT targets stay domains.
    server = m_vpn_config.vpn_server_name(account.peer)
    peer = m_vpn_config.vpn_routed_ipv4(resolved_peer if resolved_peer is not None else server)
    if type(account.port) is not int or not 1 <= account.port <= 65535:
        raise ValueError("invalid VPN server port")
    credentials = m_vpn_config.vpn_credentials_from_document(
        {"uuid": account.credentials.uuid, "ws_path": account.credentials.ws_path}
    )
    outbound: dict[str, Any] = {
        "type": "vless",
        "tag": "proxy",
        "server": peer,
        "server_port": account.port,
        "uuid": credentials.uuid,
        "transport": {"type": "ws", "path": credentials.ws_path},
    }
    host = m_vpn_config.vpn_server_name(account.ws_host) if account.ws_host else server
    if account.ws_host or server != peer:
        default_port = 443 if account.tls_server_name is not None else 80
        if not account.ws_host and account.port != default_port:
            host = f"{host}:{account.port}"
        outbound["transport"]["headers"] = {"Host": host}
    if account.tls_server_name is not None:
        outbound["tls"] = {
            "enabled": True,
            "server_name": m_vpn_config.vpn_server_name(account.tls_server_name),
            "insecure": False,
        }
    document = {
        "log": {"disabled": True},
        "inbounds": [
            {
                "type": "mixed",
                "listen": "127.0.0.1",
                "listen_port": endpoint.port,
                "users": [{"username": endpoint.username, "password": endpoint.password}],
            }
        ],
        "outbounds": [outbound],
        "route": {"final": "proxy"},
    }
    return m_vpn_config.VpnPrivateConfig(json.dumps(document, separators=(",", ":")).encode())


def mac_vpn_environment(environment: Mapping[str, str], endpoint: MacVpnEndpoint) -> dict[str, str]:
    result = {key: value for key, value in environment.items() if not key.lower().endswith("_proxy")}
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        result[key] = endpoint.url
    result.update({"NO_PROXY": "", "no_proxy": ""})
    return result


def mac_vpn_command(kind: str, command: Tuple[str, ...], endpoint: MacVpnEndpoint) -> Tuple[str, ...]:
    if kind not in ("codex", "claude") or not command or any(not part or "\x00" in part for part in command):
        raise ValueError("unsupported VPN agent command")
    if kind == "codex":
        # The outer Seatbelt is the network boundary; nested sandbox_apply fails on macOS.
        command = (command[0], "--dangerously-bypass-approvals-and-sandbox", *command[1:])
    return ("/usr/bin/sandbox-exec", "-p", mac_vpn_profile(endpoint), *command)


def mac_vpn_archive_binary(payload: bytes, machine: str) -> bytes:
    release = mac_vpn_release(machine)
    if len(payload) > 64 * 1024 * 1024 or hashlib.sha256(payload).hexdigest() != release.sha256:
        raise ValueError("proxy archive digest rejected")
    expected = f"sing-box-{release.version}-darwin-{release.architecture}/sing-box"
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            members = [member for member in archive.getmembers() if member.name == expected]
            if len(members) != 1 or not members[0].isfile() or not 0 < members[0].size <= 160 * 1024 * 1024:
                raise ValueError("proxy executable member rejected")
            stream = archive.extractfile(members[0])
            if stream is None:
                raise ValueError("proxy executable unavailable")
            with stream:
                binary = stream.read(160 * 1024 * 1024 + 1)
            if len(binary) != members[0].size:
                raise ValueError("proxy executable size rejected")
            return binary
    except (tarfile.TarError, OSError):
        raise ValueError("proxy archive rejected") from None
