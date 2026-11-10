"""Private VPN account parsing; no client, network or server action.

VpnCredentials and private configuration payloads must live outside Git worktrees.
Exceptions and repr never include credential values. Runtime and network policy
are validated by the platform-specific launcher.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import json
import os
from pathlib import Path
import stat
from urllib.parse import parse_qsl, urlsplit
import math
import uuid


class VpnSecretError(ValueError):
    pass


@dataclass(frozen=True)
class VpnCredentials:
    uuid: str = field(repr=False)
    ws_path: str = field(repr=False)


@dataclass(frozen=True)
class VpnPrivateConfig:
    payload: bytes = field(repr=False)


def vpn_private_parent(path: Path) -> tuple[int, str]:
    """Open every directory without symlinks and reject any Git ancestor."""
    if not path.is_absolute() or ".." in path.parts or path.name in ("", "."):
        raise VpnSecretError("private file requires an absolute path")
    if any(os.path.lexists(parent / ".git") for parent in path.parents):
        raise VpnSecretError("private file must be outside Git worktrees")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:-1]:
            next_directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            os.close(directory)
            directory = next_directory
        metadata = os.fstat(directory)
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise VpnSecretError("private parent ownership or mode rejected")
        return directory, path.name
    except BaseException:
        os.close(directory)
        raise


def vpn_unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise VpnSecretError("invalid credential document")
        result[key] = value
    return result


def vpn_read_private_document(path: Path, limit: int = 8192) -> dict:
    directory = None
    fd = None
    try:
        directory, name = vpn_private_parent(path)
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size > limit
        ):
            raise VpnSecretError("credential file metadata rejected")
        data = os.read(fd, limit + 1)
        after = os.fstat(fd)
        if len(data) > limit or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise VpnSecretError("credential file changed")
        document = json.loads(data, object_pairs_hook=vpn_unique_fields)
        if not isinstance(document, dict):
            raise VpnSecretError("invalid credential document")
        return document
    except (OSError, ValueError, TypeError, UnicodeError, KeyError):
        raise VpnSecretError("credential file rejected") from None
    finally:
        if fd is not None:
            os.close(fd)
        if directory is not None:
            os.close(directory)


def vpn_credentials_from_document(document: dict) -> VpnCredentials:
    try:
        if not isinstance(document, dict) or set(document) != {"uuid", "ws_path"}:
            raise VpnSecretError("invalid credential document")
        identifier, ws_path = document["uuid"], document["ws_path"]
        if not isinstance(identifier, str) or str(uuid.UUID(identifier)) != identifier or uuid.UUID(identifier).int == 0:
            raise VpnSecretError("invalid credential document")
        if (
            not isinstance(ws_path, str)
            or not ws_path.startswith("/")
            or ws_path.startswith("//")
            or len(ws_path) > 2048
            or any(not 33 <= ord(char) <= 126 for char in ws_path)
        ):
            raise VpnSecretError("invalid credential document")
        return VpnCredentials(identifier, ws_path)
    except (ValueError, TypeError, KeyError):
        raise VpnSecretError("credential document rejected") from None


def vpn_routed_ipv4(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value)
        if (
            str(address) != value
            or address.is_loopback
            or address.is_multicast
            or address.is_unspecified
            or address.is_link_local
            or value == "255.255.255.255"
        ):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("invalid numeric routed IPv4") from None
    return value


@dataclass(frozen=True)
class VpnAccount:
    credentials: VpnCredentials = field(repr=False)
    peer: str
    expires: int | None


def vpn_account(path: Path, now: float) -> VpnAccount:
    try:
        if not math.isfinite(now) or now < 0:
            raise ValueError()
        document = vpn_read_private_document(path, 65536)
        expiry = document["expire"]
        if document.get("status") != "active":
            raise ValueError()
        if expiry is not None and (type(expiry) is not int or expiry <= now):
            raise ValueError()
        proxies = document["proxies"]
        if not isinstance(proxies, dict) or set(proxies) != {"vless"}:
            raise ValueError()
        identifier = proxies["vless"]["id"]
        links = document["links"]
        if not isinstance(links, list) or len(links) != 1 or not isinstance(links[0], str) or len(links[0]) > 8192:
            raise ValueError()
        uri = urlsplit(links[0])
        pairs = parse_qsl(uri.query, keep_blank_values=True, strict_parsing=True)
        query = dict(pairs)
        if (
            uri.scheme != "vless"
            or uri.username != identifier
            or uri.password is not None
            or uri.port != 80
            or uri.path not in ("", "/")
            or len(query) != len(pairs)
            or query.get("type") != "ws"
            or query.get("security", "none") != "none"
            or query.get("encryption", "none") != "none"
            or query.get("headerType", "none") not in ("", "none")
            or any(query.get(key) for key in ("host", "sni", "flow"))
            or set(query) - {"type", "security", "path", "encryption", "headerType", "host", "sni", "flow"}
        ):
            raise ValueError()
        hostname = uri.hostname
        if not isinstance(hostname, str):
            raise ValueError()
        peer = vpn_routed_ipv4(hostname)
        credentials = vpn_credentials_from_document({"uuid": identifier, "ws_path": query["path"]})
        return VpnAccount(credentials, peer, expiry)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise VpnSecretError("VPN account profile rejected") from None
