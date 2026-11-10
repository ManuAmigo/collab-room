"""Message text and untrusted file types in a private synthetic room."""

import argparse
import contextlib
import io
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ledger import LedgerRoom  # noqa: E402
from _seam import seam  # noqa: E402


class MessageLinesTest(LedgerRoom):
    def setUp(self):
        super().setUp()
        self.mod.write_config(
            {
                "version": 2,
                "session": "synthetic",
                "participants": [{"id": "owner"}, {"id": "agent", "kind": "claude", "pane": False}],
            }
        )
        self.enterContext(seam(self.mod, "tmuxio.tmux", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")))

    def say(self, text):
        args = argparse.Namespace(sender="agent", to=["owner"], re="", quiet=False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.mod.post_message(args, text)

    def test_unicode_separators_survive_full_and_incremental_message_reads(self):
        texts = ["before", "a\u2028b\u2029c", "after"]
        for text in texts:
            self.say(text)
            self.assertEqual([m["text"] for m in self.mod.read_log_tick()], texts[: texts.index(text) + 1])
        self.assertEqual([m["text"] for m in self.mod.read_messages()], texts)

    def test_held_unicode_messages_survive_resume_and_count_as_one_record(self):
        self.mod.set_paused(True)
        texts = ["a\u2028b\u2029c", "after"]
        for text in texts:
            self.say(text)
        count = self.mod.held_count()
        self.mod.set_paused(False)
        self.assertEqual([m["text"] for m in self.mod.read_messages()], texts)
        self.assertEqual([m["text"] for m in self.mod.read_log_tick()], texts)
        self.assertFalse(self.mod.room().paths.held_path.exists())
        self.assertEqual(count, 2)


class NonRegularMessageFileTest(LedgerRoom):
    def setUp(self):
        super().setUp()
        self.mod.write_config(
            {
                "version": 2,
                "session": "synthetic",
                "participants": [{"id": "owner"}, {"id": "agent", "kind": "claude", "pane": False}],
            }
        )
        self.program = Path(__file__).resolve().parents[1] / "collab"
        self.env = {**os.environ, "COLLAB_ROOT": str(self.root), "COLLAB_NO_AUTODELIVER": "1", "COLLAB_NO_ATTACH": "1"}

    def command(self, *args):
        try:
            return subprocess.run(
                [sys.executable, str(self.program), *args], cwd=self.root, env=self.env, text=True, capture_output=True, timeout=5
            )
        except subprocess.TimeoutExpired:
            self.fail("a FIFO with no writer blocked the room command")

    def test_say_file_refuses_a_fifo_without_blocking_the_room_lock(self):
        fifo = self.root / "input.txt"
        os.mkfifo(fifo)
        result = self.command("say", "--from", "agent", "--to", "owner", "--file", str(fifo), "file")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("not a regular file", result.stderr)
        next_message = self.command("say", "--from", "agent", "--to", "owner", "after refusal")
        self.assertEqual(next_message.returncode, 0, next_message.stderr)
        self.assertEqual([m["text"] for m in self.mod.read_messages()], ["after refusal"])
        self.assertFalse((self.state / "outbox").exists())

    def check_state_fifo(self, reader):
        fifo = self.mod.room().paths.messages_path
        os.mkfifo(fifo)
        code = (
            "from importlib.machinery import SourceFileLoader; import sys; m=SourceFileLoader('fifo_reader',sys.argv[1]).load_module(); m.use_room(sys.argv[2]); "
            + reader
        )
        try:
            result = subprocess.run(
                [sys.executable, "-c", code, str(self.program), str(self.root)],
                cwd=self.root,
                env=self.env,
                text=True,
                capture_output=True,
                timeout=5,
            )
        except subprocess.TimeoutExpired:
            self.fail("a FIFO with no writer blocked the state reader")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("link", result.stderr)

    def test_full_message_reader_refuses_a_fifo_without_waiting_for_a_writer(self):
        self.check_state_fifo("m.read_state_text(m.room().paths.messages_path)")

    def test_incremental_message_reader_refuses_a_fifo_without_waiting_for_a_writer(self):
        self.check_state_fifo("m.read_log_tick()")
