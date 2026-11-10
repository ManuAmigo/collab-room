"""macOS network-only policy construction; no host proxy or owner credentials."""

import json
import socket
import subprocess
import os
from pathlib import Path
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _package import Loader

mod = Loader(
    "mac_vpn_" + uuid.uuid4().hex[:8], os.environ.get("COLLAB_VPN_TEST_CLI", str(Path(__file__).resolve().parents[1] / "collab"))
).load_module()


class MacVpnPolicyTests(unittest.TestCase):
    def test_profile_limits_network_to_one_port_and_blocks_system_dns(self):
        profile = mod.mac_vpn_profile(mod.MacVpnEndpoint(54321))
        self.assertIn("(deny network*)", profile)
        self.assertEqual(profile.count("(allow network-outbound"), 1)
        self.assertIn('(remote tcp "localhost:54321")', profile)
        self.assertIn("com.apple.dnssd.service", profile)
        self.assertIn("/var/run/mDNSResponder", profile)
        self.assertIn("(allow default)", profile)

    def test_invalid_ports_cannot_inject_policy(self):
        for value in (True, 0, 80, 65536, '1234") (allow network*)'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                mod.MacVpnEndpoint(value)

    def test_proxy_environment_replaces_bypass_and_preserves_other_settings(self):
        env = {"HTTPS_PROXY": "old", "No_Proxy": "*", "no_proxy": "*", "PATH": "/bin"}
        endpoint = mod.MacVpnEndpoint(43210)
        result = mod.mac_vpn_environment(env, endpoint)
        self.assertNotIn("No_Proxy", result)
        self.assertEqual(result["NO_PROXY"], "")
        self.assertEqual(result["no_proxy"], "")
        self.assertEqual(result["PATH"], "/bin")
        self.assertEqual(result["https_proxy"], endpoint.url)
        self.assertEqual(env["no_proxy"], "*")

    def test_launch_changes_codex_only_inside_outer_policy(self):
        endpoint = mod.MacVpnEndpoint(43210)
        for kind in ("codex", "claude"):
            argv = mod.mac_vpn_command(kind, ("/bin/agent", "prompt with spaces"), endpoint)
            self.assertEqual(argv[:2], ("/usr/bin/sandbox-exec", "-p"))
            self.assertEqual(argv[-1], "prompt with spaces")
            self.assertEqual("--dangerously-bypass-approvals-and-sandbox" in argv, kind == "codex")
        with self.assertRaises(ValueError):
            mod.mac_vpn_command("kimi", ("/bin/agent",), endpoint)

    def test_config_has_only_vless_route_and_no_local_resolver(self):
        secret = mod.VpnCredentials(str(uuid.uuid4()), "/private-ws")
        account = mod.VpnAccount(secret, "8.8.4.4", None)
        config = mod.mac_vpn_config(account, mod.MacVpnEndpoint(54321))
        self.assertNotIn(secret.uuid, repr(config))
        data = json.loads(config.payload)
        self.assertNotIn("dns", data)
        self.assertEqual(data["route"], {"final": "proxy"})
        self.assertEqual(len(data["outbounds"]), 1)
        self.assertEqual(data["outbounds"][0]["type"], "vless")
        self.assertEqual(data["outbounds"][0]["server"], account.peer)
        self.assertEqual(data["inbounds"][0]["listen"], "127.0.0.1")

    def test_tls_keeps_certificate_verification_and_ws_host(self):
        account = mod.vpn_link(
            "vless://11111111-1111-4111-8111-111111111111@8.8.4.4:443?type=ws&security=tls&path=%2Fws&sni=cert.example.org&host=ws.example.org"
        )
        config = json.loads(mod.mac_vpn_config(account, mod.MacVpnEndpoint(54321)).payload)
        outbound = config["outbounds"][0]
        self.assertEqual(outbound["server_port"], 443)
        self.assertEqual(outbound["tls"], {"enabled": True, "server_name": "cert.example.org", "insecure": False})
        self.assertEqual(outbound["transport"]["headers"], {"Host": "ws.example.org"})
        self.assertNotIn("dns", config)

    def test_domain_keeps_ws_host_after_numeric_bootstrap(self):
        account = mod.vpn_link(
            "vless://11111111-1111-4111-8111-111111111111@vpn.example.org:8443?type=ws&security=tls&path=%2Fws"
        )
        config = json.loads(mod.mac_vpn_config(account, mod.MacVpnEndpoint(54321), "8.8.4.4").payload)
        outbound = config["outbounds"][0]
        self.assertEqual(outbound["server"], "8.8.4.4")
        self.assertEqual(outbound["server_port"], 8443)
        self.assertEqual(outbound["tls"]["server_name"], "vpn.example.org")
        self.assertEqual(outbound["transport"]["headers"], {"Host": "vpn.example.org:8443"})

    def test_ws_authority_default_port_follows_tls_scheme(self):
        for security, port, expected in (
            ("none", 443, "vpn.example.org:443"),
            ("tls", 80, "vpn.example.org:80"),
            ("none", 80, "vpn.example.org"),
            ("tls", 443, "vpn.example.org"),
        ):
            with self.subTest(security=security, port=port):
                account = mod.vpn_link(
                    f"vless://11111111-1111-4111-8111-111111111111@vpn.example.org:{port}?type=ws&security={security}&path=%2Fws"
                )
                data = json.loads(mod.mac_vpn_config(account, mod.MacVpnEndpoint(54321), "8.8.4.4").payload)
                self.assertEqual(data["outbounds"][0]["transport"]["headers"]["Host"], expected)

    def test_proxy_configuration_requires_private_generation_credentials(self):
        account = mod.VpnAccount(mod.VpnCredentials(str(uuid.uuid4()), "/private-ws"), "8.8.4.4", None)
        first, second = mod.MacVpnEndpoint(54321), mod.MacVpnEndpoint(54321)
        users = json.loads(mod.mac_vpn_config(account, first).payload)["inbounds"][0].get("users", [])
        self.assertEqual(len(users), 1, "local clients must authenticate")
        self.assertTrue(users[0]["username"])
        self.assertGreaterEqual(len(users[0]["password"]), 32)
        self.assertNotEqual(users, json.loads(mod.mac_vpn_config(account, second).payload)["inbounds"][0]["users"])
        self.assertNotIn(users[0]["password"], repr(first))
        environment = mod.mac_vpn_environment({}, first)
        self.assertIn(users[0]["password"], environment["HTTPS_PROXY"])

    def test_release_pins_platform_architecture(self):
        for machine, arch in (("x86_64", "amd64"), ("arm64", "arm64")):
            release = mod.mac_vpn_release(machine)
            self.assertEqual(release.architecture, arch)
            self.assertEqual(len(release.sha256), 64)
            self.assertTrue(release.url.startswith("https://github.com/SagerNet/sing-box/releases/"))
        with self.assertRaises(ValueError):
            mod.mac_vpn_release("unknown")

    @unittest.skipUnless(sys.platform == "darwin", "Seatbelt requires macOS")
    def test_kernel_allows_only_selected_listener(self):
        with socket.socket() as allowed, socket.socket() as other:
            for listener in (allowed, other):
                listener.bind(("127.0.0.1", 0))
                listener.listen()
            port = allowed.getsockname()[1]
            other_port = other.getsockname()[1]
            code = (
                "import socket,json;out=[];"
                f"ports=({port},{other_port});"
                "\nfor p in ports:\n s=socket.socket(); s.settimeout(2);out.append(s.connect_ex(('127.0.0.1',p)));s.close()"
                "\nprint(json.dumps(out))"
            )
            result = subprocess.run(
                ["/usr/bin/sandbox-exec", "-p", mod.mac_vpn_profile(mod.MacVpnEndpoint(port)), sys.executable, "-c", code],
                capture_output=True,
                text=True,
                close_fds=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), [0, 1])

    def test_unpinned_archive_is_rejected_before_parsing(self):
        for payload in (b"", b"not an archive"):
            with self.assertRaisesRegex(ValueError, "digest rejected"):
                mod.mac_vpn_archive_binary(payload, "x86_64")
