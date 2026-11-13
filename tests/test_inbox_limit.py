"""A limited inbox explains the older mail it leaves out, including with --all."""

import argparse
import contextlib
import io
import sys
import subprocess
import shlex
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ledger import LedgerRoom  # noqa: E402


class InboxLimitTest(LedgerRoom):
    def setUp(self):
        super().setUp()
        self.mod.write_config({"participants": [{"id": "owner"}, {"id": "probe", "reply_hint": False}]})
        self.older = self.mod.append_message("owner", ["probe"], "older-request")
        self.mod.append_message("owner", ["probe"], "latest-request")
        self.mod.append_message("room", ["probe"], "quiet-notice", silent=True)

    def inbox(self, all_, limit):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.mod.cmd_inbox(argparse.Namespace(participant="probe", all=all_, limit=limit))
        return out.getvalue()

    def test_all_still_limits_mail_but_reports_older_messages_and_how_to_read_them(self):
        out = self.inbox(True, 2)
        self.assertIn("latest 2 of 3", out)
        self.assertIn("1 older", out)
        self.assertIn("--limit 3", out)
        self.assertNotIn("older-request", out)
        self.assertIn("latest-request", out)
        self.assertIn("quiet-notice", out)
        command = next(line for line in out.splitlines() if line.startswith("python3 "))
        self.assertIn("--all", shlex.split(command))
        repeated = subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=20)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertIn("older-request", repeated.stdout)

    def test_unread_count_excludes_acknowledged_mail_and_full_inbox_needs_no_warning(self):
        self.mod.write_set(self.mod.room().paths.receipts_dir, "probe", {self.older["id"]})
        out = self.inbox(False, 1)
        self.assertIn("latest 1 of 2", out)
        self.assertIn("--limit 2", out)
        self.assertNotIn("older-request", out)
        out = self.inbox(False, 2)
        self.assertNotIn("older messages are not shown", out)
        self.assertIn("latest-request", out)
        self.assertNotIn("older-request", out)
