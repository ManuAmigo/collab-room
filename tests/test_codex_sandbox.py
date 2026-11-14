"""Room launch policy is explicit and survives a saved menu change."""

import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ledger import LedgerRoom  # noqa: E402
from _seam import seam  # noqa: E402


class CodexSandboxTest(LedgerRoom):
    def test_default_new_and_resumed_commands_bypass_without_vpn(self):
        with seam(self.mod, "agents.codex_knows_no_daemon", mock.Mock(return_value=False)):
            for resume in (None, ["resume", "session-id"]):
                argv = shlex.split(self.mod.agent_command("codex", resume))
                self.assertEqual(argv.count("--dangerously-bypass-approvals-and-sandbox"), 1)

    def test_opt_out_explicitly_restores_sandbox_and_approvals(self):
        with seam(self.mod, "agents.codex_knows_no_daemon", mock.Mock(return_value=False)):
            for resume in (None, ["resume", "session-id"]):
                argv = shlex.split(self.mod.agent_command("codex", resume, codex_sandbox=True))
                self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
                self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
                self.assertEqual(argv[argv.index("--ask-for-approval") + 1], "on-request")

    def test_room_menu_setting_is_saved_validated_and_undone(self):
        self.mod.write_config({"participants": [{"id": "owner"}]})
        self.assertIn("codex_sandbox", self.mod.ROOM_FIELDS)
        self.assertEqual(self.mod.chosen_value("codex_sandbox", "on", {}), True)
        ctx = SimpleNamespace(args=SimpleNamespace(as_="owner"), wizard={}, config=self.mod.load_config(), feed=lambda _: None)
        said = []
        for line in ("/team", "room", "field:codex_sandbox", "on", "go"):
            self.mod.wizard_step(ctx, line, said)
        rec = self.mod.read_changes()[-1]
        self.assertIs(self.mod.team_of(self.mod.load_config())["codex_sandbox"], True)
        self.assertTrue(self.mod.change_problem(self.mod.load_config(), "room", "codex_sandbox", "false"))
        self.mod.undo_changes(rec["id"], "owner")
        self.assertNotIn("codex_sandbox", self.mod.team_of(self.mod.load_config()))

    def test_vpn_cannot_silently_override_opt_out(self):
        with self.assertRaisesRegex(ValueError, "VPN"):
            self.mod.mac_vpn_wrap(
                "codex",
                "/private/codex --sandbox workspace-write",
                {
                    "macos_vpn": True,
                    "team": {"codex_sandbox": True},
                },
            )

    def test_scenarios_preserve_explicit_room_policy_for_every_codex_launch(self):
        for sandbox in (True, False):
            for proposal in (False, True):
                with self.subTest(sandbox=sandbox, proposal=proposal):
                    self.mod.write_config({"participants": [{"id": "owner"}], "team": {"codex_sandbox": sandbox}})
                    seen = []

                    def launch(config, kind, folder, label, sandbox=sandbox, seen=seen, **kwargs):
                        if kind == "codex":
                            saved = self.mod.team_of(config).get("codex_sandbox")
                            self.assertIs(saved, sandbox)
                            argv = shlex.split(self.mod.agent_command(kind, codex_sandbox=saved))
                            self.assertEqual("--dangerously-bypass-approvals-and-sandbox" in argv, not sandbox)
                            seen.append(argv)
                        return label.lower().replace(" ", "-")

                    with (
                        seam(self.mod, "agents.launch_new_agent", launch),
                        seam(self.mod, "agents.codex_knows_no_daemon", mock.Mock(return_value=False)),
                        seam(
                            self.mod,
                            "log.read_messages",
                            mock.Mock(
                                return_value=[
                                    {
                                        "id": "proposal-id",
                                        "from": "owner",
                                        "proposal": {"team": "development"},
                                    }
                                ]
                            ),
                        ),
                        seam(self.mod, "store.proposal_state", mock.Mock(return_value={"proposal-id": "pending"})),
                        seam(self.mod, "store.set_proposal_state", mock.Mock()),
                    ):
                        if proposal:
                            self.mod.apply_proposal("proposal-id", "owner")
                        else:
                            self.mod.start_scenario("development", str(self.root))
                    self.assertTrue(seen)
                    self.assertIs(self.mod.team_of(self.mod.load_config())["codex_sandbox"], sandbox)

    def test_custom_command_is_not_given_codex_cli_flags(self):
        command = [sys.executable, str(self.root / "custom.py"), "custom argument"]
        calls = []
        self.mod.write_config({"participants": [{"id": "owner"}]})
        with (
            seam(self.mod, "tmuxio.tmux", mock.Mock(return_value=SimpleNamespace(returncode=0, stdout="%5", stderr=""))),
            seam(self.mod, "tmuxio.in_dir", lambda folder, program: calls.append(shlex.split(program)) or program),
        ):
            self.mod.open_room_windows(
                "synthetic", self.mod.load_config(), [{"id": "probe", "kind": "codex", "command": command}], [], [], {}
            )
        self.assertEqual(calls, [command])
