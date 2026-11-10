"""A new Claude SID in a known pane must bind without granting tmux socket access."""

import ctypes
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _package import Loader


def current_birth_record():
    # Independent fixture from the Darwin proc_bsdinfo ABI, not a production helper.
    library = ctypes.CDLL("/usr/lib/libproc.dylib")
    buffer = ctypes.create_string_buffer(136)
    if library.proc_pidinfo(os.getpid(), 3, 0, buffer, 136) != 136:
        raise RuntimeError("fixture process identity unavailable")
    return {
        "pid": os.getpid(),
        "uid": os.getuid(),
        "seconds": struct.unpack_from("=Q", buffer.raw, 120)[0],
        "microseconds": struct.unpack_from("=Q", buffer.raw, 128)[0],
    }


def serve_lookup(server, stop, parent, project):
    server.settimeout(0.1)
    while not stop.is_set():
        try:
            connection, _ = server.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        with connection:
            arguments = json.loads(connection.recv(4096))
            answer = str(parent) if arguments[-1] == "#{pane_pid}" else f"{parent}\tclaude\t\t{project}"
            connection.sendall(answer.encode())


@unittest.skipUnless(sys.platform == "darwin", "Darwin Seatbelt and process identity")
class MacClaudeClearTests(unittest.TestCase):
    def test_new_sid_hook_works_with_closed_tmux_lookup_socket(self):
        with tempfile.TemporaryDirectory() as temporary, socket.socket(socket.AF_UNIX) as server:
            home = Path(temporary).resolve()
            project = home / "project"
            project.mkdir()
            address = home / "lookup.sock"
            server.bind(str(address))
            server.listen()
            stop = threading.Event()
            thread = threading.Thread(target=serve_lookup, args=(server, stop, os.getpid(), project), daemon=True)
            thread.start()
            try:
                fake = home / "tmux-lookup"
                fake.write_text(
                    f"#!{sys.executable}\nimport socket,json,sys\ns=socket.socket(socket.AF_UNIX);s.connect({str(address)!r});s.sendall(json.dumps(sys.argv[1:]).encode());print(s.recv(4096).decode())\n"
                )
                fake.chmod(0o700)
                environment = {
                    "HOME": str(home),
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                    "SHELL": "/bin/sh",
                    "LANG": "C.UTF-8",
                    "COLLAB_TMUX": str(fake),
                }
                cli = Path(__file__).resolve().parents[1] / "collab"
                with mock.patch.dict(os.environ, environment, clear=True):
                    mod = Loader("clear_" + uuid.uuid4().hex[:8], str(cli)).load_module()
                    mod.use_room(str(project))
                    sid = str(uuid.uuid4())
                    birth = current_birth_record()
                    participant = {
                        "id": "claude",
                        "kind": "claude",
                        "role": "worker",
                        "delivery": "tmux",
                        "idle": "^❯",
                        "target": "%5",
                        "pane_pid": str(os.getpid()),
                        "started_by_room": True,
                        "claude_session": str(uuid.uuid4()),
                        "vpn_process": birth,
                    }
                    mod.write_config({"session": "clear-test", "macos_vpn": True, "participants": [participant]})
                    sessions = home / ".claude/sessions"
                    sessions.mkdir(parents=True)
                    (sessions / f"{os.getpid()}.json").write_text(
                        json.dumps(
                            {
                                "pid": os.getpid(),
                                "sessionId": sid,
                                "procStart": time.strftime("%a %b %d %H:%M:%S %Y", time.gmtime(birth["seconds"])),
                                "tmux": "own.%5",
                            }
                        )
                    )
                    command = [sys.executable, "-I", "-S", str(cli), "brief", "--hook", "--room", str(project)]
                    body = json.dumps({"session_id": sid, "source": "clear"})
                    outside = subprocess.run(
                        command, input=body, env=environment, cwd=project, capture_output=True, text=True, timeout=10
                    )
                    self.assertEqual(outside.returncode, 0, outside.stderr)
                    self.assertIn("worker", outside.stdout)
                    reset = mod.read_config_raw()
                    reset["participants"][0]["claude_session"] = str(uuid.uuid4())
                    mod.write_config(reset)
                    profile = mod.mac_vpn_profile(mod.MacVpnEndpoint(54321))
                    inside = subprocess.run(
                        ["/usr/bin/sandbox-exec", "-p", profile, *command],
                        input=body,
                        env=environment,
                        cwd=project,
                        capture_output=True,
                        text=True,
                        close_fds=True,
                        timeout=10,
                    )
                    self.assertEqual(inside.returncode, 0, inside.stderr)
                    self.assertIn("worker", inside.stdout)
                    self.assertEqual(mod.read_config_raw()["participants"][0]["claude_session"], sid)
            finally:
                stop.set()
                server.close()
                thread.join(timeout=2)
