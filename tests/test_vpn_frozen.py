"""Retired container configuration cannot silently become a host launch."""

import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _package import Loader
from _seam import seam


class FrozenContainerTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.home)}))
        self.mod = Loader("frozen_" + uuid.uuid4().hex[:8], str(Path(__file__).resolve().parents[1] / "collab")).load_module()
        self.mod.use_room(str(self.home / "project"))
        self.config = {"session": "fixture", "language": "en", "participants": [{"id": "probe", "command": ["true"]}]}
        self.mod.write_config(self.config)

    def rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.mod.load_config()

    def test_legacy_container_metadata_is_refused_even_when_empty(self):
        for value in ({}, "", {"name": "retired"}):
            with self.subTest(value=value):
                self.config["participants"][0]["container"] = value
                self.mod.write_config(self.config)
                self.rejected()

    def test_old_intent_file_cannot_fall_back_to_a_host_room(self):
        path = self.mod.room().paths.state_dir / "vpn.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        self.rejected()

    def test_stale_guard_refuses_before_any_tmux_lookup(self):
        with seam(self.mod, "tmuxio.tmux", mock.Mock(side_effect=AssertionError("no host lookup"))):
            self.assertIn("frozen", self.mod.pane_guard({"id": "probe", "container": {}}, "%5", idle=False))

    def test_removed_cli_arguments_are_rejected(self):
        parser = self.mod.build_parser()
        for arguments in (
            ["add", "codex", "--container", "old"],
            ["vpn", "on", "probe"],
            ["vpn", "on", "--agent-image", "old"],
            ["vpn", "on", "--vpn-image", "old"],
            ["vpn", "on", "--account", "old"],
        ):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser.parse_args(arguments)

    def test_plain_room_and_macos_cli_remain_available(self):
        self.assertEqual(self.mod.load_config()["session"], "fixture")
        self.assertEqual(self.mod.build_parser().parse_args(["vpn", "status"]).action, "status")
