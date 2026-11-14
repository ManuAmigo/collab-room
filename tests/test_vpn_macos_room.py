"""Opt-in launch routes with fake tmux and a private HOME; no installed CLI runs."""

import os
from pathlib import Path
import shlex
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _package import Loader
from _seam import seam


@unittest.skipUnless(sys.platform == "darwin", "macOS launch routing")
class MacVpnRoomRouteTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.home), "COLLAB_INVITES": str(self.home / "invites")}))
        self.mod = Loader(
            "mac_route_" + uuid.uuid4().hex[:8],
            os.environ.get("COLLAB_VPN_TEST_CLI", str(Path(__file__).resolve().parents[1] / "collab")),
        ).load_module()
        self.project = self.home / "project"
        self.project.mkdir()
        self.mod.use_room(str(self.project))
        self.config = {
            "session": "vpn-test",
            "macos_vpn": True,
            "participants": [{"id": "driver", "command": [sys.executable, self.mod.SELF, "chat", "--as", "driver"]}],
        }
        self.mod.write_config(self.config)
        self.rules = mock.Mock(return_value="")
        self.real_agent_command = self.mod.agent_command

        def fake_tmux(*arguments, **_):
            if arguments[0] == "display-message":
                text = str(os.getpid())
            elif "#{pane_id} #{pane_pid}" in arguments:
                text = "%5 " + str(os.getpid())
            else:
                text = "%5"
            return SimpleNamespace(returncode=0, stdout=text, stderr="")

        self.tmux = mock.Mock(side_effect=fake_tmux)
        for owner, name, replacement in (
            ("tmuxio", "tmux", self.tmux),
            ("agents", "agent_command", mock.Mock(return_value="/private/codex --no-alt-screen")),
            ("installer", "codex_room_rule", self.rules),
            ("agents", "add_participant", mock.Mock()),
            ("screen", "write_status", mock.Mock()),
            ("log", "append_message", mock.Mock()),
        ):
            self.enterContext(seam(self.mod, owner + "." + name, replacement))

    def test_room_policy_reaches_every_launch_route(self):
        participant = {
            "id": "probe",
            "kind": "codex",
            "folder": str(self.project),
            "started_by_room": True,
            "target": "%5",
            "pane_pid": "8123",
        }
        self.config["participants"].append(participant)
        with (
            seam(self.mod, "agents.agent_command", self.real_agent_command),
            seam(self.mod, "agents.codex_knows_no_daemon", mock.Mock(return_value=False)),
            seam(self.mod, "agents.agent_window_alive", mock.Mock(return_value=True)),
        ):
            for vpn in (False, True):
                for sandbox in (False, True):
                    self.config.update(macos_vpn=vpn, team={"codex_sandbox": sandbox})
                    self.mod.write_config(self.config)
                    routes = {
                        "add": lambda: self.mod.launch_new_agent(self.config, "codex", str(self.project), "New"),
                        "relaunch": lambda: self.mod.relaunch_agent("vpn-test", participant),
                        "restart": lambda: self.mod.restart_in_window("vpn-test", participant, "codex", ""),
                        "resume": lambda: self.mod.restart_in_window("vpn-test", participant, "codex", "session-id"),
                        "initial": lambda: self.mod.open_room_windows(
                            "vpn-test", self.config, [{**participant, "command": ["codex", "--no-alt-screen"]}], [], [], {}
                        ),
                    }
                    for route, launch in routes.items():
                        with self.subTest(vpn=vpn, sandbox=sandbox, route=route):
                            self.tmux.reset_mock()
                            if vpn and sandbox:
                                with self.assertRaisesRegex(ValueError, "VPN"):
                                    launch()
                                self.assertFalse(
                                    any(
                                        c.args[0] in ("new-window", "respawn-pane", "new-session")
                                        for c in self.tmux.call_args_list
                                    )
                                )
                                continue
                            launch()
                            command = next(
                                c.args[-1]
                                for c in self.tmux.call_args_list
                                if c.args[0] in ("new-window", "respawn-pane", "new-session")
                            )
                            argv = shlex.split(command)
                            self.assertEqual(argv.count("--dangerously-bypass-approvals-and-sandbox"), 0 if sandbox else 1)
                            self.assertEqual("vpn-agent" in argv, vpn)
                            if sandbox:
                                self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
                                self.assertEqual(argv[argv.index("--ask-for-approval") + 1], "on-request")

    def test_legacy_command_keeps_explicit_options_until_room_choice(self):
        self.config["macos_vpn"] = False
        for flag in ("--dangerously-bypass-approvals-and-sandbox", "--sandbox=read-only", "--full-auto"):
            with self.subTest(flag=flag):
                self.tmux.reset_mock()
                self.mod.open_room_windows(
                    "vpn-test", self.config, [{"id": "probe", "kind": "codex", "command": ["codex", flag]}], [], [], {}
                )
                command = next(c.args[-1] for c in self.tmux.call_args_list if c.args[0] == "new-session")
                self.assertIn(flag, shlex.split(command))
                if flag != "--dangerously-bypass-approvals-and-sandbox":
                    self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", shlex.split(command))

    def test_explicit_room_choice_replaces_legacy_options_preserving_arguments(self):
        self.config["macos_vpn"] = False
        for sandbox in (False, True):
            self.config["team"] = {"codex_sandbox": sandbox}
            self.tmux.reset_mock()
            self.mod.open_room_windows(
                "vpn-test",
                self.config,
                [
                    {
                        "id": "probe",
                        "kind": "codex",
                        "command": [
                            "codex",
                            "--full-auto",
                            "--dangerously-bypass-approvals-and-sandbox",
                            "--sandbox=read-only",
                            "-a",
                            "never",
                            "--model",
                            "example",
                            "--",
                            "prompt with spaces",
                        ],
                    }
                ],
                [],
                [],
                {},
            )
            command = next(c.args[-1] for c in self.tmux.call_args_list if c.args[0] == "new-session")
            argv = shlex.split(command)
            self.assertEqual("--dangerously-bypass-approvals-and-sandbox" in argv, not sandbox)
            self.assertNotIn("--full-auto", argv)
            self.assertNotIn("--sandbox=read-only", argv)
            self.assertNotIn("never", argv)
            self.assertEqual(argv[-4:], ["--model", "example", "--", "prompt with spaces"])
            if sandbox:
                self.assertIn("workspace-write", argv)
                self.assertIn("on-request", argv)

    def test_legacy_sandbox_resume_and_vpn_preserve_the_choice(self):
        commands = [
            ["codex", "resume", "-s", "read-only", "-a", "on-request", "sid"],
            ["codex", "resume", "--sandbox=read-only", "--ask-for-approval=on-request", "sid"],
            ["codex", "resume", "-sread-only", "-aon-request", "sid"],
        ]
        for command in commands:
            participant = {"id": "probe", "kind": "codex", "command": command}
            self.config["participants"] = [self.config["participants"][0], participant]
            self.config.pop("team", None)
            self.config["macos_vpn"] = False
            self.mod.write_config(self.config)
            self.tmux.reset_mock()
            self.mod.open_room_windows("vpn-test", self.config, [participant], [], [], {})
            actual = next(c.args[-1] for c in self.tmux.call_args_list if c.args[0] == "new-session")
            self.assertEqual(shlex.split(actual)[-(len(command) - 1) :], command[1:])
            self.config["macos_vpn"] = True
            self.tmux.reset_mock()
            with self.assertRaisesRegex(ValueError, "VPN"):
                self.mod.open_room_windows("vpn-test", self.config, [participant], [], [], {})
            self.tmux.assert_not_called()
            self.config["team"] = {"codex_sandbox": False}
            self.mod.write_config(self.config)
            self.mod.open_room_windows("vpn-test", self.config, [participant], [], [], {})
            actual = next(c.args[-1] for c in self.tmux.call_args_list if c.args[0] == "new-session")
            argv = shlex.split(actual)
            self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
            self.assertIn("resume", argv)
            self.assertEqual(argv[-1], "sid")

    def test_safety_words_after_separator_are_only_prompt_text(self):
        self.config["macos_vpn"] = True
        self.config["participants"].append({"id": "probe", "kind": "codex"})
        self.mod.write_config(self.config)
        self.mod.open_room_windows(
            "vpn-test",
            self.config,
            [{"id": "probe", "kind": "codex", "command": ["codex", "--", "--sandbox=read-only"]}],
            [],
            [],
            {},
        )
        actual = next(c.args[-1] for c in self.tmux.call_args_list if c.args[0] == "new-session")
        self.assertEqual(shlex.split(actual)[-2:], ["--", "--sandbox=read-only"])
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", shlex.split(actual))

    def test_add_uses_network_wrapper_when_room_enabled(self):
        self.mod.launch_new_agent(self.config, "codex", str(self.project), "Probe")
        command = next(call.args[-1] for call in self.tmux.call_args_list if call.args[0] == "new-window")
        self.assertIn("vpn-agent", command)
        self.assertIn("--kind codex", command)
        self.rules.assert_not_called()
        self.assertNotIn("sandbox-exec", command, "only wrapper acquires the verified lease at runtime")

    def test_disabled_room_keeps_previous_launch(self):
        self.config.pop("macos_vpn")
        self.mod.write_config(self.config)
        self.mod.launch_new_agent(self.config, "codex", str(self.project), "Probe")
        command = next(call.args[-1] for call in self.tmux.call_args_list if call.args[0] == "new-window")
        self.assertNotIn("vpn-agent", command)
        self.assertIn("/private/codex --no-alt-screen", command)

    def test_off_makes_the_next_add_direct_only_after_confirmed_stop(self):
        with (
            seam(self.mod, "vpn_macos_daemon.mac_vpn_folder", mock.Mock(return_value=self.home)),
            seam(self.mod, "vpn_macos_daemon.mac_control_request", mock.Mock(return_value={"actual": "off", "off_exits": True})),
            seam(self.mod, "vpn_macos_room.mac_vpn_wait_stopped", mock.Mock()) as stopped,
        ):
            result = self.mod.mac_vpn_room_action("off")
        stopped.assert_called_once_with(self.home)
        self.assertFalse(result["room_enabled"])
        self.assertFalse(self.mod.read_config_raw()["macos_vpn"])
        self.mod.launch_new_agent(self.config, "codex", str(self.project), "Probe")
        command = next(call.args[-1] for call in self.tmux.call_args_list if call.args[0] == "new-window")
        self.assertNotIn("vpn-agent", command)
        self.assertIn("/private/codex --no-alt-screen", command)

    def test_failed_off_keeps_new_launches_protected(self):
        for status in ({"actual": "blocked"}, {"actual": "ready"}):
            with (
                self.subTest(status=status),
                seam(self.mod, "vpn_macos_daemon.mac_vpn_folder", mock.Mock(return_value=self.home)),
                seam(self.mod, "vpn_macos_daemon.mac_control_request", mock.Mock(return_value=status)),
            ):
                with self.assertRaises(ValueError):
                    self.mod.mac_vpn_room_action("off")
            self.assertTrue(self.mod.read_config_raw()["macos_vpn"])
        with (
            seam(self.mod, "vpn_macos_daemon.mac_vpn_folder", mock.Mock(return_value=self.home)),
            seam(self.mod, "vpn_macos_daemon.mac_control_request", mock.Mock(return_value={"actual": "off", "off_exits": True})),
            seam(self.mod, "vpn_macos_room.mac_vpn_wait_stopped", mock.Mock(side_effect=ValueError("not stopped"))),
        ):
            with self.assertRaises(ValueError):
                self.mod.mac_vpn_room_action("off")
        self.assertTrue(self.mod.read_config_raw()["macos_vpn"])

    def test_relaunch_uses_wrapper(self):
        participant = {"id": "probe", "kind": "codex", "folder": str(self.project), "started_by_room": True}
        self.config["participants"].append(participant)
        self.mod.write_config(self.config)
        self.mod.relaunch_agent("vpn-test", participant)
        command = next(call.args[-1] for call in self.tmux.call_args_list if call.args[0] == "new-window")
        self.assertIn("vpn-agent", command)

    def test_wrapper_keeps_quoted_arguments(self):
        command = self.mod.mac_vpn_wrap("claude", shlex.join(["/private/claude", "argument with spaces"]), self.config)
        args = shlex.split(command)
        self.assertEqual(args[-2:], ["/private/claude", "argument with spaces"])
        self.assertEqual(args[args.index("vpn-agent") + 1 : args.index("--")], ["--kind", "claude"])

    def test_bad_enabled_value_is_refused(self):
        self.config["macos_vpn"] = "true"
        with self.assertRaises((ValueError, SystemExit)):
            self.mod.mac_vpn_wrap("codex", "/private/codex", self.config)

    def test_restart_uses_wrapper(self):
        participant = {"id": "probe", "kind": "codex", "folder": str(self.project), "target": "%5", "pane_pid": "8123"}
        self.config["participants"].append(participant)
        self.mod.write_config(self.config)
        with seam(self.mod, "agents.agent_window_alive", mock.Mock(return_value=True)):
            self.mod.restart_in_window("vpn-test", participant, "codex", "")
        command = next(call.args[-1] for call in self.tmux.call_args_list if call.args[0] == "respawn-pane")
        self.assertIn("vpn-agent", command)
        self.rules.assert_not_called()

    def test_initial_agent_pane_uses_wrapper_but_chat_does_not(self):
        participant = {"id": "probe", "kind": "codex", "command": ["/private/codex", "--no-alt-screen"]}
        self.config["participants"].append(participant)
        self.mod.write_config(self.config)
        self.mod.open_room_windows("vpn-test", self.config, [participant], [], [], {})
        command = next(call.args[-1] for call in self.tmux.call_args_list if call.args[0] == "new-session")
        self.assertIn("vpn-agent", command)
        self.tmux.reset_mock()
        self.mod.open_room_windows("vpn-test", self.config, [self.config["participants"][0]], [], [], {})
        command = next(call.args[-1] for call in self.tmux.call_args_list if call.args[0] == "new-session")
        self.assertNotIn("vpn-agent", command)

    def test_refresh_stopped_vpn_codex_does_not_write_host_rules(self):
        participant = {
            "id": "probe",
            "kind": "codex",
            "folder": str(self.project),
            "started_by_room": True,
            "cli": "/private/old-copy",
        }
        self.config["participants"].append(participant)
        self.mod.write_config(self.config)
        with seam(self.mod, "agents.agent_window_alive", mock.Mock(return_value=False)):
            self.mod.refresh_codex_rules()
        self.rules.assert_not_called()
        self.assertNotIn("cli", self.mod.read_config_raw()["participants"][-1])

    def test_refresh_disabled_room_preserves_native_rule_update(self):
        self.config.pop("macos_vpn")
        self.config["participants"].append({"id": "probe", "kind": "codex", "started_by_room": True, "cli": "/private/old-copy"})
        self.mod.write_config(self.config)
        self.rules.return_value = "/private/new-copy"
        with seam(self.mod, "agents.agent_window_alive", mock.Mock(return_value=False)):
            self.mod.refresh_codex_rules()
        self.rules.assert_called_once()
        self.assertEqual(self.mod.read_config_raw()["participants"][-1]["cli"], "/private/new-copy")

    def test_refresh_running_legacy_codex_keeps_old_hint_without_writing_rule(self):
        self.config["participants"].append({"id": "probe", "kind": "codex", "started_by_room": True, "cli": "/private/old-copy"})
        self.mod.write_config(self.config)
        with seam(self.mod, "agents.agent_window_alive", mock.Mock(return_value=True)):
            self.mod.refresh_codex_rules()
        self.rules.assert_not_called()
        self.assertEqual(self.mod.read_config_raw()["participants"][-1]["cli"], "/private/old-copy")


