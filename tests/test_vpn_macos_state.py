"""Generation and lease revocation rules without sockets, processes or credentials."""

import os
from pathlib import Path
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _package import Loader

mod = Loader(
    "mac_state_" + uuid.uuid4().hex[:8],
    os.environ.get("COLLAB_VPN_TEST_CLI", str(Path(__file__).resolve().parents[1] / "collab")),
).load_module()


class MacProxyStateTests(unittest.TestCase):
    def test_start_requires_probe_before_ready(self):
        decision = mod.mac_proxy_transition(mod.MacProxyState(), "on", 0)
        self.assertEqual(decision.state.actual, "starting")
        self.assertEqual(decision.actions, ("stop_agents", "start_proxy"))
        decision = mod.mac_proxy_transition(decision.state, "started", 1)
        self.assertEqual(decision.state.actual, "probing")
        self.assertEqual(decision.actions, ("probe",))
        decision = mod.mac_proxy_transition(decision.state, "probe_ok", 1)
        self.assertEqual(decision.state.actual, "ready")

    def test_only_three_restarts_and_revoke_before_start(self):
        state = mod.MacProxyState(1, "on", "ready")
        for restarts in range(1, 4):
            decision = mod.mac_proxy_transition(state, "died", state.generation)
            self.assertEqual(decision.actions, ("stop_agents", "stop_proxy", "start_proxy"))
            self.assertEqual(decision.state.restarts, restarts)
            self.assertEqual(decision.state.generation, state.generation + 1)
            state = decision.state
        decision = mod.mac_proxy_transition(state, "died", state.generation)
        self.assertEqual(decision.state.actual, "failed")
        self.assertNotIn("start_proxy", decision.actions)

    def test_foreign_listener_stops_agents_without_restart(self):
        state = mod.MacProxyState(4, "on", "ready")
        decision = mod.mac_proxy_transition(state, "foreign_listener", 4)
        self.assertEqual(decision.state.actual, "failed")
        self.assertEqual(decision.actions, ("stop_agents", "stop_proxy"))

    def test_stale_completion_cannot_reenable_after_off(self):
        state = mod.MacProxyState(4, "on", "probing")
        stopped = mod.mac_proxy_transition(state, "off", 4)
        self.assertEqual(stopped.state.desired, "off")
        self.assertEqual(stopped.actions, ("stop_agents", "stop_proxy"))
        stale = mod.mac_proxy_transition(stopped.state, "probe_ok", 4)
        self.assertEqual(stale.state, stopped.state)
        self.assertEqual(stale.actions, ())

    def test_manual_on_after_exhaustion_resets_budget(self):
        state = mod.MacProxyState(5, "on", "failed", 3)
        decision = mod.mac_proxy_transition(state, "on", 5)
        self.assertEqual(decision.state, mod.MacProxyState(6, "on", "starting", 0))
        self.assertEqual(mod.mac_proxy_transition(decision.state, "on", 6).actions, ())
