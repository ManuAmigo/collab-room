"""Codex setup tests never write the real user's configuration or launch an agent.

Codex room-communication rules are disabled: an allow rule ran the
checkout's collab outside Codex's sandbox while a sandboxed Codex could rewrite that file and
pass any --room. These tests hold that no path installs such a rule; the earlier tests of the
installing behaviour are in git history for when an immutable installed copy exists.
"""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import isolated  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/install-codex-rules.py"


class InstallCodexRulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "codex home"
        # Run alone from a room's window it passed the real room's COLLAB_ROOT and TMUX to the
        # installer: the clean environment of the other test files.
        self.env = {
            **{k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")},
            **isolated(self.root),
            "CODEX_HOME": str(self.home),
            "HOME": str(self.root / "home"),
            "COLLAB_INVITES": str(self.root / "collab-room" / "invites"),
        }
        self.target = self.home / "rules/collab-room.rules"

    def run_install(self, *args):
        return subprocess.run([sys.executable, str(INSTALLER), *args], env=self.env, text=True, capture_output=True)

    def test_allow_is_refused_and_creates_nothing(self):
        result = self.run_install("--allow")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("disabled", result.stderr)
        self.assertFalse(self.target.exists())
        self.assertFalse((self.home / "rules").exists(), "not even the rules folder")

    def test_without_allow_nothing_is_installed(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No global Codex rules", result.stdout)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.run_install("--check").returncode, 1)

    def test_rules_left_by_the_earlier_version_are_revoked_and_foreign_ones_kept(self):
        # Disabling the install left the rule already installed by 3b81cd4.
        self.target.parent.mkdir(parents=True)
        self.target.write_text(
            "# Managed by collab-room. Allows communication commands outside the sandbox.\n"
            'prefix_rule(pattern=["python3", "/x/collab", "say"], decision="allow")\n'
        )
        result = self.run_install()
        self.assertFalse(self.target.exists(), "our earlier rule is removed")
        self.assertIn("Removed earlier collab-room Codex rules", result.stdout)
        self.target.write_text("my own rules\n")
        self.run_install()
        self.assertEqual(self.target.read_text(), "my own rules\n", "a foreign file is left alone")
        self.target.unlink()
        elsewhere = self.root / "other.rules"
        elsewhere.write_text("# Managed by collab-room. but a link\n")
        self.target.symlink_to(elsewhere)
        self.run_install()
        self.assertTrue(self.target.is_symlink(), "a link is left alone")
        self.assertTrue(elsewhere.exists())

    def test_claude_plugin_conflict_is_still_reported_without_changing_it(self):
        self.home.mkdir()
        config = self.home / "config.toml"
        original = '[plugins."collab-room@collab-room"]\nenabled = true\n'
        config.write_text(original)
        result = self.run_install()
        self.assertIn("Claude Code plugin", result.stdout)
        self.assertEqual(config.read_text(), original)

    def test_collab_install_never_installs_rules_even_when_asked(self):
        tools = self.root / "tools"
        tools.mkdir()
        for name in ("codex", "tmux"):
            stub = tools / name
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)
        env = {**self.env, "PATH": f"{tools}:/usr/bin:/bin"}
        command = [sys.executable, str(ROOT / "collab"), "install", "--bin-dir", str(tools)]
        self.target.parent.mkdir(parents=True)
        self.target.write_text('# Managed by collab-room. old\nprefix_rule(pattern=["python3"], decision="allow")\n')
        for extra in ([], ["--codex-room-commands"]):
            result = subprocess.run(command + extra, env=env, cwd=self.root, input="y\n", text=True, capture_output=True)
            self.assertFalse(self.target.exists(), f"no rule with {extra or 'no flag'}")
            self.assertNotIn("Allow Codex room communication", result.stdout, "no question is asked")
        self.assertIn("No global Codex rules for collab", result.stdout)
        self.assertNotIn("✗ Codex communication permissions", result.stdout, "no ✗ next to the ✓")
        key = self.root / "collab-room" / "owner.key"
        self.assertEqual(oct(key.stat().st_mode & 0o777), "0o600", "the chat signing key, made at install")
        self.assertIn("Message signing key: kept", result.stdout, "a second install keeps it")

    def test_run_alone_from_a_rooms_window_it_passes_nothing_of_that_room(self):
        probe = (
            "import sys; sys.path.insert(0, sys.argv[1]); import test_install_codex_rules as t; "
            "c = t.InstallCodexRulesTests('test_allow_is_refused_and_creates_nothing'); c.setUp(); "
            "print(sorted(k for k in ('COLLAB_ROOT', 'TMUX', 'TMUX_PANE') if k in c.env)); "
            "print(c.env['COLLAB_STATE_HOME'].startswith(str(c.root)))"
        )
        env = {**os.environ, "COLLAB_ROOT": "/the/real/room", "TMUX": "/tmp/tmux-1/default,1,0", "TMUX_PANE": "%1"}
        out = subprocess.run(
            [sys.executable, "-c", probe, str(Path(__file__).resolve().parent)], env=env, text=True, capture_output=True
        )
        self.assertEqual(out.stdout.split("\n")[:2], ["[]", "True"], out.stdout + out.stderr)


if __name__ == "__main__":
    unittest.main()
