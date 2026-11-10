"""Owned process groups stay anchored through failed inspection and final signals."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ledger import LedgerRoom  # noqa: E402
from _seam import seam  # noqa: E402


class MonitorCleanupTest(LedgerRoom):
    def test_inspection_errors_cannot_skip_kill_or_confirm_unobserved_stop(self):
        for failure in (OSError("ps unavailable"), ValueError("bad ps"), subprocess.TimeoutExpired("ps", 2)):
            for phase in ("before", "after"):
                with self.subTest(error=type(failure).__name__, phase=phase):
                    ready = self.root / "ready"
                    ready.unlink(missing_ok=True)
                    code = (
                        "import signal,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);Path(%r).touch();time.sleep(60)"
                        % str(ready)
                    )
                    child = self.mod.start_installer_process(
                        [sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                    group = self.mod.MacAgentGroup(child.pid, child.pid)
                    inspect = self.mod.mac_group_states
                    signalled = []
                    killpg = os.killpg

                    def signal_group(pid, sig, signalled=signalled, killpg=killpg):
                        signalled.append(sig)
                        killpg(pid, sig)

                    def broken_inspection(pid, phase=phase, group=group, failure=failure, inspect=inspect):
                        if (phase == "after") == group.killed:
                            raise failure
                        return inspect(pid)

                    try:
                        deadline = time.monotonic() + 4
                        while not ready.exists() and time.monotonic() < deadline:
                            time.sleep(0.01)
                        self.assertTrue(ready.exists())
                        with (
                            seam(self.mod, "vpn_macos_launch.mac_group_states", broken_inspection),
                            seam(self.mod, "vpn_macos_launch.os.killpg", signal_group),
                        ):
                            stopped = group.stop()
                        self.assertIn(signal.SIGKILL, signalled)
                        if phase == "after":
                            self.assertFalse(stopped, "failed inspection cannot acknowledge a stopped group")
                            self.assertFalse(group.reaped, "the anchor still reserves the group")
                            self.assertTrue(group.stop(), "observation can recover without releasing the anchor early")
                        else:
                            self.assertTrue(stopped)
                        child.returncode = -signal.SIGKILL
                    finally:
                        if not group.reaped:
                            self.mod.stop_installer(child)
                        else:
                            os.close(child._collab_status_fd)
