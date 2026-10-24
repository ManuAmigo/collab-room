"""The update: an installed collab moves the clone it was installed from to the newest release tag of its
origin and installs it — by `collab update`, or by itself beside `collab start`, which never waits for the
network. Each test has a public repository of its own (a local bare one): no test reaches the network."""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import PY_BIN, TEST_ROOTS, isolated  # noqa: E402
from _package import Loader as _Loader  # noqa: E402

COLLAB = Path(__file__).resolve().parents[1] / "collab"
CURRENT = re.search(r'^VERSION = "(\d+)\.(\d+)\.(\d+)"$', COLLAB.read_text(encoding="utf-8"), re.M)
OLD = ".".join(CURRENT.groups()) if CURRENT else ""
NEW = f"{CURRENT.group(1)}.{int(CURRENT.group(2)) + 1}.0" if CURRENT else ""
GIT = {"GIT_AUTHOR_NAME": "Releaser", "GIT_AUTHOR_EMAIL": "r@example.invalid"}
GIT |= {"GIT_COMMITTER_NAME": GIT["GIT_AUTHOR_NAME"], "GIT_COMMITTER_EMAIL": GIT["GIT_AUTHOR_EMAIL"]}


@unittest.skipUnless(CURRENT and shutil.which("git") and shutil.which("tmux") and TEST_ROOTS, "needs git, tmux and VERSION")
class UpdateTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=TEST_ROOTS)  # an installed copy in a temporary folder is refused
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        tools = self.root / "tools"  # python3, git and tmux only: no claude or codex for install to set up
        tools.mkdir()
        for name in ("git", "tmux"):
            (tools / name).symlink_to(str(shutil.which(name)))
        self.env = os.environ | isolated(self.root) | GIT | {"PATH": f"{PY_BIN}:{tools}:/usr/bin:/bin"}
        # TMUX would lead every tmux call — collab start's included — to the server of whoever runs the tests.
        for name in ("COLLAB_ROOT", "TMUX", "TMUX_PANE"):
            self.env.pop(name, None)
        # A tmux server of this test's own: `collab start` and the update's restart of delivery use it.
        tmux_dir = tempfile.mkdtemp(prefix="cu-", dir="/tmp")
        self.env["TMUX_TMPDIR"] = tmux_dir
        self.socket = os.path.join(tmux_dir, f"tmux-{os.getuid()}", "default")
        self.addCleanup(shutil.rmtree, tmux_dir, True)
        self.addCleanup(self.stop_tmux)
        self.bin = self.root / "bin"
        self.clone = self.root / "clone"
        self.public = self.root / "public.git"
        self.state = self.root / "_home" / ".collab-room"
        self.env["COLLAB_UPDATE_ORIGIN"] = str(self.public)  # the releases' source the person chose: this test's
        self.release(OLD)
        self.git("clone", "-q", str(self.public), str(self.clone))
        r = self.collab(str(self.clone / "collab"), "install", "--bin-dir", str(self.bin))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.version(), OLD)

    def stop_tmux(self) -> None:
        """This test's tmux server gone, and what ran in its windows with it: kill-server only signals them, and a
        delivery still writing into the room's folder would keep that folder from being removed."""
        panes = subprocess.run(
            ["tmux", "-S", self.socket, "list-panes", "-a", "-F", "#{pane_pid}"], capture_output=True, text=True
        )
        pids = [int(x) for x in panes.stdout.split() if x.isdigit()]
        for pid in list(pids):  # and what they started: a shell's program
            kids = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()
            pids += [int(x) for x in kids if x.isdigit()]
        subprocess.run(["tmux", "-S", self.socket, "kill-server"], capture_output=True)
        end = time.monotonic() + 10
        for pid in pids:
            while time.monotonic() < end:
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
                time.sleep(0.1)

    def git(self, *args: str, cwd=None) -> str:
        run = subprocess.run(["git", *args], cwd=cwd or self.root, env=self.env, check=True, capture_output=True, text=True)
        return run.stdout.strip()

    def collab(self, program: str, *args: str, **kw) -> subprocess.CompletedProcess:
        return subprocess.run(["python3", program, *args], cwd=self.root, env=self.env, capture_output=True, text=True, **kw)

    def release(self, version: str, says: str = "", broken: str = "") -> None:
        """The public repository gets a release: collab saying `version` (or `says`), tagged v<version>;
        `broken`: a line of it replaced with an exit."""
        work = self.root / "releases"
        if not work.exists():
            self.git("init", "-q", "-b", "main", str(work))
            self.git("init", "-q", "--bare", str(self.public))
            self.git("remote", "add", "origin", str(self.public), cwd=work)
        text = COLLAB.read_text(encoding="utf-8").replace(f'VERSION = "{OLD}"', f'VERSION = "{says or version}"')
        if broken:
            self.assertIn(broken, text)
            text = text.replace(broken, 'raise SystemExit("broken")', 1)
        (work / "collab").write_text(text, encoding="utf-8")
        self.git("add", "-A", cwd=work)
        self.git("commit", "-q", "-m", f"collab {version}", cwd=work)
        self.git("tag", "-a", f"v{version}", "-m", f"collab {version}", cwd=work)
        self.git("push", "-q", "--follow-tags", "origin", "main", cwd=work)

    def released(self, version: str) -> str:
        return self.git("rev-parse", f"v{version}^{{commit}}", cwd=self.public)

    def head(self) -> str:
        return self.git("rev-parse", "HEAD", cwd=self.clone)

    def version(self) -> str:
        return self.collab(str(self.bin / "collab"), "--version").stdout.split()[-1]

    def update(self, *args: str) -> subprocess.CompletedProcess:
        return self.collab(str(self.bin / "collab"), "update", *args, timeout=120)

    def room(self) -> Path:
        project = self.root / "project"
        project.mkdir(exist_ok=True)
        r = self.collab(str(self.bin / "collab"), "init", "--no-ask", "--room", str(project))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return project

    def told(self, project: Path) -> list[str]:
        r = self.collab(str(self.bin / "collab"), "inbox", "--for", "owner", "--all", "--room", str(project))
        return [x.strip() for x in r.stdout.splitlines() if "collab" in x and ("updated" in x or "is out" in x)]

    def start(self, project: Path) -> float:
        # The update it starts may still be at work when the test ends: its folder goes after it has.
        self.addCleanup(self.until, lambda: not self.updating(), 90)
        began = time.monotonic()
        env = self.env | {"COLLAB_NO_ATTACH": "1"}
        r = subprocess.run(
            ["python3", str(self.bin / "collab"), "start"], cwd=project, env=env, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return time.monotonic() - began

    def updating(self) -> bool:
        return bool(subprocess.run(["pgrep", "-f", f"update --auto .*{self.root}"], capture_output=True).stdout.strip())

    def until(self, check, timeout: float = 60) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return True
            time.sleep(0.3)
        return False

    def deliverer(self) -> str:
        """What the room's deliverer runs: its window's process and that one's children, on this test's server."""
        panes = subprocess.run(
            ["tmux", "-S", self.socket, "list-panes", "-a", "-F", "#{window_name} #{pane_pid}"], capture_output=True, text=True
        ).stdout.splitlines()
        pid = next((x.split()[1] for x in panes if x.startswith("deliver ")), "")
        pids = [pid, *subprocess.run(["pgrep", "-P", pid], capture_output=True, text=True).stdout.split()] if pid else []
        return "".join(
            subprocess.run(["ps", "-ww", "-o", "command=", "-p", p], capture_output=True, text=True).stdout for p in pids
        )

    def test_update_moves_the_clone_to_the_newest_release_and_installs_it(self):
        self.release(NEW)
        r = self.update()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"collab updated from {OLD} to {NEW}.", r.stdout)
        self.assertEqual(self.version(), NEW, "the collab command runs the new release")
        self.assertEqual(self.head(), self.released(NEW))
        r = self.update()
        self.assertEqual((r.returncode, r.stdout.splitlines()[0]), (0, f"collab {NEW} is the newest"))

    def fake_claude(self, fails: str = "") -> Path:
        """A claude of the test's own on PATH: it writes down each call, lists the plugin at the version in a file
        and moves it to NEW on `plugin update`; `fails`: the call that exits 1 instead. Never the real one."""
        calls = self.root / "claude-calls"
        version = self.root / "claude-plugin-version"
        version.write_text(OLD, encoding="utf-8")
        script = self.root / "tools" / "claude"
        script.write_text(
            "#!/bin/sh\n"
            f'echo "$*" >> {shlex.quote(str(calls))}\n'
            f'if [ "$*" = {shlex.quote(fails)} ]; then echo "no source here" >&2; exit 1; fi\n'
            'case "$*" in\n'
            f'  \'plugin list --json\') printf \'[{{"id": "collab-room@collab-room", "version": "%s"}}]\' '
            f'"$(cat {shlex.quote(str(version))})" ;;\n'
            f"  'plugin update collab-room@collab-room') echo {NEW} > {shlex.quote(str(version))} ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return calls

    def test_the_update_moves_the_claude_plugin_too_and_says_its_agents_need_a_restart(self):
        calls = self.fake_claude()
        self.release(NEW)
        r = self.update()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        made = calls.read_text(encoding="utf-8").splitlines()
        self.assertIn("plugin marketplace update collab-room", made)
        self.assertIn("plugin update collab-room@collab-room", made)
        self.assertLess(made.index("plugin marketplace update collab-room"), made.index("plugin update collab-room@collab-room"))
        said = [x for x in r.stdout.splitlines() if "Claude plugin" in x]
        self.assertEqual(len(said), 1, r.stdout)
        self.assertIn(f"from {OLD} to {NEW}", said[0])
        self.assertIn("/restart", said[0], "the person learns the running Claude agents keep the old hooks")

    def test_a_claude_plugin_that_does_not_update_is_one_line_and_the_update_goes_on(self):
        self.fake_claude(fails="plugin marketplace update collab-room")
        self.release(NEW)
        r = self.update()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.version(), NEW, "collab is updated all the same")
        said = [x for x in r.stdout.splitlines() if "Claude plugin" in x]
        self.assertEqual(len(said), 1, r.stdout)
        self.assertIn("no source here", said[0])
        self.assertNotIn("/restart", said[0])
        (self.root / "tools" / "claude").unlink()
        r = self.update()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len([x for x in r.stdout.splitlines() if "Claude Code not found" in x]), 1, r.stdout)

    def test_the_update_beside_a_start_tells_the_room_the_plugin_moved(self):
        project = self.room()
        self.fake_claude()
        self.release(NEW)
        r = self.update("--auto", "--room", str(project))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        said = self.told(project)
        self.assertEqual(len(said), 1, said)
        self.assertIn(f"collab updated from {OLD} to {NEW}.", said[0])
        self.assertIn(f"Claude plugin updated from {OLD} to {NEW}", said[0])
        self.assertIn("/restart", said[0])

    def test_a_clone_with_changes_of_its_own_is_left_as_it_is(self):
        self.release(NEW)
        with open(self.clone / "collab", "a", encoding="utf-8") as f:
            f.write("# the person's own line\n")
        r = self.update()
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"collab {NEW} is out, not installed: your clone has changes of its own", r.stdout)
        self.assertEqual((self.version(), self.head()), (OLD, self.released(OLD)))
        self.assertIn("# the person's own line", (self.clone / "collab").read_text(encoding="utf-8"))

    def test_a_tag_made_in_the_clone_is_not_a_release(self):
        self.git("tag", "-a", f"v{NEW}", "-m", "made here", cwd=self.clone)
        r = self.update()
        self.assertEqual((r.returncode, r.stdout.splitlines()[0]), (0, f"collab {OLD} is the newest"))

    def test_a_clone_a_codex_may_write_is_not_installed_from(self):
        # By hand the person runs install from it; by itself, at each start, it would run outside the
        # sandbox whatever a sandboxed Codex wrote there.
        self.release(NEW)
        codex = Path(self.env["CODEX_HOME"])
        codex.mkdir(parents=True, exist_ok=True)
        (codex / "config.toml").write_text(f'[sandbox_workspace_write]\nwritable_roots = ["{self.root}"]\n', encoding="utf-8")
        r = self.update()
        self.assertEqual(r.returncode, 1)
        self.assertIn("is not updated by itself", r.stdout)
        self.assertEqual((self.version(), self.head()), (OLD, self.released(OLD)))

    def test_a_git_folder_elsewhere_or_an_origin_a_codex_may_write_is_not_trusted(self):
        self.release(NEW)
        codex = Path(self.env["CODEX_HOME"])
        codex.mkdir(parents=True, exist_ok=True)
        (codex / "config.toml").write_text(f'[sandbox_workspace_write]\nwritable_roots = ["{self.public}"]\n', encoding="utf-8")
        r = self.update()
        self.assertEqual(r.returncode, 1, "the releases would come from a folder a Codex may write")
        self.assertIn(f"Codex may write {self.public}", r.stdout)
        (codex / "config.toml").unlink()
        elsewhere = self.root / "elsewhere.git"
        (self.clone / ".git").rename(elsewhere)
        (self.clone / ".git").write_text(f"gitdir: {elsewhere}\n", encoding="utf-8")
        r = self.update()
        self.assertEqual(r.returncode, 1)
        self.assertIn("its .git is not a folder inside it", r.stdout)
        self.assertEqual(self.version(), OLD)

    def test_releases_come_only_from_collab_rooms_own_repository(self):
        self.release(NEW)
        del self.env["COLLAB_UPDATE_ORIGIN"]
        r = self.update()
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"its origin {self.public} is not collab-room's repository", r.stdout)
        self.assertEqual((self.version(), self.head()), (OLD, self.released(OLD)))
        mod = _Loader("collab_origin", str(COLLAB)).load_module()
        for url in ("https://github.com/ManuAmigo/collab-room.git", "git@github.com:ManuAmigo/collab-room.git"):
            self.assertTrue(mod.our_origin(url), url)
        for url in ("https://github.com/someone/collab-room.git", "https://github.com/ManuAmigo/collab-room.evil", ""):
            self.assertFalse(mod.our_origin(url), url)

    def test_a_release_that_is_not_what_its_tag_says_is_not_installed(self):
        self.release(NEW, says="9.9.9")
        r = self.update()
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"its collab says 'collab 9.9.9', not «collab {NEW}»", r.stdout)
        self.assertEqual((self.version(), self.head()), (OLD, self.released(OLD)), "the clone is back where it was")

    def test_an_install_that_stops_leaves_the_old_command_and_the_clone_as_they_were(self):
        self.release(NEW, broken='print("collab-room: install")')
        r = self.update()
        self.assertEqual(r.returncode, 1)
        self.assertIn("its install did not complete", r.stdout)
        self.assertEqual((self.version(), self.head()), (OLD, self.released(OLD)))

    def test_a_dev_install_does_not_update_itself(self):
        self.release(NEW)
        r = self.collab(str(self.clone / "collab"), "install", "--dev", "--bin-dir", str(self.bin))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.update()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not the installed copy", r.stderr)
        self.assertEqual(self.head(), self.released(OLD), "the clone untouched")

    def test_without_an_answer_from_the_origin_the_room_is_told_nothing(self):
        project = self.room()
        self.release(NEW)
        self.env["COLLAB_UPDATE_ORIGIN"] = str(self.root / "gone.git")
        self.git("remote", "set-url", "origin", self.env["COLLAB_UPDATE_ORIGIN"], cwd=self.clone)
        r = self.update("--auto", "--room", str(project))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.told(project), self.version()), ([], OLD))
        self.assertEqual(json.loads((self.state / "update.json").read_text(encoding="utf-8"))["outcome"], "offline")

    def test_the_update_beside_a_start_tells_the_room_once_per_refusal(self):
        project = self.room()
        self.release(NEW)
        (self.clone / "collab").write_text("# changed\n", encoding="utf-8")
        for _ in range(2):
            r = self.update("--auto", "--room", str(project))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.told(project)), 1, "the same refusal is not said at every start")
        self.git("checkout", "--", "collab", cwd=self.clone)
        self.update("--auto", "--room", str(project))
        self.assertEqual(self.version(), NEW)
        self.assertIn(f"collab updated from {OLD} to {NEW}.", self.told(project)[-1])

    def test_start_installs_a_newer_release_beside_the_room_and_moves_its_delivery(self):
        project = self.room()
        self.release(NEW)
        old_copy = os.path.realpath(self.bin / "collab")
        self.start(project)
        self.assertTrue(self.until(lambda: self.told(project)), "the room is told")
        self.assertIn(f"collab updated from {OLD} to {NEW}.", self.told(project)[0])
        said = self.told(project)[0]
        self.assertIn("reopen the chat", said, "the person learns their chat keeps the old code")
        self.assertIn("/restart each agent", said, "and that running agents do too")
        new_copy = os.path.realpath(self.bin / "collab")
        self.assertNotEqual(new_copy, old_copy)
        self.assertTrue(self.until(lambda: new_copy in self.deliverer()), self.deliverer())

    def test_start_does_not_wait_for_a_silent_network(self):
        project = self.room()
        # An origin that answers nothing for longer than the start may take: an ssh that only waits.
        self.env["GIT_SSH_COMMAND"] = "sh -c 'sleep 25'"
        self.env["COLLAB_UPDATE_ORIGIN"] = "ssh://example.invalid/collab-room.git"
        self.git("remote", "set-url", "origin", self.env["COLLAB_UPDATE_ORIGIN"], cwd=self.clone)
        self.assertLess(self.start(project), 15, "the start went on without the update's answer")
        state = self.state / "update.json"
        self.assertTrue(self.until(state.exists), "the update ran beside the room and gave up")
        self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["outcome"], "offline")
        self.assertEqual(self.told(project), [])

    def test_the_switch_turns_the_update_at_start_off(self):
        project = self.room()
        self.release(NEW)
        r = self.update("--off")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.start(project)  # switched off, it starts nothing: the log is made only by a started update
        self.assertFalse((self.state / "update.log").exists(), "nothing was started")
        self.assertEqual(self.version(), OLD)
        self.update("--on")
        self.start(project)
        self.assertTrue(self.until(lambda: self.version() == NEW), "on again, the next start updates")


if __name__ == "__main__":
    unittest.main()
