"""scripts/ci_parallel.py — the full suite in up to four processes, run here on a
small suite of its own."""

import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci_parallel.py"
ci_parallel = SourceFileLoader("ci_parallel", str(SCRIPT)).load_module()


class CiParallelTest(unittest.TestCase):
    def setUp(self):
        self.tests = Path(tempfile.mkdtemp(prefix="cpt-"))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tests)])
        self.seen = self.tests / "seen.txt"

    def suite(self, name: str, body: str) -> None:
        (self.tests / name).write_text(textwrap.dedent(body), encoding="utf-8")

    def env(self, tmpdir: str, keep_tmux: bool = False) -> dict:
        # The fake tests below start tmux: never with the runner's TMUX — that is the room's server.
        env = {k: v for k, v in os.environ.items() if keep_tmux or k not in ("TMUX", "TMUX_PANE")}
        # Not the merge queue's report: a run here, inside the queue's own, wrote its tests into it.
        env.pop("CI_PARALLEL_REPORT", None)
        return {
            **env,
            "CI_PARALLEL_TESTS": str(self.tests),
            # A busy machine unless a test says otherwise: the machine's own load must not choose for them.
            "CI_PARALLEL_LOAD": "99",
            "CPT_SEEN": str(self.seen),
            "CPT_DIR": str(self.tests),
            "TMPDIR": tmpdir,
            # Never the machine's own lock: a merge queue checking right now would take these runs to one worker.
            "COLLAB_QUEUE_LOCK": str(self.tests / "no-queue.lock"),
        }

    def short_tmpdir(self) -> str:
        folder = tempfile.mkdtemp(prefix="cm-", dir="/tmp")  # as a merge queue's
        self.addCleanup(shutil.rmtree, folder, True)
        return folder

    def run_script(self, *args: str, tmpdir: str = "") -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            env=self.env(tmpdir or self.short_tmpdir()),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def recording_tests(self, count: int) -> None:
        methods = "".join(f"    def test_{i:02d}(self):\n        record(self.id())\n" for i in range(count))
        self.suite(
            "test_a.py",
            "import os, unittest\n"
            "def record(tid):\n"
            "    with open(os.environ['CPT_SEEN'], 'a') as f:\n"
            "        f.write(f\"{tid} {os.environ['TMPDIR']} {os.getpid()}\\n\")\n"
            f"class A(unittest.TestCase):\n{methods}",
        )

    def test_every_test_runs_once_each_worker_in_a_process_and_tmpdir_of_its_own(self):
        self.recording_tests(10)
        caller = self.short_tmpdir()
        r = self.run_script("--workers", "3", tmpdir=caller)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rows = [line.split() for line in self.seen.read_text().splitlines()]
        self.assertEqual(sorted(tid for tid, _, _ in rows), [f"test_a.A.test_{i:02d}" for i in range(10)], "each once")
        self.assertEqual(len({pid for _, _, pid in rows}), 3, "three processes")
        tmpdirs = {tmp for _, tmp, _ in rows}
        self.assertEqual(len(tmpdirs), 3, "a TMPDIR each — so a tmux server and test folders each")
        # Under the caller's short TMPDIR — a merge queue's, which keeps a red run's logs.
        self.assertTrue(all(t.startswith(caller + "/cp-") for t in tmpdirs), tmpdirs)
        self.assertIn("Ran 10 tests", r.stdout)
        self.assertIn(f"tests from {self.tests.resolve()}, not tests/", r.stdout, "another folder is said")

    def test_under_a_long_tmpdir_the_workers_go_to_tmp(self):
        # macOS's own TMPDIR (/var/folders/…): one level more and a tmux socket passes 103 bytes.
        long = Path(self.short_tmpdir()) / ("x" * 60)
        long.mkdir()
        self.recording_tests(2)
        r = self.run_script("--workers", "2", tmpdir=str(long))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tmpdirs = {line.split()[1] for line in self.seen.read_text().splitlines()}
        self.assertTrue(all(t.startswith("/tmp/cp-") for t in tmpdirs), tmpdirs)

    @unittest.skipUnless(shutil.which("tmux"), "needs tmux")
    def test_stopped_by_sigterm_it_leaves_no_worker_and_no_tmux_server(self):
        # The merge queue stops its checks with SIGTERM; it looks for tmux sockets one level under its folder,
        # and a worker's are deeper. Each fake worker starts a server as test_collab does (ct-* in TMPDIR).
        self.suite(
            "test_server.py",
            """\
            import os, subprocess, tempfile, time, unittest
            class S(unittest.TestCase):
                def test_waits_with_a_server(self):
                    ct = tempfile.mkdtemp(prefix="ct-")
                    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
                    env["TMUX_TMPDIR"] = ct
                    subprocess.run(["tmux", "-f", "/dev/null", "new-session", "-d", "sleep 300"], env=env, check=True)
                    sock = os.path.join(ct, f"tmux-{os.getuid()}", "default")
                    assert os.path.exists(sock), "the server is this test's own"
                    with open(os.environ["CPT_SEEN"], "a") as f:
                        f.write(f"{sock} {os.getpid()}\\n")
                    time.sleep(120)
                test_a = test_b = test_waits_with_a_server
            """,
        )
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--workers", "3"],
            env=self.env(self.short_tmpdir()),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(proc.kill)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and (not self.seen.exists() or len(self.seen.read_text().splitlines()) < 3):
            time.sleep(0.2)
        rows = [line.split() for line in self.seen.read_text().splitlines()]
        self.assertEqual(len(rows), 3, "three workers, a server each")
        for sock, _ in rows:  # whatever happens below, no server of this test outlives it
            self.addCleanup(subprocess.run, ["tmux", "-S", sock, "kill-server"], capture_output=True)
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=30), 143)
        for sock, pid in rows:
            alive = subprocess.run(["tmux", "-S", sock, "has-session"], capture_output=True).returncode == 0
            self.assertFalse(alive, f"the worker's tmux server is gone: {sock}")
            with self.assertRaises(ProcessLookupError, msg="the worker is gone"):
                os.kill(int(pid), 0)

    def test_a_worker_never_has_the_callers_tmux(self):
        # With TMUX set a test's bare `tmux` reaches the caller's server — the person's or the room's.
        self.suite(
            "test_env.py",
            """\
            import os, unittest
            class E(unittest.TestCase):
                def test_env(self):
                    with open(os.environ["CPT_SEEN"], "a") as f:
                        f.write(repr((os.environ.get("TMUX"), os.environ.get("TMUX_PANE"))) + "\\n")
            """,
        )
        env = {**self.env(self.short_tmpdir(), keep_tmux=True), "TMUX": "/nonexistent/sock,1,0", "TMUX_PANE": "%9"}
        r = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.seen.read_text().strip(), "(None, None)")

    def test_four_workers_on_a_free_machine_two_on_a_busy_one(self):
        # Four beside other work took the whole machine; on a free one they halve the run.
        if (os.cpu_count() or 1) < 4:
            self.skipTest("fewer than four cores: never more workers than cores")
        self.recording_tests(8)
        for load, count in (("0.5", 4), ("99", 2)):
            self.seen.write_text("")
            r = subprocess.run(
                [sys.executable, str(SCRIPT)],
                env={**self.env(self.short_tmpdir()), "CI_PARALLEL_LOAD": load},
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn(f"in {count} workers", r.stdout, load)
            self.assertEqual(len({line.split()[2] for line in self.seen.read_text().splitlines()}), count, load)

    def queue_lock(self, pid: int) -> Path:
        lock = self.tests / "queue.lock"
        start = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        lock.write_text(json.dumps({"pid": pid, "start": start}), encoding="utf-8")
        return lock

    def test_while_the_merge_queue_checks_its_own_tests_take_one_worker_niced(self):
        # The queue's run is the one that lands; a run outside it then takes one worker, niced —
        # a module's own tests (--pattern); the full suite waits (the test below).
        self.suite(
            "test_n.py",
            "import os, unittest\nclass N(unittest.TestCase):\n    def test_n(self):\n"
            "        open(os.environ['CPT_SEEN'], 'a').write(str(os.nice(0)) + '\\n')\n",
        )
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(sleeper.kill)
        env = {**self.env(self.short_tmpdir()), "COLLAB_QUEUE_LOCK": str(self.queue_lock(sleeper.pid))}
        env.pop("COLLAB_QUEUE_RUN", None)
        run = lambda *a, **e: subprocess.run(
            [sys.executable, str(SCRIPT), *a], env={**env, **e}, capture_output=True, text=True, timeout=120
        )
        mine = ("--pattern", "test_n.py")
        r = run(*mine)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"the merge queue is checking (pid {sleeper.pid}): 1 worker", r.stdout)
        self.assertIn("in 1 workers", r.stdout)
        self.assertGreaterEqual(int(self.seen.read_text().split()[-1]), 10, "niced")
        # The queue's own run is not slowed by its own lock; a chosen --workers stands; a dead lock is none.
        for args, extra in (((), {"COLLAB_QUEUE_RUN": "1"}), ((*mine, "--workers", "2"), {})):
            r = run(*args, **extra)
            self.assertIn("in 2 workers", r.stdout, (args, extra))
        lock = json.loads(Path(env["COLLAB_QUEUE_LOCK"]).read_text())
        Path(env["COLLAB_QUEUE_LOCK"]).write_text(json.dumps({**lock, "start": "Mon Jan  1 00:00:00 2001"}))
        self.assertIn("in 2 workers", run().stdout, "the pid lives, but it is another process: no lock")
        self.queue_lock(sleeper.pid)
        sleeper.kill()
        sleeper.wait()
        self.assertIn("in 2 workers", run().stdout, "the lock's process is gone: no lock")

    def test_while_the_merge_queue_checks_the_full_suite_waits_for_it(self):
        # One full suite on the machine — outside the queue it waits for
        # the queue's checks to end, whatever --workers it was given, and runs after them.
        self.recording_tests(1)
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(sleeper.kill)
        env = {**self.env(self.short_tmpdir()), "COLLAB_QUEUE_LOCK": str(self.queue_lock(sleeper.pid))}
        env.pop("COLLAB_QUEUE_RUN", None)
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--workers", "2", "--poll", "0.1"], env=env, stdout=subprocess.PIPE, text=True
        )
        self.addCleanup(proc.kill)
        assert proc.stdout is not None
        self.assertEqual(
            proc.stdout.readline(), f"the merge queue is checking (pid {sleeper.pid}): the full suite waits for it to end\n"
        )
        time.sleep(1)  # polls of the lock: no test runs meanwhile
        self.assertIsNone(proc.poll())
        self.assertFalse(self.seen.exists(), "no test ran beside the queue's checks")
        sleeper.kill()
        sleeper.wait()
        out, _ = proc.communicate(timeout=120)
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("in 2 workers", out)
        self.assertEqual(len(self.seen.read_text().splitlines()), 1, "it ran once the queue's checks ended")

    def test_more_than_four_workers_is_four(self):
        self.recording_tests(8)
        r = self.run_script("--workers", "9")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("in 4 workers", r.stdout)
        self.assertEqual(len({line.split()[2] for line in self.seen.read_text().splitlines()}), 4)

    def test_a_test_red_in_the_run_and_green_alone_stays_red(self):
        # The failed test runs again alone, both results are printed, red stays red.
        self.suite(
            "test_flaky.py",
            """\
            import os, unittest
            class F(unittest.TestCase):
                def test_green(self):
                    pass
                def test_red_once(self):
                    mark = os.path.join(os.environ["CPT_DIR"], "ran-once")
                    first = not os.path.exists(mark)
                    open(mark, "w").close()
                    self.assertFalse(first, "red the first time only")
            """,
        )
        r = self.run_script("--workers", "2")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("red the first time only", r.stdout, "the parallel run's failure is printed")
        self.assertIn("alone: test_flaky.F.test_red_once — OK", r.stdout)
        self.assertIn("FAILED (red=1", r.stdout)

    def test_a_red_test_is_one_line_its_full_text_left_in_its_workers_log(self):
        # Whoever reads the run pays for each line: the tracebacks of red tests were most of a red run's text.
        self.suite(
            "test_r.py",
            """\
            import unittest
            class R(unittest.TestCase):
                def test_red(self):
                    def deep(n):
                        return deep(n - 1) if n else self.assertEqual(29, 3)
                    deep(5)
            """,
        )
        r = self.run_script("--workers", "1")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stdout)
        line = next((x for x in r.stdout.splitlines() if x.startswith("FAIL: test_red (test_r.R.test_red)")), r.stdout)
        found = re.search(r" — AssertionError: 29 != 3 · (\S+):(\d+)$", line)
        self.assertIsNotNone(found, line)
        assert found
        text = Path(found.group(1)).read_text(encoding="utf-8").splitlines()
        n = int(found.group(2))
        self.assertEqual(text[n - 1], "FAIL: test_red (test_r.R.test_red)", "the line points at the test's full text")
        self.assertIn("Traceback", "\n".join(text[n:]), "the full text is there")

    def test_what_the_tests_print_stays_in_the_runs_folder(self):
        # A test's stray lines — a collab it starts saying it cannot read a room — were most of a green run's text.
        self.suite(
            "test_p.py",
            """\
            import sys, unittest
            class P(unittest.TestCase):
                def test_prints(self):
                    print("stray stderr of a test", file=sys.stderr)
                    print("stray stdout of a test")
            """,
        )
        green = self.run_script("--workers", "1")
        self.assertEqual(green.returncode, 0, green.stdout + green.stderr)
        self.assertNotIn("stray", green.stdout + green.stderr)
        # The positive pair: the lines were printed — into the run's folder, kept when the run is red.
        self.suite(
            "test_q.py", "import unittest\nclass Q(unittest.TestCase):\n    def test_red(self):\n        self.fail('red')\n"
        )
        red = self.run_script("--workers", "1")
        self.assertNotIn("stray", red.stdout + red.stderr)
        folder = re.search(r"worker logs: (\S+)", red.stdout)
        self.assertIsNotNone(folder, red.stdout)
        assert folder
        kept = "".join(f.read_text(encoding="utf-8") for f in Path(folder.group(1)).glob("w*/output.log"))
        self.assertIn("stray stderr of a test", kept)
        self.assertIn("stray stdout of a test", kept)

    def test_the_run_says_what_it_ran_on(self):
        # The summary is the run's own facts, not what its reader remembers: the commit, Python, tmux.
        self.recording_tests(2)
        r = self.run_script("--workers", "1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        git = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=SCRIPT.parent, capture_output=True, text=True)
        self.assertIn(f"suite: {git.stdout.strip() or 'no commit'}", r.stdout)
        self.assertIn(f"· Python {platform.python_version()} ·", r.stdout)

    def test_a_module_that_does_not_import_is_red(self):
        self.recording_tests(3)
        self.suite("test_broken.py", "import no_such_module_here\n")
        r = self.run_script("--workers", "2")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("No module named 'no_such_module_here'", r.stdout)
        self.assertIn("alone: unittest.loader._FailedTest.test_broken — FAILED", r.stdout)

    def test_a_module_that_does_not_import_in_one_worker_only_is_red(self):
        # Had each worker found its own tests, worker 1's list (with test_collab failing to import
        # there — its tmux server did not start) split differently, and a share ran nowhere: «OK».
        self.suite(
            "test_some.py",
            """\
            import os, unittest
            if os.path.basename(os.environ["TMPDIR"]) == "w1":
                raise ImportError("not in worker 1")
            class M(unittest.TestCase):
                def test_0(self): pass
                def test_1(self): pass
                def test_2(self): pass
                def test_3(self): pass
            """,
        )
        r = self.run_script("--workers", "2")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("not in worker 1", r.stdout)

    def test_a_listed_test_missing_in_its_worker_is_red(self):
        self.suite(
            "test_some.py",
            """\
            import os, unittest
            class M(unittest.TestCase):
                def test_0(self): pass
            if os.path.basename(os.environ["TMPDIR"]) != "w0":
                M.test_1 = M.test_2 = M.test_0
            """,
        )
        r = self.run_script("--workers", "1")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_a_red_test_run_again_alone_never_has_the_callers_tmux(self):
        # Launch dropped TMUX, run_alone did not — the same way to the room's server.
        self.suite(
            "test_env.py",
            """\
            import os, unittest
            class E(unittest.TestCase):
                def test_red_once(self):
                    with open(os.environ["CPT_SEEN"], "a") as f:
                        f.write(repr((os.environ.get("TMUX"), os.environ.get("TMUX_PANE"))) + "\\n")
                    mark = os.path.join(os.environ["CPT_DIR"], "ran-once")
                    first = not os.path.exists(mark)
                    open(mark, "w").close()
                    self.assertFalse(first)
            """,
        )
        env = {**self.env(self.short_tmpdir(), keep_tmux=True), "TMUX": "/nonexistent/sock,1,0", "TMUX_PANE": "%9"}
        r = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
        self.assertIn("alone: test_env.E.test_red_once — OK", r.stdout, r.stdout + r.stderr)
        self.assertEqual(self.seen.read_text().splitlines(), ["(None, None)", "(None, None)"], "in the run and alone")

    @unittest.skipUnless(shutil.which("tmux"), "needs tmux")
    def test_no_tmux_server_of_the_run_outlives_it(self):
        # The servers of the lister (test_collab starts one when imported) and of the tests run
        # again alone were not stopped — only the workers' were.
        self.suite(
            "test_server.py",
            """\
            import os, subprocess, tempfile, unittest
            def server():
                ct = tempfile.mkdtemp(prefix="ct-")
                env = {k: v for k, v in os.environ.items() if k != "TMUX"}
                env["TMUX_TMPDIR"] = ct
                subprocess.run(["tmux", "-f", "/dev/null", "new-session", "-d", "sleep 300"], env=env, check=True)
                sock = os.path.join(ct, f"tmux-{os.getuid()}", "default")
                assert os.path.exists(sock), "the server is this test's own"
                with open(os.environ["CPT_SEEN"], "a") as f:
                    f.write(sock + "\\n")
            server()  # on import, as test_collab does — in the lister too
            class S(unittest.TestCase):
                def test_red_once(self):
                    server()
                    mark = os.path.join(os.environ["CPT_DIR"], "ran-once")
                    first = not os.path.exists(mark)
                    open(mark, "w").close()
                    self.assertFalse(first)
            """,
        )
        r = self.run_script("--workers", "1")
        socks = self.seen.read_text().splitlines()
        for sock in socks:  # whatever the assertions say, no server of this test outlives it
            self.addCleanup(subprocess.run, ["tmux", "-S", sock, "kill-server"], capture_output=True)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertGreaterEqual(len(socks), 5, "lister, worker (import, test), alone (import, test)")
        alive = [s for s in socks if subprocess.run(["tmux", "-S", s, "has-session"], capture_output=True).returncode == 0]
        self.assertEqual(alive, [])

    def test_the_longest_go_first_to_the_least_loaded_and_each_keeps_discovery_order(self):
        ids = [f"t{i}" for i in range(6)]
        durations = {"t0": 1, "t1": 9, "t2": 1, "t3": 8, "t4": 1, "t5": 1}
        shares = ci_parallel.share(ids, 2, durations)
        self.assertEqual(shares, [["t1", "t2", "t5"], ["t0", "t3", "t4"]])
        self.assertEqual(sorted(sum(shares, [])), ids)
        self.assertEqual(ci_parallel.share(ids, 2, durations), shares, "the same split in every worker")
        # A test with no measure counts as the median of the measured ones, not as zero.
        self.assertEqual(ci_parallel.share(["new", "a", "b"], 2, {"a": 5, "b": 3}), [["a", "b"], ["new"]])


if __name__ == "__main__":
    unittest.main()