@unittest.skipUnless(sys.platform == "darwin", "macOS room setup")
class MacVpnRoomActionTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.folder)}))
        self.mod = Loader("mac_action_" + uuid.uuid4().hex[:8], str(Path(__file__).resolve().parents[1] / "collab")).load_module()
        self.config = {"participants": [{"id": "driver", "command": ["/usr/bin/true"]}]}
        self.enterContext(seam(self.mod, "store.load_config", mock.Mock(side_effect=lambda: self.config)))
        self.enterContext(seam(self.mod, "vpn_macos_daemon.mac_vpn_folder", mock.Mock(return_value=self.folder)))
        self.control = self.enterContext(
            seam(self.mod, "vpn_macos_daemon.mac_control_request", mock.Mock(return_value={"actual": "ready"}))
        )
        self.install = self.enterContext(
            seam(self.mod, "vpn_macos_install.mac_vpn_install", mock.Mock(return_value=self.folder / "sing-box"))
        )
        self.ensure = self.enterContext(seam(self.mod, "vpn_macos_room.mac_vpn_ensure", mock.Mock()))
        self.record = self.enterContext(
            seam(
                self.mod,
                "vpn_macos_room.mac_vpn_record_enabled",
                mock.Mock(side_effect=lambda value: self.config.update(macos_vpn=value)),
            )
        )
        self.account = self.enterContext(seam(self.mod, "vpn_config.vpn_account", mock.Mock()))

    def test_setup_records_opt_in_only_after_ready(self):
        result = self.mod.mac_vpn_room_action("setup")
        self.assertTrue(result["room_enabled"])
        self.assertTrue(result["restart_required"])
        self.record.assert_called_once_with(True)
        self.install.assert_called_once()
        self.control.assert_called_once_with(self.folder, "on")
        self.assertEqual(self.account.call_args.args[0], self.folder / ".config/collab-vpn/agents.json")

    def test_failed_setup_cannot_enable_room(self):
        self.control.return_value = {"actual": "failed"}
        with self.assertRaises(ValueError):
            self.mod.mac_vpn_room_action("setup")
        self.record.assert_not_called()

    def test_on_without_setup_never_starts_process(self):
        with self.assertRaises(ValueError):
            self.mod.mac_vpn_room_action("on")
        self.ensure.assert_not_called()
        self.install.assert_not_called()

    def test_off_has_no_installer_or_direct_launch(self):
        self.config["macos_vpn"] = True
        self.control.return_value = {"actual": "off"}
        self.assertEqual(self.mod.mac_vpn_room_action("off")["actual"], "off")
        self.install.assert_not_called()
        self.ensure.assert_not_called()
        self.control.assert_called_once_with(self.folder, "off")

    def test_repeated_off_is_idempotent_but_keeps_unconfirmed_cleanup_red(self):
        self.config["macos_vpn"] = True
        self.control.side_effect = FileNotFoundError()
        self.assertEqual(self.mod.mac_vpn_room_action("off")["actual"], "off")
        self.assertEqual(self.mod.mac_vpn_room_action("off")["actual"], "off")
        (self.folder / "unconfirmed.json").write_text("{}")
        with self.assertRaises(FileNotFoundError):
            self.mod.mac_vpn_room_action("off")
        self.install.assert_not_called()
        self.ensure.assert_not_called()

    def test_status_does_not_start_missing_daemon(self):
        self.control.side_effect = FileNotFoundError()
        self.assertEqual(self.mod.mac_vpn_room_action("status")["actual"], "off")
        (self.folder / "unconfirmed.json").write_text("{}")
        self.assertEqual(self.mod.mac_vpn_room_action("status")["actual"], "blocked")
        self.install.assert_not_called()
        self.ensure.assert_not_called()

    def test_vpn_setup_parser_is_room_scoped(self):
        args = self.mod.build_parser().parse_args(["vpn", "setup"])
        self.assertEqual(args.action, "setup")
        self.assertEqual(args.participant, "")
