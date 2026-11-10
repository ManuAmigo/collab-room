"""The complete delivery line shares the agent's byte budget."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ledger import LedgerRoom  # noqa: E402
from _seam import seam  # noqa: E402


class PointerBudgetTest(LedgerRoom):
    def test_long_batch_pointer_and_old_open_ids_share_one_budget(self):
        mod = self.mod
        # The exact command is long but fits; previews, hints and open IDs need not.
        cli = "/" + "long-CLI-path/" * 41 + "collab"
        p = {"id": "agent", "kind": "kimi", "cli": cli, "paste_limit": 1000}
        config = {"participants": [{"id": "owner"}, p]}
        pending = [
            {"id": f"new{i}", "from": "room", "to": ["agent"], "at": mod.now(), "text": "длинная сводка " * 80} for i in range(3)
        ]
        old = [{"id": f"old{i}", "from": "room", "to": ["agent"], "at": mod.now(), "text": "old"} for i in range(8)]
        captured = []
        for name, value in (
            ("log.read_log_tick", lambda: pending + old),
            ("deliver.undelivered", lambda *a: (pending, set())),
            ("deliver.long_open", lambda *a: old),
            ("deliver.gathering", lambda *a: False),
            ("deliver.keep_left", lambda: None),
            ("installer.check_codex_rule_python", lambda *a: None),
            ("installer.rule_python", lambda: ["/usr/local/bin/python3.13", "-I", "-S"]),
            ("tmuxio.tmux", lambda *a, **k: SimpleNamespace(returncode=0, stdout="0", stderr="")),
            # Stop after the real common assembly, before any native input or Enter.
            ("deliver.too_long", lambda p, batch, text, rules, **k: captured.append(text) or True),
        ):
            self.enterContext(seam(mod, name, value))
        for hint in (True, False):
            with self.subTest(reply_hint=hint):
                p["reply_hint"] = hint
                mod.deliver_to(p, "%1", "idle", config)
                text = captured[-1]
                pointer = mod.room_cmd("inbox", "--for agent", cli, "agent")
                self.assertIn(pointer, text)
                self.assertLessEqual(len(text.encode()), 1000)
                self.assertNotIn("old0", text)

    def test_compact_digest_keeps_provenance_urgency_and_whole_pointer_or_refuses(self):
        mod = self.mod
        p = {"id": "agent", "cli": "/" + "program/" * 60 + "collab"}
        config = {"participants": [{"id": "owner"}, {"id": "lead"}, p], "team": {"lead": "lead"}}
        pending = [
            {"id": "m1", "from": "owner", "to": ["agent"], "at": mod.now(), "text": "unsigned " * 50},
            {"id": "m2", "from": "lead", "to": ["agent"], "at": mod.now(), "text": "urgent " * 50, "urgent": True},
        ]
        self.enterContext(seam(mod, "installer.check_codex_rule_python", lambda *a: None))
        self.enterContext(seam(mod, "installer.rule_python", lambda: ["/usr/local/bin/python3.13", "-I", "-S"]))
        self.enterContext(seam(mod, "log.signing_from", lambda: (0, {})))
        rules = {"paste_limit": 1000}
        line = mod.digest_text(p, pending, config, rules)
        self.assertIn(mod.room_cmd("inbox", "--for agent", p["cli"], "agent"), line)
        self.assertIn(mod.UNSIGNED, line)
        self.assertIn("URGENT", line)
        self.assertFalse(mod.too_long(p, pending, line, rules, tell=False))
        p["cli"] += "x" * 1000
        oversized = mod.digest_text(p, pending, config, rules)
        self.assertIn(p["cli"], oversized)
        self.assertIn(mod.UNSIGNED, oversized)
        self.assertTrue(mod.too_long(p, pending, oversized, rules, tell=False))
