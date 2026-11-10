"""A managed launch's hook and reversible config, in isolated files only."""

import json
import os
import subprocess
import shlex
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ledger import LedgerRoom  # noqa: E402
from _seam import seam  # noqa: E402


class KimiCollectorTest(LedgerRoom):
    def setUp(self):
        super().setUp()
        self.home = self.root / "kimi"
        self.enterContext(mock.patch.dict(os.environ, {"KIMI_CODE_HOME": str(self.home)}))
        self.token = "a" * 32
        self.agent = {
            "id": "auto",
            "kind": "kimi",
            "pane_pid": str(os.getpid()),
            "target": "%1",
            "join_code": "run1",
            "folder": str(self.root),
            "started_by_room": True,
            "kimi_usage_generation": self.token,
            "idle": ">",
        }
        self.mod.write_config({"participants": [self.agent]})
        start = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(os.getpid())], capture_output=True, text=True, check=True
        ).stdout.strip()
        self.proof = {
            "id": "auto",
            "token": self.token,
            "generation": "run1",
            "pane_pid": str(os.getpid()),
            "target": "%1",
            "start_identity": start,
            "folder": str(self.root),
            "at": self.mod.now(),
            "sessions_root": str(self.home / "sessions"),
            "workdir_key": "project",
            "adapter": "openai-compatible-2.4.1",
        }
        self.mod.write_json(self.state / "kimi-launches.json", {self.token: self.proof})
        self.event = {
            "hook_event_name": "SessionStart",
            "source": "startup",
            "client_type": "kimi_code_cli",
            "session_id": "session_one",
            "cwd": str(self.root),
        }
        self.path = self.home / "sessions/project/session_one/agents/main/wire.jsonl"
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps(
                {
                    "type": "usage.record",
                    "agentId": "main",
                    "model": "probe",
                    "usageScope": "turn",
                    "time": int(time.time() * 1000),
                    "usage": {"inputOther": 100, "output": 30, "inputCacheRead": 20, "inputCacheCreation": 0},
                }
            )
            + "\n"
        )

    def hook(self, event=None, **env):
        cli = Path(__file__).resolve().parents[1] / "collab"
        return subprocess.run(
            [sys.executable, "-B", str(cli), "usage", "kimi-start"],
            input=json.dumps(self.event if event is None else event),
            text=True,
            capture_output=True,
            timeout=5,
            env={
                **os.environ,
                "COLLAB_KIMI_ROOM": str(self.root),
                "COLLAB_KIMI_AGENT": "auto",
                "COLLAB_KIMI_GENERATION": self.token,
                **env,
            },
        )

    def pending_then_startup(self, pid):
        self.assertEqual(self.hook().returncode, 0)
        with seam(self.mod, "ledger.append_usage", mock.Mock(side_effect=OSError("unavailable"))), self.assertRaises(OSError):
            self.mod.record(self.agent)
        token = "b" * 32
        newer = {**self.agent, "id": pid, "join_code": "run2", "kimi_usage_generation": token}
        proof = {**self.proof, "id": pid, "token": token, "generation": "run2"}
        self.mod.write_config({"participants": [newer] if pid == "auto" else [self.agent, newer]})
        manifests = self.mod.read_json(self.state / "kimi-launches.json")
        manifests[token] = proof
        self.mod.write_json(self.state / "kimi-launches.json", manifests)
        path = self.home / "sessions/project/session_two/agents/main/wire.jsonl"
        path.parent.mkdir(parents=True)
        usage = json.loads(self.path.read_text())
        usage.update(model="new-launch", time=4102444800000)
        usage["usage"]["inputOther"] = 200
        path.write_text(json.dumps(usage) + "\n")
        hook = self.hook({**self.event, "session_id": "session_two"}, COLLAB_KIMI_AGENT=pid, COLLAB_KIMI_GENERATION=token)
        self.assertEqual(hook.returncode, 0, hook.stderr)
        cursor_path = self.state / "kimi-usage-cursor.json"
        expected = self.mod.read_json(cursor_path)[pid]
        self.mod.record(newer if pid == "auto" else self.agent)
        self.assertEqual(self.mod.read_json(cursor_path)[pid], expected, "retry must not overwrite a newer startup cursor")
        self.mod.record(newer)
        self.mod.record(newer)
        rows = self.mod.read_jsonl(self.mod.usage_path())
        self.assertEqual(sorted((r["model"], r["input"]) for r in rows), [("new-launch", 200), ("probe", 100)])

    def test_pending_retry_preserves_another_participants_real_startup_cursor(self):
        self.pending_then_startup("peer")

    def test_pending_retry_preserves_a_real_rebind_and_its_first_usage(self):
        self.pending_then_startup("auto")

    def test_automatic_hook_counts_first_usage_already_written_before_hook(self):
        self.hook()
        self.assertEqual(
            self.mod.record(self.agent), 1, "the automatic hook must bind and keep first usage, not start at late EOF"
        )
        line = self.mod.read_jsonl(self.mod.usage_path())[0]
        self.assertEqual((line["input"], line["output"], line["coverage"]), (100, 30, "partial"))
        self.hook()
        self.assertEqual(self.mod.record(self.agent), 0, "repeated startup hook cannot rewind the cursor")

    def test_dotted_and_hidden_workdirs_bind_automatically_and_count_usage(self):
        original = self.path.read_text()
        for basename in ("project.v2", ".hidden"):
            with self.subTest(basename=basename):
                folder = self.root / basename
                folder.mkdir()
                self.agent["folder"] = str(folder)
                self.mod.write_config({"participants": [self.agent]})
                self.proof.update(folder=str(folder), workdir_key=self.mod.kimi_workdir_key(str(folder)))
                self.proof.pop("session_id", None)
                self.mod.write_json(self.state / "kimi-launches.json", {self.token: self.proof})
                self.event["cwd"] = str(folder)
                wire = self.home / "sessions" / self.proof["workdir_key"] / "session_one/agents/main/wire.jsonl"
                wire.parent.mkdir(parents=True)
                wire.write_text(original)
                self.hook()
                self.assertEqual(self.mod.record(self.agent), 1)
                self.assertEqual(self.mod.kimi_binding(self.agent)["workdir_key"], self.proof["workdir_key"])

    def test_dotted_workdir_does_not_relax_session_agent_or_traversal(self):
        for field, value in (
            ("workdir_key", "../project.v2"),
            ("workdir_key", "wd_x/../x_0123456789ab"),
            ("session_id", "session.one"),
            ("session_id", ".."),
            ("agent_id", "main.v2"),
        ):
            with self.subTest(field=field, value=value):
                self.hook()
                path = self.state / "kimi-usage-bindings.json"
                bindings = self.mod.read_json(path)
                bindings["auto"][field] = value
                self.mod.write_json(path, bindings)
                self.assertEqual(self.mod.kimi_binding(self.agent), {})
                self.assertEqual(self.mod.record(self.agent), 0)
        self.proof["workdir_key"] = "../project.v2"
        self.mod.write_json(self.state / "kimi-launches.json", {self.token: self.proof})
        self.mod.write_json(self.state / "kimi-usage-bindings.json", {})
        self.hook()
        self.assertEqual(self.mod.kimi_binding(self.agent), {})

    def test_persistent_lock_error_is_still_refused_and_symlink_is_not_followed(self):
        from _seam import seam

        original_open = os.open
        attempts = []

        def missing(path, *args, **kwargs):
            if path == "room.lock":
                attempts.append(path)
                raise FileNotFoundError(2, "persistent missing lock")
            return original_open(path, *args, **kwargs)

        env = {"COLLAB_KIMI_AGENT": "auto", "COLLAB_KIMI_GENERATION": self.token}
        with mock.patch.dict(os.environ, env), seam(self.mod, "store.os.open", missing):
            with self.assertRaises(SystemExit):
                self.mod.collect_kimi_start(self.event)
        self.assertLessEqual(len(attempts), 3)
        outside = self.root / "outside-lock"
        outside.write_text("must stay unchanged")
        (self.state / "room.lock").symlink_to(outside)
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(SystemExit):
                self.mod.collect_kimi_start(self.event)
        self.assertEqual(outside.read_text(), "must stay unchanged")

    def test_managed_launch_initializes_lock_and_finishes_manifest_before_early_hook(self):
        from types import SimpleNamespace
        from _seam import seam
        import shlex

        self.mod.write_config({"session": "probe", "participants": [{"id": "observer", "label": "Observer"}]})
        self.assertFalse((self.state / "room.lock").exists())
        phase = self.root / "hook-attempted"
        children = []
        code = """import sys,json,time,importlib.machinery
from pathlib import Path
m=importlib.machinery.SourceFileLoader('early_hook',sys.argv[1]).load_module()
m.use_room(sys.argv[2])
e=json.loads(sys.argv[3])
p=Path(sys.argv[4]);p.parent.mkdir(parents=True,exist_ok=True)
p.write_text(json.dumps({'type':'usage.record','agentId':'main','model':'probe','usageScope':'turn','time':int(time.time()*1000),'usage':{'inputOther':100,'output':30,'inputCacheRead':20,'inputCacheCreation':0}})+'\\n')
Path(sys.argv[5]).write_text('attempting')
m.collect_kimi_start(e)
"""

        def tmux(*args, **kwargs):
            if args[0] == "new-window":
                self.assertTrue(
                    (self.state / "room.lock").is_file(), "writer synchronization must exist before an agent can spawn hooks"
                )
                words = shlex.split(args[-1])
                token = next(x.split("=", 1)[1] for x in words if x.startswith("COLLAB_KIMI_GENERATION="))
                event = {**self.event, "session_id": "session_early"}
                wire = self.home / "sessions" / self.mod.kimi_workdir_key(str(self.root)) / "session_early/agents/main/wire.jsonl"
                env = {**os.environ, "COLLAB_KIMI_AGENT": "newauto", "COLLAB_KIMI_GENERATION": token}
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-B",
                        "-c",
                        code,
                        str(Path(__file__).resolve().parents[1] / "collab"),
                        str(self.root),
                        json.dumps(event),
                        str(wire),
                        str(phase),
                    ],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                children.append(child)
                end = time.monotonic() + 5
                while not phase.exists() and time.monotonic() < end:
                    time.sleep(0.01)
                self.assertTrue(phase.exists())
                time.sleep(0.03)
                self.assertIsNone(child.poll(), "an early hook must wait for the launch writer, not reject incomplete metadata")
                return SimpleNamespace(returncode=0, stdout="%1 " + str(os.getpid()), stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        try:
            with (
                seam(self.mod, "tmuxio.tmux", tmux),
                seam(self.mod, "usage.kimi_usage_adapter", lambda command: self.mod.KIMI_NORMALIZED),
            ):
                pid = self.mod.launch_new_agent(self.mod.read_config_raw(), "kimi", str(self.root), "newauto")
            for child in children:
                out, err = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0, err)
            p = next(p for p in self.mod.read_config_raw()["participants"] if p["id"] == pid)
            self.assertEqual(self.mod.record(p), 1)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate()

    def test_replayed_startup_does_not_rebaseline_lost_cursor_at_zero(self):
        self.hook()
        self.assertEqual(self.mod.record(self.agent), 1)
        (self.state / "kimi-usage-cursor.json").unlink()
        self.hook()
        self.assertEqual(self.mod.record(self.agent), 0, "a lost cursor must not recount the already collected session")

    def test_confirmed_resume_unbinds_and_does_not_read_old_session(self):
        self.hook()
        self.assertEqual(self.mod.record(self.agent), 1)
        resume = {**self.event, "source": "resume", "session_id": "session_other"}
        self.hook(resume, COLLAB_KIMI_GENERATION="b" * 32)
        self.assertTrue(self.mod.kimi_binding(self.agent), "foreign generation cannot erase a binding")
        self.proof["start_identity"] = "not the parent process"
        self.mod.write_json(self.state / "kimi-launches.json", {self.token: self.proof})
        self.hook(resume)
        self.assertTrue(self.mod.kimi_binding(self.agent), "wrong process identity cannot erase it")
        self.proof["start_identity"] = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(os.getpid())], capture_output=True, text=True
        ).stdout.strip()
        self.mod.write_json(self.state / "kimi-launches.json", {self.token: self.proof})
        self.hook(resume)
        self.assertFalse(self.mod.kimi_binding(self.agent))
        self.assertIn("resumed", self.mod.unread(self.agent))
        with self.path.open("a") as f:
            f.write(self.path.read_text().replace('"output": 30', '"output": 31'))
        self.assertEqual(self.mod.record(self.agent), 0, "old session is not consumed after confirmed resume")
        self.hook()
        self.assertFalse(self.mod.kimi_binding(self.agent), "old startup replay cannot revive a resumed generation")

    def test_verified_cli_format_does_not_require_provider_or_model_guessing(self):
        self.proof["adapter"] = self.mod.KIMI_NORMALIZED
        self.mod.write_json(self.state / "kimi-launches.json", {self.token: self.proof})
        self.hook()
        self.assertEqual(self.mod.record(self.agent), 1)
        self.assertEqual(self.mod.kimi_binding(self.agent)["adapter"], "kimi-code-2.4.1-normalized")

    def test_version_is_an_exact_launcher_check_with_no_fallback(self):
        from types import SimpleNamespace
        from _seam import seam

        cases = [(0, "2.4.1\n", self.mod.KIMI_NORMALIZED), (0, "2.4.2\n", ""), (1, "2.4.1\n", ""), (0, "", "")]
        for code, version, expected in cases:
            with seam(self.mod, "usage.subprocess.run", mock.Mock(return_value=SimpleNamespace(returncode=code, stdout=version))):
                self.assertEqual(self.mod.kimi_usage_adapter("'/a path/kimi' --model named-kimi"), expected)
        with seam(self.mod, "usage.subprocess.run", mock.Mock(side_effect=subprocess.TimeoutExpired("kimi", 2))):
            self.assertEqual(self.mod.kimi_usage_adapter("kimi"), "")

    def test_unknown_native_adapter_is_no_data(self):
        self.proof["adapter"] = ""
        self.mod.write_json(self.state / "kimi-launches.json", {self.token: self.proof})
        self.hook()
        self.assertEqual(self.mod.record(self.agent), 0)
        self.assertIn("adapter has not been verified", self.mod.unread(self.agent))

    def test_unmanaged_hook_is_noop_and_payload_parent_is_not_authority(self):
        before = (self.state / "kimi-launches.json").read_bytes()
        self.assertEqual(self.hook(COLLAB_KIMI_GENERATION="").returncode, 0)
        self.assertEqual((self.state / "kimi-launches.json").read_bytes(), before)
        self.proof["pane_pid"] = "99999999"
        self.mod.write_json(self.state / "kimi-launches.json", {self.token: self.proof})
        self.hook({**self.event, "parent_pid": 99999999})
        self.assertFalse(self.mod.kimi_binding(self.agent))

    def test_manifest_symlink_and_fifo_are_not_sources_of_authority(self):
        manifest = self.state / "kimi-launches.json"
        outside = self.root / "outside-proof.json"
        outside.write_bytes(manifest.read_bytes())
        manifest.unlink()
        manifest.symlink_to(outside)
        self.hook()
        self.assertFalse(self.mod.kimi_binding(self.agent), "a followed manifest must not authorize collection")
        manifest.unlink()
        os.mkfifo(manifest)
        self.assertEqual(self.hook().returncode, 0, "FIFO state must be refused without waiting for a writer")
        self.assertFalse(self.mod.kimi_binding(self.agent))

    def test_changed_session_in_one_generation_unbinds(self):
        self.hook()
        self.hook({**self.event, "session_id": "session_new"})
        self.assertFalse(self.mod.kimi_binding(self.agent))

    def resumed_launch(self):
        self.hook()
        self.assertEqual(self.mod.record(self.agent), 1)
        binding = self.mod.kimi_binding(self.agent)
        proof = self.mod.read_json(self.state / "kimi-launches.json")[self.token]
        old = self.token
        self.token = "b" * 32
        self.agent.update(join_code="run2", kimi_usage_generation=self.token)
        proof.update(token=self.token, generation="run2", resume_binding=binding)
        proof.pop("session_id", None)
        self.mod.write_json(self.state / "kimi-launches.json", {self.token: proof})
        self.mod.write_config({"participants": [self.agent]})
        return old

    def test_managed_exact_resume_keeps_old_late_and_new_usage_once(self):
        self.resumed_launch()
        row = self.path.read_text()
        with self.path.open("a") as f:
            f.write(row.replace('"output": 30', '"output": 31'))
        self.hook({**self.event, "source": "resume"})
        self.assertEqual(self.mod.kimi_binding(self.agent).get("session_id"), self.event["session_id"])
        with self.path.open("a") as f:
            f.write(row.replace('"output": 30', '"output": 32'))
        self.assertEqual(self.mod.record(self.agent), 2, "the old tail and new answer are both retained")
        self.hook({**self.event, "source": "resume"})
        self.assertEqual(self.mod.record(self.agent), 0, "repeat resume cannot rewind the cursor")
        self.assertEqual(len(self.mod.read_jsonl(self.mod.usage_path())), 3)

    def test_resume_hook_recovers_after_cursor_handoff_before_manifest_commit(self):
        self.resumed_launch()
        event = {**self.event, "source": "resume"}
        self.hook(event)
        manifest = self.state / "kimi-launches.json"
        proofs = self.mod.read_json(manifest)
        proofs[self.token].pop("session_id")
        self.mod.write_json(manifest, proofs)
        self.hook(event)
        self.assertTrue(self.mod.kimi_binding(self.agent))
        self.assertEqual(self.mod.record(self.agent), 0)

    def test_managed_resume_refuses_wrong_session_source_and_lost_cursor(self):
        self.resumed_launch()
        paths = [self.state / n for n in ("kimi-launches.json", "kimi-usage-bindings.json", "kimi-usage-cursor.json")]
        saved = [p.read_bytes() for p in paths]
        for event in ({**self.event, "source": "resume", "session_id": "other"}, self.event):
            for path, data in zip(paths, saved):
                path.write_bytes(data)
            self.hook(event)
            self.assertFalse(self.mod.kimi_binding(self.agent))
        for path, data in zip(paths, saved):
            path.write_bytes(data)
        (self.state / "kimi-usage-cursor.json").unlink()
        self.hook({**self.event, "source": "resume"})
        self.assertFalse(self.mod.kimi_binding(self.agent))
        self.assertEqual(self.mod.record(self.agent), 0)

    def test_restart_requires_exact_binding_before_any_window_mutation(self):
        from _seam import seam
        from types import SimpleNamespace

        self.hook()
        self.mod.record(self.agent)
        calls = []

        def tmux(*args, **kwargs):
            calls.append(args)
            if args[0] == "display-message":
                return SimpleNamespace(returncode=0, stdout=str(os.getpid()), stderr="")
            return SimpleNamespace(returncode=0, stdout="%2", stderr="")

        with (
            seam(self.mod, "tmuxio.tmux", tmux),
            seam(self.mod, "agents.agent_window_alive", lambda p: True),
            seam(self.mod, "usage.kimi_usage_adapter", lambda command: self.mod.KIMI_ADAPTER),
        ):
            for fresh, sid in ((False, "other"), (True, "")):
                with self.assertRaises((SystemExit, self.mod.OperationFailed)):
                    self.mod.restart_agent("probe", self.agent, fresh=fresh, sid=sid)
            self.assertEqual(calls, [])
            self.mod.restart_agent("probe", self.agent)
        command = str(calls[0][-1])
        self.assertIn("--session " + self.event["session_id"], command)
        self.assertNotIn("--continue", command)
        current = self.mod.read_config_raw()["participants"][0]
        self.assertNotEqual(current["kimi_usage_generation"], self.token)
        self.assertEqual(current["joined"], "pending")

    def test_restart_refuses_legacy_stale_cursor_and_replaced_source(self):
        from _seam import seam

        self.hook()
        self.mod.record(self.agent)
        cursor = self.state / "kimi-usage-cursor.json"
        saved = cursor.read_bytes()
        with seam(self.mod, "tmuxio.tmux", mock.Mock(side_effect=AssertionError("no window operation permitted"))):
            for updates in (
                {"started_by_room": False},
                {"kimi_usage_generation": "c" * 32},
                {"folder": str(self.root / "other")},
            ):
                with self.subTest(updates=updates), self.assertRaises((SystemExit, self.mod.OperationFailed)):
                    self.mod.restart_agent("probe", {**self.agent, **updates})
            cursor.unlink()
            with self.assertRaises((SystemExit, self.mod.OperationFailed)):
                self.mod.restart_agent("probe", self.agent)
            cursor.write_bytes(saved)
            original = self.path.read_bytes()
            self.path.unlink()
            self.path.write_bytes(original)
            with self.assertRaises((SystemExit, self.mod.OperationFailed)):
                self.mod.restart_agent("probe", self.agent)

    def test_restart_menu_includes_kimi_but_unbound_confirmation_never_offers_fresh(self):
        from types import SimpleNamespace
        from _seam import seam

        ctx = SimpleNamespace(wizard={}, feed=lambda text: None, stale_chat=lambda: False, config=self.mod.read_config_raw())
        said = []
        self.mod.wizard_step(ctx, "/restart", said)
        self.assertIn(("auto", "auto"), ctx.wizard["options"])
        ctx.wizard.update(step="restart_confirm", pid="auto")
        with seam(self.mod, "tmuxio.tmux", mock.Mock(side_effect=AssertionError("no window mutation"))):
            self.mod.wizard_step(ctx, "y", said)
        self.assertFalse(ctx.wizard)
        self.assertEqual(
            said[-1],
            "auto: its conversation could not be confirmed. The running program was left unchanged.",
        )
        self.mod.speak("ru")
        ctx.wizard.update(step="restart_confirm", pid="auto")
        with seam(self.mod, "tmuxio.tmux", mock.Mock(side_effect=AssertionError("no window mutation"))):
            self.mod.wizard_step(ctx, "y", said)
        expected = "auto: не удалось подтвердить его разговор. Работающая программа оставлена без изменений."
        self.assertEqual(said[-1], expected)
        self.assertFalse(ctx.wizard)
        import contextlib
        import io

        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises((SystemExit, self.mod.OperationFailed)):
            self.mod.restart_agent("probe", self.agent)
        self.assertIn("auto: нет подтверждённой управляемой сессии и курсора; перезапуск отменён", errors.getvalue())

    def test_departed_participant_refuses_under_lock_without_touching_window(self):
        from _seam import seam

        self.hook()
        self.mod.record(self.agent)
        self.mod.write_config({"participants": []})
        with seam(self.mod, "tmuxio.tmux", mock.Mock(side_effect=AssertionError("no window mutation"))):
            with self.assertRaises((SystemExit, self.mod.OperationFailed)):
                self.mod.restart_in_window("probe", self.agent, "kimi", self.event["session_id"])

    def test_restart_tmux_failure_preserves_old_proof_binding_cursor(self):
        from _seam import seam
        from types import SimpleNamespace

        self.hook()
        self.mod.record(self.agent)
        paths = [self.state / n for n in ("kimi-launches.json", "kimi-usage-bindings.json", "kimi-usage-cursor.json")]
        before = [p.read_bytes() for p in paths]
        with (
            seam(self.mod, "tmuxio.tmux", lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="refused")),
            seam(self.mod, "agents.agent_window_alive", lambda p: True),
            seam(self.mod, "usage.kimi_usage_adapter", lambda command: self.mod.KIMI_ADAPTER),
        ):
            with self.assertRaises((SystemExit, self.mod.OperationFailed)):
                self.mod.restart_agent("probe", self.agent)
        self.assertEqual([p.read_bytes() for p in paths], before)


