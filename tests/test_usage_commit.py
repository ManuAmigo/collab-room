"""Usage journal retries use the real session reader and retain each counted portion."""

import json
import os
import sys
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ledger import LedgerRoom  # noqa: E402
from _package import Loader  # noqa: E402
from _seam import seam  # noqa: E402


class UsageCommitTest(LedgerRoom):
    def setUp(self):
        super().setUp()
        self.home = self.root / "claude"
        self.home.mkdir()
        self.enterContext(mock.patch.dict(os.environ, {"COLLAB_CLAUDE_HOME": str(self.home)}))
        (self.home / "projects").mkdir()
        self.path = self.home / "projects" / "session.jsonl"
        self.path.touch()
        self.agent = {"id": "agent", "kind": "claude"}
        self.bind_reader()
        self.mod.increment(self.agent)

    def bind_reader(self):
        self.enterContext(seam(self.mod, "usage.claude_files", lambda p: [self.path]))

    def restart(self):
        self.mod = Loader(
            "usage_restarted_" + uuid.uuid4().hex[:8], str(Path(__file__).resolve().parents[1] / "collab")
        ).load_module()
        self.mod.use_room(str(self.root))
        self.bind_reader()

    def spend(self, mid, model="model-one"):
        message = {"id": mid, "model": model, "usage": {"input_tokens": 10, "output_tokens": 3}}
        with self.path.open("a") as out:
            out.write(json.dumps({"type": "assistant", "timestamp": "2099-01-01T00:00:00Z", "message": message}) + "\n")

    def lines(self):
        return self.mod.read_jsonl(self.mod.usage_path())

    def test_failed_append_then_restart_loses_neither_spend_nor_wakeups(self):
        self.spend("answer-one")
        self.mod.woke("agent")
        self.mod.woke("agent")
        with (
            seam(self.mod, "ledger.append_usage", mock.Mock(side_effect=OSError("journal unavailable"))),
            self.assertRaises(OSError),
        ):
            self.mod.record(self.agent)
        self.restart()
        self.mod.record(self.agent)
        self.mod.record(self.agent)
        rows = self.lines()
        self.assertEqual([(r["input"], r["output"], r["wakeups"]) for r in rows], [(10, 3, 2)])

    def test_torn_multi_model_append_is_recovered_exactly_once(self):
        self.spend("answer-one")
        self.spend("answer-two", "model-two")
        self.mod.woke("agent")

        def torn(lines):
            text = json.dumps(lines[0]) + "\n" + json.dumps(lines[1])[:25]
            self.mod.write_state(self.mod.usage_path(), text, append=True)
            raise OSError("interrupted append")

        with seam(self.mod, "ledger.append_usage", torn), self.assertRaises(OSError):
            self.mod.record(self.agent)
        self.restart()
        self.mod.record(self.agent)
        self.mod.record(self.agent)
        rows = self.lines()
        self.assertEqual(sorted((r["model"], r["input"]) for r in rows), [("model-one", 10), ("model-two", 10)])
        self.assertEqual(sum(r["wakeups"] for r in rows), 1)

    def test_cursor_failure_after_append_keeps_new_spend_and_no_duplicate_portion(self):
        self.spend("answer-one")
        write = self.mod.write_json

        def failed_cursor(path, data):
            if path.name == "usage-cursor.json":
                raise OSError("cursor unavailable")
            return write(path, data)

        with seam(self.mod, "store.write_json", failed_cursor), self.assertRaises(OSError):
            self.mod.record(self.agent)
        self.assertEqual(len(self.lines()), 1, "the journal precedes cursor commit")
        self.spend("answer-two")
        self.restart()
        self.mod.record(self.agent)
        self.mod.record(self.agent)
        rows = self.lines()
        self.assertEqual([r["input"] for r in rows], [10, 10])
        self.assertEqual(len({r["id"] for r in rows}), 2)

    def test_retry_preserves_wakeups_that_arrived_after_the_failed_append(self):
        self.spend("answer-one")
        self.mod.woke("agent")
        with seam(self.mod, "ledger.append_usage", mock.Mock(side_effect=OSError("unavailable"))), self.assertRaises(OSError):
            self.mod.record(self.agent)
        self.mod.woke("agent")
        self.mod.record(self.agent)
        self.spend("answer-two")
        self.mod.record(self.agent)
        self.assertEqual([r["wakeups"] for r in self.lines()], [1, 1])

    def test_completed_journal_and_context_can_be_recovered_after_checkpoint_error(self):
        self.spend("answer-one")
        self.mod.woke("agent")
        write = self.mod.write_json

        def interrupted(path, data):
            write(path, data)
            if path == self.mod.context_path():
                raise OSError("interrupted after context write")

        with seam(self.mod, "store.write_json", interrupted), self.assertRaises(OSError):
            self.mod.record(self.agent)
        self.mod.woke("agent")
        self.mod.record(self.agent)
        self.spend("answer-two")
        self.mod.record(self.agent)
        self.assertEqual([(r["input"], r["wakeups"]) for r in self.lines()], [(10, 1), (10, 1)])

    def test_codex_checkpoint_is_not_consumed_by_a_failed_journal_append(self):
        self.agent["kind"] = "codex"
        self.enterContext(mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}))
        self.path = self.home / "sessions" / "session.jsonl"
        self.path.parent.mkdir()
        self.path.touch()
        files = lambda p, known, code: ([self.path], code)
        self.enterContext(seam(self.mod, "usage.codex_file", files))
        self.mod.increment(self.agent)
        payload = {"thread_id": "thread", "response_id": "answer", "usage": {"input_tokens": 10, "output_tokens": 3}}
        self.path.write_text(
            json.dumps({"type": "token_usage_record", "timestamp": "2099-01-01T00:00:00Z", "payload": payload}) + "\n"
        )
        self.mod.woke("agent")
        with seam(self.mod, "ledger.append_usage", mock.Mock(side_effect=OSError("unavailable"))), self.assertRaises(OSError):
            self.mod.record(self.agent)
        self.restart()
        self.enterContext(seam(self.mod, "usage.codex_file", files))
        self.mod.record(self.agent)
        self.mod.record(self.agent)
        self.assertEqual([(r["input"], r["output"], r["wakeups"]) for r in self.lines()], [(10, 3, 1)])
