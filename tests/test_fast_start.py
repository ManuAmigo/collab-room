"""The start as a command: the file's first lines are sh, which runs the program compiled once per content,
kept in the person's own folder. What is kept runs unchecked — so a kept file that is not plainly
the person's own is never taken, and a changed file is never run from an older version."""

import hashlib
import marshal
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import PY_BIN, TEST_ROOTS, isolated  # noqa: E402

COLLAB = Path(__file__).resolve().parents[1] / "collab"
PLANTED = "print('from the cache')"


@unittest.skipUnless(TEST_ROOTS, "needs a test folder outside the temporary ones")
class FastStartTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=TEST_ROOTS)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.program = self.root / "collab"
        shutil.copy2(COLLAB, self.program)
        self.env = os.environ | isolated(self.root) | {"PATH": f"{PY_BIN}:/usr/bin:/bin"}
        self.code = self.root / "_home" / ".collab-room" / "code"
        shutil.rmtree(self.code.parent)  # isolated() put the suite's compiled collab there: these start without
        self.version = subprocess.run(
            ["python3", str(COLLAB), "--version"], capture_output=True, text=True, env=self.env
        ).stdout.strip()
        self.assertTrue(self.version.startswith("collab "), self.version)

    def run_command(self, *args: str, cwd=None) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.program), *args], capture_output=True, text=True, env=self.env, cwd=cwd or self.root)

    def kept(self) -> list[Path]:
        return sorted(p for p in self.code.iterdir() if not p.name.startswith(".")) if self.code.is_dir() else []

    def plant(self) -> Path:
        """The one kept file, its code replaced by another program under the same key, with its hash."""
        (kept,) = self.kept()
        code = marshal.dumps(compile(PLANTED, "planted", "exec"))
        head = f"collab code {kept.name} {hashlib.sha256(code).hexdigest()}\n".encode()
        kept.write_bytes(head + code)
        self.assertEqual(self.run_command("--version").stdout.strip(), "from the cache")
        return kept

    def test_the_first_run_keeps_the_compiled_program_closed_to_others(self):
        r = self.run_command("--version")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, self.version), r.stderr)
        (kept,) = self.kept()
        self.assertEqual(oct(self.code.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct(kept.stat().st_mode & 0o777), "0o600")

    def test_the_next_run_takes_the_kept_code(self):
        self.run_command("--version")
        self.plant()

    def test_a_changed_file_runs_its_own_code_not_the_kept(self):
        self.run_command("--version")
        self.plant()
        text = self.program.read_text(encoding="utf-8")
        old = self.version.split()[1]
        self.program.write_text(text.replace(f'VERSION = "{old}"', 'VERSION = "9.9.9"', 1), encoding="utf-8")
        self.assertEqual(self.run_command("--version").stdout.strip(), "collab 9.9.9")
        self.assertEqual(len(self.kept()), 2)

    def test_arguments_and_exit_code_are_the_programs(self):
        r = self.run_command("no-such-command")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("usage: collab", r.stderr)

    def test_a_module_in_the_current_folder_is_not_imported(self):
        here = self.root / "work"
        here.mkdir()
        (here / "json.py").write_text("print('the folder json')\n", encoding="utf-8")
        r = self.run_command("--version", cwd=here)
        self.assertEqual(r.stdout.strip(), self.version, r.stderr)

    def test_a_kept_file_others_may_write_is_not_taken(self):
        self.run_command("--version")
        kept = self.plant()
        for mode in (0o620, 0o602):
            with self.subTest(mode=oct(mode)):
                kept.chmod(mode)
                self.assertEqual(self.run_command("--version").stdout.strip(), self.version)

    def test_a_folder_open_to_others_is_not_read(self):
        self.run_command("--version")
        self.plant()
        self.code.chmod(0o750)
        self.assertEqual(self.run_command("--version").stdout.strip(), self.version)

    def test_a_linked_folder_is_not_read(self):
        self.run_command("--version")
        self.plant()
        elsewhere = self.root / "elsewhere"
        self.code.rename(elsewhere)
        self.code.symlink_to(elsewhere)
        self.assertEqual(self.run_command("--version").stdout.strip(), self.version)

    def test_a_kept_file_that_is_a_link_or_has_two_names_is_not_taken(self):
        self.run_command("--version")
        kept = self.plant()
        planted = self.root / "planted"
        kept.rename(planted)
        kept.symlink_to(planted)
        self.assertEqual(self.run_command("--version").stdout.strip(), self.version, "a link")
        kept.unlink()
        os.link(planted, kept)
        self.assertEqual(self.run_command("--version").stdout.strip(), self.version, "a second name")

    def test_a_kept_file_under_another_key_is_not_taken(self):
        self.run_command("--version")
        kept = self.plant()
        data = kept.read_bytes()
        kept.write_bytes(data.replace(kept.name.encode(), b"0" * 64, 1))
        self.assertEqual(self.run_command("--version").stdout.strip(), self.version)

    def test_code_that_does_not_match_its_hash_is_not_taken(self):
        self.run_command("--version")
        (kept,) = self.kept()
        head = kept.read_bytes().split(b"\n", 1)[0] + b"\n"
        kept.write_bytes(head + marshal.dumps(compile(PLANTED, "planted", "exec")))  # damaged — or changed
        r = self.run_command("--version")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, self.version), r.stderr)

    def test_a_home_in_a_temporary_folder_keeps_nothing(self):
        home = Path(tempfile.mkdtemp())  # where Codex writes by default
        self.addCleanup(shutil.rmtree, home, True)
        self.env["HOME"] = str(home)
        for _ in range(2):
            r = self.run_command("--version")
            self.assertEqual((r.returncode, r.stdout.strip()), (0, self.version), r.stderr)
        code = home / ".collab-room" / "code"
        self.assertEqual([p.name for p in code.iterdir()] if code.is_dir() else [], [])

    def test_a_temporary_home_spelled_in_another_case_keeps_nothing(self):
        if sys.platform != "darwin":
            self.skipTest("macOS's disks ignore case")
        home = Path(tempfile.mkdtemp(dir="/private/tmp"))
        self.addCleanup(shutil.rmtree, home, True)
        self.env["HOME"] = str(home).upper()
        for _ in range(2):
            self.assertEqual(self.run_command("--version").stdout.strip(), self.version)
        code = home / ".collab-room" / "code"
        self.assertEqual([p.name for p in code.iterdir()] if code.is_dir() else [], [])

    def test_a_killed_write_is_cleaned_away(self):
        self.run_command("--version")
        left = self.code / f".{'0' * 64}.123.tmp"
        left.write_bytes(b"half")
        os.utime(left, (0, 0))
        fresh = self.code / f".{'1' * 64}.124.tmp"  # another collab writing now
        fresh.write_bytes(b"half")
        text = self.program.read_text(encoding="utf-8")
        self.program.write_text(text + "\n", encoding="utf-8")  # a new key: a write
        self.run_command("--version")
        self.assertFalse(left.exists())
        self.assertTrue(fresh.exists())

    def test_a_broken_kept_file_is_compiled_anew(self):
        self.run_command("--version")
        (kept,) = self.kept()
        kept.write_bytes(kept.read_bytes()[:1000])
        r = self.run_command("--version")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, self.version), r.stderr)

    def test_a_kept_file_that_fails_to_read_is_compiled_anew(self):
        text = self.program.read_bytes()
        block = text[text.index(b"# fast-start" + b":begin") : text.index(b"# fast-start" + b":end")]
        start: dict = {}
        exec(block, start)  # the block alone, as the sh line runs it
        key = "a" * 64
        start["_fast_keep"](str(self.code), key, compile("pass", "kept", "exec"))
        self.assertIsNotNone(start["_fast_cached"](str(self.code), key, "kept"))
        failing = OSError(5, "Input/output error")
        opened = os.fdopen

        def unreadable(fd, mode):
            f = opened(fd, mode)
            f.read = mock.Mock(side_effect=failing)  # a wrapper over the file: its own read fails
            return f

        for broken, how in (("os.fstat", {"side_effect": failing}), ("os.fdopen", {"side_effect": unreadable})):
            with self.subTest(broken), mock.patch(broken, **how):
                self.assertIsNone(start["_fast_cached"](str(self.code), key, "kept"))

    def test_a_cache_folder_that_cannot_be_made_still_runs(self):
        (self.root / "_home" / ".collab-room").write_text("", encoding="utf-8")  # a file where the folder would be
        r = self.run_command("--version")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, self.version), r.stderr)

    def test_the_oldest_versions_go(self):
        text = self.program.read_text(encoding="utf-8")
        old = self.version.split()[1]
        for n in range(18):
            self.program.write_text(text.replace(f'VERSION = "{old}"', f'VERSION = "9.9.{n}"', 1), encoding="utf-8")
            self.run_command("--version")
        self.assertEqual(len(self.kept()), 16)
        self.assertEqual(self.run_command("--version").stdout.strip(), "collab 9.9.17")


if __name__ == "__main__":
    unittest.main()
