"""Room commands and bound Claude hooks inside Seatbelt, using only fixture files."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _package import Loader


@unittest.skipUnless(sys.platform == "darwin", "Seatbelt requires macOS")
class MacRoomIpcTests(unittest.TestCase):
    def test_room_queue_locks_and_bound_hooks_need_no_socket_exception(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            project = home / "project"
            project.mkdir()
            environment = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "SHELL": "/bin/sh", "LANG": "C.UTF-8"}
            with mock.patch.dict(os.environ, environment, clear=True):
                cli = Path(__file__).resolve().parents[1] / "collab"
                mod = Loader("ipc_" + uuid.uuid4().hex[:8], str(cli)).load_module()
                mod.use_room(str(project))
                sid = str(uuid.uuid4())
                mod.write_config(
                    {
                        "session": "ipc-test",
                        "participants": [
                            {"id": "driver", "command": ["/usr/bin/true"], "delivery": "pull"},
                            {"id": "codex", "kind": "codex", "command": ["/usr/bin/true"], "delivery": "pull"},
                            {
                                "id": "claude",
                                "kind": "claude",
                                "claude_session": sid,
                                "command": ["/usr/bin/true"],
                                "delivery": "pull",
                                "role": "worker",
                            },
                        ],
                    }
                )
                message = mod.append_message("driver", ["codex"], "ipc-message")
                profile = mod.mac_vpn_profile(mod.MacVpnEndpoint(54321))
                prefix = ["/usr/bin/sandbox-exec", "-p", profile, sys.executable, "-I", "-S", str(cli)]
                cases = [
                    (["inbox", "--for", "codex"], None),
                    (["say", "--from", "codex", "--to", "driver", "ipc-reply"], None),
                    (["ack", "--for", "codex", "--no-reply", "--id", message["id"]], None),
                    (["take", "--from", "codex", "IPC probe"], None),
                    (["status", "--as", "claude", "idle", "--hook", "--if-room"], json.dumps({"session_id": sid})),
                    (["hook-model", "--as", "claude"], json.dumps({"session_id": sid, "model": "claude-sonnet-4-6"})),
                    (["brief", "--hook"], json.dumps({"session_id": sid, "source": "compact"})),
                ]
                for arguments, body in cases:
                    with self.subTest(command=arguments[0]):
                        result = subprocess.run(
                            [*prefix, *arguments, "--room", str(project)],
                            input=body or "",
                            env=environment,
                            cwd=project,
                            capture_output=True,
                            text=True,
                            close_fds=True,
                            timeout=10,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        if arguments[0] == "inbox":
                            self.assertIn("ipc-message", result.stdout)
                        if arguments[0] == "brief":
                            self.assertIn("worker", result.stdout)
                self.assertEqual(mod.read_status()["claude"]["state"], "idle")
                self.assertTrue(mod.read_status()["claude"].get("model"))
                self.assertTrue(any(row["text"] == "ipc-reply" for row in mod.read_messages()))