class KimiAdoptionTmuxTest(LedgerRoom):
    def test_adoption_guard_runs_after_config_update_then_native_resume_binds(self):
        if not self.mod.shutil.which("tmux"):
            self.skipTest("tmux is not installed")
        home = self.root / "kimi"
        self.enterContext(mock.patch.dict(os.environ, {"KIMI_CODE_HOME": str(home)}))
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        cli = Path(__file__).resolve().parents[1] / "collab"
        native_log = self.root / "native.jsonl"
        guard_log = self.root / "guard.log"
        pane_log = self.root / "pane.log"
        native = bin_dir / "kimi"
        native.write_text(
            f"#!{sys.executable}\n"
            "import json,os,subprocess,sys\n"
            f"with open({str(native_log)!r},'a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')\n"
            "if '--version' in sys.argv:\n"
            "    print('2.4.1');raise SystemExit(0)\n"
            f"event={{'hook_event_name':'SessionStart','source':'resume','client_type':'kimi_code_cli','session_id':'session_one','cwd':{str(self.root)!r}}}\n"
            f"result=subprocess.run([{sys.executable!r},{str(cli)!r},'usage','kimi-start'],input=json.dumps(event),text=True,capture_output=True)\n"
            "print(result.stderr,file=sys.stderr)\n"
            "raise SystemExit(result.returncode)\n"
        )
        native.chmod(0o755)
        lsof = bin_dir / "lsof"
        lsof.write_text(
            f"#!{sys.executable}\n"
            "import json,os,time\n"
            "if os.environ.get('COLLAB_KIMI_GENERATION'):\n"
            f"    with open({str(guard_log)!r},'a') as f:f.write('lsof-started\\n')\n"
            "    deadline=time.monotonic()+10\n"
            "    while time.monotonic()<deadline:\n"
            "        try:\n"
            f"            with open({str(self.mod.room().paths.config_path)!r}) as f:data=json.load(f)\n"
            "            participant=data['participants'][0]\n"
            "            if participant.get('kimi_usage_generation'):\n"
            f"                with open({str(guard_log)!r},'a') as f:f.write('guard-after-config\\n')\n"
            "                break\n"
            "        except Exception as exc:\n"
            f"            with open({str(guard_log)!r},'a') as f:f.write(type(exc).__name__+': '+str(exc)+'\\n')\n"
            "        time.sleep(0.05)\n"
            "    else:\n"
            "        raise SystemExit(2)\n"
            "raise SystemExit(1)\n"
        )
        lsof.chmod(0o755)
        subprocess.run([sys.executable, "-m", "py_compile", str(native), str(lsof)], check=True, capture_output=True)
        tmux_tmp = self.root / "tmux"
        tmux_tmp.mkdir()
        session_name = f"adopt-{os.getpid()}-{time.time_ns()}"
        env = {
            **os.environ,
            "TMUX_TMPDIR": str(tmux_tmp),
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "FAKE_NATIVE_LOG": str(native_log),
            "FAKE_GUARD_LOG": str(guard_log),
            "ADOPTION_COLLAB": str(cli),
            "ADOPTION_PYTHON": sys.executable,
            "ADOPTION_ROOM": str(self.root),
            "ADOPTION_CONFIG": str(self.root / ".collab.json"),
            "ADOPTION_STATE": str(self.state),
        }
        self.enterContext(mock.patch.dict(os.environ, env))
        probe = subprocess.run([str(native), "--session", "session_one"], env=env, capture_output=True, text=True)
        self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)
        native_log.unlink()

        def tmux(*args):
            return subprocess.run(["tmux", *args], env=env, capture_output=True, text=True, check=True)

        tmux(
            "new-session",
            "-d",
            "-s",
            session_name,
            "-n",
            "agent",
            "/bin/sh",
            "-c",
            "printf '╭────╮\\n│ >  │\\n╰────╯\\n'; exec sleep 300",
        )
        self.addCleanup(subprocess.run, ["tmux", "kill-session", "-t", f"={session_name}"], env=env, capture_output=True)
        tmux("set-option", "-t", f"={session_name}:", "@collab_root", os.path.realpath(self.root))
        target = tmux("display-message", "-p", "-t", f"={session_name}:agent", "#{pane_id}").stdout.strip()
        old_pid = tmux("display-message", "-p", "-t", f"={session_name}:agent", "#{pane_pid}").stdout.strip()
        os.environ["ADOPTION_OLD_PID"] = old_pid
        agent = {
            "id": "auto",
            "kind": "kimi",
            "pane_pid": old_pid,
            "target": target,
            "join_code": "old-generation",
            "folder": str(self.root),
            "started_by_room": True,
            "idle": ">",
        }
        self.mod.write_config({"participants": [agent]})
        session = home / "sessions" / self.mod.kimi_workdir_key(str(self.root)) / "session_one"
        wire = session / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True)
        wire.touch()
        (session / "state.json").write_text(
            json.dumps(
                {
                    "id": "session_one",
                    "version": 1,
                    "cwd": str(self.root),
                    "archived": False,
                    "agents": {"main": {"homedir": str(home), "type": "main"}},
                }
            )
        )
        (home / "config.toml").write_text(self.mod.kimi_hook_fragment())
        rules = self.mod.screen_rules(agent)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            shot = self.mod.look(rules, target)
            if self.mod.screen_state(rules, target, shot)[0] == "idle":
                break
            time.sleep(0.05)
        self.assertEqual(self.mod.screen_state(rules, target, shot)[0], "idle", shot.stdout)
        tmux("pipe-pane", "-o", "-t", target, f"cat >> {shlex.quote(str(pane_log))}")
        from _seam import seam

        original_guard = self.mod._kimi_adoption_guard_command

        def logged_guard(*args):
            command = original_guard(*args)
            (self.root / "command.log").write_text(command)
            return command

        with seam(self.mod, "agents._kimi_adoption_guard_command", logged_guard):
            result = self.mod.adopt_kimi_session(session_name, agent, "session_one", execute=True)
        self.assertEqual(result["execution"], "started", result)
        current = self.mod.read_config_raw()["participants"][0]
        deadline = time.monotonic() + 10
        binding = {}
        while time.monotonic() < deadline:
            binding = self.mod.kimi_binding(current)
            if binding:
                break
            time.sleep(0.05)
        pane_status = subprocess.run(
            ["tmux", "display-message", "-p", "-t", current["target"], "#{pane_pid} #{pane_current_command} #{pane_dead}"],
            env=env,
            capture_output=True,
            text=True,
        )
        details = (
            result,
            current,
            pane_status.stdout + pane_status.stderr,
            guard_log.read_text() if guard_log.exists() else "no guard log",
            native_log.read_text() if native_log.exists() else "no native log",
            pane_log.read_text() if pane_log.exists() else "no pane log",
            (self.root / "command.log").read_text(),
            self.mod.kimi_state(self.state / "kimi-launches.json"),
        )
        self.assertEqual(binding.get("session_id"), "session_one", details)
        self.assertIn("guard-after-config", guard_log.read_text())
        self.assertEqual(
            [json.loads(line) for line in native_log.read_text().splitlines()], [["--version"], ["--session", "session_one"]]
        )
        cursor = self.mod.kimi_state(self.state / "kimi-usage-cursor.json")["auto"]
        signature = self.mod.hashlib.sha256(self.mod.json.dumps(binding, sort_keys=True).encode()).hexdigest()
        self.assertEqual(cursor["binding"], signature)
        self.assertNotEqual(current["pane_pid"], old_pid)


