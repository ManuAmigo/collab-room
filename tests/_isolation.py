"""One place that keeps a test away from the person's own files: HOME and every
location of collab, Claude Code and Codex point into the test's temporary folder."""

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Whoever runs the tests may be inside a room: an agent there has COLLAB_ROOT, COLLAB_NO_ATTACH… and
# the tests took them in — `collab open` answered «run it in a terminal tab» for every agent that
# ran the suite. The settings of collab and of the agents go before any test starts.
INHERITED = ("COLLAB_", "CODEX_HOME", "CLAUDE_CONFIG_DIR")
# The suite's own switch stays (tests/_package.py): dropped here, a run «on the package» would quietly
# run the file — collab never reads it.
KEPT = ("COLLAB_TEST_PACKAGE",)
for _name in [k for k in os.environ if k.startswith(INHERITED) and k not in KEPT]:
    del os.environ[_name]
# Colors and the terminal as a test expects them, whatever the runner has (NO_COLOR and
# TERM=dumb in an agent's shell turned three color tests red).
for _name in ("NO_COLOR", "FORCE_COLOR", "CLICOLOR", "CLICOLOR_FORCE"):
    os.environ.pop(_name, None)
os.environ["TERM"] = "xterm-256color"

# «python3» in a test is the interpreter running the tests (collab needs 3.11+): on
# macOS /usr/bin/python3 is 3.9, and a test with PATH=/usr/bin:/bin ran collab there, which refuses.
# Tests that narrow PATH put PY_BIN first.
PY_BIN = tempfile.mkdtemp(prefix="collab-py-")
os.symlink(sys.executable, os.path.join(PY_BIN, "python3"))
os.environ["PATH"] = PY_BIN + os.pathsep + os.environ.get("PATH", "")
# Removed when the run ends: one folder was left behind for every run (357 of them).
atexit.register(shutil.rmtree, PY_BIN, True)


# Each test's own folder, outside the temporary ones (a copy of collab in a temporary
# folder — where Codex writes by default — is refused always): the install tests keep their HOME and
# copies there. The person's cache folder — never ~/.codex, ~/.claude or ~/.local — and not the
# checkout: a root inside a git repository turned «report outside git» into git. One folder per run,
# gone when it ends. The real HOME is read here, before test_collab points HOME into the run; a HOME
# that is itself temporary leaves the roots where they were.
def _outside_temp(path) -> bool:
    return not (os.path.realpath(path) + "/").startswith(("/private/", "/tmp/", "/var/folders/"))


_CACHE = Path.home() / ("Library/Caches" if sys.platform == "darwin" else ".cache") / "collab-tests"
TEST_ROOTS = None
if _outside_temp(_CACHE.parent):
    _CACHE.mkdir(parents=True, exist_ok=True)
    TEST_ROOTS = tempfile.mkdtemp(prefix="tests-", dir=_CACHE)
    atexit.register(shutil.rmtree, TEST_ROOTS, True)


def isolated(root) -> dict:
    root = Path(root)
    (root / "_home").mkdir(exist_ok=True)
    return {
        "HOME": str(root / "_home"),  # owner.key, ~/.local, ~/.claude, ~/.codex, git config
        "COLLAB_INVITES": str(root / "_invites"),  # ~/.collab-room: invites, consent, owner.key, welcomed
        "COLLAB_ROOMS": str(root / "_rooms.json"),
        "COLLAB_CLAUDE_HOME": str(root / "_claude_home"),
        "COLLAB_CODEX_HOME": str(root / "_codex_home"),
        "CODEX_HOME": str(root / "_codex"),
        "COLLAB_SAFE_DIR": str(root / "_safe"),
        "COLLAB_STATE_HOME": str(root / "_room_homes"),  # where rooms keep their files
        "COLLAB_TEMP_PREFIXES": "/nonexistent-temp-prefix/",  # the test's rooms count as real rooms
        "COLLAB_CLIPBOARD": f"sh -c 'cat > {root / '_clipboard'}'",
    }
