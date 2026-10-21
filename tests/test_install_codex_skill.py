"""Installer checks use temporary skills directories, never the user's install."""

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import isolated  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/install-codex-skill.py"


class InstallCodexSkillTests(unittest.TestCase):
    def installer(self, installer, *args, **env):
        """The installer with a home of its own: it ran with the person's HOME, and its `git log`
        read their ~/.gitconfig (a test never reads the person's files)."""
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        return subprocess.run(
            [sys.executable, str(installer), *args],
            text=True,
            capture_output=True,
            env={**os.environ, **isolated(home.name), **env},
        )

    def run_install(self, directory):
        return self.installer(INSTALLER, "--skills-dir", str(directory))

    def test_install_and_repeat(self):
        with tempfile.TemporaryDirectory() as temporary:
            skills = Path(temporary) / "skills with spaces"
            first = self.run_install(skills)
            self.assertEqual(first.returncode, 0, first.stderr)
            source = ROOT / "integrations/codex/skill/SKILL.md"
            self.assertEqual((skills / "collab-room/SKILL.md").read_bytes(), source.read_bytes())
            second = self.run_install(skills)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("Already installed:", second.stdout)

    def test_conflict_is_preserved_and_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            skills = Path(temporary)
            target = skills / "collab-room"
            target.mkdir()
            existing = target / "SKILL.md"
            existing.write_text("User-owned skill\n")
            result = self.run_install(skills)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Refusing to overwrite", result.stderr)
            self.assertEqual(existing.read_text(), "User-owned skill\n")

    def checkout(self, root, text):
        """A copy of the installer whose bundled skill says `text`."""
        installer = root / "scripts/install-codex-skill.py"
        installer.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(INSTALLER, installer)
        (root / "integrations/codex/skill").mkdir(parents=True, exist_ok=True)
        (root / "integrations/codex/skill/SKILL.md").write_text(text)
        return installer

    def git(self, root, *args):
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)

    def test_our_unchanged_copy_is_updated_with_a_backup(self):
        # After updating the checkout, install refused to replace its own old copy.
        with tempfile.TemporaryDirectory() as temporary:
            root, skills = Path(temporary) / "checkout", Path(temporary) / "codex/skills"
            installer = self.checkout(root, "old skill\n")
            run = lambda: self.installer(installer, "--skills-dir", str(skills))
            self.assertEqual(run().returncode, 0)
            (root / "integrations/codex/skill/SKILL.md").write_text("new skill\n")
            result = run()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Updated:", result.stdout)
            self.assertEqual((skills / "collab-room/SKILL.md").read_text(), "new skill\n")
            backups = list((Path(temporary) / "codex/collab-room-skill-backups").iterdir())
            self.assertEqual([(b / "SKILL.md").read_text() for b in backups], ["old skill\n"])
            # Only one skill stays where Codex looks for skills.
            self.assertEqual([p.name for p in skills.iterdir()], ["collab-room"])
            self.assertIn("Already installed:", run().stdout)

    def test_copy_from_before_the_manifest_is_recognised_by_history(self):
        # Installs made before the manifest existed (e.g. from 3b81cd4) carry only the files.
        with tempfile.TemporaryDirectory() as temporary:
            root, skills = Path(temporary) / "checkout", Path(temporary) / "codex/skills"
            installer = self.checkout(root, "old skill\n")
            self.git(root, "init", "-q")
            self.git(root, "add", ".")
            self.git(root, "commit", "-qm", "old")
            (skills / "collab-room").mkdir(parents=True)
            shutil.copyfile(root / "integrations/codex/skill/SKILL.md", skills / "collab-room/SKILL.md")
            (root / "integrations/codex/skill/SKILL.md").write_text("new skill\n")
            self.git(root, "commit", "-qam", "new")
            result = self.installer(installer, "--skills-dir", str(skills))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((skills / "collab-room/SKILL.md").read_text(), "new skill\n")

    def test_our_copy_changed_by_hand_is_kept(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, skills = Path(temporary) / "checkout", Path(temporary) / "codex/skills"
            installer = self.checkout(root, "old skill\n")
            run = lambda: self.installer(installer, "--skills-dir", str(skills))
            self.assertEqual(run().returncode, 0)
            (skills / "collab-room/SKILL.md").write_text("old skill\nmy own note\n")
            (root / "integrations/codex/skill/SKILL.md").write_text("new skill\n")
            result = run()
            self.assertEqual(result.returncode, 1)
            self.assertIn("Refusing to overwrite", result.stderr)
            self.assertEqual((skills / "collab-room/SKILL.md").read_text(), "old skill\nmy own note\n")
            self.assertFalse((Path(temporary) / "codex/collab-room-skill-backups").exists())

    def test_symlink_is_preserved_and_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            skills = Path(temporary) / "skills"
            skills.mkdir()
            target = skills / "collab-room"
            target.symlink_to(ROOT / "integrations/codex/skill", target_is_directory=True)
            result = self.run_install(skills)
            self.assertEqual(result.returncode, 1)
            self.assertTrue(target.is_symlink())

    def test_codex_home_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = Path(temporary) / "custom codex home"
            result = self.installer(INSTALLER, CODEX_HOME=str(codex_home))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((codex_home / "skills/collab-room/SKILL.md").is_file())

    def test_missing_bundle_does_not_create_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installer = root / "scripts/install-codex-skill.py"
            installer.parent.mkdir()
            shutil.copyfile(INSTALLER, installer)
            # An incomplete checkout must not be installed as an empty skill.
            (root / "integrations/codex/skill").mkdir(parents=True)
            target = root / "destination"
            result = self.installer(installer, "--skills-dir", str(target))
            self.assertEqual(result.returncode, 1)
            self.assertIn("bundled SKILL.md is missing", result.stderr)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
