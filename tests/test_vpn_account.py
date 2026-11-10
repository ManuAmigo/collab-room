"""Private profile expiry validation using synthetic files, never owner credentials."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _package import Loader

mod = Loader(
    "vpn_account_" + uuid.uuid4().hex[:8],
    os.environ.get("COLLAB_VPN_TEST_CLI", str(Path(__file__).resolve().parents[1] / "collab")),
).load_module()


class VpnAccountExpiryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.path = self.root / "account.json"
        identifier = "11111111-1111-4111-8111-111111111111"
        self.document = {
            "username": "example",
            "status": "active",
            "expire": None,
            "proxies": {"vless": {"id": identifier}},
            "links": ["vless://" + identifier + "@8.8.4.4:80?type=ws&security=none&path=%2Ffixture&encryption=none"],
        }

    def profile(self):
        self.path.write_text(json.dumps(self.document))
        self.path.chmod(0o600)
        return mod.vpn_account(self.path, 100)

    def test_explicit_null_expiry_accepts_permanent_active_profile(self):
        account = self.profile()
        self.assertIsNone(account.expires)
        self.assertEqual(account.peer, "8.8.4.4")

    def test_future_expiry_remains_supported(self):
        self.document["expire"] = 200
        self.assertEqual(self.profile().expires, 200)

    def test_expired_malformed_and_absent_expiry_are_rejected(self):
        for expiry in (True, False, 0, -1, 100, "200", 200.0):
            with self.subTest(expiry=expiry), self.assertRaises(mod.VpnSecretError):
                self.document["expire"] = expiry
                self.profile()
        del self.document["expire"]
        with self.assertRaises(mod.VpnSecretError):
            self.profile()

    def test_permanent_profile_still_requires_active_status(self):
        for status in ("disabled", "expired", "limited", "on_hold"):
            with self.subTest(status=status), self.assertRaises(mod.VpnSecretError):
                self.document["status"] = status
                self.profile()

    def test_link_rejects_controls_without_exposing_credentials(self):
        self.document["links"][0] = self.document["links"][0].replace("vless://", "vless://\n")
        with self.assertRaises(mod.VpnSecretError) as failure:
            self.profile()
        self.assertNotIn("11111111", str(failure.exception))

    def test_supported_link_and_sanitized_refusals(self):
        link = self.document["links"][0]
        self.assertEqual(mod.vpn_link(link + "#My%20VPN").peer, "8.8.4.4")
        for bad in (
            link.replace("none", "reality", 1),
            link.replace("type=ws", "type=tcp"),
            link.replace("8.8.4.4", "-bad.example.org"),
            link.replace(":80?", ":65536?"),
            link + "&type=ws",
            link + "&unexpected=yes",
            link.replace("vless://", "https://"),
            link.replace("8.8.4.4", "[broken"),
            link + "\n",
            link.replace("%2Ffixture", "%ZZ"),
        ):
            with self.subTest(kind=bad[-8:]), self.assertRaises(mod.VpnSecretError) as failure:
                mod.vpn_link(bad)
            self.assertNotIn("11111111", str(failure.exception))
            self.assertNotIn("fixture", str(failure.exception))

    def test_save_is_private_atomic_and_refuses_symlinks(self):
        from _seam import seam

        home = self.root / "home"
        home.mkdir(mode=0o700)
        link = self.document["links"][0]
        with seam(mod, "vpn_macos_room.Path.home", lambda: home):
            mod.mac_vpn_save_link(link)
            path = home / ".config/collab-vpn/agents.json"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(mod.vpn_account(path, 100).peer, "8.8.4.4")
            before = path.stat().st_ino
            mod.mac_vpn_save_link(link.replace("8.8.4.4", "1.1.1.1"))
            self.assertNotEqual(path.stat().st_ino, before)
            self.assertEqual(mod.vpn_account(path, 100).peer, "1.1.1.1")
            original = path.read_bytes()
            with self.assertRaises(mod.VpnSecretError):
                mod.mac_vpn_save_link("bad")
            self.assertEqual(path.read_bytes(), original)
            path.unlink()
            path.symlink_to(self.path)
            with self.assertRaises(mod.VpnSecretError):
                mod.mac_vpn_save_link(link)
            self.assertTrue(path.is_symlink())
            self.assertFalse(self.path.exists())
            path.unlink()
            path.parent.chmod(0o755)
            with self.assertRaises(mod.VpnSecretError):
                mod.mac_vpn_save_link(link)
            self.assertFalse(path.exists())

    def test_domain_tls_with_sni_and_ws_host_preserves_profile_without_dns(self):
        link = self.document["links"][0].replace("8.8.4.4:80", "vpn.example.org:443").replace("security=none", "security=tls")
        self.document["links"] = [link + "&sni=cert.example.org&host=ws.example.org"]
        account = self.profile()
        self.assertEqual(account.peer, "vpn.example.org")
        self.assertEqual(account.port, 443)
        self.assertEqual(account.tls_server_name, "cert.example.org")
        self.assertEqual(account.ws_host, "ws.example.org")
        self.assertIsNone(account.expires)

    def test_tls_defaults_and_unsupported_options_are_explicit(self):
        link = self.document["links"][0].replace("8.8.4.4:80", "vpn.example.org:8443").replace("security=none", "security=tls")
        account = mod.vpn_link(link)
        self.assertEqual(account.tls_server_name, "vpn.example.org")
        self.assertEqual(account.port, 8443)
        for bad in (
            link.replace("security=tls", "security=reality"),
            link + "&allowInsecure=1",
            link + "&sni=bad%0d%0aHeader%3Avalue",
            link + "&host=bad%20host.example.org",
            link.replace("vpn.example.org", "localhost"),
            link.replace("vpn.example.org", "127.1"),
            link.replace("vpn.example.org", "[::1]"),
            link.replace("type=ws", "type=grpc"),
        ):
            with self.subTest(kind=bad[-12:]), self.assertRaises(mod.VpnSecretError) as failure:
                mod.vpn_link(bad)
            self.assertNotIn("11111111", str(failure.exception))
