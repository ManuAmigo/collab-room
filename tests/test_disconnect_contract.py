"""Room disconnection checks in isolated directories; no real agent processes."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import isolated  # noqa: E402

CLI = Path(__file__).resolve().parents[1] / "collab"


class DisconnectContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.room = Path(self.temp.name)
        self.config = self.room / ".collab.json"
        self.config.write_text(
            json.dumps(
                {
                    "version": 2,
                    "participants": [
                        {"id": "owner", "label": "Owner", "delivery": "pull"},
                        {"id": "agent", "label": "Reviewer", "delivery": "pull"},
                    ],
                }
            )
        )
        self.env = {**os.environ, **isolated(self.room), "COLLAB_ROOT": str(self.room)}
        for name in ("TMUX", "TMUX_PANE"):
            self.env.pop(name, None)

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, str(CLI), *args], cwd=self.room, env=self.env, capture_output=True, text=True)

    def test_disconnect_preserves_history_and_other_people(self):
        self.assertEqual(self.cli("say", "--from", "agent", "--to", "owner", "RETAINED-HISTORY").returncode, 0)
        for name in ("status.json", "awaiting.json"):
            (self.room / ".collab" / name).write_text(
                json.dumps({"agent": {"marker": "remove"}, "owner": {"marker": "preserve"}})
            )
        before = json.loads(self.config.read_text())["participants"][0]
        result = self.cli("remove", "agent")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.config.read_text())["participants"], [before])
        self.assertIn("RETAINED-HISTORY", (self.room / ".collab/messages.jsonl").read_text())
        for name in ("status.json", "awaiting.json"):
            self.assertEqual(json.loads((self.room / ".collab" / name).read_text()), {"owner": {"marker": "preserve"}})
        self.assertNotEqual(self.cli("say", "--from", "owner", "--to", "agent", "not delivered").returncode, 0)

    def test_owner_cannot_be_disconnected(self):
        before = self.config.read_bytes()
        result = self.cli("remove", "owner")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
