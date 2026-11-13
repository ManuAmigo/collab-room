import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _package import Loader  # noqa: E402
from _seam import seam  # noqa: E402


COLLAB = Path(__file__).resolve().parents[1] / "collab"


class AgentInstallTest(unittest.TestCase):
    def setUp(self):
        self.mod = Loader(f"agent_install_{id(self)}", str(COLLAB)).load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.owner_home = Path(self.tmp.name) / "owner home"
        self.owner_home.mkdir()
        self.enterContext(seam(self.mod, "agents.agent_user_home", lambda: self.owner_home))
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.owner_home), "PATH": "/usr/bin:/bin"}, clear=False))

    def installer(self, root, content):
        script = root / "installer.sh"
        script.write_text(content)
        return script

    def download_url(self, respond):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    respond(self)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever)
        thread.start()

        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(2)

        self.addCleanup(cleanup)
        return f"http://127.0.0.1:{server.server_port}/installer.sh"

    def test_existing_launcher_is_used_and_broken_existing_is_not_replaced(self):
        launcher = self.owner_home / ".local" / "bin" / "claude"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\nexit 0\n")
        launcher.chmod(0o755)
        download = mock.Mock(side_effect=AssertionError("installer must not run"))
        with seam(self.mod, "agents.download_agent_script", download):
            self.assertEqual(self.mod.ensure_agent_cli("claude"), str(launcher))
            launcher.write_text("#!/bin/sh\nexit 1\n")
            with self.assertRaisesRegex(self.mod.AgentCliError, "will not be replaced"):
                self.mod.ensure_agent_cli("claude")
        download.assert_not_called()

    def test_fake_installers_write_only_managed_root_and_do_not_leak_environment(self):
        for kind in ("claude", "codex", "kimi"):
            with self.subTest(kind=kind):
                root = Path(self.tmp.name) / f"managed {kind}"
                legacy = Path(self.tmp.name) / f"legacy {kind}"
                legacy.mkdir()
                for command in ("claude", "codex", "kimi"):
                    shim = legacy / command
                    shim.write_text("#!/bin/sh\nexit 0\n")
                owner_codex_home = Path(self.tmp.name) / "owner codex"
                owner_kimi_home = Path(self.tmp.name) / "owner kimi"
                env = {
                    "PATH": str(legacy) + os.pathsep + "/usr/bin:/bin",
                    "CLAUDE_CONFIG_DIR": str(Path(self.tmp.name) / "owner claude"),
                    "CODEX_HOME": str(owner_codex_home),
                    "CODEX_RELEASE": "owner-version",
                    "KIMI_CODE_HOME": str(owner_kimi_home),
                    "OWNER_ONLY": "must not leak",
                }
                with mock.patch.dict(os.environ, env, clear=False):
                    with seam(self.mod, "agents.agent_install_root", lambda _kind, root=root: root):
                        if kind == "claude":
                            body = r"""
[ -z "${CLAUDE_CONFIG_DIR+x}" ] && [ -z "${CODEX_HOME+x}" ] && [ -z "${KIMI_CODE_HOME+x}" ] && [ -z "${OWNER_ONLY+x}" ] || exit 90
command -v claude >/dev/null 2>&1 && exit 91
mkdir -p "$HOME/.local/bin"
printf '#!/bin/sh\nexit 0\n' > "$HOME/.local/bin/claude"
chmod +x "$HOME/.local/bin/claude"
"""
                        elif kind == "codex":
                            body = r"""
[ -z "${CLAUDE_CONFIG_DIR+x}" ] && [ -z "${KIMI_CODE_HOME+x}" ] && [ -z "${CODEX_RELEASE+x}" ] && [ -z "${OWNER_ONLY+x}" ] || exit 90
[ "$CODEX_NON_INTERACTIVE" = 1 ] || exit 92
command -v codex >/dev/null 2>&1 && exit 91
mkdir -p "$CODEX_INSTALL_DIR"
printf '#!/bin/sh\nexit 0\n' > "$CODEX_INSTALL_DIR/codex"
chmod +x "$CODEX_INSTALL_DIR/codex"
printf '%s' "$HOME" > "$HOME/profile-write"
"""
                        else:
                            body = r"""
[ -z "${CLAUDE_CONFIG_DIR+x}" ] && [ -z "${KIMI_CODE_HOME+x}" ] && [ -z "${CODEX_HOME+x}" ] && [ -z "${OWNER_ONLY+x}" ] || exit 90
[ -n "$KIMI_NO_MODIFY_PATH" ] || exit 92
command -v kimi >/dev/null 2>&1 && exit 91
mkdir -p "$KIMI_INSTALL_DIR/bin"
printf '#!/bin/sh\nexit 0\n' > "$KIMI_INSTALL_DIR/bin/kimi"
chmod +x "$KIMI_INSTALL_DIR/bin/kimi"
"""
                        with seam(
                            self.mod,
                            "agents.download_agent_script",
                            lambda _kind, folder, cancel=None, body=body: self.installer(folder, body),
                        ):
                            program = self.mod.ensure_agent_cli(kind)
                        runtime = dict(os.environ)
                expected = {
                    "claude": root / "home" / ".local" / "bin" / "claude",
                    "codex": root / "bin" / "codex",
                    "kimi": root / "install" / "bin" / "kimi",
                }[kind]
                self.assertEqual(program, str(expected))
                self.assertTrue(expected.exists())
                self.assertTrue((legacy / kind).exists(), "an owner shim was not migrated")
                self.assertEqual(runtime["HOME"], str(self.owner_home))
                self.assertEqual(runtime["CODEX_HOME"], str(owner_codex_home))
                self.assertEqual(runtime["KIMI_CODE_HOME"], str(owner_kimi_home))
                self.assertNotEqual(runtime.get("XDG_CONFIG_HOME"), str(root / "xdg" / "config"))
                self.assertFalse((self.owner_home / ".claude").exists())
                self.assertFalse(owner_codex_home.exists())
                self.assertFalse(owner_kimi_home.exists())

    def test_successful_script_without_a_working_command_does_not_continue(self):
        root = Path(self.tmp.name) / "verify failure"
        with seam(self.mod, "agents.agent_install_root", lambda _kind: root):
            with seam(
                self.mod, "agents.download_agent_script", lambda _kind, folder, cancel=None: self.installer(folder, "exit 0\n")
            ):
                with self.assertRaises(self.mod.AgentCliError):
                    self.mod.ensure_agent_cli("codex")

    def test_cancel_stops_the_whole_installer_process_group(self):
        root = Path(self.tmp.name) / "cancel"
        body = 'sleep 60 &\necho $! > "$HOME/child.pid"\nwait\n'
        started = time.monotonic()
        with seam(self.mod, "agents.agent_install_root", lambda _kind: root):
            with seam(self.mod, "agents.download_agent_script", lambda _kind, folder, cancel=None: self.installer(folder, body)):
                with self.assertRaisesRegex(self.mod.AgentCliError, "canceled"):
                    self.mod.ensure_agent_cli("kimi", cancel=lambda: time.monotonic() - started > 0.15)
        pid = int((root / "home" / "child.pid").read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_managed_cli_is_reused_after_path_reset_for_every_kind(self):
        for kind in ("claude", "codex", "kimi"):
            with self.subTest(kind=kind):
                root = self.owner_home / ".local" / "share" / "collab-room" / "agent-cli" / kind / "persistent"
                downloads = []

                def fake_download(_kind, folder, cancel=None, downloads=downloads):
                    downloads.append(str(folder))
                    path = self.mod.installed_agent_path(_kind, folder)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("#!/bin/sh\nexit 0\n")
                    path.chmod(0o700)
                    return self.installer(folder, "exit 0\n")

                with seam(self.mod, "agents.agent_install_root", lambda _kind, root=root: root):
                    with seam(self.mod, "agents.download_agent_script", fake_download):
                        first = self.mod.ensure_agent_cli(kind)
                        os.environ["PATH"] = "/usr/bin:/bin"
                        second = self.mod.ensure_agent_cli(kind)
                self.assertEqual(first, second)
                self.assertEqual(len(downloads), 1)

    def test_command_exit_retains_a_live_anchor_until_cleanup(self):
        child = self.mod.start_installer_process([sys.executable, "-c", "raise SystemExit(7)"])
        try:
            deadline = time.monotonic() + 3
            while (status := self.mod.installer_status(child)) is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(status, 7)
            self.assertIsNone(child.returncode)
            self.assertEqual(os.getpgid(child.pid), child.pid)
            self.assertEqual(self.mod.installer_status(child), 7)
            self.mod.stop_installer(child)
            self.assertIsNotNone(child.returncode)
        finally:
            if child.returncode is None:
                self.mod.stop_installer(child)

    def test_cleanup_never_signals_group_after_reaping_its_anchor(self):
        from types import SimpleNamespace

        child = SimpleNamespace(pid=43210, returncode=None)
        events = []

        def wait(**kwargs):
            events.append("reap")
            child.returncode = 0
            return 0

        child.wait = wait
        with (
            seam(self.mod, "agents.os.killpg", lambda *args, events=events: events.append("signal")),
            seam(self.mod, "agents.os.getpgid", lambda pid: pid),
            seam(self.mod, "vpn_macos_launch.mac_group_states", lambda group: {}),
        ):
            self.mod.stop_installer(child)
        self.assertEqual(events, ["signal", "signal", "reap"])
        self.assertEqual(events.count("signal"), 2)
        events.clear()
        with seam(self.mod, "agents.os.killpg", lambda *args: events.append("foreign signal")):
            self.mod.stop_installer(child)
        self.assertEqual(events, [])

    def test_inspection_failure_still_kills_reaps_and_closes_status_pipe(self):
        from types import SimpleNamespace

        for failure in (OSError("unavailable"), ValueError("invalid"), subprocess.TimeoutExpired("ps", 2)):
            with self.subTest(failure=type(failure).__name__):
                read_fd, write_fd = os.pipe()
                os.close(write_fd)
                child = SimpleNamespace(pid=43210, returncode=None, _collab_status_fd=read_fd)
                events = []
                child.wait = lambda events=events: events.append("reap")
                try:
                    with (
                        seam(self.mod, "agents.os.killpg", lambda *args, events=events: events.append("signal")),
                        seam(self.mod, "vpn_macos_launch.mac_group_states", mock.Mock(side_effect=failure)),
                    ):
                        self.mod.stop_installer(child)
                    self.assertEqual(events, ["signal", "signal", "reap"])
                    with self.assertRaises(OSError):
                        os.fstat(read_fd)
                    self.assertIsNone(child._collab_status_fd)
                finally:
                    if child._collab_status_fd is not None:
                        os.close(read_fd)

    def test_stop_installer_kills_term_ignoring_children_after_the_leader_exits(self):
        child_file = Path(self.tmp.name) / "term-child.pid"
        code = (
            "import os,signal,time;from pathlib import Path;"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            f"Path({str(child_file)!r}).write_text(str(os.getpid()));time.sleep(60)"
        )
        parent = Path(self.tmp.name) / "term-parent.sh"
        parent.write_text("#!/bin/sh\n" + shlex.join([sys.executable, "-c", code]) + " &\nwait\n")
        process = subprocess.Popen(
            ["/bin/sh", str(parent)], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        try:
            deadline = time.monotonic() + 3
            while not child_file.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            child = int(child_file.read_text())
            self.mod.stop_installer(process)
            self.assertLess(process.poll(), 0)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait()

    def test_download_runs_in_a_fresh_interpreter_without_parent_urlopen(self):
        root = Path(self.tmp.name) / "fresh download"
        root.mkdir()
        body = b"#!/bin/sh\nexit 0\n"

        def respond(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)

        url = self.download_url(respond)
        processes = []
        real_popen = self.mod.subprocess.Popen

        def tracked_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        with seam(self.mod, "agents.AGENT_CLI_SOURCES", {"kimi": (url, "bash")}):
            with seam(self.mod, "agents.subprocess.Popen", tracked_popen):
                with mock.patch.object(self.mod.urllib_request, "urlopen", side_effect=AssertionError("inherited mock")):
                    script = self.mod.download_agent_script("kimi", root)
        self.assertEqual(script.read_bytes(), body)
        self.assertTrue(processes)
        self.assertTrue(all(process.poll() is not None for process in processes))

    def test_download_has_cancel_and_a_wall_clock_deadline(self):
        def slow(handler):
            handler.send_response(200)
            handler.end_headers()
            while True:
                handler.wfile.write(b"x")
                handler.wfile.flush()
                time.sleep(0.03)

        url = self.download_url(slow)
        root = Path(self.tmp.name) / "download"
        root.mkdir()
        started = time.monotonic()
        with seam(self.mod, "agents.AGENT_CLI_SOURCES", {"kimi": (url, "bash")}):
            with self.assertRaisesRegex(self.mod.AgentCliError, "canceled"):
                self.mod.download_agent_script("kimi", root, cancel=lambda: time.monotonic() - started > 0.1)
            with seam(self.mod, "agents.AGENT_CLI_DOWNLOAD_TIMEOUT", 0.1):
                with self.assertRaisesRegex(self.mod.AgentCliError, "timed out"):
                    self.mod.download_agent_script("kimi", root)

        finished = threading.Event()

        def eof(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            finished.set()

        eof_url = self.download_url(eof)
        with seam(self.mod, "agents.AGENT_CLI_SOURCES", {"kimi": (eof_url, "bash")}):
            with self.assertRaisesRegex(self.mod.AgentCliError, "canceled"):
                self.mod.download_agent_script("kimi", root, cancel=finished.is_set)

    def test_blocked_buffered_read_and_close_are_reaped_on_cancel_and_deadline(self):
        for mode in ("cancel", "deadline"):
            with self.subTest(mode=mode):
                entered = threading.Event()
                release = threading.Event()
                cancel = threading.Event()

                def respond(handler, entered=entered, release=release):
                    handler.send_response(200)
                    handler.end_headers()
                    entered.set()
                    release.wait(30)

                url = self.download_url(respond)
                root = Path(self.tmp.name) / f"blocked download {mode}"
                root.mkdir()
                outcome = []
                processes = []
                real_popen = self.mod.subprocess.Popen

                def tracked_popen(*args, _popen=real_popen, _processes=processes, **kwargs):
                    process = _popen(*args, **kwargs)
                    _processes.append(process)
                    return process

                def work(root=root, cancel=cancel, outcome=outcome):
                    try:
                        self.mod.download_agent_script("kimi", root, cancel=cancel.is_set)
                    except self.mod.AgentCliError as exc:
                        outcome.append(str(exc))

                with seam(self.mod, "agents.AGENT_CLI_SOURCES", {"kimi": (url, "bash")}):
                    # Cancellation must win over an independent download timeout, even
                    # when starting the worker takes time on a busy machine.
                    with seam(self.mod, "agents.AGENT_CLI_DOWNLOAD_TIMEOUT", 30 if mode == "cancel" else 2):
                        with seam(self.mod, "agents.subprocess.Popen", tracked_popen):
                            worker = threading.Thread(target=work)
                            worker.start()
                            try:
                                self.assertTrue(entered.wait(3))
                                if mode == "cancel":
                                    cancel.set()
                                worker.join(0.5 if mode == "cancel" else 3)
                                self.assertFalse(worker.is_alive())
                            finally:
                                release.set()
                                worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(outcome, ["installation canceled" if mode == "cancel" else "kimi download timed out"])
                self.assertFalse((root / "installer.sh").exists())
                self.assertTrue(processes)
                self.assertTrue(all(process.poll() is not None for process in processes))

    def test_partial_and_malformed_worker_output_do_not_block_cancellation(self):
        for mode in ("cancel", "deadline", "malformed"):
            with self.subTest(mode=mode):
                root = Path(self.tmp.name) / f"worker output {mode}"
                root.mkdir()
                code = (
                    "print('not json',flush=True)"
                    if mode == "malformed"
                    else "import sys,time;sys.stdout.write('[');sys.stdout.flush();time.sleep(60)"
                )
                processes = []
                real_popen = self.mod.subprocess.Popen

                def tracked_popen(*args, _popen=real_popen, _processes=processes, **kwargs):
                    process = _popen(*args, **kwargs)
                    _processes.append(process)
                    return process

                started = time.monotonic()
                outcome = []

                def work(root=root, mode=mode, started=started, outcome=outcome):
                    try:
                        self.mod.download_agent_script(
                            "kimi", root, cancel=(lambda: time.monotonic() - started > 0.15) if mode == "cancel" else None
                        )
                    except BaseException as exc:
                        outcome.append((type(exc), str(exc)))

                with seam(self.mod, "agents.AGENT_DOWNLOAD_BOOTSTRAP", code):
                    with seam(self.mod, "agents.AGENT_CLI_DOWNLOAD_TIMEOUT", 0.2):
                        with seam(self.mod, "agents.subprocess.Popen", tracked_popen):
                            worker = threading.Thread(target=work)
                            worker.start()
                            try:
                                worker.join(0.7)
                                self.assertFalse(worker.is_alive())
                            finally:
                                for process in processes:
                                    if process.poll() is None:
                                        self.mod.stop_installer(process)
                                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertTrue(processes)
                self.assertTrue(all(process.poll() is not None for process in processes))
                self.assertEqual(len(outcome), 1)
                error, message = outcome[0]
                self.assertIs(error, self.mod.AgentCliError)
                expected = "canceled" if mode == "cancel" else "timed out" if mode == "deadline" else "could not download"
                self.assertIn(expected, message)

    def test_exited_worker_descendant_and_object_schema_do_not_block_or_pass(self):
        cases = {
            "stderr_descendant": "import os,time;pid=os.fork();time.sleep(.1) if pid==0 else None;os.close(1);time.sleep(60) if pid==0 else None",
            "dict_schema": 'import time;time.sleep(.25);print(\'{"ok":null,"detail":null}\',flush=True)',
        }
        for mode, code in cases.items():
            with self.subTest(mode=mode):
                root = Path(self.tmp.name) / f"worker protocol {mode}"
                root.mkdir()
                processes = []
                real_popen = self.mod.subprocess.Popen

                def tracked_popen(*args, _popen=real_popen, _processes=processes, **kwargs):
                    process = _popen(*args, **kwargs)
                    _processes.append(process)
                    return process

                outcome = []

                def work(root=root, outcome=outcome):
                    try:
                        self.mod.download_agent_script("kimi", root)
                    except BaseException as exc:
                        outcome.append((type(exc), str(exc)))

                with seam(self.mod, "agents.AGENT_DOWNLOAD_BOOTSTRAP", code):
                    # Keep the protocol assertion independent of startup; the outer join still bounds failure.
                    with seam(self.mod, "agents.subprocess.Popen", tracked_popen):
                        worker = threading.Thread(target=work)
                        worker.start()
                        try:
                            worker.join(0.7)
                            self.assertFalse(worker.is_alive())
                        finally:
                            for process in processes:
                                self.mod.stop_installer(process)
                            worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertTrue(processes)
                self.assertTrue(all(process.poll() is not None for process in processes))
                self.assertEqual(len(outcome), 1)
                error, message = outcome[0]
                self.assertIs(error, self.mod.AgentCliError)
                self.assertIn("could not download", message)
                self.assertFalse((root / "installer.sh").exists())

    def test_terminal_worker_results_reap_term_ignoring_descendants(self):
        for status in ("ok", "agent", "download", "internal"):
            with self.subTest(status=status):
                root = Path(self.tmp.name) / f"terminal worker {status}"
                root.mkdir()
                child_file = root / "child.pid"
                child = (
                    "import os,signal,time;from pathlib import Path;"
                    "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                    f"Path({str(child_file)!r}).write_text(str(os.getpid()));time.sleep(60)"
                )
                code = (
                    "import json,subprocess,sys,time;from pathlib import Path;"
                    f"subprocess.Popen([sys.executable,'-c',{child!r}]);"
                    f"p=Path({str(child_file)!r})\n"
                    "while not p.exists():time.sleep(.01)\n"
                    f"print(json.dumps([{status!r},'detail']),flush=True)"
                )
                processes = []
                real_popen = self.mod.subprocess.Popen

                def tracked_popen(*args, _popen=real_popen, _processes=processes, **kwargs):
                    process = _popen(*args, **kwargs)
                    _processes.append(process)
                    return process

                def alive(pid):
                    try:
                        os.kill(pid, 0)
                        return True
                    except ProcessLookupError:
                        return False

                try:
                    with seam(self.mod, "agents.AGENT_DOWNLOAD_BOOTSTRAP", code):
                        with seam(self.mod, "agents.subprocess.Popen", tracked_popen):
                            if status == "ok":
                                self.assertEqual(self.mod.download_agent_script("kimi", root), root / "installer.sh")
                            else:
                                with self.assertRaises(self.mod.AgentCliError):
                                    self.mod.download_agent_script("kimi", root)
                    pid = int(child_file.read_text())
                    deadline = time.monotonic() + 0.7
                    while alive(pid) and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertFalse(alive(pid))
                    self.assertTrue(processes)
                    self.assertTrue(all(process.poll() is not None for process in processes))
                finally:
                    for process in processes:
                        self.mod.stop_installer(process)

    def test_progress_uses_room_language(self):
        for lang, kind, expected in (
            ("en", "claude", "Downloading the official claude installer…"),
            ("ru", "kimi", "Скачивается официальный установщик kimi…"),
        ):
            with self.subTest(lang=lang):
                root = Path(self.tmp.name) / f"progress {lang}"
                self.mod.speak(lang)
                progress = []

                def fake_download(_kind, folder, cancel=None):
                    path = self.mod.installed_agent_path(_kind, folder)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("#!/bin/sh\nexit 0\n")
                    path.chmod(0o700)
                    return self.installer(folder, "exit 0\n")

                with seam(self.mod, "store.room_language", lambda config, lang=lang: lang):
                    with seam(self.mod, "agents.agent_install_root", lambda _kind, root=root: root):
                        with seam(self.mod, "agents.download_agent_script", fake_download):
                            self.mod.ensure_agent_cli(kind, progress=progress.append)
                self.assertEqual(progress[0], expected)


if __name__ == "__main__":
    unittest.main()
