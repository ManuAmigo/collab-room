"""Private handoff storage and isolated process/PTY integration tests."""

import dataclasses
import json
import multiprocessing
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _package import Loader as _Loader  # noqa: E402
from _seam import seam  # noqa: E402
from test_collab import COLLAB, HAVE_TMUX, Base  # noqa: E402
from test_ledger import LedgerRoom  # noqa: E402


class ChatHandoffTest(LedgerRoom):
    def setUp(self):
        super().setUp()
        self.store = self.mod.HandoffStore(self.state / "handoff")
        self.seat = self.mod.Seat(str(self.root), "owner", "%7", 12345, 1)
        self.root.mkdir(exist_ok=True)

    def payload(self, text="  Привет 🌍\nвторая строка  ", recipient=None):
        dm = self.mod.DmPayload("Личное\nчерновик 🌱", 7, None, ("agent-a", "agent-b"), "agent-a", True, 3)
        return self.mod.HandoffPayload(text, len(text) - 2, recipient, dm)

    def test_unicode_multiline_cursor_and_none_recipient_survive_claim_and_recovery(self):
        payload = self.payload()
        token = self.store.save(self.seat, payload)
        path = self.state / "handoff" / f"{token}.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.state / "handoff").stat().st_mode & 0o777, 0o700)
        restored = self.store.claim(token, self.seat)
        self.assertEqual(restored, payload)
        self.assertIsNone(restored.recipient)
        self.assertEqual(restored.text, "  Привет 🌍\nвторая строка  ")
        self.assertEqual(restored.cursor, len(payload.text) - 2)
        self.assertEqual(self.store.claim(token, self.seat), payload)
        self.assertTrue(path.exists(), "recovery snapshot must survive a claim and possible target crash")
        self.store.complete(token, self.seat)
        self.assertFalse(path.exists())
        with self.assertRaises(self.mod.HandoffError):
            self.store.claim(token, self.seat)

    def test_every_seat_field_and_generation_is_exact(self):
        token = self.store.save(self.seat, self.payload())
        changes = (
            {"room_id": str(self.root / "other")},
            {"participant": "other"},
            {"pane": "%8"},
            {"source_pid": 12346},
            {"generation": 2},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(self.mod.HandoffError):
                self.store.claim(token, dataclasses.replace(self.seat, **change))
        self.assertEqual(self.store.claim(token, self.seat).recipient, None)
        with self.assertRaises(self.mod.HandoffError):
            self.store.complete(token, dataclasses.replace(self.seat, generation=2))
        self.assertTrue((self.state / "handoff" / f"{token}.json").exists())

    def test_unsafe_files_are_refused_after_a_valid_positive_claim(self):
        for kind in ("symlink", "hardlink", "fifo", "mode", "oversize"):
            with self.subTest(kind=kind):
                store = self.mod.HandoffStore(self.state / kind)
                token = store.save(self.seat, self.payload())
                path = self.state / kind / f"{token}.json"
                self.assertEqual(store.claim(token, self.seat).text, self.payload().text)
                original = path.read_bytes()
                external = self.root / f"{kind}-external"
                external.write_bytes(original)
                external.chmod(0o600)
                path.unlink()
                if kind == "symlink":
                    path.symlink_to(external)
                elif kind == "hardlink":
                    os.link(external, path)
                elif kind == "fifo":
                    os.mkfifo(path, 0o600)
                elif kind == "mode":
                    path.write_bytes(original)
                    path.chmod(0o640)
                else:
                    path.write_bytes(b"x" * (self.mod.HANDOFF_LIMIT + 1))
                    path.chmod(0o600)
                with self.assertRaises(self.mod.HandoffError):
                    store.claim(token, self.seat)
                self.assertEqual(external.read_bytes(), original)

    def test_claim_write_fault_preserves_previous_snapshot_and_can_recover(self):
        token = self.store.save(self.seat, self.payload())
        path = self.state / "handoff" / f"{token}.json"
        original = path.read_bytes()
        with mock.patch("os.fsync", side_effect=OSError("injected fsync failure")):
            with self.assertRaises(self.mod.HandoffError):
                self.store.claim(token, self.seat)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list((self.state / "handoff").glob("*.tmp")), [])
        self.assertEqual(self.store.claim(token, self.seat), self.payload())

    def test_save_replace_fault_leaves_no_partial_or_temporary_file(self):
        store = self.mod.HandoffStore(self.state / "replace-fault")
        with (
            mock.patch.object(self.mod.secrets, "token_hex", return_value="a" * 32),
            mock.patch("os.replace", side_effect=OSError("injected replace failure")),
            self.assertRaises(self.mod.HandoffError),
        ):
            store.save(self.seat, self.payload())
        self.assertEqual(list((self.state / "replace-fault").iterdir()), [])
        with mock.patch.object(self.mod.secrets, "token_hex", return_value="a" * 32):
            token = store.save(self.seat, self.payload())
        self.assertEqual(store.claim(token, self.seat), self.payload())

    def test_competing_initial_claims_have_only_one_write_transition(self):
        token = self.store.save(self.seat, self.payload())
        original_load = self.store._load
        original_write = self.store._write
        first_write = threading.Event()
        release_write = threading.Event()
        second_loaded = threading.Event()
        writes = []
        results = []
        errors = []
        second_thread = None

        def racing_load(token, seat):
            loaded = original_load(token, seat)
            if threading.current_thread() is second_thread:
                second_loaded.set()
            return loaded

        def slow_write(path, snapshot):
            writes.append(snapshot["claim"]["pid"])
            first_write.set()
            if not release_write.wait(2):
                raise RuntimeError("test did not release the first claim")
            original_write(path, snapshot)

        def claim():
            try:
                results.append(self.store.claim(token, self.seat))
            except Exception as error:
                errors.append(error)

        self.store._load = racing_load
        self.store._write = slow_write
        first_thread = threading.Thread(target=claim)
        first_thread.start()
        try:
            self.assertTrue(first_write.wait(2))
            second_thread = threading.Thread(target=claim)
            second_thread.start()
            self.assertFalse(second_loaded.wait(0.1), "second claim entered before the first claim released its lock")
        finally:
            release_write.set()
            first_thread.join(2)
            if second_thread is not None:
                second_thread.join(2)
        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(writes, [os.getpid()])
        self.assertEqual(results, [self.payload(), self.payload()])

    def test_live_process_claimants_are_exclusive_and_death_allows_recovery(self):
        token = self.store.save(self.seat, self.payload())
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(2)
        results = context.Queue()
        release = context.Event()

        def contender():
            barrier.wait()
            try:
                self.store.claim(token, self.seat)
                results.put(("claimed", os.getpid()))
                release.wait(5)
            except self.mod.HandoffError:
                results.put(("refused", os.getpid()))
                try:
                    self.store.complete(token, self.seat)
                    results.put(("delete-allowed", os.getpid()))
                except self.mod.HandoffError:
                    results.put(("delete-refused", os.getpid()))

        children = [context.Process(target=contender) for _ in range(2)]
        for child in children:
            child.start()
        try:
            first = [results.get(timeout=5), results.get(timeout=5)]
            self.assertEqual(sorted(message[0] for message in first), ["claimed", "refused"])
            self.assertEqual(results.get(timeout=5)[0], "delete-refused")
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
            for child in children:
                child.join(5)
        self.assertTrue(all(child.exitcode is not None for child in children))
        self.assertTrue(any(child.exitcode < 0 for child in children), "winning claimant must die before recovery")
        self.assertEqual(self.store.claim(token, self.seat), self.payload())
        self.store.complete(token, self.seat)

    def test_pid_start_forgery_and_unknown_previous_process_are_refused(self):
        token = self.store.save(self.seat, self.payload())
        current = self.mod.ProcessIdentity.current()
        forged = dataclasses.replace(current, start=current.start + " reused")
        with self.assertRaises(self.mod.HandoffError):
            self.store.claim(token, self.seat, forged)
        self.assertEqual(self.store.claim(token, self.seat), self.payload())
        with self.assertRaises(self.mod.HandoffError):
            self.store.complete(token, self.seat, forged)

        _, snapshot, _ = self.store._load(token, self.seat)
        snapshot["claim"] = {"pid": current.pid + 1, "start": "unverified start"}
        self.store._write(self.store._path(token), snapshot)
        real_start = self.mod._process_start

        def unknown_previous(pid):
            if pid == os.getpid():
                return real_start(pid)
            raise self.mod.HandoffError("unknown previous process")

        original = (self.state / "handoff" / f"{token}.json").read_bytes()
        with seam(self.mod, "chat_handoff._process_start", unknown_previous):
            with self.assertRaises(self.mod.HandoffError):
                self.store.claim(token, self.seat)
        self.assertEqual((self.state / "handoff" / f"{token}.json").read_bytes(), original)

    def test_restore_refuses_nonprivate_directory_with_positive_pair(self):
        token = self.store.save(self.seat, self.payload())
        folder = self.state / "handoff"
        folder.chmod(0o777)
        with self.assertRaises(self.mod.HandoffError):
            self.store.claim(token, self.seat)
        folder.chmod(0o700)
        self.assertEqual(self.store.claim(token, self.seat), self.payload())

    def test_hung_lock_holder_times_out_and_releases_after_death(self):
        token = self.store.save(self.seat, self.payload())
        context = multiprocessing.get_context("fork")
        locked = context.Event()
        release = context.Event()

        def holder():
            with self.store._claim_lock(token):
                locked.set()
                release.wait(5)

        child = context.Process(target=holder)
        child.start()
        try:
            self.assertTrue(locked.wait(5))
            bounded = self.mod.HandoffStore(self.state / "handoff", lock_timeout=0.05)
            with self.assertRaisesRegex(self.mod.HandoffError, "busy"):
                bounded.claim(token, self.seat)
        finally:
            release.set()
            child.join(5)
        self.assertEqual(child.exitcode, 0)
        self.assertEqual(self.store.claim(token, self.seat), self.payload())

    def test_duplicate_json_identity_is_rejected_with_valid_pair(self):
        token = self.store.save(self.seat, self.payload())
        path = self.state / "handoff" / f"{token}.json"
        self.assertEqual(self.store.claim(token, self.seat), self.payload())
        text = path.read_text()
        path.write_text(text.replace(f'"token":"{token}"', f'"token":"{token}","token":"{"0" * 32}"', 1))
        path.chmod(0o600)
        with self.assertRaises(self.mod.HandoffError):
            self.store.claim(token, self.seat)

    def test_exec_failure_restores_old_input_and_preserves_draft(self):
        import contextlib
        import pty
        from types import SimpleNamespace

        mod = self.mod
        mod.write_config({"participants": [{"id": "owner", "label": "Owner"}, {"id": "agent", "label": "Agent", "pane": False}]})
        mod.write_json(mod.room().paths.panes_path, {"panes": {"owner": "%1"}})
        (self.root / "welcomed").write_text("seen\n")
        (self.root / "language").write_text("en\n")
        master, slave = pty.openpty()
        stdin = open(slave, encoding="utf-8", closefd=False)
        stdout = open(slave, "w", encoding="utf-8", closefd=False)
        seen = bytearray()
        calls = []

        def tmux(*args, **kwargs):
            if "display-message" in args and "#{pane_width}" in args:
                out = "135\n"
            elif "display-message" in args and "#{pane_active}" in args:
                out = "1\n"
            else:
                out = ""
            return SimpleNamespace(returncode=0, stdout=out, stderr="")

        def failed_exec(*args):
            calls.append(args)
            raise OSError("injected exec failure")

        def drain():
            with contextlib.suppress(OSError):
                while chunk := os.read(master, 65536):
                    seen.extend(chunk)

        self.enterContext(seam(mod, "tmuxio.tmux", tmux))
        self.enterContext(seam(mod, "chat.os.execv", failed_exec))
        self.enterContext(mock.patch.dict(os.environ, {"TMUX_PANE": "%1", "COLUMNS": "100", "LINES": "30"}))
        self.enterContext(mock.patch.object(sys, "stdin", stdin))
        self.enterContext(mock.patch.object(sys, "stdout", stdout))
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        args = mod.build_parser().parse_args(["chat", "--as", "owner", "--interval", "0.1"])
        chat = threading.Thread(target=mod.cmd_chat, args=(args,), daemon=True)
        chat.start()

        def wait_for(check, timeout=10):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if check():
                    return True
                time.sleep(0.05)
            return False

        try:
            self.assertTrue(wait_for(lambda: b"Owner" in seen), bytes(seen[-500:]))
            os.write(master, "draft-🌍".encode())
            self.assertTrue(wait_for(lambda: "draft-🌍".encode() in seen))
            before = mod.read_messages()
            mod.request_update("owner")
            self.assertTrue(wait_for(lambda: bool(calls)), "exec was not attempted")
            handoff = self.state / "handoff"
            self.assertTrue(wait_for(lambda: handoff.exists() and not list(handoff.glob("*.json"))))
            os.write(master, b"-continued")
            self.assertTrue(wait_for(lambda: b"-continued" in seen), bytes(seen[-1000:]))
            self.assertTrue(chat.is_alive(), "exec failure must return to the old input loop")
            self.assertEqual(mod.read_messages(), before)
            self.assertEqual(len(calls), 1)
        finally:
            os.write(master, b"\x03\x03")
            chat.join(5)
            self.assertFalse(chat.is_alive())
            os.close(slave)
            reader.join(5)
            os.close(master)

    def test_dm_ack_uses_snapshot_token_and_freeze_times_out_without_late_input_loss(self):
        import contextlib
        import pty
        from types import SimpleNamespace

        mod = self.mod
        mod.write_config({"participants": [{"id": "owner", "label": "Owner"}, {"id": "agent", "label": "Agent", "pane": False}]})
        mod.set_dm_tabs("owner", ["agent"], "agent")
        mod.write_json(
            mod.room().paths.panes_path,
            {"panes": {"owner": "%9", "owner:dm": {"pane": "%10", "pid": str(os.getpid())}}},
        )
        master, slave = pty.openpty()
        stdin = open(slave, encoding="utf-8", closefd=False)
        stdout = open(slave, "w", encoding="utf-8", closefd=False)
        seen = bytearray()

        def tmux(*args, **kwargs):
            out = "%9\t3\n" if "#{pane_id}\t#{@collab_below}" in args else "1\n"
            return SimpleNamespace(returncode=0, stdout=out, stderr="")

        def drain():
            with contextlib.suppress(OSError):
                while chunk := os.read(master, 65536):
                    seen.extend(chunk)

        self.enterContext(seam(mod, "tmuxio.tmux", tmux))
        self.enterContext(mock.patch.dict(os.environ, {"TMUX_PANE": "%10", "COLUMNS": "80", "LINES": "24"}))
        self.enterContext(mock.patch.object(sys, "stdin", stdin))
        self.enterContext(mock.patch.object(sys, "stdout", stdout))
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        args = SimpleNamespace(as_="owner", interval=0.1, handoff_token=None)
        panel = threading.Thread(target=mod.cmd_dm, args=(args,), daemon=True)
        panel.start()

        def wait_for(check, timeout=10):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if check():
                    return True
                time.sleep(0.05)
            return False

        try:
            self.assertTrue(wait_for(lambda: b"Owner" in seen))
            os.write(master, b"base")
            self.assertTrue(wait_for(lambda: b"base" in seen))
            request = {
                "v": mod.HANDOFF_VERSION,
                "token": mod.secrets.token_hex(16),
                "generation": time.time_ns(),
                "target": "dm",
                "participant": "owner",
                "pane": "%10",
            }
            mod.write_control(mod.control_path("dm-request", "owner", "%10"), request)
            ack = None
            self.assertTrue(wait_for(lambda: mod.read_dm_ack(request, "%10") is not None))
            ack = mod.read_dm_ack(request, "%10")
            self.assertNotEqual(ack["snapshot_token"], request["token"])
            snapshot = self.state / "handoff" / f"{ack['snapshot_token']}.json"
            self.assertTrue(snapshot.exists())
            os.write(master, b"-late")
            time.sleep(0.3)  # Before the freeze deadline, late input must not reach the draft.
            self.assertNotIn(b"-late", seen)
            self.assertTrue(wait_for(lambda: not snapshot.exists(), timeout=5))
            self.assertTrue(wait_for(lambda: b"-late" in seen), bytes(seen[-1000:]))
            new_request = {
                **request,
                "token": mod.secrets.token_hex(16),
                "generation": request["generation"] + 1,
            }
            mod.write_control(mod.control_path("dm-request", "owner", "%10"), new_request)
            self.assertTrue(wait_for(lambda: mod.read_dm_ack(new_request, "%10") is not None))
            new_ack = mod.read_dm_ack(new_request, "%10")
            new_snapshot = self.state / "handoff" / f"{new_ack['snapshot_token']}.json"
            self.assertTrue(new_snapshot.exists())
            mod.write_ready(request, "ready", "%10")
            self.assertFalse(mod.current_dm_request(request, "%10"))
            time.sleep(0.3)  # A delayed main ready must not remove the newer generation's snapshot.
            self.assertTrue(new_snapshot.exists())
            mod.write_ready(new_request, "ready", "%10")
            self.assertTrue(wait_for(lambda: not new_snapshot.exists()))
            self.assertEqual(mod.read_messages(), [])
        finally:
            os.write(master, b"\x1d")
            panel.join(5)
            self.assertFalse(panel.is_alive())
            os.close(slave)
            reader.join(5)
            os.close(master)

    def test_malformed_request_token_type_is_a_handoff_error(self):
        request = {
            "v": self.mod.HANDOFF_VERSION,
            "token": 7,
            "generation": time.time_ns(),
            "target": "target",
            "participant": "owner",
            "pane": "%1",
        }
        path = self.mod.control_path("request", "owner", "%1")
        self.mod.write_control(path, request)
        with self.assertRaises(self.mod.HandoffError):
            self.mod.read_request("owner", "%1")
        request["token"] = "a" * 32
        self.mod.write_control(path, request)
        self.assertEqual(self.mod.read_request("owner", "%1"), request)

    def test_lock_timeout_must_be_finite_and_positive(self):
        for value in (float("nan"), float("inf"), -float("inf"), 0, -1, True, "2"):
            with self.subTest(value=value), self.assertRaises(self.mod.HandoffError):
                self.mod.HandoffStore(self.state / "timeout", value)
        self.assertEqual(self.mod.HandoffStore(self.state / "timeout", 0.01).lock_timeout, 0.01)

    def test_cursor_bounds_and_explicit_discard(self):
        with self.assertRaises(self.mod.HandoffError):
            self.mod.HandoffPayload("abc", 4, None)
        token = self.store.save(self.seat, self.payload(recipient="agent-a"))
        self.assertEqual(self.store.claim(token, self.seat).recipient, "agent-a")
        self.store.discard(token, self.seat)
        with self.assertRaises(self.mod.HandoffError):
            self.store.discard(token, self.seat)


