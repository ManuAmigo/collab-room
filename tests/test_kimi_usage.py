"""Synthetic bound Kimi usage: no CLI, hooks or API calls."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _seam import seam  # noqa: E402
from test_ledger import LedgerRoom, entry  # noqa: E402


class KimiUsageTest(LedgerRoom):
    def setUp(self):
        super().setUp()
        self.home = self.root / "kimi"
        self.enterContext(mock.patch.dict(os.environ, {"KIMI_CODE_HOME": str(self.home)}))
        self.agent = {"id": "auto", "kind": "kimi", "pane_pid": "123", "target": "%1", "join_code": "generation-1"}
        self.binding = {
            "generation": "generation-1",
            "pane_pid": "123",
            "target": "%1",
            "sessions_root": str(self.home / "sessions"),
            "workdir_key": "project",
            "session_id": "session_one",
            "agent_id": "main",
            "adapter": "openai-compatible-2.4.1",
        }
        self.path = self.home / "sessions/project/session_one/agents/main/wire.jsonl"
        self.path.parent.mkdir(parents=True)
        self.path.touch()
        self.mod.write_json(self.state / "kimi-usage-bindings.json", {"auto": self.binding})

    def append(self, at=4102444800000, **usage):
        u = {"inputOther": 100, "output": 30, "inputCacheRead": 20, "inputCacheCreation": 0}
        u.update(usage)
        r = {"type": "usage.record", "agentId": "main", "model": "model-b", "time": at, "usageScope": "turn", "usage": u}
        with self.path.open("a") as f:
            f.write(json.dumps(r) + "\n")

    def test_failed_usage_append_then_restart_keeps_kimi_spend_and_wakeups(self):
        self.mod.increment(self.agent)
        self.append()
        self.mod.woke(self.agent["id"])
        with seam(self.mod, "ledger.append_usage", mock.Mock(side_effect=OSError("unavailable"))), self.assertRaises(OSError):
            self.mod.record(self.agent)
        from _package import Loader

        loaded = Loader("kimi_pending_reload", str(Path(__file__).resolve().parents[1] / "collab")).load_module()
        loaded.use_room(str(self.root))
        loaded.record(self.agent)
        loaded.record(self.agent)
        rows = loaded.read_jsonl(loaded.usage_path())
        self.assertEqual([(r["input"], r["output"], r["wakeups"], r["coverage"]) for r in rows], [(100, 30, 1, "partial")])
        self.assertIsNone(rows[0]["cache_write"])

    def test_bound_spend_is_counted_as_partial_with_unknown_cache_write(self):
        self.assertEqual(self.mod.increment(self.agent), [])
        self.append()
        got = self.mod.increment(self.agent)
        self.assertEqual(len(got), 1, "bound Kimi usage must produce spend, not silently disappear")
        self.assertEqual((got[0]["input"], got[0]["output"], got[0]["cache_read"]), (100, 30, 20))
        self.assertIsNone(got[0]["cache_write"])
        self.assertEqual(got[0]["coverage"], "partial")

    def test_unknown_metrics_survive_usage_line(self):
        e = entry(input=None, output=None, cache_read=None, cache_write=None, coverage="unknown", reason="zero-or-missing")
        line = self.mod.usage_line("auto", "Auto", "kimi", e, None, 0, False)
        self.assertIsNone(line["input"], "missing usage must not become a measured zero")
        self.assertEqual(line["coverage"], "unknown")

    def test_replay_rotation_truncation_and_incomplete_line_do_not_double_count(self):
        self.mod.increment(self.agent)
        self.append()
        text = self.path.read_text()
        self.assertEqual(len(self.mod.increment(self.agent)), 1)
        self.assertEqual(self.mod.increment(self.agent), [])
        self.path.unlink()
        self.path.write_text(text)
        self.assertEqual(self.mod.increment(self.agent), [], "rotation replay")
        self.path.write_text("")
        self.mod.increment(self.agent)
        self.path.write_text(text)
        self.assertEqual(self.mod.increment(self.agent), [], "truncate replay")
        self.path.write_text(text + text.replace("4102444800000", "4102444800001").rstrip("\n"))
        self.assertEqual(self.mod.increment(self.agent), [], "unfinished record waits")
        with self.path.open("a") as f:
            f.write("\n")
        self.assertEqual(len(self.mod.increment(self.agent)), 1)

    def test_missing_and_zero_usage_remain_unknown_in_rebuilt_reports(self):
        self.mod.increment(self.agent)
        self.append(inputOther=0, output=0, inputCacheRead=0)
        self.assertEqual(self.mod.record(self.agent), 1)
        rows = self.mod.read_jsonl(self.mod.usage_path())
        self.assertIsNone(rows[0]["input"])
        self.assertEqual(rows[0]["coverage"], "unknown")
        self.assertIn("reported-cli 2.4.1", rows[0]["reason"])
        for fmt in ("md", "csv"):
            report = self.mod.report("agent", fmt=fmt)
            self.assertIn("unknown", report)
            self.assertIn("zero-or-missing", report)
            self.assertNotIn("SECRET", report)
            self.assertEqual(report, self.mod.report("agent", fmt=fmt), "SQLite rebuild preserves unknown")

    def test_partial_and_complete_spend_in_same_group_remains_partial(self):
        self.mod.increment(self.agent)
        self.append()
        self.mod.record(self.agent)
        self.use("auto", entry(input=7, cache_write=50))
        row = self.table("agent")["auto"]
        self.assertEqual(row[2], "107")
        self.assertEqual(row[5], "unknown", "known cache writes must not hide unknown ones")
        self.assertEqual(row[8], "unknown", "derived totals must preserve unknown")
        self.assertEqual(row[12], "partial")

    def test_other_session_and_generation_are_not_read(self):
        self.mod.increment(self.agent)
        other = self.path.parent.parent.parent.parent / "session_two/agents/main/wire.jsonl"
        other.parent.mkdir(parents=True)
        self.append()
        other.write_text(self.path.read_text())
        self.path.write_text("")
        self.assertEqual(self.mod.increment(self.agent), [])
        self.agent["join_code"] = "generation-2"
        self.append()
        self.assertEqual(self.mod.increment(self.agent), [])
        self.assertIn("no confirmed", self.mod.unread(self.agent))

    def test_cursor_cannot_redirect_source_and_symlinks_are_refused(self):
        self.mod.increment(self.agent)
        self.append()
        outside = self.root / "outside.jsonl"
        outside.write_text(self.path.read_text())
        self.path.unlink()
        self.path.symlink_to(outside)
        self.assertEqual(self.mod.increment(self.agent), [])
        self.assertIn("unsafe", self.mod.unread(self.agent))
        self.path.unlink()
        self.path.write_text("")
        root = self.home / "sessions"
        root.rename(self.home / "moved")
        root.symlink_to(self.home / "moved", target_is_directory=True)
        self.assertEqual(self.mod.increment(self.agent), [])
        self.assertIn("unsafe", self.mod.unread(self.agent))

    def test_register_uses_launcher_identity_and_refuses_hook_generation_or_other_adapter(self):
        launch = {k: self.binding[k] for k in ("generation", "pane_pid", "target", "sessions_root", "workdir_key", "adapter")}
        event = {
            "hook_event_name": "SessionStart",
            "source": "startup",
            "client_type": "kimi_code_cli",
            "session_id": "session_one",
            "parent_pid": 999,
        }
        self.assertFalse(self.mod.register_kimi_binding(self.agent, launch, event))
        event["parent_pid"] = 123
        self.assertTrue(self.mod.register_kimi_binding(self.agent, launch, event))
        self.assertFalse(self.mod.register_kimi_binding(self.agent, {**launch, "generation": "other"}, event))
        self.assertFalse(self.mod.register_kimi_binding(self.agent, {**launch, "adapter": "other"}, event))
        self.assertFalse(self.mod.register_kimi_binding(self.agent, launch, {**event, "session_id": "../other"}))

    def test_wrong_scope_agent_invalid_numbers_and_status_totals_are_not_spend(self):
        self.mod.increment(self.agent)
        self.append(output=-1)
        base = {
            "type": "usage.record",
            "agentId": "main",
            "model": "model-b",
            "time": 4102444800000,
            "usageScope": "turn",
            "usage": {"inputOther": 100, "output": 30},
        }
        with self.path.open("a") as f:
            for patch in ({"type": "status.total"}, {"agentId": "other"}, {"usageScope": "session"}):
                f.write(json.dumps({**base, **patch}) + "\n")
        self.assertEqual(self.mod.increment(self.agent), [])

    def test_unbound_report_is_no_data_and_cursor_reload_keeps_dedup(self):
        self.agent.update(idle=">")
        self.mod.write_config({"participants": [self.agent]})
        self.mod.write_json(self.state / "kimi-usage-bindings.json", {})
        self.assertIn("no confirmed Kimi session binding", self.mod.report("agent"))
        self.mod.write_json(self.state / "kimi-usage-bindings.json", {"auto": self.binding})
        self.mod.increment(self.agent)
        self.append()
        self.mod.record(self.agent)
        from _package import Loader

        loaded = Loader("kimi_cursor_reload", str(Path(__file__).resolve().parents[1] / "collab")).load_module()
        loaded.use_room(str(self.root))
        self.assertEqual(loaded.increment(self.agent), [])
        self.assertIn("partial", loaded.report("model"))

    def test_numeric_records_are_put_down_to_task_at_their_time(self):
        events = [
            {"at": "2099-12-31T23:59:00+00:00", "event": "take", "pid": "auto", "task": "one", "kind": "fix", "lane": "B"},
            {"at": "2100-01-01T00:00:01+00:00", "event": "done", "pid": "auto", "task": "one"},
            {"at": "2100-01-01T00:00:02+00:00", "event": "take", "pid": "auto", "task": "two", "kind": "review", "lane": "C"},
        ]
        self.mod.write_state(self.state / "tasks.jsonl", "".join(json.dumps(e) + "\n" for e in events))
        self.mod.increment(self.agent)
        for delta in (0, 1500, 2500):
            self.append(at=4102444800000 + delta)
        self.assertEqual(self.mod.record(self.agent), 3)
        rows = self.mod.read_jsonl(self.mod.usage_path())
        self.assertEqual([r["task"] for r in rows], ["one", None, "two"])
        self.assertEqual([r["coverage"] for r in rows], ["partial"] * 3)

    def event(self, record):
        with self.path.open("a") as f:
            f.write(json.dumps(record) + "\n")

    def quota(self, turn=222, envelope=False):
        self.event(
            {
                "type": "context.append_loop_event" if envelope else "step.end",
                "agentId": "main",
                **(
                    {
                        "event": {
                            "type": "step.end",
                            "uuid": f"error-step-{turn}",
                            "turnId": turn,
                            "step": 1,
                            "finishReason": "error",
                        }
                    }
                    if envelope
                    else {"uuid": f"error-step-{turn}", "turnId": turn, "step": 1, "finishReason": "error"}
                ),
            }
        )
        self.event(
            {
                "type": "turn.step.interrupted",
                "agentId": "main",
                "turnId": turn,
                "step": 1,
                "reason": "error",
                "message": "[provider.api_error] 429",
            }
        )
        self.event(
            {
                "type": "turn.ended",
                "agentId": "main",
                "turnId": turn,
                "reason": "failed",
                "error": {
                    "code": "provider.api_error",
                    "name": "APIProviderQuotaExhaustedError",
                    "details": {"statusCode": 429, "requestId": None, "traceId": f"trace-{turn}"},
                    "retryable": False,
                },
                "traceId": f"trace-{turn}",
            }
        )

    def complete(self, turn=223, envelope=False):
        self.event(
            {
                "type": "context.append_loop_event" if envelope else "step.end",
                "agentId": "main",
                **(
                    {
                        "event": {
                            "type": "step.end",
                            "uuid": f"complete-step-{turn}",
                            "turnId": turn,
                            "step": 1,
                            "finishReason": "end_turn",
                            "providerFinishReason": "completed",
                            "rawFinishReason": "stop",
                        }
                    }
                    if envelope
                    else {
                        "uuid": f"complete-step-{turn}",
                        "turnId": turn,
                        "step": 1,
                        "finishReason": "end_turn",
                        "providerFinishReason": "completed",
                        "rawFinishReason": "stop",
                    }
                ),
            }
        )
        self.event({"type": "turn.ended", "agentId": "main", "turnId": turn, "reason": "completed", "traceId": f"trace-{turn}"})

    def hold(self, mod=None):
        mod = mod or self.mod
        text = (Path(__file__).resolve().parents[1] / "tests" / "screens" / "kimi-2.4.1-limit-current.txt").read_text()
        mod.write_config({"participants": [{"id": "owner"}, {**self.agent, "joined": True}]})
        shot = mod.Shot(0, text, "123\tkimi\t\t" + str(self.root))
        return mod.hold_for_limit(self.agent, "%1", "idle", mod.load_config(), shot)

    def adoption_fixture(self, writers="current"):
        self.mod.write_json(self.state / "kimi-usage-bindings.json", {})
        self.agent.update(started_by_room=True, folder=str(self.root))
        self.mod.write_config({"participants": [self.agent]})
        session = self.home / "sessions" / self.mod.kimi_workdir_key(str(self.root)) / "session_one"
        wire = session / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True, exist_ok=True)
        wire.touch()
        state = {
            "id": "session_one",
            "version": 1,
            "cwd": str(self.root),
            "archived": False,
            "agents": {"main": {"homedir": str(self.home), "type": "main"}},
        }
        (session / "state.json").write_text(json.dumps(state))
        (self.home / "config.toml").write_text(self.mod.kimi_hook_fragment())
        output = {"current": "p123\nf25u\n", "second": "p123\nf25u\np999\nf17w\n", "none": ""}[writers]
        runner = mock.Mock(return_value=mock.Mock(returncode=1 if writers == "none" else 0, stdout=output, stderr=""))
        self.adoption_run = runner
        idle_text = (Path(__file__).resolve().parents[1] / "tests" / "screens" / "kimi-2.4.1-idle.txt").read_text()
        shot = self.mod.Shot(0, idle_text, "123\tkimi\t\t" + str(self.root))
        self.enterContext(seam(self.mod, "screen.look", lambda *a, **k: shot))
        self.enterContext(seam(self.mod, "screen.pane_guard", lambda *a, **k: ""))
        self.enterContext(seam(self.mod, "usage.kimi_process_start", lambda pid: "process-start"))
        self.enterContext(seam(self.mod, "installer.subprocess.run", runner))
        return session, wire

    def adoption_launch(self, returncode=0):
        from types import SimpleNamespace

        calls = []

        def tmux(*args, **kwargs):
            calls.append(args)
            if args[0] == "display-message":
                return SimpleNamespace(returncode=0, stdout="456", stderr="")
            return SimpleNamespace(returncode=returncode, stdout="%1", stderr="refused" if returncode else "")

        self.enterContext(seam(self.mod, "tmuxio.tmux", tmux))
        self.enterContext(seam(self.mod, "agents.agent_window_alive", lambda p: True))
        self.enterContext(seam(self.mod, "usage.kimi_usage_adapter", lambda command: self.mod.KIMI_NORMALIZED))
        return calls

    def adoption_proof(self, wire):
        token = "a" * 32
        info = wire.stat()
        resume = {
            "generation": self.agent["join_code"],
            "pane_pid": self.agent["pane_pid"],
            "target": self.agent["target"],
            "sessions_root": str(self.home / "sessions"),
            "workdir_key": self.mod.kimi_workdir_key(str(self.root)),
            "session_id": "session_one",
            "agent_id": "main",
            "adapter": self.mod.KIMI_NORMALIZED,
        }
        proof = {
            "id": "auto",
            "token": token,
            "generation": "new-generation",
            "folder": str(self.root),
            "sessions_root": str(self.home / "sessions"),
            "workdir_key": self.mod.kimi_workdir_key(str(self.root)),
            "adapter": self.mod.KIMI_NORMALIZED,
            "resume_binding": resume,
            "adoption": {
                "old_pid": self.agent["pane_pid"],
                "old_start": "process-start",
                "target": self.agent["target"],
                "session_id": "session_one",
                "dev": info.st_dev,
                "ino": info.st_ino,
            },
        }
        self.mod.write_json(self.state / "kimi-launches.json", {token: proof})
        return token

    def tree_bytes(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def test_later_completed_turn_recovers_only_after_quota_boundary(self):
        self.complete(221)
        self.quota(222)
        boundary = self.mod.kimi_quota_boundary(self.agent)
        self.assertEqual(boundary["turn"], 222)
        self.assertGreater(boundary["off"], 0)
        self.assertFalse(self.mod.kimi_quota_recovered(self.agent, boundary))
        self.complete(223)
        self.assertTrue(self.mod.kimi_quota_recovered(self.agent, boundary))

    def test_failed_before_completed_is_allowed_but_failed_after_completed_is_not(self):
        self.quota(222)
        boundary = self.mod.kimi_quota_boundary(self.agent)
        self.complete(223)
        self.assertTrue(self.mod.kimi_quota_recovered(self.agent, boundary))
        self.quota(224)
        self.assertFalse(self.mod.kimi_quota_recovered(self.agent, boundary))
        self.complete(225)
        self.assertTrue(self.mod.kimi_quota_recovered(self.agent, boundary))

    def test_native_envelope_and_append_between_stat_and_read(self):
        self.quota(222, envelope=True)
        boundary = self.mod.kimi_quota_boundary(self.agent)
        original = self.mod.os.fstat

        def append_after_stat(fd):
            self.complete(223, envelope=True)
            return original(fd)

        with seam(self.mod, "usage.os.fstat", append_after_stat):
            self.assertTrue(self.mod.kimi_quota_recovered(self.agent, boundary))

    def test_chunk_progress_is_persisted_between_reads(self):
        self.quota(222, envelope=True)
        boundary = self.mod.kimi_quota_boundary(self.agent)
        self.event(
            {"type": "usage.record", "agentId": "main", "usageScope": "turn", "usage": {"inputOther": 600, "text": "x" * 1000}}
        )
        self.complete(223, envelope=True)
        with seam(self.mod, "usage.CHUNK", 512):
            first = self.mod.kimi_quota_recovery_state(self.agent, boundary)
            self.assertFalse(first["done"])
            self.assertGreater(first["cursor"]["off"], boundary["off"])
            progress = first
            for _ in range(4):
                progress = self.mod.kimi_quota_recovery_state(self.agent, progress["cursor"])
                if progress["done"]:
                    break
            self.assertTrue(progress["done"])
            self.assertTrue(progress["recovered"])

    def test_quota_offset_is_after_its_own_record_not_the_chunk_end(self):
        self.complete(221)
        self.quota(222)
        expected = self.path.stat().st_size
        self.complete(223)
        boundary = self.mod.kimi_quota_boundary(self.agent)
        self.assertEqual(boundary["off"], expected)
        self.assertTrue(self.mod.kimi_quota_recovered(self.agent, boundary))

    def test_success_step_and_turn_end_survive_serialized_loader_boundary(self):
        self.quota(222)
        boundary = self.mod.kimi_quota_boundary(self.agent)
        self.event(
            {
                "type": "step.end",
                "agentId": "main",
                "uuid": "complete-step-223",
                "turnId": 223,
                "step": 1,
                "finishReason": "end_turn",
                "providerFinishReason": "completed",
                "rawFinishReason": "stop",
            }
        )
        first = self.mod.kimi_quota_recovery_state(self.agent, boundary)
        self.assertFalse(first["recovered"])
        self.assertEqual(first["fsm"]["completed_steps"], [223])
        cursor = json.loads(json.dumps(first["cursor"]))
        from _package import Loader

        loaded = Loader("kimi_recovery_fsm_reload", str(Path(__file__).resolve().parents[1] / "collab")).load_module()
        loaded.use_room(str(self.root))
        self.event({"type": "turn.ended", "agentId": "main", "turnId": 223, "reason": "completed"})
        second = loaded.kimi_quota_recovery_state(self.agent, cursor)
        self.assertTrue(second["done"])
        self.assertTrue(second["recovered"])

    def test_completed_candidate_survives_loader_and_empty_eof_read(self):
        self.quota(222)
        boundary = self.mod.kimi_quota_boundary(self.agent)
        self.event({"type": "metadata", "text": "x" * 1000})
        self.complete(223)
        remaining = self.path.stat().st_size - boundary["off"]
        with seam(self.mod, "usage.CHUNK", remaining):
            first = self.mod.kimi_quota_recovery_state(self.agent, boundary)
        self.assertFalse(first["done"])
        self.assertTrue(first["recovered"])
        self.assertEqual(first["fsm"]["latest_completed"], 223)
        cursor = json.loads(json.dumps(first["cursor"]))
        from _package import Loader

        loaded = Loader("kimi_recovery_latest_reload", str(Path(__file__).resolve().parents[1] / "collab")).load_module()
        loaded.use_room(str(self.root))
        second = loaded.kimi_quota_recovery_state(self.agent, cursor)
        self.assertTrue(second["done"])
        self.assertTrue(second["recovered"])

    def test_unfinished_success_record_is_reread_from_its_first_byte(self):
        self.quota(222)
        boundary = self.mod.kimi_quota_boundary(self.agent)
        step = {
            "type": "step.end",
            "agentId": "main",
            "uuid": "complete-step-223",
            "turnId": 223,
            "step": 1,
            "finishReason": "end_turn",
            "providerFinishReason": "completed",
            "rawFinishReason": "stop",
        }
        encoded = json.dumps(step)
        with self.path.open("a") as f:
            f.write(encoded[:40])
        first = self.mod.kimi_quota_recovery_state(self.agent, boundary)
        self.assertFalse(first["done"])
        self.assertEqual(first["cursor"]["off"], boundary["off"])
        self.assertEqual(first["cursor"]["mid"], 0)
        with self.path.open("a") as f:
            f.write(encoded[40:] + "\n")
        self.event({"type": "turn.ended", "agentId": "main", "turnId": 223, "reason": "completed"})
        second = self.mod.kimi_quota_recovery_state(self.agent, first["cursor"])
        self.assertTrue(second["done"])
        self.assertTrue(second["recovered"])

    def test_device_identity_is_pinned_and_checked(self):
        self.quota(222)
        boundary = self.mod.kimi_quota_boundary(self.agent)
        self.assertEqual(boundary["dev"], self.path.stat().st_dev)
        self.complete(223)
        wrong_device = {**boundary, "dev": boundary["dev"] + 1}
        self.assertFalse(self.mod.kimi_quota_recovered(self.agent, wrong_device))
        self.assertTrue(self.mod.kimi_quota_recovered(self.agent, boundary))

    def test_boundary_scan_stops_at_its_chunk_and_time_budgets(self):
        self.quota(222)
        calls = 0

        def endless(path, at):
            nonlocal calls
            calls += 1
            if calls > 32:
                self.fail("boundary scan exceeded its chunk budget")
            return {
                "records": [({"type": "metadata"}, at["off"] + 1)],
                "cursor": {**at, "off": at["off"] + 1, "mid": 0},
                "eof": False,
            }

        with seam(self.mod, "usage._read_records", endless):
            self.assertEqual(self.mod.kimi_quota_boundary(self.agent), {})
        self.assertEqual(calls, 32)
        reads = 0

        def unexpected_read(path, at):
            nonlocal reads
            reads += 1
            return None

        clock = mock.Mock(side_effect=[0, 3])
        with seam(self.mod, "usage.time.monotonic", clock), seam(self.mod, "usage._read_records", unexpected_read):
            self.assertEqual(self.mod.kimi_quota_boundary(self.agent), {})
        self.assertEqual(reads, 0)

    def test_foreign_generation_and_pid_reuse_never_recover(self):
        self.quota(222)
        boundary = self.mod.kimi_quota_boundary(self.agent)
        for changed in ({"join_code": "generation-2"}, {"pane_pid": "999"}):
            original = self.agent.copy()
            self.agent.update(changed)
            self.complete(223)
            self.assertFalse(self.mod.kimi_quota_recovered(self.agent, boundary))
            self.agent = original
        self.agent["join_code"] = "generation-1"
        self.complete(223)
        self.assertTrue(self.mod.kimi_quota_recovered(self.agent, boundary))

    def test_partial_rotation_and_truncate_are_not_recovery(self):
        self.quota(222)
        boundary = self.mod.kimi_quota_boundary(self.agent)
        with self.path.open("a") as f:
            f.write('{"type":"turn.ended","turnId":223,"reason":"completed"')
        self.assertFalse(self.mod.kimi_quota_recovered(self.agent, boundary))
        with self.path.open("a") as f:
            f.write("\n")
        self.complete(223)
        self.assertFalse(self.mod.kimi_quota_recovered(self.agent, boundary))
        self.path.write_text("")
        self.quota(222)
        boundary = self.mod.kimi_quota_boundary(self.agent)
        self.complete(223)
        self.assertTrue(self.mod.kimi_quota_recovered(self.agent, boundary))
        rotated = self.path.with_suffix(".old")
        self.path.rename(rotated)
        self.path.write_text(rotated.read_text())
        self.assertFalse(self.mod.kimi_quota_recovered(self.agent, boundary))
        self.path.write_text("")
        self.assertFalse(self.mod.kimi_quota_recovered(self.agent, boundary))

    def test_adoption_dry_run_without_owner_id_is_write_free_and_unknown(self):
        self.adoption_fixture("none")
        before = self.tree_bytes()
        report = self.mod.kimi_adoption_preflight(self.agent)
        self.assertTrue(report["dry_run"])
        self.assertFalse(report["ready"])
        self.assertEqual(report["gates"]["participant"], "PASS")
        self.assertEqual(report["gates"]["process"], "PASS")
        self.assertEqual(report["gates"]["hook"], "PASS")
        for gate in ("session_id", "metadata", "writer", "checkpoint"):
            self.assertEqual(report["gates"][gate], "UNKNOWN")
        self.assertEqual(self.adoption_run.call_count, 0, "no wire scan or native launch without an exact ID")
        self.assertEqual(self.tree_bytes(), before)

    def test_adoption_dry_run_accepts_telemetry_append_at_new_checkpoint(self):
        session, wire = self.adoption_fixture()
        before = self.tree_bytes()
        first = self.mod.kimi_adoption_preflight(self.agent, "session_one")
        self.assertTrue(first["ready"])
        self.assertTrue(all(status == "PASS" for status in first["gates"].values()))
        self.assertEqual(self.tree_bytes(), before)
        wire.write_text('{"type":"metadata"}\n')
        before_second = self.tree_bytes()
        second = self.mod.kimi_adoption_preflight(self.agent, "session_one")
        self.assertTrue(second["ready"])
        self.assertGreater(second["snapshot"]["wire"]["off"], first["snapshot"]["wire"]["off"])
        self.assertEqual(second["snapshot"]["wire"]["ino"], first["snapshot"]["wire"]["ino"])
        self.assertIn("refresh", second["checkpoint_policy"]["append"])
        self.assertIn("rescan", second["checkpoint_policy"]["quota"])
        self.assertEqual(self.tree_bytes(), before_second)
        self.assertEqual(session.name, "session_one")

    def test_adoption_dry_run_refuses_second_writer_without_writes(self):
        session, wire = self.adoption_fixture("second")
        before = self.tree_bytes()
        report = self.mod.kimi_adoption_preflight(self.agent, "session_one")
        self.assertFalse(report["ready"])
        self.assertEqual(report["gates"]["writer"], "FAIL")
        self.assertEqual(report["snapshot"]["writer_pids"], ["123", "999"])
        self.assertEqual(self.tree_bytes(), before)

    def test_adoption_dry_run_rejects_unknown_id_and_metadata_mismatch(self):
        session, wire = self.adoption_fixture()
        before = self.tree_bytes()
        invalid = self.mod.kimi_adoption_preflight(self.agent, "../other")
        self.assertEqual(invalid["gates"]["session_id"], "FAIL")
        self.assertEqual(self.adoption_run.call_count, 0)
        self.assertEqual(self.tree_bytes(), before)
        original = json.loads((session / "state.json").read_text())
        for patch in ({"id": "other"}, {"cwd": "/other"}, {"archived": True}, {"agents": {}}):
            with self.subTest(patch=patch):
                (session / "state.json").write_text(json.dumps({**original, **patch}))
                before_report = self.tree_bytes()
                report = self.mod.kimi_adoption_preflight(self.agent, "session_one")
                self.assertFalse(report["ready"])
                self.assertEqual(report["gates"]["metadata"], "FAIL")
                self.assertEqual(report["gates"]["checkpoint"], "UNKNOWN")
                self.assertEqual(self.tree_bytes(), before_report)

    def test_adoption_dry_run_requires_process_identity_and_exact_hook(self):
        session, wire = self.adoption_fixture()
        with seam(self.mod, "usage.kimi_process_start", lambda pid: ""):
            report = self.mod.kimi_adoption_preflight(self.agent, "session_one")
        self.assertEqual(report["gates"]["process"], "FAIL")
        self.assertFalse(report["ready"])
        config = self.home / "config.toml"
        original = config.read_bytes()
        config.write_text("other = true\n")
        before = config.read_bytes()
        report = self.mod.kimi_adoption_preflight(self.agent, "session_one")
        self.assertEqual(report["gates"]["hook"], "FAIL")
        self.assertIn("prerequisite", report["reasons"]["hook"])
        self.assertEqual(config.read_bytes(), before)
        self.assertNotEqual(before, original)

    def test_adoption_execute_refuses_busy_approval_draft_and_background(self):
        from types import SimpleNamespace

        session, wire = self.adoption_fixture()
        for name in ("working", "approval", "draft", "footer-two-running"):
            with self.subTest(name=name):
                text = (Path(__file__).resolve().parents[1] / "tests" / "screens" / f"kimi-2.4.1-{name}.txt").read_text()
                shot = self.mod.Shot(0, text, "123\tkimi\t\t" + str(self.root))
                calls = []

                def tmux(*args, calls=calls, text=text, **kwargs):
                    calls.append(args)
                    return SimpleNamespace(returncode=0, stdout=text, stderr="")

                with seam(self.mod, "screen.look", lambda *a, shot=shot, **k: shot), seam(self.mod, "tmuxio.tmux", tmux):
                    result = self.mod.adopt_kimi_session("probe", self.agent, "session_one", execute=True)
                self.assertEqual(result["execution"], "refused")
                self.assertEqual(result["gates"]["process"], "FAIL")
                self.assertFalse(any(args[0] in ("respawn-pane", "new-window") for args in calls))
                self.assertEqual(self.mod.kimi_state(self.state / "kimi-launches.json"), {})

    def test_adoption_execute_never_replaces_missing_live_pane_with_new_window(self):
        session, wire = self.adoption_fixture()
        self.adoption_launch()
        with (
            seam(self.mod, "agents.agent_window_alive", lambda p: False),
            seam(self.mod, "tmuxio.tmux", mock.Mock(side_effect=AssertionError("no new window allowed"))),
        ):
            result = self.mod.adopt_kimi_session("probe", self.agent, "session_one", execute=True)
        self.assertEqual(result["execution"], "failed")
        self.assertEqual(result["recovery_command"], "kimi --session session_one")
        self.assertEqual(self.mod.kimi_state(self.state / "kimi-launches.json"), {})
        self.assertEqual(self.mod.kimi_state(self.state / "kimi-usage-cursor.json"), {})

    def test_process_status_only_documented_absence_is_gone(self):
        for returncode, stdout, stderr, expected in (
            (0, "Wed Nov 4 12:00:00 2026", "", "alive"),
            (0, "", "", "unknown"),
            (1, "", "", "gone"),
            (2, "", "", "unknown"),
            (1, "", "ps: error", "unknown"),
        ):
            with self.subTest(returncode=returncode, stderr=stderr, expected=expected):
                result = mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)
                with seam(self.mod, "usage.subprocess.run", mock.Mock(return_value=result)):
                    self.assertEqual(self.mod.kimi_process_status("12345"), expected)

    def test_adoption_poststop_guard_uses_manifest_not_config_and_rejects_unknown(self):
        from types import SimpleNamespace

        session, wire = self.adoption_fixture("none")
        token = self.adoption_proof(wire)
        info = wire.stat()
        tmux = lambda *a, **k: SimpleNamespace(returncode=0, stdout="456", stderr="")
        self.mod.write_config({"participants": [{"id": "auto", "pane_pid": "456", "join_code": "new-generation"}]})
        with (
            mock.patch.object(self.mod.os, "getppid", return_value=456),
            seam(self.mod, "tmuxio.tmux", tmux),
            seam(self.mod, "usage.kimi_process_start", lambda pid: "new-start"),
            seam(self.mod, "usage.kimi_process_status", lambda pid: "gone"),
        ):
            self.assertTrue(self.mod.kimi_adoption_poststop("auto", token, "123", "session_one", info.st_dev, info.st_ino))
            with self.assertRaises((SystemExit, self.mod.OperationFailed)):
                self.mod.kimi_adoption_poststop("auto", token, "123", "session_one", info.st_dev + 1, info.st_ino)
            for status in ("alive", "unknown"):
                with self.subTest(status=status), seam(self.mod, "usage.kimi_process_status", lambda pid, status=status: status):
                    with self.assertRaises((SystemExit, self.mod.OperationFailed)):
                        self.mod.kimi_adoption_poststop("auto", token, "123", "session_one", info.st_dev, info.st_ino)
        session, wire = self.adoption_fixture("second")
        token = self.adoption_proof(wire)
        info = wire.stat()
        with (
            mock.patch.object(self.mod.os, "getppid", return_value=456),
            seam(self.mod, "tmuxio.tmux", tmux),
            seam(self.mod, "usage.kimi_process_start", lambda pid: "new-start"),
            seam(self.mod, "usage.kimi_process_status", lambda pid: "gone"),
        ):
            with self.assertRaises((SystemExit, self.mod.OperationFailed)):
                self.mod.kimi_adoption_poststop("auto", token, "123", "session_one", info.st_dev, info.st_ino)

    def test_adoption_execute_resumes_exact_session_with_checkpoint_cursor(self):
        session, wire = self.adoption_fixture()
        self.path = wire
        self.append()
        self.quota(222)
        dry = self.mod.adopt_kimi_session("probe", self.agent, "session_one")
        self.assertEqual(dry["execution"], "dry-run")
        self.assertTrue(dry["adoption_eligible"])
        first_offset = dry["snapshot"]["wire"]["off"]
        self.complete(223)
        calls = self.adoption_launch()
        result = self.mod.adopt_kimi_session("probe", self.agent, "session_one", execute=True)
        self.assertEqual(result["execution"], "started")
        self.assertIn("kimi-adoption-guard", str(calls[0][-1]))
        self.assertIn(" && exec ", str(calls[0][-1]))
        self.assertIn("--session session_one", str(calls[0][-1]))
        self.assertNotIn("--continue", str(calls[0][-1]))
        current = self.mod.read_config_raw()["participants"][0]
        token = current["kimi_usage_generation"]
        cursor = self.mod.kimi_state(self.state / "kimi-usage-cursor.json")["auto"]
        self.assertGreater(cursor["files"]["wire"]["off"], first_offset)
        self.assertEqual(cursor["files"]["wire"]["off"], wire.stat().st_size)
        event = {
            "hook_event_name": "SessionStart",
            "source": "resume",
            "client_type": "kimi_code_cli",
            "session_id": "session_one",
            "cwd": str(self.root),
        }
        with (
            mock.patch.dict(os.environ, {"COLLAB_KIMI_GENERATION": token, "COLLAB_KIMI_AGENT": "auto"}),
            mock.patch.object(self.mod.os, "getppid", return_value=456),
        ):
            self.assertTrue(self.mod.collect_kimi_start(event))
        bound = self.mod.kimi_binding(current)
        self.assertEqual(bound["session_id"], "session_one")
        signature = self.mod.hashlib.sha256(self.mod.json.dumps(bound, sort_keys=True).encode()).hexdigest()
        cursor = self.mod.kimi_state(self.state / "kimi-usage-cursor.json")["auto"]
        self.assertEqual(cursor["binding"], signature)
        boundary = self.mod.kimi_quota_boundary(current)
        self.assertTrue(self.mod.kimi_quota_recovered(current, boundary))
        self.append(at=4102444801000)
        self.assertEqual(len(self.mod.increment(current)), 1, "only usage after the refreshed checkpoint is counted")

    def test_adoption_execute_refuses_process_toctou_before_window_mutation(self):
        session, wire = self.adoption_fixture()
        identities = iter(("process-start", "changed-start"))
        with (
            seam(self.mod, "usage.kimi_process_start", lambda pid: next(identities)),
            seam(self.mod, "tmuxio.tmux", mock.Mock(side_effect=AssertionError("no window mutation allowed"))),
        ):
            result = self.mod.adopt_kimi_session("probe", self.agent, "session_one", execute=True)
        self.assertEqual(result["execution"], "failed")
        self.assertEqual(result["recovery_command"], "kimi --session session_one")
        self.assertEqual(self.mod.kimi_state(self.state / "kimi-launches.json"), {})
        self.assertEqual(self.mod.kimi_state(self.state / "kimi-usage-cursor.json"), {})
        self.assertNotIn("kimi_usage_generation", self.mod.read_config_raw()["participants"][0])

    def test_adoption_execute_missing_hook_never_launches_or_installs(self):
        session, wire = self.adoption_fixture()
        config = self.home / "config.toml"
        config.write_text("other = true\n")
        before = config.read_bytes()
        with seam(self.mod, "tmuxio.tmux", mock.Mock(side_effect=AssertionError("no window mutation allowed"))):
            result = self.mod.adopt_kimi_session("probe", self.agent, "session_one", execute=True)
        self.assertEqual(result["execution"], "refused")
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(self.mod.kimi_state(self.state / "kimi-launches.json"), {})
        self.assertEqual(self.mod.kimi_state(self.state / "kimi-usage-cursor.json"), {})

    def test_adoption_poststop_failure_cleans_baseline_and_never_starts_fresh(self):
        session, wire = self.adoption_fixture()
        calls = self.adoption_launch(returncode=1)
        result = self.mod.adopt_kimi_session("probe", self.agent, "session_one", execute=True)
        self.assertEqual(result["execution"], "failed")
        self.assertEqual(result["recovery_command"], "kimi --session session_one")
        self.assertIn("--session session_one", str(calls[0][-1]))
        self.assertNotIn("--continue", str(calls[0][-1]))
        self.assertEqual(self.mod.kimi_state(self.state / "kimi-launches.json"), {})
        self.assertEqual(self.mod.kimi_state(self.state / "kimi-usage-cursor.json"), {})
        self.assertEqual(self.mod.kimi_state(self.state / "kimi-usage-bindings.json"), {})
        self.assertNotIn("kimi_usage_generation", self.mod.read_config_raw()["participants"][0])

    def test_production_launch_binds_session_and_cursor_end_to_end(self):
        token = self.mod.prepare_kimi_launch("auto", "generation-1", str(self.root), "openai-compatible-2.4.1")
        self.agent.update(started_by_room=True, kimi_usage_generation=token, folder=str(self.root))
        self.mod.write_config({"participants": [self.agent]})
        with (
            seam(self.mod, "usage.kimi_process_start", lambda pid: "process-start"),
            mock.patch.object(self.mod.os, "getppid", return_value=123),
            mock.patch.dict(os.environ, {"COLLAB_KIMI_GENERATION": token, "COLLAB_KIMI_AGENT": "auto"}),
        ):
            self.mod.finish_kimi_launch(self.agent, token)
            event = {
                "hook_event_name": "SessionStart",
                "source": "startup",
                "client_type": "kimi_code_cli",
                "session_id": "session_one",
                "parent_pid": 123,
                "cwd": str(self.root),
            }
            self.assertTrue(self.mod.collect_kimi_start(event))
        bound = self.mod.kimi_binding(self.agent)
        self.assertEqual(bound["session_id"], "session_one")
        signature = self.mod.hashlib.sha256(self.mod.json.dumps(bound, sort_keys=True).encode()).hexdigest()
        cursor = self.mod.kimi_state(self.state / "kimi-usage-cursor.json")["auto"]
        self.assertEqual(cursor["binding"], signature)
        self.assertFalse(cursor["observed"])
        self.assertEqual(self.mod.kimi_state(self.state / "kimi-launches.json")[token]["session_id"], "session_one")

    def test_deliver_recovers_bound_quota_once_and_legacy_requires_explicit_clear(self):
        self.quota(222)
        self.assertTrue(self.hold())
        self.complete(223)
        self.assertFalse(self.hold())
        self.assertIn("over", self.mod.read_json(self.mod.room().paths.limits_path)["auto"])
        self.assertFalse(self.hold())
        self.mod.write_json(self.mod.room().paths.limits_path, {})
        self.mod.write_json(self.state / "kimi-usage-bindings.json", {})
        self.quota(224)
        self.assertTrue(self.hold())
        self.complete(225)
        self.assertTrue(self.hold())
        self.assertTrue(self.mod.clear_limit("auto")[1])
        self.assertFalse(self.hold())

    def test_deliver_ticks_persist_nested_fsm_and_hold_until_terminal_success(self):
        from _package import Loader

        self.quota(222)
        self.assertTrue(self.hold())
        self.event({"type": "metadata", "text": "x" * 1000})
        self.complete(223)
        self.quota(224)
        failed_eof = self.path.stat().st_size
        tick_number = 0

        def tick():
            nonlocal tick_number
            tick_number += 1
            loaded = Loader(f"kimi_deliver_tick_{tick_number}", str(Path(__file__).resolve().parents[1] / "collab")).load_module()
            loaded.use_room(str(self.root))
            with seam(loaded, "usage.CHUNK", 512):
                return self.hold(loaded)

        for _ in range(6):
            self.assertTrue(tick(), "incomplete or failed recovery must keep delivery held")
        recovery = self.mod.read_json(self.mod.room().paths.limits_path)["auto"]["recovery"]
        cursor = recovery["cursor"]
        self.assertEqual(cursor["off"], failed_eof)
        self.assertIsNone(cursor["fsm"]["latest_completed"])
        self.assertEqual(cursor["fsm"]["completed_steps"], [223])
        self.assertNotIn("cursor", cursor, "deliver ticks must not nest the previous cursor")
        self.complete(225)
        self.assertFalse(tick(), "a completed turn at terminal EOF releases delivery")
        self.assertFalse(tick(), "the released limit stays released")
        messages = self.mod.read_jsonl(self.mod.room().paths.messages_path)
        notices = [m for m in messages if m.get("said", {}).get("t") == "{name} is back from its usage limit."]
        self.assertEqual(len(notices), 1)

    def test_non_object_wire_json_holds_without_an_exception(self):
        for value in ([], None, 7):
            with self.subTest(value=value):
                self.path.write_text("")
                self.mod.write_json(self.mod.room().paths.limits_path, {})
                self.quota(222)
                self.assertTrue(self.hold())
                self.event(value)
                self.complete(223)
                self.assertEqual(self.mod._terminal_event(value), {})
                self.assertTrue(self.hold(), "unknown wire schema must fail closed")

    def test_fifo_without_writer_is_refused_without_blocking_and_regular_file_opens(self):
        fifo = self.root / "usage-fifo"
        os.mkfifo(fifo)
        script = """import importlib.machinery, os, sys
from pathlib import Path
m = importlib.machinery.SourceFileLoader('fifo_usage_test', sys.argv[1]).load_module()
fd = m.open_under(Path('/'), Path(sys.argv[2]))
print('refused' if fd is None else os.read(fd, 5).decode())
if fd is not None:
    os.close(fd)
"""
        cli = str(Path(__file__).resolve().parents[1] / "collab")
        try:
            result = subprocess.run(
                [sys.executable, "-B", "-c", script, cli, str(fifo)], capture_output=True, text=True, timeout=3, check=True
            )
        except subprocess.TimeoutExpired:
            self.fail("opening a FIFO with no writer blocked instead of refusing a non-regular source")
        self.assertEqual(result.stdout.strip(), "refused")
        regular = self.root / "regular-usage"
        regular.write_text("usage")
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, cli, str(regular)], capture_output=True, text=True, timeout=3, check=True
        )
        self.assertEqual(result.stdout.strip(), "usage")
