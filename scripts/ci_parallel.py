#!/usr/bin/env python3
"""The full suite in up to four processes at once: most of its time is waiting on
tmux, not the processor. Each worker is its own unittest process with its own TMPDIR — so its own
tmux server and test folders, as a whole run has (tests/_isolation.py, tests/test_collab.py).

    python3 scripts/ci_parallel.py [--workers N] [--save-durations]

The tests are shared out by their measured durations (scripts/ci-durations.json; --save-durations
writes what this run measured). A test that fails runs once more alone and both results are printed;
red stays red — the exit code is the parallel run's.
"""

import argparse
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# Another folder only for this script's own tests (tests/test_ci_parallel.py); the workers inherit it.
TESTS = Path(os.environ.get("CI_PARALLEL_TESTS") or REPO / "tests").resolve()
DURATIONS = REPO / "scripts" / "ci-durations.json"
# More gains little and loads the machine: a flaky red under load costs a whole round of the merge
# queue (test_esc_goes_back_one_step_everywhere failed under load, passed alone).
MAX_WORKERS = 4
# Two by default: four, beside other work, can take the whole machine and leave it unresponsive.
# --workers 3 or 4 when the machine is free.
DEFAULT_WORKERS = 2
# A merge queue's lock while its checks run: outside the queue, one worker, niced — the
# queue's run is the one that lands. Its own checks carry COLLAB_QUEUE_RUN and are not slowed.
# In the person's own folder, not /tmp: a link planted there under this name would have been written through.
QUEUE_LOCK = Path(os.environ.get("COLLAB_QUEUE_LOCK") or Path.home() / ".collab-room" / "queue-checks.lock")
# How often a full suite outside the queue looks whether the queue's checks are over.
POLL = 5.0


def queue_checking() -> int:
    """The pid of the merge queue's checks running now, or 0: the lock's process, if it is alive."""
    if os.environ.get("COLLAB_QUEUE_RUN"):
        return 0
    try:
        lock = json.loads(QUEUE_LOCK.read_text(encoding="utf-8"))
        pid = int(lock["pid"])
        # By the pid and its start, as the queue's places: a pid is given out again.
        start = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return 0
    return pid if start and start == lock.get("start") else 0


