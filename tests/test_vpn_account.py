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
