"""Own local lease server and dummy agent: no model requests or host configuration."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest


@unittest.skipUnless(sys.platform == "darwin", "Seatbelt requires macOS")
class MacAgentLeaseTests(unittest.TestCase):
    def test_exec_preserves_pid_closes_fds_and_lease_stop_terminates_agent(self):
        self.run_agent(False)

    def test_supervisor_eof_terminates_agent(self):
        self.run_agent(True)

    def test_term_resistant_descendant_is_dead_before_ack(self):
        self.run_agent(False, resistant=True)

    def test_four_parallel_queued_lease_responses_can_arrive_after_request_frame_budget(self):
        with ThreadPoolExecutor(max_workers=4) as workers:
            futures = [workers.submit(self.run_agent, False, admission_delay=3.2) for _ in range(4)]
            for future in futures:
                future.result()

    def run_agent(self, disconnect, resistant=False, admission_delay=0):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary, socket.socket() as proxy, socket.socket() as forbidden:
            folder = Path(temporary).resolve()
            for listener in (proxy, forbidden):
                listener.bind(("127.0.0.1", 0))
                listener.listen()
            with socket.socket(socket.AF_UNIX) as server:
                address = folder / "control.sock"
                server.bind(str(address))
                os.chmod(address, 0o600)
                server.listen()
                evidence = folder / "agent.json"
                read_fd, write_fd = os.pipe()
                pipe_info = os.fstat(read_fd)
                child_file = folder / "descendant.pid"
                child_code = (
                    "import signal,os,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                    f'open({str(child_file)!r},"w").write(str(os.getpid()));time.sleep(60)'
                )
                prefix = ""
                if resistant:
                    prefix = (
                        "import subprocess,sys,os,time;"
                        f'subprocess.Popen([sys.executable,"-c",{child_code!r}]);'
                        "deadline=time.monotonic()+3;"
                        f"\nwhile not os.path.exists({str(child_file)!r}) and time.monotonic()<deadline:time.sleep(.01)\n"
                    )
                probe = prefix + (
                    "import os,socket,json,time;"
                    "s=socket.socket();s.settimeout(2);"
                    f'direct=s.connect_ex(("127.0.0.1",{forbidden.getsockname()[1]}));s.close();'
                    "s=socket.socket();s.settimeout(2);"
                    f'proxied=s.connect_ex(("127.0.0.1",{proxy.getsockname()[1]}));s.close();'
                    "inherited=False;"
                    "\ntry:\n info=os.fstat("
                    + str(read_fd)
                    + ");inherited=(info.st_ino,info.st_dev)=="
                    + repr((pipe_info.st_ino, pipe_info.st_dev))
                    + "\nexcept OSError:pass\n"
                    f'open({str(evidence)!r},"w").write(json.dumps(dict(pid=os.getpid(),direct=direct,proxy=proxied,inherited=inherited)));'
                    "time.sleep(60)"
                )
                script = (
                    "import importlib.machinery,sys;from pathlib import Path;"
                    f'm=importlib.machinery.SourceFileLoader("lease_test",{str(root / "collab")!r}).load_module();'
                    f'm.mac_vpn_agent_run(Path({str(folder)!r}),"claude",(sys.executable,"-c",{probe!r}))'
                )
                parent = subprocess.Popen(
                    [sys.executable, "-c", script],
                    start_new_session=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    pass_fds=(read_fd,),
                )
                os.close(read_fd)
                os.close(write_fd)
                connection = None
                try:
                    server.settimeout(5)
                    connection, _ = server.accept()
                    connection.settimeout(5)
                    self.assertIn(b"lease", connection.recv(4096))
                    if admission_delay:
                        time.sleep(admission_delay)
                    try:
                        connection.sendall(
                            json.dumps(
                                {
                                    "actual": "ready",
                                    "port": proxy.getsockname()[1],
                                    "pid": os.getpid(),
                                    "username": "a" * 32,
                                    "password": "b" * 64,
                                }
                            ).encode()
                            + b"\n"
                        )
                    except BrokenPipeError:
                        pass  # The assertion below reports premature agent exit, not a server write failure.
                    deadline = time.monotonic() + 5
                    while not evidence.exists() and parent.poll() is None and time.monotonic() < deadline:
                        threading.Event().wait(0.02)
                    if parent.poll() is not None:
                        output, error = parent.communicate(timeout=2)
                        self.fail("dummy agent exited: " + error.decode(errors="replace"))
                    self.assertTrue(evidence.exists(), "dummy agent failed to start")
                    row = json.loads(evidence.read_text())
                    self.assertEqual(row["pid"], parent.pid)
                    self.assertEqual(row["direct"], 1)
                    self.assertEqual(row["proxy"], 0)
                    self.assertFalse(row["inherited"])
                    if disconnect:
                        connection.close()
                        connection = None
                    else:
                        connection.sendall(b'{"stop":true}\n')
                    self.assertEqual(parent.wait(timeout=5), -signal.SIGTERM)
                    if connection is not None:
                        received = b""
                        while not received.endswith(b"\n"):
                            part = connection.recv(4096)
                            if not part:
                                break
                            received += part
                        self.assertEqual(json.loads(received), {"stopped": True})
                    if resistant:
                        descendant = int(child_file.read_text())
                        state = subprocess.run(
                            ["/bin/ps", "-o", "stat=", "-p", str(descendant)], capture_output=True, text=True
                        ).stdout.strip()
                        self.assertTrue(not state or state.startswith("Z"), "TERM-resistant descendant is still alive after ACK")
                finally:
                    if connection is not None:
                        connection.close()
                    if resistant and child_file.exists():
                        descendant = int(child_file.read_text())
                        try:
                            if os.getpgid(descendant) == parent.pid:
                                os.killpg(parent.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    if parent.poll() is None:
                        os.killpg(parent.pid, signal.SIGKILL)
                        parent.wait(timeout=5)
                    parent.communicate(timeout=5)
