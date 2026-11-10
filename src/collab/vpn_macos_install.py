"""Install only a hash-pinned proxy executable into an already private directory."""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import stat
import urllib.request as mac_proxy_http

import collab.vpn_config as m_vpn_config
import collab.vpn_macos as m_vpn_macos


def mac_proxy_download(url: str) -> bytes:
    request = mac_proxy_http.Request(url, headers={"User-Agent": "collab-room"})
    with mac_proxy_http.urlopen(request, timeout=30) as response:
        payload = response.read(64 * 1024 * 1024 + 1)
    if len(payload) > 64 * 1024 * 1024:
        raise ValueError("proxy download exceeds limit")
    return payload


def mac_proxy_cache_read(directory: int, name: str) -> bytes | None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > 64 * 1024 * 1024
        ):
            raise ValueError("proxy cache metadata rejected")
        payload = stream.read(64 * 1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
        if len(payload) > 64 * 1024 * 1024 or (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("proxy cache changed while reading")
        return payload


def mac_proxy_write(directory: int, name: str, payload: bytes, mode: int) -> None:
    try:
        metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        metadata = None
    if metadata is not None and (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_nlink != 1):
        raise ValueError("proxy destination metadata rejected")
    temporary = ".proxy-" + secrets.token_hex(16)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def mac_vpn_install(folder: Path, machine: str) -> Path:
    release = m_vpn_macos.mac_vpn_release(machine)
    directory, _ = m_vpn_config.vpn_private_parent(folder / "sing-box")
    name = f"sing-box-{release.version}-{release.architecture}"
    try:
        payload = mac_proxy_cache_read(directory, release.archive)
        downloaded = payload is None
        if payload is None:
            payload = mac_proxy_download(release.url)
        # Authentication precedes tar parsing and every installation, including cache reuse.
        binary = m_vpn_macos.mac_vpn_archive_binary(payload, machine)
        if downloaded:
            mac_proxy_write(directory, release.archive, payload, 0o600)
        mac_proxy_write(directory, name, binary, 0o500)
    finally:
        os.close(directory)
    return folder / name
