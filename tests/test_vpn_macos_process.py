"""Birth stamps and native hook binding in a fixture HOME; no owner session files."""

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid
from dataclasses import asdict

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _package import Loader
from _seam import seam


@unittest.skipUnless(sys.platform == "darwin", "Darwin process birth identity")
class MacHookBindingTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.home), "COLLAB_CLAUDE_HOME": str(self.home / ".claude")}))
        self.mod = Loader("birth_" + uuid.uuid4().hex[:8], str(Path(__file__).resolve().parents[1] / "collab")).load_module()
        project = self.home / "project"
        project.mkdir()
        self.mod.use_room(str(project))
        self.identity, _ = self.mod.mac_process_info(os.getpid())
        self.sid = str(uuid.uuid4())
        self.old = str(uuid.uuid4())
        self.participant = {
            "id": "claude",
            "kind": "claude",
            "pane_pid": str(self.identity.pid),
            "target": "%5",
            "command": ["/usr/bin/true"],
            "role": "worker",
            "claude_session": self.old,
            "vpn_process": asdict(self.identity),
        }
        self.config = {"session": "birth-test", "macos_vpn": True, "participants": [self.participant]}
        self.mod.write_config(self.config)
        sessions = self.home / ".claude/sessions"
        sessions.mkdir(parents=True)
        self.record = sessions / f"{self.identity.pid}.json"
        self.record.write_text(
            json.dumps(
                {
                    "pid": self.identity.pid,
                    "sessionId": self.sid,
                    "tmux": "own.%5",
                    "procStart": time.strftime("%a %b %d %H:%M:%S %Y", time.gmtime(self.identity.seconds)),
                }
            )
        )
        self.ancestors = self.enterContext(
            seam(self.mod, "vpn_macos_process.mac_hook_ancestors", mock.Mock(return_value=[self.identity]))
        )

    def test_kernel_identity_matches_current_owned_process(self):
        self.assertEqual(self.identity.pid, os.getpid())
        self.assertEqual(self.identity.uid, os.geteuid())
        self.assertGreater(self.identity.seconds, 0)
        self.assertEqual(self.mod.mac_process_info(os.getpid())[0], self.identity)

    def test_new_session_is_bound_after_matching_birth_and_ancestry(self):
        self.assertEqual(self.mod.mac_vpn_bind_hook(self.sid), "claude")
        self.assertEqual(self.mod.read_config_raw()["participants"][0]["claude_session"], self.sid)

    def test_reused_pid_with_different_birth_is_refused(self):
        self.participant["vpn_process"]["microseconds"] = (self.identity.microseconds + 1) % 1000000
        self.mod.write_config(self.config)
        self.assertEqual(self.mod.mac_vpn_bind_hook(self.sid), "")
        self.assertEqual(self.mod.read_config_raw()["participants"][0]["claude_session"], self.old)

    def test_process_outside_current_ancestry_is_refused(self):
        self.ancestors.return_value = []
        self.assertEqual(self.mod.mac_vpn_bind_hook(self.sid), "")

    def test_session_record_sid_mismatch_is_refused(self):
        self.assertEqual(self.mod.mac_vpn_bind_hook(str(uuid.uuid4())), "")

    def test_changed_ancestry_before_write_is_refused(self):
        self.ancestors.side_effect = [[self.identity], []]
        self.assertEqual(self.mod.mac_vpn_bind_hook(self.sid), "")
        self.assertEqual(self.mod.read_config_raw()["participants"][0]["claude_session"], self.old)

    def test_two_participants_with_same_identity_are_ambiguous(self):
        self.config["participants"].append({**self.participant, "id": "other"})
        self.mod.write_config(self.config)
        self.assertEqual(self.mod.mac_vpn_bind_hook(self.sid), "")

    def test_unavailable_birth_api_cannot_bind(self):
        self.ancestors.side_effect = ValueError("synthetic process inspection refusal")
        self.assertEqual(self.mod.mac_vpn_bind_hook(self.sid), "")

    def test_disabled_room_cannot_use_native_binding(self):
        self.config["macos_vpn"] = False
        self.mod.write_config(self.config)
        self.assertEqual(self.mod.mac_vpn_bind_hook(self.sid), "")

    def test_wrong_uid_and_native_start_time_are_refused(self):
        self.participant["vpn_process"]["uid"] = self.identity.uid + 1
        self.mod.write_config(self.config)
        self.assertEqual(self.mod.mac_vpn_bind_hook(self.sid), "")
        self.participant["vpn_process"] = asdict(self.identity)
        self.mod.write_config(self.config)
        record = json.loads(self.record.read_text())
        record["procStart"] = time.strftime("%a %b %d %H:%M:%S %Y", time.gmtime(self.identity.seconds - 10))
        self.record.write_text(json.dumps(record))
        self.assertEqual(self.mod.mac_vpn_bind_hook(self.sid), "")

    def test_native_record_for_another_pane_is_refused(self):
        record = json.loads(self.record.read_text())
        record["tmux"] = "own.%6"
        self.record.write_text(json.dumps(record))
        self.assertEqual(self.mod.mac_vpn_bind_hook(self.sid), "")

    def new_native_record(self):
        return {
            "pid": self.identity.pid,
            "sessionId": self.sid,
            "startedAt": int(time.time() * 1000),
            "version": "2.1.322",
            "pidDomain": "darwin",
            "kind": "interactive",
        }

    def test_millisecond_native_record_binds_without_legacy_fields(self):
        self.record.write_text(json.dumps(self.new_native_record()))
        self.assertEqual(self.mod.mac_vpn_bind_hook(self.sid), "claude")
        self.assertEqual(self.mod.read_config_raw()["participants"][0]["claude_session"], self.sid)

    def test_new_schema_invalid_or_mixed_metadata_cannot_bind(self):
        for field, value in (
            ("startedAt", True),
            ("startedAt", int((self.identity.seconds - 10) * 1000)),
            ("startedAt", int((time.time() + 60) * 1000)),
            ("pidDomain", "linux"),
            ("kind", "background"),
            ("procStart", "bad"),
            ("tmux", "other.%6"),
        ):
            with self.subTest(field=field, value=value):
                record = self.new_native_record()
                record[field] = value
                self.record.write_text(json.dumps(record))
                self.assertEqual(self.mod.mac_vpn_bind_hook(self.sid), "")
                self.assertEqual(self.mod.read_config_raw()["participants"][0]["claude_session"], self.old)

    def test_new_schema_still_requires_exact_process_birth_and_ancestry(self):
        self.record.write_text(json.dumps(self.new_native_record()))
        self.ancestors.return_value = []
        self.assertEqual(self.mod.mac_vpn_bind_hook(self.sid), "")
        self.ancestors.return_value = [self.identity]
        self.participant["vpn_process"]["microseconds"] = (self.identity.microseconds + 1) % 1000000
        self.mod.write_config(self.config)
        self.assertEqual(self.mod.mac_vpn_bind_hook(self.sid), "")