class KimiHookInstallerTest(LedgerRoom):
    def setUp(self):
        super().setUp()
        self.config = self.root / "config.toml"
        self.original = b'# keep exactly\n[[hooks]]\nevent = "SessionStart"\ncommand = "true"\nmatcher = "startup"\n[models.other]\nprovider = "x"\nmodel = "x"'
        self.config.write_bytes(self.original)

    def test_install_is_idempotent_and_removal_preserves_foreign_bytes(self):
        self.assertTrue(self.mod.set_kimi_hook(self.config))
        installed = self.config.read_bytes()
        self.assertTrue(installed.startswith(self.original))
        self.assertFalse(self.mod.set_kimi_hook(self.config))
        self.assertEqual(installed, self.config.read_bytes())
        self.assertTrue(self.mod.set_kimi_hook(self.config, remove=True))
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse(self.mod.set_kimi_hook(self.config, remove=True))

    def test_shell_quoting_and_toml_escaping_are_both_correct(self):
        import tomllib

        tool = self.root / "client ' $(touch injected)"
        tool.write_text('import sys\nprint(" ".join(sys.argv[1:]))\n')
        fragment = self.mod.kimi_hook_fragment(sys.executable, str(tool))
        command = tomllib.loads(fragment)["hooks"][0]["command"]
        got = subprocess.run(["/bin/sh", "-c", command], cwd=self.root, capture_output=True, text=True, check=True)
        self.assertEqual(got.stdout.strip(), "usage kimi-start")
        self.assertFalse((self.root / "injected").exists())
        self.assertEqual(tomllib.loads(fragment)["hooks"][0]["matcher"], "^(startup|resume)$")

    def test_edited_owned_rule_and_symlinks_are_refused(self):
        self.mod.set_kimi_hook(self.config)
        self.config.write_text(self.config.read_text().replace("timeout = 5", "timeout = 6"))
        changed = self.config.read_bytes()
        with self.assertRaises(ValueError):
            self.mod.set_kimi_hook(self.config, remove=True)
        self.assertEqual(self.config.read_bytes(), changed)
        other = self.root / "other"
        other.mkdir()
        (other / "config.toml").write_bytes(self.original)
        link = self.root / "linked"
        link.symlink_to(other, target_is_directory=True)
        with self.assertRaises(OSError):
            self.mod.set_kimi_hook(link / "config.toml")
        self.assertEqual((other / "config.toml").read_bytes(), self.original)

    def test_config_replaced_during_preparation_is_not_overwritten(self):
        original_stat = os.stat
        replacement = self.root / "replacement"
        replacement.write_text("# someone else updated the config\n")

        def raced(path, *args, **kwargs):
            if path == "config.toml" and kwargs.get("dir_fd") is not None:
                os.replace(replacement, self.config)
            return original_stat(path, *args, **kwargs)

        with mock.patch.object(os, "stat", side_effect=raced):
            with self.assertRaises(ValueError):
                self.mod.set_kimi_hook(self.config)
        self.assertEqual(self.config.read_text(), "# someone else updated the config\n")
