"""Pure lifecycle decisions for one shared macOS proxy generation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Tuple


@dataclass(frozen=True)
class MacProxyState:
    generation: int = 0
    desired: str = "off"
    actual: str = "off"
    restarts: int = 0

    def __post_init__(self) -> None:
        if (
            type(self.generation) is not int
            or self.generation < 0
            or type(self.restarts) is not int
            or not 0 <= self.restarts <= 3
            or self.desired not in ("on", "off")
            or self.actual not in ("off", "starting", "probing", "ready", "failed", "blocked")
        ):
            raise ValueError("invalid proxy lifecycle state")
        if self.desired == "off" and self.actual not in ("off", "blocked"):
            raise ValueError("disabled proxy cannot be active")


@dataclass(frozen=True)
class MacProxyDecision:
    state: MacProxyState
    actions: Tuple[str, ...] = ()


def mac_proxy_transition(state: MacProxyState, event: str, generation: int) -> MacProxyDecision:
    if type(generation) is not int or generation != state.generation:
        return MacProxyDecision(state)
    if event == "off":
        return MacProxyDecision(MacProxyState(state.generation + 1), ("stop_agents", "stop_proxy"))
    if event == "on":
        if state.actual == "blocked":
            return MacProxyDecision(state)
        if state.desired == "on" and state.actual != "failed":
            return MacProxyDecision(state)
        return MacProxyDecision(MacProxyState(state.generation + 1, "on", "starting"), ("stop_agents", "start_proxy"))
    if state.desired != "on":
        return MacProxyDecision(state)
    if event == "started" and state.actual == "starting":
        return MacProxyDecision(replace(state, actual="probing"), ("probe",))
    if event == "probe_ok" and state.actual == "probing":
        return MacProxyDecision(replace(state, actual="ready"))
    if event == "foreign_listener":
        return MacProxyDecision(replace(state, actual="failed"), ("stop_agents", "stop_proxy"))
    if event in ("died", "probe_failed") and state.actual in ("starting", "probing", "ready"):
        # Revoke all agent leases before a new generation may listen on another port.
        if state.restarts >= 3:
            return MacProxyDecision(replace(state, actual="failed"), ("stop_agents", "stop_proxy"))
        return MacProxyDecision(
            replace(state, generation=state.generation + 1, actual="starting", restarts=state.restarts + 1),
            ("stop_agents", "stop_proxy", "start_proxy"),
        )
    return MacProxyDecision(state)
