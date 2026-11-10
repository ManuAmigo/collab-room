"""Proxy process ownership with a fake child; only own ephemeral bind is real."""

from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _package import Loader
from _seam import seam

mod = Loader("mac_process_" + uuid.uuid4().hex[:8], str(Path(__file__).resolve().parents[1] / "collab")).load_module()


class MacProxyProcessTests(unittest.TestCase):
    def setUp(self):
        self.child = mock.Mock(pid=8123)
        self.child.poll.return_value = None
        self.child.stdin.closed = False
        self.processes = SimpleNamespace(
            Popen=mock.Mock(return_value=self.child),
            PIPE=-1,
            DEVNULL=-3,
            TimeoutExpired=mod.subprocess.TimeoutExpired,
            SubprocessError=mod.subprocess.SubprocessError,
            run=mock.Mock(),
        )
        self.enterContext(seam(mod, "vpn_macos_service.subprocess", self.processes))
        self.owners = mock.Mock(return_value=frozenset({8123}))
        self.enterContext(seam(mod, "vpn_macos_service.mac_proxy_listeners", self.owners))
        self.account = mod.VpnAccount(mod.VpnCredentials(str(uuid.uuid4()), "/ws"), "8.8.4.4", None)
        self.runtime = mod.MacProxyProcess()

    def test_child_receives_secret_only_via_pipe_and_is_closed_fds(self):
        identity = self.runtime.start(Path("/private/sing-box"), self.account, 4)
        args, options = self.processes.Popen.call_args
        self.assertEqual(args[0], ["/private/sing-box", "run", "-c", "/dev/stdin"])
        self.assertTrue(options["close_fds"])
        payload = self.child.stdin.write.call_args.args[0]
        self.assertIn(self.account.credentials.uuid.encode(), payload)
        self.child.stdin.close.assert_called_once()
        self.assertEqual(identity.generation, 4)
        self.assertTrue(self.runtime.owned())

    def test_foreign_listener_terminates_only_owned_child(self):
        self.owners.return_value = frozenset({9999})
        with self.assertRaisesRegex(ValueError, "foreign proxy listener"):
            self.runtime.start(Path("/private/sing-box"), self.account, 1)
        self.child.terminate.assert_called_once()
        self.child.wait.assert_called_once()
        self.assertIsNone(self.runtime.process)

    def test_dead_child_never_passes_listener_identity(self):
        self.runtime.start(Path("/private/sing-box"), self.account, 1)
        self.child.poll.return_value = 1
        self.assertFalse(self.runtime.owned())

    def test_transport_success_needs_ownership_after_probe(self):
        self.runtime.start(Path("/private/sing-box"), self.account, 1)
        self.processes.run.return_value = SimpleNamespace(returncode=0, stdout=b"8.8.8.8")
        self.owners.side_effect = [frozenset({8123}), frozenset({9999})]
        with self.assertRaisesRegex(ValueError, "probe failed"):
            self.runtime.probe()

    def test_probe_and_stop_positive(self):
        self.runtime.start(Path("/private/sing-box"), self.account, 1)
        self.processes.run.return_value = SimpleNamespace(returncode=0, stdout=b"8.8.8.8")
        self.assertEqual(self.runtime.probe(), "8.8.8.8")
        endpoint = self.runtime.identity.endpoint
        arguments, options = self.processes.run.call_args
        self.assertNotIn(endpoint.password, str(arguments))
        self.assertIn(endpoint.password.encode(), options["input"])
        self.runtime.stop()
        self.child.terminate.assert_called_once()
        self.assertIsNone(self.runtime.identity)

    def test_restart_cannot_overwrite_live_child(self):
        self.runtime.start(Path("/private/sing-box"), self.account, 1)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.runtime.start(Path("/private/sing-box"), self.account, 2)
        self.assertEqual(self.processes.Popen.call_count, 1)

    def test_unconfirmed_stop_retains_handle_blocks_start_and_can_retry(self):
        self.runtime.start(Path("/private/sing-box"), self.account, 1)
        identity = self.runtime.identity
        self.child.wait.side_effect = self.processes.TimeoutExpired("child", 3)
        with self.assertRaises(self.processes.TimeoutExpired):
            self.runtime.stop()
        self.assertIs(self.runtime.process, self.child)
        self.assertEqual(self.runtime.identity, identity)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.runtime.start(Path("/private/sing-box"), self.account, 2)
        self.child.wait.side_effect = None
        self.child.wait.return_value = 0
        self.runtime.stop()
        self.assertIsNone(self.runtime.process)
        self.assertIsNone(self.runtime.identity)

    def test_signal_error_retains_child_identity(self):
        self.runtime.start(Path("/private/sing-box"), self.account, 1)
        self.child.terminate.side_effect = PermissionError("synthetic denial")
        with self.assertRaises(PermissionError):
            self.runtime.stop()
        self.assertIs(self.runtime.process, self.child)
        self.assertIsNotNone(self.runtime.identity)

    def test_only_vpn_server_is_resolved_then_numeric_peer_is_pinned(self):
        import json

        self.account = mod.vpn_link(
            "vless://11111111-1111-4111-8111-111111111111@vpn.example.org:443?type=ws&security=tls&path=%2Fprivate&sni=cert.example.org&host=ws.example.org"
        )
        self.processes.run.return_value = SimpleNamespace(returncode=0, stdout=b'["8.8.4.4"]')
        self.runtime.start(Path("/private/sing-box"), self.account, 4)
        argv, options = self.processes.run.call_args
        self.assertEqual(argv[0][-1], "vpn.example.org")
        self.assertEqual(argv[0][1:3], ["-I", "-S"])
        self.assertEqual(options["timeout"], 3)
        self.assertEqual(options["stderr"], self.processes.DEVNULL)
        self.assertNotIn("cert.example.org", str(argv))
        self.assertNotIn("11111111", str(argv))
        self.assertNotIn("private", str(argv))
        payload = json.loads(self.child.stdin.write.call_args.args[0])
        outbound = payload["outbounds"][0]
        self.assertEqual(outbound["server"], "8.8.4.4")
        self.assertEqual(outbound["tls"]["server_name"], "cert.example.org")
        self.assertEqual(outbound["transport"]["headers"]["Host"], "ws.example.org")
        self.assertNotIn("dns", payload)
        self.processes.run.assert_called_once()

    def test_bootstrap_dns_refuses_bad_answers_and_timeout_before_proxy_start(self):
        self.account = mod.vpn_link(
            "vless://11111111-1111-4111-8111-111111111111@vpn.example.org:443?type=ws&security=tls&path=%2Fws"
        )
        for answer in (b"[]", b'["127.0.0.1"]', b'["8.8.4.4","169.254.169.254"]', b'["::1"]', b"{}", b"not json"):
            with self.subTest(answer=answer):
                self.processes.run.return_value = SimpleNamespace(returncode=0, stdout=answer)
                with self.assertRaisesRegex(mod.MacProxyError, "bootstrap DNS"):
                    self.runtime.start(Path("/private/sing-box"), self.account, 1)
                self.assertIsNone(self.runtime.process)
                self.processes.Popen.assert_not_called()
        self.processes.run.side_effect = self.processes.TimeoutExpired("private-value", 3)
        with self.assertRaises(mod.MacProxyError) as failure:
            self.runtime.start(Path("/private/sing-box"), self.account, 1)
        self.assertNotIn("private-value", str(failure.exception))
        self.processes.Popen.assert_not_called()

    def test_numeric_server_never_runs_bootstrap_dns(self):
        self.runtime.start(Path("/private/sing-box"), self.account, 1)
        self.processes.run.assert_not_called()

    def test_dns_child_does_not_hide_answers_beyond_validation_limit(self):
        import subprocess

        addresses = [f"198.51.100.{n}" for n in range(1, 18)]

        def run(argv, **options):
            # Execute the actual DNS child program with only the system lookup replaced.
            prefix = "import socket;socket.getaddrinfo=lambda *_: [(2,1,6,None,(ip,0)) for ip in " + repr(addresses) + "];"
            result = subprocess.run([*argv[:4], prefix + argv[4], *argv[5:]], **options)
            self.assertEqual(result.returncode, 0, "DNS fixture child must execute successfully")
            return result

        self.processes.run.side_effect = run
        with self.assertRaisesRegex(mod.MacProxyError, "bootstrap DNS"):
            mod.mac_vpn_resolve_peer("vpn.example.org")
        self.processes.Popen.assert_not_called()
