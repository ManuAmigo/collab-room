"""The chat VPN menu uses room actions; no host VPN, agent or download is started."""

import argparse
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ledger import LedgerRoom  # noqa: E402
from _seam import seam  # noqa: E402


class ChatVpnTest(LedgerRoom):
    def setUp(self):
        super().setUp()
        self.mod.write_config({"version": 2, "session": "synthetic", "participants": [{"id": "owner"}]})
        self.said = []
        self.ctx = SimpleNamespace(
            wizard={},
            config=self.mod.load_config(),
            args=argparse.Namespace(as_="owner"),
            feed=self.said.append,
            commands=lambda: [("/vpn", "")],
        )

    def step(self, line):
        self.mod.wizard_step(self.ctx, line, self.said)
        deadline = time.monotonic() + 3
        while self.ctx.wizard.get("step") == "install_running" and time.monotonic() < deadline:
            self.mod.pump_install_job(self.ctx)
            time.sleep(0.005)
        self.assertNotEqual(self.ctx.wizard.get("step"), "install_running", "menu work did not finish")

    def test_menu_sets_up_once_then_toggles_without_restarting_agents(self):
        actions = []
        status = {"actual": "off", "room_enabled": False}

        def action(verb):
            actions.append(verb)
            if verb == "setup":
                self.mod.mac_vpn_record_setup()
                status.update(actual="ready", room_enabled=True, restart_required=True)
            elif verb == "off":
                status.update(actual="off")
            elif verb == "on":
                status.update(actual="ready", restart_required=False)
            return dict(status)

        with seam(self.mod, "vpn_macos_room.mac_vpn_room_action", action):
            self.assertIn("/vpn", self.mod.menu_order({}))
            self.step("/vpn")
            self.assertEqual(self.ctx.wizard["step"], "vpn_pick")
            self.assertIn("off", self.ctx.wizard.get("prompt", ""))
            self.step("on")
            self.assertTrue(self.mod.load_config()["macos_vpn"])
            self.assertIn("restart", " ".join(self.said).lower())
            self.step("/vpn")
            self.assertIn("running", self.ctx.wizard.get("prompt", ""))
            self.step("off")
            self.step("/vpn")
            self.step("on")
        self.assertEqual(actions, ["status", "setup", "status", "off", "status", "on"])
        self.assertFalse(self.ctx.wizard)

    def test_first_enable_without_account_is_a_clear_refusal_not_a_download(self):
        status = {"actual": "off", "room_enabled": False}
        self.enterContext(seam(self.mod, "vpn_macos_daemon.mac_vpn_folder", lambda: self.root / "vpn-runtime"))
        self.enterContext(seam(self.mod, "vpn_macos_room.sys.platform", "darwin"))
        install = mock.Mock(side_effect=AssertionError("missing account must not download"))
        self.enterContext(seam(self.mod, "vpn_macos_install.mac_vpn_install", install))
        with seam(self.mod, "vpn_macos_room.mac_vpn_room_action", lambda verb: status):
            self.step("/vpn")
        self.step("on")
        self.assertEqual(self.ctx.wizard.get("step"), "vpn_link")
        self.assertTrue(self.ctx.wizard.get("secret"))
        self.assertFalse(self.mod.load_config().get("macos_vpn", False))
        install.assert_not_called()

    def test_failed_state_is_visible_and_back_changes_nothing(self):
        action = mock.Mock(return_value={"actual": "failed", "desired": "on", "room_enabled": True})
        with seam(self.mod, "vpn_macos_room.mac_vpn_room_action", action):
            self.step("/vpn")
            self.assertIn("failed", self.ctx.wizard.get("prompt", ""))
            self.step("back")
        action.assert_called_once_with("status")
        self.assertFalse(self.ctx.wizard)

    def test_waiting_backend_does_not_block_feed_or_escape_and_cannot_reopen_menu(self):
        entered, release, returned = threading.Event(), threading.Event(), threading.Event()
        lock = threading.RLock()
        self.ctx.lock = lock
        self.ctx.buffer = lambda: []
        self.ctx.ui = SimpleNamespace()
        errors = []

        def waiting(verb):
            entered.set()
            release.wait(3)
            return {"actual": "off", "room_enabled": False}

        def ui_call():
            try:
                with lock:
                    self.mod.wizard_step(self.ctx, "/vpn", self.said)
            except BaseException as exc:
                errors.append(exc)
            finally:
                returned.set()

        with seam(self.mod, "vpn_macos_room.mac_vpn_room_action", waiting):
            worker = threading.Thread(target=ui_call)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(returned.wait(0.5), "VPN I/O held the chat input lock")
                self.assertEqual(errors, [])
                with lock:
                    self.ctx.feed("a new message while VPN waits")
                    result = self.ctx.wizard["install_result"]
                    self.mod.chat_wizard_back(self.ctx)
                    self.assertFalse(self.ctx.wizard)
                release.set()
                deadline = time.monotonic() + 3
                while result["value"] is None and result["error"] is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertIsNotNone(result["value"])
                self.assertFalse(self.mod.pump_install_job(self.ctx))
                self.assertFalse(self.ctx.wizard, "late status cannot reopen a closed menu")
            finally:
                release.set()
                worker.join(4)

    def test_status_counts_live_agents_without_a_matching_vpn_process_birth(self):
        entries = [
            {"id": "native", "kind": "codex", "pane_pid": "10"},
            {"id": "protected", "kind": "claude", "pane_pid": "20", "vpn_process": {"pid": 20}},
            {"id": "reused", "kind": "codex", "pane_pid": "30", "vpn_process": {"pid": 99}},
            {"id": "stopped", "kind": "codex", "pane_pid": "40"},
            {"id": "unknown", "kind": "claude", "pane_pid": "unavailable"},
        ]
        for entry in entries:
            entry["idle"] = ">"
        self.mod.write_config({"macos_vpn": True, "participants": [{"id": "owner"}, *entries]})
        with (
            seam(self.mod, "vpn_macos_room.mac_vpn_room_action", lambda verb: {"actual": "ready", "room_enabled": True}),
            seam(self.mod, "agents.agent_window_alive", lambda p: p["id"] != "stopped"),
            seam(self.mod, "vpn_macos_process.mac_vpn_process_fields", lambda config, pid: {"vpn_process": {"pid": int(pid)}}),
        ):
            self.step("/vpn")
        self.assertIn("3 running Codex/Claude", self.ctx.wizard.get("prompt", ""))
        self.assertIn("/restart", self.ctx.wizard.get("prompt", ""))

    def test_missing_account_opens_hidden_link_field(self):
        self.enterContext(seam(self.mod, "vpn_macos_daemon.mac_vpn_folder", lambda: self.root / "vpn-runtime"))
        self.enterContext(seam(self.mod, "vpn_macos_room.sys.platform", "darwin"))
        status = {"actual": "off", "room_enabled": False}
        with seam(self.mod, "vpn_macos_room.mac_vpn_room_action", lambda verb: status):
            self.step("/vpn")
        self.step("on")
        self.assertEqual(self.ctx.wizard.get("step"), "vpn_link")
        self.assertTrue(self.ctx.wizard.get("secret"))

    def test_private_input_never_enters_feed_or_exit_draft(self):
        import io

        link = "vless://11111111-1111-4111-8111-111111111111@8.8.4.4:80?type=ws&path=%2Fprivate"
        self.mod.vpn_link_prompt(self.ctx)
        self.ctx.buf = list(link)
        self.ctx.buffer = lambda: self.ctx.buf
        self.ctx.ui = SimpleNamespace(pos=len(link), selected=None, menu_index=0, mine_count=0, top=None, popup=0, reading=False)
        self.ctx.rows = lambda: 24
        self.ctx.menu_items = lambda: self.mod.chat_menu_items(self.ctx)
        self.ctx.dm_focus = (False, None)
        self.ctx.draw_bottom = lambda: None
        self.ctx.repaint = lambda: None
        output = io.StringIO()
        with seam(self.mod, "chat_menu.sys.stdout", output), seam(self.mod, "chat_base.input_head", lambda *_: "input"):
            self.mod.chat_render(self.ctx)
        self.assertNotIn("vless", output.getvalue())
        self.assertNotIn("11111111", output.getvalue())
        self.mod.chat_ask_exit(self.ctx)
        self.assertEqual(self.ctx.wizard["draft"], [])
        self.assertEqual(self.ctx.buf, [])
        self.mod.chat_stay_open(self.ctx)
        self.assertTrue(self.ctx.wizard["secret"])
        self.assertEqual(self.ctx.buf, [])
        self.step("/stop")  # A secret field accepts data, never slash commands.
        self.assertEqual(self.ctx.wizard.get("step"), "vpn_link")
        self.assertNotIn("/stop", " ".join(self.said))
        self.assertNotIn("11111111", " ".join(self.said))

    def test_link_is_saved_before_reload_and_setup_in_background(self):
        events = []
        link = "vless://11111111-1111-4111-8111-111111111111@8.8.4.4:80?type=ws&path=%2Fprivate"

        def save(value):
            self.assertEqual(value, link)
            events.append("save")

        def control(folder, action):
            events.append(action)
            return {"actual": "off", "account_reload": True, "standard_account": True}

        def action(verb):
            events.append(verb)
            return {"actual": "ready", "room_enabled": True}

        with (
            seam(self.mod, "vpn_macos_room.mac_vpn_save_link", save),
            seam(self.mod, "vpn_macos_daemon.mac_control_request", control),
            seam(self.mod, "vpn_macos_daemon.mac_vpn_folder", lambda: self.root),
            seam(self.mod, "vpn_macos_room.mac_vpn_room_action", action),
        ):
            self.mod.vpn_link_prompt(self.ctx)
            self.step(link)
        self.assertEqual(events, ["status", "save", "reload", "setup"])
        self.assertNotIn("11111111", " ".join(self.said))
        self.assertFalse(self.ctx.wizard)

    def test_noninteractive_chat_never_offers_secret_entry(self):
        self.ctx.tty = False
        self.mod.vpn_link_prompt(self.ctx)
        self.assertFalse(self.ctx.wizard)
        self.assertIn("interactive", " ".join(self.said))

    def test_old_supervisor_refuses_change_before_writing_account(self):
        save = mock.Mock()
        with (
            seam(self.mod, "vpn_macos_room.mac_vpn_save_link", save),
            seam(self.mod, "vpn_macos_daemon.mac_control_request", lambda *_: {"actual": "ready"}),
            seam(self.mod, "vpn_macos_daemon.mac_vpn_folder", lambda: self.root),
            seam(self.mod, "vpn_macos_room.mac_vpn_room_action", lambda *_: {"actual": "ready", "room_enabled": True}),
        ):
            self.mod.vpn_link_prompt(self.ctx)
            self.step("vless://synthetic")
        save.assert_not_called()
        self.assertIn("supervisor", " ".join(self.said))
        self.assertNotIn("vless://synthetic", " ".join(self.said))

    def test_nonstandard_account_refuses_change_before_saving(self):
        save = mock.Mock()
        with (
            seam(self.mod, "vpn_macos_room.mac_vpn_save_link", save),
            seam(
                self.mod,
                "vpn_macos_daemon.mac_control_request",
                lambda *_: {"actual": "ready", "account_reload": True, "standard_account": False},
            ),
            seam(self.mod, "vpn_macos_daemon.mac_vpn_folder", lambda: self.root),
            seam(self.mod, "vpn_macos_room.mac_vpn_room_action", lambda *_: {"actual": "ready", "room_enabled": True}),
        ):
            self.mod.vpn_link_prompt(self.ctx)
            self.step("vless://synthetic")
        save.assert_not_called()
        self.assertIn("different account", " ".join(self.said))
