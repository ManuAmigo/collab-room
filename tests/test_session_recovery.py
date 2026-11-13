"""Confirmed launch bindings recover lost windows without guessing another conversation."""

import json
import os
import sys
import time
import re
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ledger import LedgerRoom  # noqa: E402
from _seam import seam  # noqa: E402


class SessionRecoveryTest(LedgerRoom):
    SID = "01a14da8-636e-7f72-b641-4efd93e5f7e8"

    def setUp(self):
        super().setUp()
        self.agent = {
            "id": "probe",
            "kind": "codex",
            "started_by_room": True,
            "target": "%51",
            "pane_pid": "321",
            "join_code": "ddeb",
            "joined_at": self.mod.now(),
            "folder": str(self.root),
        }
        self.mod.write_config({"participants": [{"id": "owner"}, self.agent]})
        self.logs = self.root / "codex-home" / "sessions"
        self.logs.mkdir(parents=True)
        self.log = self.logs / "rollout-probe.jsonl"
        self.log.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": self.SID, "cwd": str(self.root)}})
            + "\n"
            + json.dumps({"text": '[room → probe] Connection check: reply in the room with "connected ddeb".'})
            + "\n"
        )
        self.enterContext(mock.patch.dict(os.environ, {"CODEX_HOME": str(self.logs.parent)}))

    def saved(self):
        return self.mod.read_config_raw()["participants"][1]

    def remember(self):
        with (
            seam(self.mod, "tmuxio.tmux", lambda *a, **k: SimpleNamespace(returncode=0, stdout="321\n")),
            seam(
                self.mod,
                "screen.subprocess.run",
                lambda *a, **k: SimpleNamespace(returncode=0, stdout="n" + str(self.log) + "\n"),
            ),
        ):
            self.mod.remember_window_session(self.agent, "%51")

    def test_confirmed_live_codex_session_survives_its_window(self):
        self.remember()
        with seam(self.mod, "tmuxio.tmux", lambda *a, **k: SimpleNamespace(returncode=1, stdout="")):
            self.assertEqual(self.mod.agent_session(self.saved()), self.SID)

    def test_saved_session_cannot_cross_launch_generations_or_reused_live_panes(self):
        self.remember()
        saved = self.saved()
        with seam(self.mod, "tmuxio.tmux", lambda *a, **k: SimpleNamespace(returncode=1, stdout="")):
            self.assertEqual(self.mod.agent_session(saved), self.SID)
            for key, value in (
                ("join_code", "new1"),
                ("pane_pid", "999"),
                ("target", "%99"),
                ("folder", "/other"),
                ("joined_at", "other"),
            ):
                with self.subTest(key=key):
                    self.assertEqual(self.mod.agent_session({**saved, key: value}), "")
        with seam(self.mod, "tmuxio.tmux", lambda *a, **k: SimpleNamespace(returncode=0, stdout="999\n")):
            self.assertEqual(self.mod.agent_session(saved), "")

    def test_unconfirmed_lost_window_offers_new_session_only_after_owner_confirmation(self):
        ctx = SimpleNamespace(
            wizard={"step": "restart_pick"}, config=self.mod.load_config(), feed=lambda text: None, refresh_people=lambda: None
        )
        started = mock.Mock()
        with (
            seam(self.mod, "screen.agent_session", lambda p: ""),
            seam(self.mod, "agents.agent_window_alive", lambda p: False),
            seam(self.mod, "agents.restart_agent", started),
        ):
            self.mod.wizard_step(ctx, "probe", [])
            started.assert_not_called()
            self.assertEqual(ctx.wizard.get("step"), "restart_new_confirm")
            self.mod.wizard_step(ctx, "n", [])
            started.assert_not_called()
            ctx.wizard.update(step="restart_pick")
            self.mod.wizard_step(ctx, "probe", [])
            self.mod.wizard_step(ctx, "y", [])
            self.assertTrue(started.call_args.kwargs["fresh"])

    def test_discovery_is_outside_room_lock_and_cannot_save_across_a_concurrent_restart(self):
        held = []

        def discovered(p):
            held.append(self.mod._ROOM_LOCK["depth"])
            data = self.mod.read_config_raw()
            data["participants"][1]["join_code"] = "new1"
            self.mod.write_config(data)
            return self.SID

        with (
            seam(self.mod, "screen.agent_session", discovered),
            seam(self.mod, "tmuxio.tmux", lambda *a, **k: SimpleNamespace(returncode=0, stdout="321\n")),
        ):
            self.mod.remember_window_session(self.agent, "%51")
        self.assertEqual(held, [0])
        self.assertNotIn("confirmed_session", self.saved())

    def test_changed_agent_or_returned_window_invalidates_new_session_confirmation(self):
        for returned in (False, True):
            with self.subTest(returned=returned):
                self.mod.write_config({"participants": [{"id": "owner"}, self.agent]})
                ctx = SimpleNamespace(wizard={"step": "restart_pick"}, config=self.mod.load_config(), feed=lambda text: None)
                started = mock.Mock()
                with (
                    seam(self.mod, "screen.agent_session", lambda p: ""),
                    seam(self.mod, "agents.agent_window_alive", mock.Mock(side_effect=[False, returned])),
                    seam(self.mod, "agents.restart_agent", started),
                ):
                    self.mod.wizard_step(ctx, "probe", [])
                    if not returned:
                        data = self.mod.read_config_raw()
                        data["participants"][1]["join_code"] = "new1"
                        self.mod.write_config(data)
                    self.mod.wizard_step(ctx, "y", [])
                    started.assert_not_called()

    def test_recovery_starts_a_new_window_with_exact_resume_and_clears_old_binding(self):
        self.remember()
        saved = self.saved()
        commands = []

        def tmux(*args, **kwargs):
            commands.append(args)
            if args[0] == "new-window":
                return SimpleNamespace(returncode=0, stdout="%52\n", stderr="")
            if args[0] == "display-message" and args[3] == "%52":
                return SimpleNamespace(returncode=0, stdout="456\n", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="missing")

        with (
            seam(self.mod, "tmuxio.tmux", tmux),
            seam(self.mod, "agents.agent_window_alive", lambda p: False),
            seam(self.mod, "agents.agent_command", lambda kind, resume, *a, **k: "codex " + " ".join(resume)),
            seam(self.mod, "installer.codex_room_rule", lambda *a: ""),
            seam(self.mod, "screen.write_status", lambda *a: None),
            seam(self.mod, "log.append_message", lambda *a: None),
        ):
            self.assertEqual(self.mod.restart_agent("synthetic", saved), "%52")
        launch = next(args[-1] for args in commands if args[0] == "new-window")
        self.assertIn("resume " + self.SID, launch)
        self.assertFalse(any(args[0] == "respawn-pane" for args in commands))
        self.assertEqual(self.saved()["target"], "%52")
        self.assertNotIn("confirmed_session", self.saved())
        self.assertNotIn("window_session", self.saved())

    def test_managed_claude_never_saves_or_resumes_a_reused_windows_conversation(self):
        home = self.root / "claude-home"
        (home / "sessions").mkdir(parents=True)
        project = home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(self.root))
        project.mkdir(parents=True)
        (project / (self.SID + ".jsonl")).write_text("{}\n")
        for pid in ("999", "321"):
            with self.subTest(pid=pid):
                agent = {**self.agent, "kind": "claude"}
                self.mod.write_config({"participants": [{"id": "owner"}, agent]})
                self.mod._WINDOW_SESSION_SEEN.clear()
                (home / "sessions" / (pid + ".json")).write_text(
                    json.dumps(
                        {
                            "pid": int(pid),
                            "procStart": time.strftime("%a %b %d %H:%M:%S %Y", time.gmtime()),
                            "tmux": "synthetic.%51",
                            "sessionId": self.SID,
                        }
                    )
                )
                with (
                    mock.patch.dict(os.environ, {"COLLAB_CLAUDE_HOME": str(home)}),
                    seam(self.mod, "tmuxio.tmux", lambda *a, pid=pid, **k: SimpleNamespace(returncode=0, stdout=pid + "\n")),
                    seam(
                        self.mod,
                        "screen.subprocess.run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout=time.strftime("%a %b %d %H:%M:%S %Y")),
                    ),
                ):
                    self.mod.remember_window_session(agent, "%51")
                    self.assertEqual(self.mod.agent_session(agent), self.SID if pid == "321" else "")
                    if pid == "321":
                        self.assertIn("confirmed_session", self.saved())
                    else:
                        self.assertNotIn("confirmed_session", self.saved())
                        self.assertNotIn("window_session", self.saved())
                with (
                    mock.patch.dict(os.environ, {"COLLAB_CLAUDE_HOME": str(home)}),
                    seam(self.mod, "tmuxio.tmux", lambda *a, **k: SimpleNamespace(returncode=1, stdout="")),
                ):
                    self.assertEqual(self.mod.agent_session(self.saved()), self.SID if pid == "321" else "")
