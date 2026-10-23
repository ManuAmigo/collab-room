from __future__ import annotations
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
import collab.text as m_text
import collab.store as m_store


VIEW_OF = "@collab_view_of"  # a view's mark (/open): the session of the room whose window it shows


def session_room(session: str) -> str:
    """The real path of the room a running tmux session was started for ("" — not marked: older)."""
    r = tmux("show-options", "-v", "-t", f"={session}:", "@collab_root", check=False)
    return (r.stdout or "").strip() if r.returncode == 0 else ""


TMUX = os.environ.get("COLLAB_TMUX", "tmux")


def tmux_dir(folder: str) -> str:
    """A folder for tmux's -c: tmux expands formats there too (tmux 3.8: a folder
    «x#{session_name}y» started its window in «xy», a missing one — in ~). `##` is a plain `#`. The
    pane's `cd` (in_dir) still enters the right folder; this keeps tmux's own idea of it right."""
    return folder.replace("#", "##")


def in_dir(folder: str, command: str) -> str:
    """The pane's command, entering its folder itself. tmux's own -c is not enough: a tmux server
    started from a restricted program could not enter folders and silently started every new
    window in its own, already deleted, folder — an agent would have worked in the wrong project.
    `exec` keeps the program the pane's own process.
    No VIRTUAL_ENV: a tmux server started from a shell with a project's venv active keeps it for
    every window it opens, and `-e` can set a variable but not remove it. `env -u`, not `unset`:
    tmux runs this line in its default-shell, and fish has no `unset`; env execs the program."""
    shell = shlex.quote(os.environ.get("SHELL", "/bin/sh"))
    return f"cd {shlex.quote(folder)} && exec env -u VIRTUAL_ENV {command or shell}"


# tmux answers at once; one that does not (typing into a scrolled window without a client) must not
# hang the deliverer, the chat or a command with it.
TMUX_TIMEOUT = 15


def mac_on_intel() -> bool:
    """An Intel Mac. Asked of the machine, not of this Python: one running under Rosetta on Apple
    Silicon says x86_64 too, while the hardware's own flag says arm64."""
    if sys.platform != "darwin":
        return False
    try:
        # By its full path: /usr/sbin is not on every PATH, and without it the Mac read as Apple Silicon.
        r = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "hw.optional.arm64"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False  # unknown: the general advice, as before
    return r.stdout.strip() != "1"


