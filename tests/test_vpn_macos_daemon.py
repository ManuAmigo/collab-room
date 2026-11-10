"""Supervisor fake-child lifecycle and own socketpair leases, without a daemon."""

from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _package import Loader

mod = Loader("mac_daemon_" + uuid.uuid4().hex[:8], str(Path(__file__).resolve().parents[1] / "collab")).load_module()


class MacProxySupervisorTests(unittest.TestCase):
    def setUp(self):
        self.runtime = mock.Mock()
        self.runtime.identity = mod.MacProxyIdentity(8123, 1, mod.MacVpnEndpoint(54321))
        self.runtime.owned.return_value = True
        self.runtime.probe.return_value = "8.8.8.8"
        self.account = mod.VpnAccount(mod.VpnCredentials(str(uuid.uuid4()), "/ws"), "8.8.4.4", None)
        self.supervisor = mod.MacProxySupervisor(self.runtime, Path("/private/sing-box"), self.account)
        self.addCleanup(self.supervisor.revoke)

    def lease(self):
        client, peer = socket.socketpair()
        peer.settimeout(5)
        self.supervisor.admit(client)
        response = mod.mac_control_read(peer)
        self.assertEqual(response["actual"], "ready")
        endpoint = self.runtime.identity.endpoint
        self.assertEqual(response["username"], endpoint.username)
        self.assertEqual(response["password"], endpoint.password)
        self.assertNotIn(endpoint.password, str(self.supervisor.status()))
        stopped = threading.Event()

        def worker():
            try:
                if mod.mac_control_read(peer, timeout=5) == {"stop": True}:
                    stopped.set()
                    mod.mac_control_send(peer, {"stopped": True})
            finally:
                peer.close()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 0.5)
        self.addCleanup(self.supervisor.revoke)
        return stopped

    def test_ready_after_probe_and_lease(self):
        self.supervisor.apply("on")
        self.assertEqual(self.supervisor.status()["actual"], "ready")
        self.runtime.probe.assert_called_once()
        self.lease()
        self.assertEqual(len(self.supervisor.leases), 1)

    def test_death_revokes_lease_before_new_child(self):
        self.supervisor.apply("on")
        peer = self.lease()
        self.runtime.start.side_effect = lambda *_: self.assertTrue(peer.is_set())
        self.supervisor.apply("died")
        self.assertEqual(self.supervisor.state.generation, 2)
        self.assertEqual(self.supervisor.state.actual, "ready")
        self.assertEqual(self.supervisor.leases, [])

    def test_failed_probe_is_bounded_to_three_restarts(self):
        self.runtime.probe.side_effect = ValueError("synthetic probe failure")
        self.supervisor.apply("on")
        self.assertEqual(self.runtime.start.call_count, 4)
        self.assertEqual(self.supervisor.state.restarts, 3)
        self.assertEqual(self.supervisor.state.actual, "failed")
        client, peer = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(peer.close)
        with self.assertRaises(ValueError):
            self.supervisor.admit(client)

    def test_foreign_listener_never_retries(self):
        self.runtime.start.side_effect = mod.MacProxyForeign("foreign listener")
        self.supervisor.apply("on")
        self.assertEqual(self.runtime.start.call_count, 1)
        self.assertEqual(self.supervisor.state.actual, "failed")

    def test_unconfirmed_stop_is_blocked_and_cannot_start(self):
        self.supervisor.apply("on")
        peer = self.lease()
        self.runtime.stop.side_effect = PermissionError("synthetic signal denial")
        with self.assertRaises(PermissionError):
            self.supervisor.apply("off")
        self.assertTrue(peer.is_set())
        self.assertEqual(self.supervisor.status()["actual"], "blocked")
        self.supervisor.apply("on")
        self.assertEqual(self.runtime.start.call_count, 1)
        self.runtime.stop.side_effect = None
        self.supervisor.apply("off")
        self.assertEqual(self.supervisor.status()["actual"], "off")

    def test_oversized_control_request_rejected(self):
        client, peer = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(peer.close)
        peer.sendall(b"x" * 4097)
        with self.assertRaisesRegex(ValueError, "too large"):
            mod.mac_control_read(client)

    def test_drip_control_cannot_delay_proxy_death_detection(self):
        from types import SimpleNamespace
        from _seam import seam

        self.supervisor.apply("on")
        peer = self.lease()
        clock = [0.0]
        payload = iter(b'{"action":"status"}\n')

        def receive(_):
            clock[0] += 0.2
            return bytes([next(payload)])

        client = mock.Mock()
        client.recv.side_effect = receive
        self.runtime.owned.side_effect = lambda: clock[0] == 0
        self.runtime.process.poll.return_value = 1
        fake_time = SimpleNamespace(monotonic=lambda: clock[0], time=lambda: clock[0])
        with seam(mod, "vpn_macos_daemon.time", fake_time):
            mod.mac_proxy_control_client(self.supervisor, client)
        self.assertGreater(self.supervisor.state.generation, 1)
        self.assertTrue(peer.is_set())
        self.assertLessEqual(clock[0], 3.2)

    def test_drip_control_cannot_delay_account_expiry(self):
        from types import SimpleNamespace
        from _seam import seam

        self.supervisor.apply("on")
        peer = self.lease()
        self.supervisor.account = mod.VpnAccount(self.account.credentials, self.account.peer, 1)
        clock = [0.0]
        payload = iter(b'{"action":"status"}\n')

        def receive(_):
            clock[0] += 0.2
            return bytes([next(payload)])

        client = mock.Mock()
        client.recv.side_effect = receive
        with seam(mod, "vpn_macos_daemon.time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: clock[0])):
            mod.mac_proxy_control_client(self.supervisor, client)
        self.assertEqual(self.supervisor.state.desired, "off")
        self.assertTrue(peer.is_set())
        self.assertLessEqual(clock[0], 3.2)

    def test_missing_agent_stop_confirmation_blocks_next_generation(self):
        self.supervisor.apply("on")
        client, peer = socket.socketpair()
        self.addCleanup(peer.close)
        self.supervisor.leases.append(client)
        mod.mac_control_send(peer, {"stopped": False})
        with self.assertRaisesRegex(ValueError, "stop unconfirmed"):
            self.supervisor.apply("off")
        self.assertEqual(self.supervisor.state.actual, "blocked")
        self.assertEqual(self.supervisor.pending_stops, [client])
        self.supervisor.apply("on")
        self.assertEqual(self.runtime.start.call_count, 1)
        mod.mac_control_send(peer, {"stopped": True})
        self.supervisor.apply("off")
        self.assertEqual(self.supervisor.pending_stops, [])
        self.assertEqual(self.supervisor.state.actual, "off")

    def test_cleanup_runs_after_unconfirmed_agent_stop_and_keeps_blocked_marker(self):
        import tempfile
        from types import SimpleNamespace
        from _seam import seam

        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary).resolve()
            supervisor = mock.Mock()
            supervisor.leases = []
            supervisor.pending_stops = []
            supervisor.state = mod.MacProxyState(3, "on", "ready")
            supervisor.revoke.side_effect = ValueError("synthetic unconfirmed stop")
            event = mock.Mock()
            event.is_set.return_value = True
            signals = SimpleNamespace(SIGTERM=15, SIGINT=2, signal=mock.Mock(return_value=None))
            with (
                seam(mod, "vpn_config.vpn_account", mock.Mock(return_value=self.account)),
                seam(mod, "vpn_macos_daemon.MacProxySupervisor", mock.Mock(return_value=supervisor)),
                seam(mod, "vpn_macos_daemon.threading", SimpleNamespace(Event=lambda: event)),
                seam(mod, "vpn_macos_daemon.signal", signals),
            ):
                with self.assertRaises(ValueError):
                    mod.mac_proxy_daemon_locked(folder, Path("/private/binary"), Path("/private/account"))
            supervisor.runtime.stop.assert_called_once()
            self.assertFalse((folder / "control.sock").exists())
            self.assertEqual(signals.signal.call_count, 4)
            self.assertTrue((folder / "unconfirmed.json").exists())

    def test_cleanup_still_restores_socket_when_proxy_stop_also_fails(self):
        import tempfile
        from types import SimpleNamespace
        from _seam import seam

        with tempfile.TemporaryDirectory() as temporary, socket.socket(socket.AF_UNIX) as server:
            folder = Path(temporary).resolve()
            address = folder / "control.sock"
            server.bind(str(address))
            info = address.stat()
            supervisor = mock.Mock()
            supervisor.leases = []
            supervisor.pending_stops = []
            supervisor.revoke.side_effect = RuntimeError("synthetic ACK error")
            supervisor.runtime.stop.side_effect = PermissionError("synthetic signal error")
            signals = SimpleNamespace(signal=mock.Mock())
            with seam(mod, "vpn_macos_daemon.signal", signals), self.assertRaises(ValueError):
                mod.mac_proxy_cleanup(supervisor, folder, address, (info.st_dev, info.st_ino), {15: None, 2: None})
            supervisor.runtime.stop.assert_called_once()
            self.assertFalse(address.exists())
            self.assertEqual(signals.signal.call_count, 2)
            marker = __import__("json").loads((folder / "unconfirmed.json").read_text())
            self.assertEqual(marker["failures"], ["agents", "proxy"])
            with self.assertRaisesRegex(ValueError, "previous supervisor stop"):
                mod.mac_proxy_daemon(folder, Path("/private/binary"), Path("/private/account"))

    def test_confirmed_cleanup_removes_startup_marker(self):
        import tempfile
        from types import SimpleNamespace
        from _seam import seam

        with tempfile.TemporaryDirectory() as temporary, socket.socket(socket.AF_UNIX) as server:
            folder = Path(temporary).resolve()
            address = folder / "control.sock"
            server.bind(str(address))
            info = address.stat()
            supervisor = mock.Mock()
            supervisor.leases = []
            supervisor.pending_stops = []
            mod.mac_proxy_marker(folder, {"kind": "mac-vpn-supervisor", "actual": "unconfirmed"})
            with seam(mod, "vpn_macos_daemon.signal", SimpleNamespace(signal=mock.Mock())):
                mod.mac_proxy_cleanup(supervisor, folder, address, (info.st_dev, info.st_ino), {15: None})
            self.assertFalse((folder / "unconfirmed.json").exists())
            self.assertFalse(address.exists())

    def test_reload_revokes_leases_before_replacing_account(self):
        import json
        import tempfile

        self.supervisor.apply("on")
        stopped = self.lease()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "account.json"
            identifier = "11111111-1111-4111-8111-111111111111"
            path.write_text(
                json.dumps(
                    {
                        "status": "active",
                        "expire": None,
                        "proxies": {"vless": {"id": identifier}},
                        "links": ["vless://" + identifier + "@1.1.1.1:80?type=ws&path=%2Fws"],
                    }
                )
            )
            path.chmod(0o600)
            client, peer = socket.socketpair()
            with peer:
                mod.mac_control_send(peer, {"action": "reload"})
                mod.mac_proxy_control_client(self.supervisor, client, path)
                self.assertEqual(mod.mac_control_read(peer)["actual"], "off")
            self.assertTrue(stopped.is_set())
            self.assertEqual(self.supervisor.account.peer, "1.1.1.1")
            self.supervisor.apply("on")
            self.assertEqual(self.runtime.start.call_args.args[1].peer, "1.1.1.1")

    def test_off_requests_daemon_exit_only_after_confirmed_agent_stop(self):
        self.supervisor.apply("on")
        stopped = self.lease()
        client, peer = socket.socketpair()
        with peer:
            mod.mac_control_send(peer, {"action": "off"})
            should_exit = mod.mac_proxy_control_client(self.supervisor, client)
            self.assertEqual(mod.mac_control_read(peer)["actual"], "off")
        self.assertTrue(stopped.is_set())
        self.assertTrue(should_exit)

    def test_socket_off_removes_daemon_and_releases_singleton_for_next_start(self):
        import tempfile
        import time
        from types import SimpleNamespace
        from _seam import seam

        errors = []
        stop = threading.Event()
        self.addCleanup(stop.set)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory).resolve()

            def run():
                try:
                    mod.mac_proxy_daemon(folder, Path("/unused/binary"), Path("/unused/account"))
                except Exception as error:
                    errors.append(error)

            with (
                seam(mod, "vpn_config.vpn_account", lambda *_: self.account),
                seam(mod, "vpn_macos_service.MacProxyProcess", lambda: self.runtime),
                seam(mod, "vpn_macos_daemon.signal.signal", lambda *_: None),
                seam(mod, "vpn_macos_daemon.threading", SimpleNamespace(Event=lambda: stop)),
            ):
                for _ in range(2):
                    stop.clear()
                    thread = threading.Thread(target=run, daemon=True)
                    thread.start()
                    deadline = time.monotonic() + 3
                    while not (folder / "control.sock").exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    status = mod.mac_control_request(folder, "off")
                    self.assertEqual(status["actual"], "off")
                    self.assertTrue(status["off_exits"])
                    mod.mac_vpn_wait_stopped(folder)
                    thread.join(1)
                    self.assertFalse(thread.is_alive())
                    self.assertFalse((folder / "control.sock").exists())
                    self.assertFalse((folder / "unconfirmed.json").exists())
            self.assertEqual(errors, [])

    def test_status_identifies_standard_account_without_disclosing_custom_path(self):
        standard = Path.home() / ".config/collab-vpn/agents.json"
        self.assertTrue(
            mod.MacProxySupervisor(self.runtime, Path("/unused"), self.account, standard).status()["standard_account"]
        )
        other = mod.MacProxySupervisor(self.runtime, Path("/unused"), self.account, Path("/private/custom-secret.json"))
        self.assertFalse(other.status()["standard_account"])
        self.assertNotIn("custom-secret", str(other.status()))
