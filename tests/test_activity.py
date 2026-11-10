"""Known queue activity is separate from the agent's input readiness."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ledger import LedgerRoom  # noqa: E402


class ActivityTest(LedgerRoom):
    def setUp(self):
        super().setUp()
        self.mod.write_config({"participants": [{"id": "owner"}, {"id": "agent", "label": "Agent", "kind": "kimi"}]})
        self.token = "a" * 32

    def test_idle_input_with_known_check_is_not_reported_as_no_work(self):
        import subprocess

        self.mod.write_status("agent", "idle", "", "screen", "model")
        self.mod.write_json(
            self.state / "activity.json",
            {
                "v": 1,
                "room": str(self.root),
                "entries": {"agent": {"run": self.token, "phase": "checking", "updated": time.time(), "lease": 30}},
            },
        )
        cli = Path(__file__).resolve().parents[1] / "collab"
        out = subprocess.run([sys.executable, str(cli), "status", "--room", str(self.root)], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("ready", out.stdout)
        self.assertIn("check running", out.stdout)
        self.assertEqual(self.mod.read_status()["agent"]["state"], "idle")

    def test_old_heartbeat_and_cleanup_cannot_replace_a_new_run(self):
        m = self.mod
        self.assertTrue(m.publish_activity("agent", self.token, "waiting", begin=True))
        old = m.ActivityLease("agent", self.token)
        old.phase("checking")
        newer = "b" * 32
        self.assertTrue(m.publish_activity("agent", newer, "waiting", begin=True))
        old.finish("failed")
        self.assertEqual(m.activity_records()["agent"]["run"], newer)
        self.assertEqual(m.activity_records()["agent"]["phase"], "waiting")
        self.assertFalse(old.phase())

    def test_expired_or_interrupted_publisher_is_stale_not_done(self):
        from unittest import mock

        m = self.mod
        m.publish_activity("agent", self.token, "waiting", begin=True)
        m.publish_activity("agent", self.token, "checking")
        stamp = m.activity_records()["agent"]["updated"]
        with mock.patch("time.time", return_value=stamp + 31):
            self.assertEqual(m.activity_projection(m.read_config_raw()), {"agent": "stale"})
        m.publish_activity("agent", self.token, "failed")
        self.assertEqual(m.activity_projection(m.read_config_raw()), {"agent": "failed"})

    def test_room_and_participant_boundaries_and_legacy_queue_are_not_guessing(self):
        m = self.mod
        m.publish_activity("agent", self.token, "waiting", begin=True)
        original = m.activity_file()
        other = self.root / "other"
        other.mkdir()
        m.use_room(str(other))
        m.private_dir(m.room().paths.state_dir)
        m.write_config({"participants": [{"id": "agent"}]})
        self.assertFalse(m.publish_activity("agent", self.token, "checking", root=str(self.root)))
        m.write_json(m.room().paths.state_dir / "activity.json", original)
        self.assertEqual(m.activity_records(), {})
        m.use_room(str(self.root))
        m.write_json(self.state / "activity.json", {})
        m.write_json(
            self.state / "merge-queue.json",
            {"queue": [{"id": "agent", "pid": 123, "start": "known"}, {"id": "foreign"}, {"id": []}]},
        )
        self.assertEqual(m.activity_projection(m.read_config_raw()), {"agent": "stale"})
        self.assertFalse(m.publish_activity("foreign", self.token, "waiting", begin=True))

    def test_reader_refuses_oversize_links_fifo_and_unbounded_payload(self):
        import os

        m = self.mod
        path = self.state / "activity.json"
        path.write_text("x" * (m.ACTIVITY_LIMIT + 1))
        self.assertEqual(m.activity_records(), {})
        path.unlink()
        target = self.root / "external"
        target.write_text("{}")
        path.symlink_to(target)
        self.assertEqual(m.activity_records(), {})
        path.unlink()
        os.mkfifo(path)
        self.assertEqual(m.activity_records(), {})
        path.unlink()
        m.write_json(
            path,
            {
                "v": 1,
                "room": str(self.root),
                "entries": {
                    "agent": {
                        "run": self.token,
                        "phase": {},
                        "updated": time.time(),
                        "lease": 30,
                        "command": "must not be shown",
                    }
                },
            },
        )
        self.assertEqual(m.activity_projection(m.read_config_raw()), {})

    def test_silent_child_does_not_stop_heartbeat_and_interruption_expires(self):
        import subprocess
        import threading
        from unittest import mock

        m = self.mod
        lease = m.ActivityLease("agent", self.token, interval=0.02)
        lease.start()
        self.addCleanup(lease.finish, "failed")
        lease.phase("checking")
        first = m.activity_records()["agent"]["updated"]
        with subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.readline()"], stdin=subprocess.PIPE, text=True
        ) as child:
            end = time.monotonic() + 3
            while m.activity_records()["agent"]["updated"] <= first and time.monotonic() < end:
                threading.Event().wait(0.01)
            self.assertIsNone(child.poll(), "the child is still silently waiting")
            self.assertGreater(m.activity_records()["agent"]["updated"], first)
            child.communicate("\n", timeout=3)
        # Simulate abrupt loss: stop renewal without publishing a terminal outcome.
        lease.event.set()
        lease.thread.join(timeout=1)
        stamp = m.activity_records()["agent"]["updated"]
        with mock.patch("time.time", return_value=stamp + 31):
            self.assertEqual(m.activity_projection(m.read_config_raw())["agent"], "stale")

    def test_narrow_activity_rows_keep_all_models_and_input_cells(self):
        import os

        m = self.mod
        people = [{"id": f"a{i}", "label": f"Agent{i}"} for i in range(6)]
        config = {"participants": people, "language": "en"}
        m.write_config(config)
        for p in people:
            m.publish_activity(p["id"], self.token, "waiting", begin=True)
            m.publish_activity(p["id"], self.token, "checking")
        cells = [(p["label"], f"Model{i}", "Ready", " ", "", 40) for i, p in enumerate(people)]
        rows = m.status_bar_rows(cells, [], os.terminal_size((30, 24)), activity=m.activity_lines(config, 30))
        plain = m.ANSI.sub("", "\n".join(rows))
        for i, p in enumerate(people):
            self.assertIn(p["label"], plain)
            self.assertIn(f"Model{i}", plain)
        self.assertTrue(all(m.text_cols(m.ANSI.sub("", row)) <= 30 for row in rows))
        self.assertLessEqual(len(rows), 14)
        self.assertIn("check running", plain)

    def test_new_queue_run_cannot_inherit_an_old_completed_record(self):
        m = self.mod
        m.publish_activity("agent", self.token, "waiting", begin=True)
        m.publish_activity("agent", self.token, "done")
        m.write_json(
            self.state / "merge-queue.json",
            {
                "queue": [
                    {
                        "id": "agent",
                        "pid": 123,
                        "start": "known",
                        "activity_run": "b" * 32,
                    }
                ]
            },
        )
        self.assertEqual(m.activity_projection(m.read_config_raw()), {"agent": "stale"})

    def test_chat_uses_the_same_activity_without_changing_input_state(self):
        from types import SimpleNamespace

        m = self.mod
        m.write_status("agent", "idle", "", "screen", "Model")
        m.publish_activity("agent", self.token, "waiting", begin=True)
        m.publish_activity("agent", self.token, "checking")
        ctx = SimpleNamespace(
            old_versions=lambda: [],
            known=["owner", "agent"],
            args=SimpleNamespace(as_="owner", stale=30.0, interval=1.0),
            config=m.read_config_raw(),
            shown_state={},
            stale_chat=lambda: False,
            status_bar_rows=m.status_bar_rows,
            ui=m.ChatState(),
        )
        m.chat_states(ctx)
        self.assertIn("check running", ctx.ui.bar)
        self.assertIn("Model", ctx.ui.bar)
        self.assertEqual(m.read_status()["agent"]["state"], "idle")

    def test_short_window_keeps_stale_visible_before_a_long_name(self):
        import os
        from unittest import mock

        m = self.mod
        config = {"participants": [{"id": "agent", "label": "LongParticipantNameXX"}], "language": "en"}
        m.write_config(config)
        m.publish_activity("agent", self.token, "waiting", begin=True)
        stamp = m.activity_records()["agent"]["updated"]
        with mock.patch("time.time", return_value=stamp + 31):
            rows = m.status_bar_rows(
                [("LongParticipantNameXX", "Model", "Ready", " ", "", 40)],
                [],
                os.terminal_size((30, 12)),
                activity=m.activity_lines(config, 30),
            )
        text = m.ANSI.sub("", "\n".join(rows))
        self.assertIn("stale", text, "the phase must not be lost when optional activity rows are clipped")
        self.assertIn("Model", text)

    def render_badges(self):
        from types import SimpleNamespace

        m = self.mod
        frames = []

        def rows(cells, parts, size, **kwargs):
            frames.append(cells)
            return m.status_bar_rows(cells, parts, size, **kwargs)

        ctx = SimpleNamespace(
            old_versions=lambda: [],
            known=["owner", "agent"],
            args=SimpleNamespace(as_="owner", stale=30.0, interval=1.0),
            config=m.read_config_raw(),
            shown_state={},
            stale_chat=lambda: False,
            status_bar_rows=rows,
            ui=m.ChatState(),
        )
        m.chat_states(ctx)
        m.chat_states(ctx)
        return [frame[0] for frame in frames], ctx.ui.bar

    def test_queue_work_blinks_ready_badge_for_every_kind_without_inventing_count(self):
        m = self.mod
        for kind in ("claude", "codex", "kimi"):
            m.write_config({"participants": [{"id": "owner"}, {"id": "agent", "label": "Agent", "kind": kind}]})
            for phase in ("waiting", "checking", "finishing"):
                with self.subTest(kind=kind, phase=phase):
                    m.write_status("agent", "idle", "", "screen", "Model")
                    m.publish_activity("agent", self.token, "waiting", begin=True)
                    m.publish_activity("agent", self.token, phase)
                    before = m.read_status()
                    frames, bar = self.render_badges()
                    self.assertEqual({cell[5] for cell in frames}, {40, 220}, "ready background work must blink green/yellow")
                    for cell in frames:
                        self.assertEqual(cell[1:3], ("Model", "Ready"))
                        self.assertIn(cell[3], m.SPINNER, "queue activity proves no native task count")
                        self.assertEqual(cell[4], "", "a lease timestamp is not a task start time")
                    self.assertIn("Model", bar)
                    self.assertEqual(m.read_status(), before, "rendering must not change delivery readiness")

    def test_terminal_stale_foreign_or_superseded_activity_does_not_blink(self):
        m = self.mod
        for phase, age, foreign, superseded in (
            ("done", 0, False, False),
            ("failed", 0, False, False),
            ("checking", 31, False, False),
            ("checking", 0, True, False),
            ("checking", 0, False, True),
        ):
            with self.subTest(phase=phase, age=age, foreign=foreign, superseded=superseded):
                m.write_status("agent", "idle", "", "screen", "Model")
                m.write_json(
                    self.state / "activity.json",
                    {
                        "v": 1,
                        "room": str(self.root) + ("/other" if foreign else ""),
                        "entries": {"agent": {"run": self.token, "phase": phase, "updated": time.time() - age, "lease": 30}},
                    },
                )
                m.write_json(
                    self.state / "merge-queue.json",
                    {
                        "queue": [
                            {
                                "id": "agent",
                                "pid": 1,
                                "start": "known",
                                "activity_run": "b" * 32,
                            }
                        ]
                        if superseded
                        else []
                    },
                )
                frames, _ = self.render_badges()
                self.assertEqual([cell[5] for cell in frames], [40, 40])
                self.assertEqual([cell[3] for cell in frames], [" ", " "])

    def test_queue_indicator_preserves_native_count_clock_and_priority_states(self):
        m = self.mod
        m.publish_activity("agent", self.token, "waiting", begin=True)
        m.publish_activity("agent", self.token, "checking")
        m.write_status("agent", "idle", "background 2", "screen", "Model")
        native_frames, _ = self.render_badges()
        self.assertEqual({cell[5] for cell in native_frames}, {40, 220})
        for cell in native_frames:
            self.assertEqual(cell[3][1:], " 2", "the queue must not be added to a native count")
            self.assertTrue(cell[4], "the native background clock stays visible")
            self.assertEqual(cell[1:3], ("Model", "Ready"))
        for state, short, fill in (
            ("needs_owner", "Needs your input", 196),
            ("limited", "Out of limit", 208),
            ("draft", "Unsent draft", 196),
            ("lost", "Disconnected", 196),
            ("working", "Working", 220),
        ):
            with self.subTest(state=state):
                m.write_status("agent", state, "detail", "screen", "Model")
                frames, _ = self.render_badges()
                self.assertEqual([cell[2] for cell in frames], [short, short])
                self.assertEqual([cell[5] for cell in frames], [fill, fill])
        config = m.read_config_raw()
        config["participants"][1]["muted"] = True
        m.write_config(config)
        m.write_status("agent", "idle", "", "screen", "Model")
        frames, _ = self.render_badges()
        self.assertEqual([cell[2] for cell in frames], ["Muted", "Muted"])
        self.assertEqual([cell[5] for cell in frames], [208, 208])
