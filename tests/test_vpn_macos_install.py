"""Pinned installer tests use synthetic release bytes, never a network download."""

import hashlib
import io
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _package import Loader
from _seam import seam

mod = Loader("mac_install_" + uuid.uuid4().hex[:8], str(Path(__file__).resolve().parents[1] / "collab")).load_module()


class MacVpnInstallTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.binary = b"synthetic executable"
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            member = tarfile.TarInfo("sing-box-1.14.1-darwin-amd64/sing-box")
            member.size = len(self.binary)
            archive.addfile(member, io.BytesIO(self.binary))
        self.payload = buffer.getvalue()
        self.release = mod.MacVpnRelease("amd64", hashlib.sha256(self.payload).hexdigest())
        self.enterContext(seam(mod, "vpn_macos.mac_vpn_release", mock.Mock(return_value=self.release)))
        self.download = self.enterContext(seam(mod, "vpn_macos_install.mac_proxy_download", mock.Mock(return_value=self.payload)))

    def test_verified_install_and_cache_reuse(self):
        binary = mod.mac_vpn_install(self.folder, "x86_64")
        self.assertEqual(binary.read_bytes(), self.binary)
        self.assertEqual(binary.stat().st_mode & 0o777, 0o500)
        self.assertEqual((self.folder / self.release.archive).stat().st_mode & 0o777, 0o600)
        mod.mac_vpn_install(self.folder, "x86_64")
        self.assertEqual(self.download.call_count, 1)

    def test_wrong_digest_does_not_create_executable_or_cache(self):
        changed = bytearray(self.payload)
        changed[4] ^= 1  # A valid gzip header with a different digest still must not install.
        self.download.return_value = bytes(changed)
        with self.assertRaisesRegex(ValueError, "digest rejected"):
            mod.mac_vpn_install(self.folder, "x86_64")
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_modified_cached_archive_is_rejected_without_redownload(self):
        mod.mac_vpn_install(self.folder, "x86_64")
        (self.folder / self.release.archive).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "digest rejected"):
            mod.mac_vpn_install(self.folder, "x86_64")
        self.assertEqual(self.download.call_count, 1)

    def test_symlink_cache_and_destination_are_refused(self):
        outside = self.folder / "outside"
        outside.write_bytes(b"keep")
        cache = self.folder / self.release.archive
        cache.symlink_to(outside)
        with self.assertRaises(OSError):
            mod.mac_vpn_install(self.folder, "x86_64")
        cache.unlink()
        destination = self.folder / "sing-box-1.14.1-amd64"
        destination.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "destination metadata"):
            mod.mac_vpn_install(self.folder, "x86_64")
        self.assertEqual(outside.read_bytes(), b"keep")

    def test_nonprivate_parent_is_refused(self):
        os.chmod(self.folder, 0o755)
        with self.assertRaises(ValueError):
            mod.mac_vpn_install(self.folder, "x86_64")
        self.download.assert_not_called()