@unittest.skipUnless(HAVE_TMUX, "isolated handoff flow needs tmux")
class ChatHandoffFlowTest(Base):
    def setUp(self):
        super().setUp()
        stubs = self.root / "_stubs"
        stubs.mkdir()
        for name in ("claude", "codex"):
            (stubs / name).write_text(
                '#!/bin/sh\nif [ "$1" = "--version" ]; then echo test-version; exit 0; fi\nsleep 30\n', encoding="utf-8"
            )
            (stubs / name).chmod(0o755)
        os.environ["PATH"] = f"{stubs}{os.pathsep}{os.environ['PATH']}"
        self.session = f"collab-test-{uuid.uuid4().hex[:6]}"
        agent = self.root / "agent.sh"
        agent.write_text('#!/bin/sh\necho READY\nwhile read l; do echo "got:$l"; echo READY; done\n', encoding="utf-8")
        agent.chmod(0o755)

    def tearDown(self):
        subprocess.run(["tmux", "kill-session", "-t", f"={self.session}"], capture_output=True)
        super().tearDown()

    def screen(self, pane):
        return subprocess.run(["tmux", "capture-pane", "-p", "-t", pane], capture_output=True, text=True).stdout

    def wait_for(self, pane, needle, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end:
            if needle in self.screen(pane):
                return True
            time.sleep(0.1)
        return False

    def literal(self, pane, text):
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", text], check=True)

    def key(self, pane, *keys):
        subprocess.run(["tmux", "send-keys", "-t", pane, *keys], check=True)

    def multiline(self, pane, first, second):
        self.literal(pane, first)
        self.key(pane, "Escape", "Enter")
        self.literal(pane, second)

    def pane_identities(self):
        return subprocess.run(
            ["tmux", "list-panes", "-t", f"={self.session}", "-F", "#{pane_id} #{pane_pid}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def client_selection(self):
        return subprocess.run(
            [
                "tmux",
                "display-message",
                "-p",
                "-t",
                f"={self.session}",
                "#{window_id} #{pane_id} #{window_active} #{pane_active}",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def test_full_main_and_dm_drafts_survive_one_explicit_update(self):
        safe = self.root.parent / f"{self.root.name}-safe"
        os.environ["COLLAB_SAFE_DIR"] = str(safe)
        self.addCleanup(shutil.rmtree, safe, True)
        self.write_config(
            [
                {
                    "id": "owner",
                    "label": "Owner",
                    "command": [
                        str(Path(__file__).resolve().parents[1] / "collab"),
                        "chat",
                        "--as",
                        "owner",
                        "--interval",
                        "0.1",
                    ],
                },
                {
                    "id": "agent",
                    "label": "Agent",
                    "command": [str(self.root / "agent.sh")],
                    "delivery": "tmux",
                    "idle": r"^READY$",
                },
            ],
            session=self.session,
        )
        self.assertEqual(self.run_collab("start").returncode, 0)
        panes = json.loads((self.state / "panes.json").read_text())["panes"]
        chat = panes["owner"]
        subprocess.run(["tmux", "resize-window", "-t", chat, "-x", "135", "-y", "35"], check=True)
        self.assertTrue(self.wait_for(chat, "Owner → Everyone"), self.screen(chat))
        self.literal(chat, "/dm agent")
        self.key(chat, "Enter")
        dm = self.until(lambda: json.loads((self.state / "panes.json").read_text())["panes"].get("owner:dm"))
        self.assertTrue(dm)
        self.assertTrue(self.wait_for(dm["pane"], "Owner → Agent"), self.screen(dm["pane"]))

        self.literal(dm["pane"], "  DM 🌱 dm-second  ")
        self.key(dm["pane"], "C-o")
        self.key(chat, "Tab")
        self.multiline(chat, "  Привет 🌍", "main-second  ")
        self.assertTrue(self.wait_for(chat, "main-second"), self.screen(chat))
        self.assertTrue(self.wait_for(dm["pane"], "dm-second"), self.screen(dm["pane"]))
        before_messages = self.messages()
        before_panes = self.pane_identities()
        before_selection = self.client_selection()

        result = self.run_collab("chat-update", "--as", "owner")
        self.assertEqual(result.returncode, 0, result.stderr)
        handoff = self.state / "handoff"
        self.assertTrue(self.until(lambda: handoff.exists() and not list(handoff.glob("*.json")), timeout=15))
        self.assertTrue(self.wait_for(chat, "  Привет 🌍"), self.screen(chat))
        self.assertTrue(self.wait_for(chat, "main-second"), self.screen(chat))
        self.assertIn("24 chars", self.screen(chat))
        self.assertTrue(self.wait_for(chat, "Owner → Agent"), self.screen(chat))
        self.assertTrue(self.wait_for(dm["pane"], "  DM 🌱"), self.screen(dm["pane"]))
        self.assertTrue(self.wait_for(dm["pane"], "dm-second"), self.screen(dm["pane"]))
        self.literal(dm["pane"], "after-ready")
        self.assertTrue(self.wait_for(dm["pane"], "after-re"), self.screen(dm["pane"]))
        self.assertEqual(self.messages(), before_messages, "recovery must never submit either draft")
        self.assertEqual(self.pane_identities(), before_panes)
        self.assertEqual(self.client_selection(), before_selection)

    def test_startup_failure_respawns_main_and_dm_with_unicode_drafts(self):
        safe = self.root.parent / f"{self.root.name}-safe"
        os.environ["COLLAB_SAFE_DIR"] = str(safe)
        self.addCleanup(shutil.rmtree, safe, True)
        self.write_config(
            [
                {
                    "id": "owner",
                    "label": "Owner",
                    "command": [str(COLLAB), "chat", "--as", "owner", "--interval", "0.1"],
                },
                {
                    "id": "agent",
                    "label": "Agent",
                    "command": [str(self.root / "agent.sh")],
                    "delivery": "tmux",
                    "idle": r"^READY$",
                },
            ],
            session=self.session,
        )
        self.assertEqual(self.run_collab("start").returncode, 0)
        panes = json.loads((self.state / "panes.json").read_text())["panes"]
        chat = panes["owner"]
        subprocess.run(["tmux", "resize-window", "-t", chat, "-x", "135", "-y", "35"], check=True)
        self.assertTrue(self.wait_for(chat, "Owner → Everyone"), self.screen(chat))
        self.literal(chat, "/dm agent")
        self.key(chat, "Enter")
        dm = self.until(lambda: json.loads((self.state / "panes.json").read_text())["panes"].get("owner:dm"))
        self.assertTrue(dm)
        self.key(dm["pane"], "C-o")
        self.key(chat, "Tab")
        self.multiline(chat, "  Main 🌍", "recover-main  ")
        self.assertTrue(self.wait_for(chat, "recover-main"), self.screen(chat))
        before_messages = self.messages()
        before_other = self.pane_identities()

        mod = _Loader(f"collab_recovery_{uuid.uuid4().hex[:6]}", str(COLLAB)).load_module()
        mod.use_room(str(self.root))
        correlation = mod.secrets.token_hex(16)
        request = {
            "v": mod.HANDOFF_VERSION,
            "token": correlation,
            "generation": time.time_ns(),
            "target": str(mod.command_copy()),
            "participant": "owner",
            "pane": chat,
        }
        pane_pid = lambda pane: int(
            subprocess.run(
                ["tmux", "display-message", "-p", "-t", pane, "#{pane_pid}"], capture_output=True, text=True, check=True
            ).stdout.strip()
        )
        main_pid = pane_pid(chat)
        dm_pid = pane_pid(dm["pane"])
        dm_text = "  DM 🌱\nrecover-dm  "
        dm_payload = mod.DmPayload(dm_text, len(dm_text) - 2, None, ("agent",), "agent", False, 0)
        dm_seat = mod.Seat(str(self.root), "owner", dm["pane"], dm_pid, request["generation"])
        dm_token = mod.HandoffStore(self.state / "handoff").save(
            dm_seat, mod.HandoffPayload(dm_text, dm_payload.cursor, "agent", dm_payload)
        )
        dm_request = {**request, "target": "dm", "pane": dm["pane"]}
        mod.write_control(mod.control_path("dm-request", "owner", dm["pane"]), dm_request)
        mod.write_dm_ack(dm_request, dm_token, dm_pid, ["agent"], "agent", False)
        ack = mod.read_dm_ack(dm_request, dm["pane"])
        self.assertTrue(ack)
        self.assertNotEqual(ack["snapshot_token"], correlation)
        raw_dm = mod.HandoffStore(self.state / "handoff")._read(self.state / "handoff" / f"{dm_token}.json")
        self.assertIn("recover-dm", raw_dm["payload"]["text"])
        request["source_pid"] = main_pid
        mod.write_control(mod.control_path("request", "owner", chat), request)
        main_text = "  Main 🌍\nrecover-main  "
        main_seat = mod.Seat(str(self.root), "owner", chat, main_pid, request["generation"])
        main_token = mod.HandoffStore(self.state / "handoff").save(
            main_seat, mod.HandoffPayload(main_text, len(main_text) - 2, "agent")
        )
        for pane in (chat, dm["pane"]):
            subprocess.run(["tmux", "set-option", "-w", "-t", pane, "remain-on-exit", "on"], check=True)
        os.kill(main_pid, signal.SIGKILL)
        os.kill(dm_pid, signal.SIGKILL)

        def dead(pid):
            return subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode != 0

        self.assertTrue(self.until(lambda: dead(main_pid) and dead(dm_pid)))
        command = [
            sys.executable,
            request["target"],
            "chat",
            "--as",
            "owner",
            "--room",
            str(self.root),
            "--interval",
            "0.1",
            "--handoff-token",
            main_token,
        ]
        restarted = subprocess.run(
            [
                "tmux",
                "respawn-pane",
                "-k",
                "-t",
                chat,
                "-c",
                mod.tmux_dir(str(self.root)),
                *mod.pane_env(),
                mod.in_dir(str(self.root), shlex.join(command)),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertTrue(self.wait_for(chat, "recover-main"), self.screen(chat))
        self.assertTrue(self.wait_for(dm["pane"], "recover-dm"), self.screen(dm["pane"]))
        handoff = self.state / "handoff"
        self.assertTrue(
            self.until(lambda: not list(handoff.glob("*.json")), timeout=15),
            {
                "files": [path.name for path in handoff.glob("*.json")],
                "main_token": main_token,
                "dm_token": ack["snapshot_token"],
                "dm_screen": self.screen(dm["pane"]),
            },
        )
        self.literal(dm["pane"], "after-crash")
        self.assertTrue(self.wait_for(dm["pane"], "after-c"), self.screen(dm["pane"]))
        self.assertNotEqual(pane_pid(chat), main_pid)
        self.assertNotEqual(pane_pid(dm["pane"]), dm_pid)
        excluded = (chat + " ", dm["pane"] + " ")
        filter_other = lambda text: "\n".join(line for line in text.splitlines() if not line.startswith(excluded))
        self.assertEqual(filter_other(self.pane_identities()), filter_other(before_other))
        self.assertEqual(self.messages(), before_messages)

    def test_delayed_target_after_dm_timeout_restores_main_without_touching_live_dm(self):
        safe = self.root.parent / f"{self.root.name}-safe"
        os.environ["COLLAB_SAFE_DIR"] = str(safe)
        self.addCleanup(shutil.rmtree, safe, True)
        self.write_config(
            [
                {
                    "id": "owner",
                    "label": "Owner",
                    "command": [str(COLLAB), "chat", "--as", "owner", "--interval", "0.1"],
                },
                {
                    "id": "agent",
                    "label": "Agent",
                    "command": [str(self.root / "agent.sh")],
                    "delivery": "tmux",
                    "idle": r"^READY$",
                },
            ],
            session=self.session,
        )
        self.assertEqual(self.run_collab("start").returncode, 0)
        panes = json.loads((self.state / "panes.json").read_text())["panes"]
        chat = panes["owner"]
        subprocess.run(["tmux", "resize-window", "-t", chat, "-x", "135", "-y", "35"], check=True)
        self.assertTrue(self.wait_for(chat, "Owner → Everyone"), self.screen(chat))
        self.literal(chat, "/dm agent")
        self.key(chat, "Enter")
        dm = self.until(lambda: json.loads((self.state / "panes.json").read_text())["panes"].get("owner:dm"))
        self.assertTrue(dm)
        self.literal(dm["pane"], "  DM 🌱 before-timeout  ")
        self.assertTrue(self.wait_for(dm["pane"], "before-timeout"), self.screen(dm["pane"]))
        self.key(dm["pane"], "C-o")
        self.key(chat, "Tab")
        self.multiline(chat, "  Main 🌍", "delayed-main  ")
        self.assertTrue(self.wait_for(chat, "delayed-main"), self.screen(chat))
        before_messages = self.messages()
        before_other = self.pane_identities()

        mod = _Loader(f"collab_delayed_{uuid.uuid4().hex[:6]}", str(COLLAB)).load_module()
        mod.use_room(str(self.root))
        request = {
            "v": mod.HANDOFF_VERSION,
            "token": mod.secrets.token_hex(16),
            "generation": time.time_ns(),
            "target": str(mod.command_copy()),
            "participant": "owner",
            "pane": chat,
        }
        mod.write_dm_request(request, dm["pane"])
        ack = self.until(lambda: mod.read_dm_ack(request, dm["pane"]))
        self.assertTrue(ack)
        pane_pid = lambda pane: int(
            subprocess.run(
                ["tmux", "display-message", "-p", "-t", pane, "#{pane_pid}"], capture_output=True, text=True, check=True
            ).stdout.strip()
        )
        main_pid = pane_pid(chat)
        dm_pid = pane_pid(dm["pane"])
        request["source_pid"] = main_pid
        mod.write_control(mod.control_path("request", "owner", chat), request)
        main_text = "  Main 🌍\ndelayed-main  "
        main_seat = mod.Seat(str(self.root), "owner", chat, main_pid, request["generation"])
        main_token = mod.HandoffStore(self.state / "handoff").save(
            main_seat, mod.HandoffPayload(main_text, len(main_text) - 2, "agent")
        )
        subprocess.run(["tmux", "set-option", "-w", "-t", chat, "remain-on-exit", "on"], check=True)
        os.kill(main_pid, signal.SIGKILL)
        self.assertTrue(
            self.until(
                lambda: (
                    mod.read_dm_ack(request, dm["pane"]) is None
                    and mod.read_ready(request, dm_pane=dm["pane"])["status"] == "failed"
                )
            ),
            "DM handshake did not reach its terminal timeout",
        )
        self.literal(dm["pane"], "late-input")
        self.assertTrue(self.wait_for(dm["pane"], "late-input"), self.screen(dm["pane"]))
        command = [
            sys.executable,
            request["target"],
            "chat",
            "--as",
            "owner",
            "--room",
            str(self.root),
            "--interval",
            "0.1",
            "--handoff-token",
            main_token,
        ]
        restarted = subprocess.run(
            [
                "tmux",
                "respawn-pane",
                "-k",
                "-t",
                chat,
                "-c",
                mod.tmux_dir(str(self.root)),
                *mod.pane_env(),
                mod.in_dir(str(self.root), shlex.join(command)),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertTrue(self.wait_for(chat, "delayed-main"), self.screen(chat))
        self.assertTrue(self.wait_for(dm["pane"], "late-input"), self.screen(dm["pane"]))
        self.assertEqual(pane_pid(dm["pane"]), dm_pid)
        self.assertNotEqual(pane_pid(chat), main_pid)
        self.literal(dm["pane"], "after-main")
        self.assertTrue(self.wait_for(dm["pane"], "after-main"), self.screen(dm["pane"]))
        excluded = (chat + " ",)
        filter_other = lambda text: "\n".join(line for line in text.splitlines() if not line.startswith(excluded))
        self.assertEqual(filter_other(self.pane_identities()), filter_other(before_other))
        self.assertEqual(self.messages(), before_messages)
        self.assertEqual(mod.read_ready(request, dm_pane=dm["pane"])["status"], "failed")

    def start_chat_only(self):
        safe = self.root.parent / f"{self.root.name}-safe"
        os.environ["COLLAB_SAFE_DIR"] = str(safe)
        self.addCleanup(shutil.rmtree, safe, True)
        self.write_config(
            [
                {
                    "id": "owner",
                    "label": "Owner",
                    "command": [
                        str(Path(__file__).resolve().parents[1] / "collab"),
                        "chat",
                        "--as",
                        "owner",
                        "--interval",
                        "0.1",
                    ],
                },
                {
                    "id": "agent",
                    "label": "Agent",
                    "command": [str(self.root / "agent.sh")],
                    "delivery": "tmux",
                    "idle": r"^READY$",
                },
            ],
            session=self.session,
        )
        self.assertEqual(self.run_collab("start").returncode, 0)
        chat = json.loads((self.state / "panes.json").read_text())["panes"]["owner"]
        self.assertTrue(self.wait_for(chat, "Owner → Everyone"), self.screen(chat))
        return chat

    def assert_update_deferred(self):
        time.sleep(0.5)  # A negative timing claim needs a short interval with no exec to observe.
        self.assertTrue(list((self.state / "handoff").glob("*.request.json")))

    def test_partial_utf8_paste_and_wizard_defer_until_safe_boundary(self):
        chat = self.start_chat_only()
        before = self.messages()
        subprocess.run(["tmux", "send-keys", "-t", chat, "-H", "f0", "9f"], check=True)
        self.assertEqual(self.run_collab("chat-update", "--as", "owner").returncode, 0)
        self.assert_update_deferred()
        subprocess.run(["tmux", "send-keys", "-t", chat, "-H", "8c", "8d"], check=True)
        handoff = self.state / "handoff"
        self.assertTrue(self.until(lambda: not list(handoff.glob("*.json")), timeout=15))
        self.assertTrue(self.wait_for(chat, "🌍"), self.screen(chat))

        subprocess.run(["tmux", "send-keys", "-t", chat, "-H", "1b", "5b", "32", "30", "30", "7e"], check=True)
        self.literal(chat, "paste")
        self.assertEqual(self.run_collab("chat-update", "--as", "owner").returncode, 0)
        self.assert_update_deferred()
        subprocess.run(["tmux", "send-keys", "-t", chat, "-H", "1b", "5b", "32", "30", "31", "7e"], check=True)
        self.assertTrue(self.until(lambda: not list(handoff.glob("*.json")), timeout=15))
        self.assertTrue(self.wait_for(chat, "paste"), self.screen(chat))

        self.key(chat, "C-c")
        self.assertEqual(self.run_collab("chat-update", "--as", "owner").returncode, 0)
        self.assert_update_deferred()
        self.key(chat, "Escape")
        self.assertTrue(self.until(lambda: not list(handoff.glob("*.json")), timeout=15))
        self.assertEqual(self.messages(), before)

    def test_unsafe_target_keeps_old_input_and_allows_later_update(self):
        chat = self.start_chat_only()
        before = self.messages()
        self.key(chat, "C-c")
        self.assertEqual(self.run_collab("chat-update", "--as", "owner").returncode, 0)
        request_path = next((self.state / "handoff").glob("*.request.json"))
        request = json.loads(request_path.read_text())
        Path(request["target"]).unlink()
        self.key(chat, "Escape")
        handoff = self.state / "handoff"
        self.assertTrue(self.until(lambda: not list(handoff.glob("*.json")), timeout=15))
        self.assertTrue(self.wait_for(chat, "Chat update was refused"), self.screen(chat))
        self.literal(chat, "still-here")
        self.assertTrue(self.wait_for(chat, "still-here"), self.screen(chat))
        self.assertEqual(self.run_collab("chat-update", "--as", "owner").returncode, 0)
        self.assertTrue(self.until(lambda: not list(handoff.glob("*.json")), timeout=15))
        self.assertTrue(self.wait_for(chat, "still-here"), self.screen(chat))
        self.assertEqual(self.messages(), before)

    def test_removed_recipient_restores_as_none_instead_of_everyone(self):
        chat = self.start_chat_only()
        before = self.messages()
        self.key(chat, "Tab")
        self.literal(chat, "keep-me")
        self.key(chat, "C-c")
        self.assertEqual(self.run_collab("chat-update", "--as", "owner").returncode, 0)
        self.write_config([{"id": "owner", "label": "Owner"}], session=self.session)
        self.key(chat, "Escape")
        handoff = self.state / "handoff"
        self.assertTrue(self.until(lambda: not list(handoff.glob("*.json")), timeout=15))
        self.assertTrue(self.wait_for(chat, "choose with Tab"), self.screen(chat))
        self.assertTrue(self.wait_for(chat, "keep-me"), self.screen(chat))
        self.assertNotIn("Owner → Everyone ›", self.screen(chat))
        self.assertEqual(self.messages(), before)
