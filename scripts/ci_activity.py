#!/usr/bin/env python3
"""Publish a real Linux CI child's activity through the room's existing lease."""

from __future__ import annotations

import contextlib
import argparse
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid

REPO = Path(__file__).resolve().parents[1]


class CiInterrupted(Exception):
    def __init__(self, signum: int):
        self.signum = signum


def load_collab() -> Any:
    loader = importlib.machinery.SourceFileLoader("collab_for_ci", str(REPO / "collab"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def cleanup_group(child: subprocess.Popen) -> None:
    load_collab().stop_installer(child)


def cleanup_containers(directory: Path, token: str) -> None:
    candidates = set()
    for version in ("3.11", "3.12", "3.13", "3.9"):
        with contextlib.suppress(OSError, ValueError):
            candidates.add((directory / (version + ".cid")).read_text().strip())
    # Also find our labeled create request if interruption preceded the cidfile write.
    for _ in range(3):
        try:
            found = subprocess.run(
                ["docker", "ps", "-aq", "--no-trunc", "--filter", "label=collab.ci.run=" + token],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if found.returncode == 0:
                candidates.update(found.stdout.split())
            for cid in candidates.copy():
                if not re.fullmatch(r"[0-9a-f]{64}", cid):
                    continue
                result = subprocess.run(
                    ["docker", "inspect", "--format", '{{ index .Config.Labels "collab.ci.run" }}', cid],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip() == token:
                    removed = subprocess.run(["docker", "rm", "--force", cid], capture_output=True, timeout=5)
                    if removed.returncode == 0:
                        candidates.discard(cid)
                elif result.returncode == 0:
                    candidates.discard(cid)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        time.sleep(0.1)


def run_checks(command: list[str], lease: Any = None, *, env: dict[str, str] | None = None, cleanup: Any = None) -> int:
    child = None
    outcome = "failed"
    previous = signal.getsignal(signal.SIGTERM)

    def interrupted(signum: int, frame: Any) -> None:
        raise CiInterrupted(signum)

    signal.signal(signal.SIGTERM, interrupted)
    try:
        if lease:
            lease.start()
            lease.phase("checking")
        module = load_collab()
        child = module.start_installer_process(command, env=env)
        while (code := module.installer_status(child)) is None:
            time.sleep(0.02)
        if lease:
            lease.phase("finishing")
        outcome = "done" if code == 0 else "failed"
        return code if code >= 0 else 128 - code
    except CiInterrupted as exc:
        return 128 + exc.signum
    except KeyboardInterrupt:
        return 130
    finally:
        # A second interrupt must not abandon the cleanup halfway through.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        previous_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            try:
                if child:
                    cleanup_group(child)
            finally:
                if cleanup:
                    cleanup()
        finally:
            if lease:
                lease.finish(outcome)
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="sender")
    parser.add_argument("--room")
    args = parser.parse_args(argv)
    if bool(args.sender) != bool(args.room):
        parser.error("--from and --room must be supplied together")
    lease = None
    if args.sender:
        c = load_collab()
        c.use_room(str(Path(args.room).resolve()))
        if args.sender not in c.ids_of(c.read_config_raw()):
            parser.error("--from must name a participant in the selected room")
        lease = c.ActivityLease(args.sender, uuid.uuid4().hex)
    token = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="collab-ci-containers-") as directory:
        return run_checks(
            ["bash", str(REPO / "scripts" / "ci"), "--linux-worker"],
            lease,
            env={**os.environ, "COLLAB_CI_CONTAINER_DIR": directory, "COLLAB_CI_RUN": token},
            cleanup=lambda: cleanup_containers(Path(directory), token),
        )


if __name__ == "__main__":
    sys.exit(main())