def flat(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    out: list[unittest.TestCase] = []
    for t in suite:
        out.extend(flat(t) if isinstance(t, unittest.TestSuite) else [t])
    return out


def discover(pattern: str = "test*.py") -> list[unittest.TestCase]:
    """What `python3 -m unittest discover -s tests` runs, in its order."""
    sys.path.insert(0, str(TESTS))
    return flat(unittest.TestLoader().discover(str(TESTS), pattern=pattern, top_level_dir=str(TESTS)))


def known_durations() -> dict[str, float]:
    try:
        data = json.loads(DURATIONS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: float(v) for k, v in data.items()} if isinstance(data, dict) else {}


def share(ids: list[str], workers: int, durations: dict[str, float]) -> list[list[str]]:
    """The longest first, each to the least loaded worker; then each worker's in discovery order, so a
    class's tests stay together."""
    known = sorted(durations.values())
    default = known[len(known) // 2] if known else 1.0  # a new test: the median
    load = [0.0] * workers
    mine: list[list[str]] = [[] for _ in range(workers)]
    for tid in sorted(ids, key=lambda t: (-durations.get(t, default), t)):
        k = load.index(min(load))
        mine[k].append(tid)
        load[k] += durations.get(tid, default)
    order = {tid: i for i, tid in enumerate(ids)}
    return [sorted(m, key=order.__getitem__) for m in mine]


class TimedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.durations_s: dict[str, float] = {}
        self._started = 0.0

    def startTest(self, test: unittest.TestCase) -> None:
        self._started = time.monotonic()
        super().startTest(test)

    def stopTest(self, test: unittest.TestCase) -> None:
        super().stopTest(test)
        self.durations_s[test.id()] = round(time.monotonic() - self._started, 2)


def list_tests(out: Path, pattern: str) -> int:
    """The ids the run must run, found once, here: had each worker found its own, a module that did not
    import in one of them changed its list and its split, and a share of the tests ran nowhere."""
    # test_collab starts its tmux server when imported: kill_servers stops it with the rest, by socket.
    (out / "ids.json").write_text(json.dumps([t.id() for t in discover(pattern)]), encoding="utf-8")
    return 0


def load(ids: list[str]) -> list[unittest.TestCase]:
    """The tests by their ids, in that order. A module that did not import when the run listed it is
    loaded by its name; an id not found here is a failing test (unittest makes one), never a skip."""
    sys.path.insert(0, str(TESTS))
    loader = unittest.TestLoader()
    return flat(unittest.TestSuite(loader.loadTestsFromName(t.removeprefix("unittest.loader._FailedTest.")) for t in ids))


def run_worker(k: int, out: Path) -> int:
    suite = unittest.TestSuite(load(json.loads((out / f"ids-{k}.json").read_text(encoding="utf-8"))))
    with open(out / f"worker-{k}.log", "w", encoding="utf-8") as log:
        result = unittest.TextTestRunner(stream=log, verbosity=1, resultclass=TimedResult).run(suite)
    assert isinstance(result, TimedResult)
    report = {
        "ran": result.testsRun,
        "failed": [t.id() for t, _ in result.failures + result.errors] + [t.id() for t in result.unexpectedSuccesses],
        "skipped": len(result.skipped),
        "durations": result.durations_s,
    }
    (out / f"worker-{k}.json").write_text(json.dumps(report), encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


def failures_text(log: str) -> str:
    """The FAIL/ERROR sections unittest printed, without the dots before and the summary after."""
    start = log.find("=" * 70)
    if start < 0:
        return log
    end = log.rfind("-" * 70)
    return log[start:end].rstrip() + "\n"


def clean_env(tmpdir: Path) -> dict[str, str]:
    """A test process's environment: its own TMPDIR, and not the caller's tmux server — with TMUX set, a
    test's bare `tmux` reaches it, the person's or the room's (a test of this script did)."""
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    return {**env, "TMPDIR": str(tmpdir)}


def run_alone(tid: str, base: Path) -> bool:
    folder = Path(tempfile.mkdtemp(prefix="alone-", dir=base))
    # A module that did not import is run by its name: unittest.loader._FailedTest.<module>.
    target = tid.removeprefix("unittest.loader._FailedTest.")
    r = subprocess.run([sys.executable, "-m", "unittest", target], cwd=TESTS, env=clean_env(folder), capture_output=True)
    return r.returncode == 0


def scratch() -> Path:
    """The workers' folder: under the caller's TMPDIR when that is short — a merge queue's /tmp/cm-…,
    which keeps a red run's logs — else under /tmp. Short either way: a worker's tmux socket under it must fit
    in 103 bytes, and macOS's own TMPDIR (/var/folders/…) leaves no room for more levels."""
    here = os.environ.get("TMPDIR", "")
    parent = here if here and len(os.path.realpath(here).encode()) <= 40 else "/tmp"
    return Path(tempfile.mkdtemp(prefix="cp-", dir=parent))


def helper(base: Path, name: str, *args: str) -> subprocess.Popen:
    """This script again, as a worker or the lister, in a folder of its own under base."""
    tmp = base / name
    tmp.mkdir()
    command = [sys.executable, __file__, *args, "--out", str(base)]
    return subprocess.Popen(command, cwd=REPO, env=clean_env(tmp))


def kill_servers(base: Path) -> None:
    """The tmux servers this run's tests left — only those, by their sockets under base (never
    anyone else's); the merge queue looks one level under its folder only, and these are deeper."""
    tmux = shutil.which("tmux")
    if tmux:
        for sock in base.glob("*/ct-*/tmux-*/*"):
            subprocess.run([tmux, "-S", str(sock), "kill-server"], capture_output=True, timeout=10)


def stop(procs: list[subprocess.Popen]) -> None:
    """Stopped before their end (SIGTERM from the merge queue, Ctrl-C), the helpers go too."""
    for p in procs:
        if p.poll() is None:
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()


def run_all(base: Path, workers: int, pattern: str) -> tuple[list[str], list[int]]:
    """The ids, found once, shared out to the workers; the ids and the workers' exit codes."""
    procs = [helper(base, "list", "--list", "--pattern", pattern)]
    if procs[0].wait() != 0 or not (base / "ids.json").exists():
        return [], []
    ids = json.loads((base / "ids.json").read_text(encoding="utf-8"))
    # A module that did not import is one failing test of its own: worker 0 runs it.
    broken = [i for i in ids if i.startswith("unittest.loader.")]
    shares = share([i for i in ids if i not in broken], workers, known_durations())
    shares[0] = broken + shares[0]
    for k, mine in enumerate(shares):
        (base / f"ids-{k}.json").write_text(json.dumps(mine), encoding="utf-8")
    procs = [helper(base, f"w{k}", "--worker", str(k)) for k in range(workers)]
    try:
        return ids, [p.wait() for p in procs]
    finally:
        stop(procs)


def collect(base: Path, ids: list[str], codes: list[int]) -> tuple[int, int, list[str], dict[str, float]]:
    """The workers' reports summed — tests run, skipped, red ids, durations — and their red printed.
    Every listed id ran once, in one worker: a test lost or run twice is red, not a smaller «Ran»."""
    ran = skipped = 0
    failed: list[str] = []
    durations: dict[str, float] = {}
    ran_ids: list[str] = []
    for k, code in enumerate(codes):
        log = base / f"worker-{k}.log"
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        report_file = base / f"worker-{k}.json"
        if not report_file.exists():  # the worker died before its report: its log says why
            print(f"worker {k}: no report (exit {code})\n{text}")
            failed.append(f"worker {k}")
            continue
        report = json.loads(report_file.read_text(encoding="utf-8"))
        ran += report["ran"]
        skipped += report["skipped"]
        failed += report["failed"]
        durations.update(report["durations"])
        if report["failed"]:
            print(failures_text(text))
        print(f"worker {k}: {report['ran']} tests, {sum(report['durations'].values()):.0f}s, {len(report['failed'])} red")
        ran_ids += list(report["durations"])
    if not ids:
        print("the tests could not be listed: see the lister's output above")
        failed.append("listing")
    lost = sorted(set(ids) - set(ran_ids))
    twice = sorted({t for t in ran_ids if ran_ids.count(t) > 1})
    if codes and (lost or twice or len(ran_ids) != len(ids)):
        print(f"not run: {lost[:20]}; run twice: {twice[:20]}; listed {len(ids)}, ran {len(ran_ids)}")
        failed.append("accounting")
    return ran, skipped, failed, durations


def main() -> int:
    ap = argparse.ArgumentParser(description="The full suite in up to four processes at once.")
    ap.add_argument("--workers", type=int, help=f"default {DEFAULT_WORKERS}; 1, niced, while the merge queue checks")
    ap.add_argument("--save-durations", action="store_true", help=f"write what this run measured to {DURATIONS.name}")
    ap.add_argument(
        "--pattern", help="test files to run (default: all, as unittest discover — and then not beside the merge queue's checks)"
    )
    ap.add_argument("--worker", type=int, help=argparse.SUPPRESS)
    ap.add_argument("--list", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--out", help=argparse.SUPPRESS)
    ap.add_argument("--poll", type=float, default=POLL, help=argparse.SUPPRESS)
    args = ap.parse_args()
    pattern = args.pattern or "test*.py"
    if args.list:
        return list_tests(Path(args.out), pattern)
    if args.worker is not None:
        return run_worker(args.worker, Path(args.out))

    workers = max(1, min(args.workers, MAX_WORKERS)) if args.workers is not None else min(DEFAULT_WORKERS, os.cpu_count() or 1)
    if args.pattern is None:
        # One full suite on the machine: the whole suite waits for the queue's checks to end, not runs
        # beside them — two at once took the whole machine.
        told = 0
        while waiting := queue_checking():
            if waiting != told:
                print(f"the merge queue is checking (pid {waiting}): the full suite waits for it to end", flush=True)
                told = waiting
            time.sleep(args.poll)
    queue = queue_checking() if args.workers is None else 0
    if queue:
        workers = 1
        os.nice(10)
        print(f"the merge queue is checking (pid {queue}): 1 worker, niced; --workers N to choose")
    base = scratch()
    started = time.monotonic()
    # SIGTERM (the merge queue stopping its checks) ends the run through the finally below, as Ctrl-C does.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        ids, codes = run_all(base, workers, pattern)
        ran, skipped, failed, durations = collect(base, ids, codes)
        took = time.monotonic() - started
        if os.environ.get("CI_PARALLEL_REPORT"):
            # The merge queue's journal reads the run's times here: this run's folder is gone before it looks.
            # Only for the journal: a report that cannot be written never turns the run red.
            with contextlib.suppress(OSError):
                Path(os.environ["CI_PARALLEL_REPORT"]).write_text(json.dumps({"durations": durations}), encoding="utf-8")
        if args.save_durations and not failed:
            DURATIONS.write_text(json.dumps(dict(sorted(durations.items())), indent=0) + "\n", encoding="utf-8")
            print(f"durations saved: {DURATIONS.relative_to(REPO)}")
        # Each red test once more, alone: printed beside the first result, never instead of it.
        for tid in failed:
            if tid not in ("listing", "accounting") and not tid.startswith("worker "):
                print(f"alone: {tid} — {'OK' if run_alone(tid, base) else 'FAILED'} (the parallel run's red stands)")
    finally:
        kill_servers(base)
    if TESTS != REPO / "tests":  # set by CI_PARALLEL_TESTS: never a full suite of another folder unsaid
        print(f"tests from {TESTS}, not tests/")
    print("-" * 70)
    print(f"Ran {ran} tests in {took:.3f}s in {workers} workers")
    if failed:
        print(f"FAILED (red={len(failed)}, skipped={skipped}); worker logs: {base}")
        return 1
    print(f"OK (skipped={skipped})" if skipped else "OK")
    shutil.rmtree(base, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