def working_brew() -> str:
    """Homebrew that runs on this Mac — its path as the person types it («brew» on PATH), or "".
    An Apple Silicon Homebrew carried over to an Intel Mac stays at /opt/homebrew, answers `--version`
    (a shell script) and then fails on its arm64 Ruby with «Bad CPU type in executable»: on an Intel
    Mac /opt/homebrew, Apple Silicon's own prefix, does not count. Any other is asked for its version.
    Homebrew just installed is often not on PATH yet — then its full path. A dead one on PATH does not
    hide a working one at the other prefix."""
    on_path = shutil.which("brew") or ""
    for found in dict.fromkeys(b for b in (on_path, "/opt/homebrew/bin/brew", "/usr/local/bin/brew") if b):
        if not os.access(found, os.X_OK):
            continue
        if os.path.realpath(found).startswith("/opt/homebrew/") and mac_on_intel():
            continue
        try:
            r = subprocess.run([found, "--version"], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            continue
        if not r.returncode:
            return "brew" if found == on_path else found
    return ""


def tmux_install_hint() -> str:
    """How to get tmux, where it is missing. On a new Mac there is no Homebrew, and «brew install tmux»
    was a dead end: install sets Python up itself, tmux it only named. Homebrew installs on Apple Silicon
    only now: an Intel Mac is sent to MacPorts, not to a Homebrew that will not run there."""
    if sys.platform != "darwin":
        return "install it with your system's package manager (apt install tmux, dnf install tmux)"
    brew = working_brew()
    if brew:
        return f"install it: {brew} install tmux"
    if mac_on_intel():
        return "Homebrew no longer installs on Intel Macs — install MacPorts (https://www.macports.org), then: sudo port install tmux"
    return "install Homebrew first (the one-line command on https://brew.sh), then: brew install tmux"


def tmux(*argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """A tmux command, never waiting forever: typing into a scrolled window without a client hung
    tmux — and the deliverer with it."""
    try:
        r = subprocess.run([TMUX, *argv], text=True, capture_output=True, timeout=TMUX_TIMEOUT)
    except FileNotFoundError:
        # No tmux at all: pause, resume and stop died with a traceback. Asked in
        # passing, it is «no room running»; needed, it is one line saying what to install.
        if check:
            m_store.fail(f"tmux is required: {tmux_install_hint()}, and run `collab start` again")
        return subprocess.CompletedProcess([TMUX, *argv], 127, "", "tmux is not installed")
    except subprocess.TimeoutExpired:
        if check:
            m_store.fail(
                f"tmux did not answer in {TMUX_TIMEOUT} s to `{argv[0]}`: check that it runs (`tmux ls`), "
                "then run the command again"
            )
        return subprocess.CompletedProcess([TMUX, *argv], 124, "", f"tmux did not answer in {TMUX_TIMEOUT} s")
    # Needed and refused (no room for a window, a server gone): tmux's own reason, not a
    # CalledProcessError traceback of the whole command line.
    if check and r.returncode:
        reason = m_text.visible(r.stderr.strip()) or f"exit code {r.returncode}"
        m_store.fail(f"tmux {argv[0]} failed: {reason}")
    return r


def path_without_venv() -> str:
    """This process's PATH without the active venv's bin (VIRTUAL_ENV): a room started from a shell
    with a project's venv active ran its chat, delivery and every window on that venv's python3."""
    path = os.environ.get("PATH", "")
    venv = os.environ.get("VIRTUAL_ENV", "")
    if not venv:
        return path
    venv_bin = os.path.normpath(os.path.join(venv, "bin"))
    return os.pathsep.join(d for d in path.split(os.pathsep) if not d or os.path.normpath(d) != venv_bin)


def leave_venv() -> str:
    """The active venv, left out of this process — PATH, VIRTUAL_ENV — and so out of everything it
    starts: the tmux server, the programs it finds on PATH. Returns the venv's folder, or ""."""
    venv = os.environ.get("VIRTUAL_ENV", "")
    if venv:
        os.environ["PATH"] = path_without_venv()
        del os.environ["VIRTUAL_ENV"]
    return venv


def pane_env() -> list[str]:
    """Panes the room opens get this process's PATH and the room's path: the tmux server's own
    environment may be older or different (a test once launched the real Codex).
    Not an active venv's bin: the room's python3 is not a project's."""
    rp = m_store.room().paths
    env = ["-e", f"PATH={path_without_venv()}", "-e", f"COLLAB_ROOT={rp.root}"]
    # The room's own settings go along too (where invitations, the room list, the welcome mark and
    # the agents' history live): without them a test's chat wrote into the person's real home.
    # CODEX_HOME too: the room writes a Codex agent's rules there, and a Codex started without it
    # read ~/.codex — no rules, no sign-in. COLLAB_SAFE_DIR too: without it a window
    # kept the read-only copies for Codex rules in the tmux server's ~/.local/share — in the tests,
    # the person's real one.
    for name in (
        "COLLAB_INVITES",
        "COLLAB_ROOMS",
        "COLLAB_CLAUDE_HOME",
        "COLLAB_CODEX_HOME",
        "COLLAB_TEMP_PREFIXES",
        "COLLAB_CLIPBOARD",
        "CODEX_HOME",
        "COLLAB_STATE_HOME",
        "COLLAB_SAFE_DIR",
    ):
        if os.environ.get(name):
            env += ["-e", f"{name}={os.environ[name]}"]
    return env


def exact(target: str) -> str:
    """`codex` → `=codex:`, `work:0.1` → `=work:0.1`: tmux otherwise matches a session by prefix."""
    if target.startswith(("=", "%")):
        return target
    session, _, rest = target.partition(":")
    # `=name` alone names a session, not a pane; `=name:` is its current window's active pane.
    return f"={session}:{rest}"


def tmux_text(text: str, clock: bool = False) -> str:
    """Text for a tmux format (status-left): `#(…)` there runs a shell command, and names are set by
    agents themselves (join --name, /name) — a name ran a command at /open.
    `##` is a plain `#`, so #(…), #[…] and #{…} stay text; control characters are dropped.
    clock: the option is read as a time format too (status-left, set-titles-string) — «50%d» showed
    as «5018»; `%%` is a plain `%` there. Not for a value a format only refers to
    (a border's @collab_title): there `%%` would stay doubled."""
    text = "".join(c for c in text if c.isprintable()).replace("#", "##")
    return text.replace("%", "%%") if clock else text


def attach(session: str) -> None:
    """Put the person into the room: switch inside tmux, attach from a plain terminal."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()) or args_no_attach():
        print(f"Attach: tmux attach -t {session}")
        return
    if os.environ.get("TMUX"):
        os.execvp(TMUX, [TMUX, "switch-client", "-t", f"={session}"])
    os.execvp(TMUX, [TMUX, "attach", "-t", f"={session}"])


def tmux_at_least(major: int, minor: int) -> bool:
    found = re.search(r"(\d+)\.(\d+)", tmux("-V", check=False).stdout or "")
    return bool(found) and (int(found.group(1)), int(found.group(2))) >= (major, minor)


def args_no_attach() -> bool:
    return bool(os.environ.get("COLLAB_NO_ATTACH"))


def tmux_can_read(folder: Path) -> bool:
    """Can the running tmux server read the room's folder? Its windows start there. On macOS a
    server started from a program without access to ~/Documents (privacy protection) opens the
    windows and they all die at once: the room «[exited]» with no reason.
    No server yet — the one about to start inherits this terminal's access: assume yes."""
    if tmux("has-session", check=False).returncode != 0 and "no server" in (tmux("list-sessions", check=False).stderr or ""):
        return True
    # `##` is a plain `#` to run-shell: tmux expands #(…) in its command before any shell sees it, so a
    # folder named «#(touch x)» ran that command when chosen in /add or started.
    # `--`: a folder «-la» is a folder, not ls's option.
    command = f"ls -- {shlex.quote(str(folder))} >/dev/null 2>&1 || echo NO-ACCESS".replace("#", "##")
    r = tmux("run-shell", command, check=False)
    return "NO-ACCESS" not in (r.stdout or "")
