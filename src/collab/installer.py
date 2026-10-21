from __future__ import annotations
import argparse
import contextlib
import errno
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
import collab.boot as m_boot
import collab.text as m_text
import collab.term as m_term
import collab.store as m_store
import collab.tmuxio as m_tmuxio
import collab.log as m_log
import collab.screen as m_screen


# A Codex the room starts answers in that room without asking («adding Codex must be
# easy»), but safely (a rule for the repo's collab let a sandboxed Codex
# rewrite it and run it unsandboxed, for any room). So: consent once at install; then for each
# Codex the room starts, an allow rule for exactly its room and its own name, running a read-only
# copy of collab kept where a sandboxed Codex cannot write.
RULES_HEADER = "# collab-room: lets each Codex a room started answer in that room. Managed by collab.\n"


def copy_name(data: bytes | None = None) -> str:
    return f"collab-{hashlib.sha256(Path(m_boot.SELF).read_bytes() if data is None else data).hexdigest()[:16]}"


def installed_collab() -> Path:
    """The collab the `collab` command runs now (this one, when install has not said)."""
    inst = m_boot.paths()
    with contextlib.suppress(OSError):
        real = Path(os.path.realpath(m_boot.read_private(inst.installed_file) or m_boot.SELF))
        if real.is_file():
            return real
    return Path(m_boot.SELF)


def installed_name() -> str:
    """The content name of what the `collab` command runs now — to tell a chat or a Codex it is older."""
    return copy_name(installed_collab().read_bytes())


class LinkedCopies(OSError):
    """A folder on the way to the read-only copies is a link: a copy written through it lands where
    the link leads, perhaps where a Codex writes — no protection at all."""


def refuse_linked_copies(path: Path) -> None:
    """Refuse when SAFE_COPIES or any level below it down to `path` is a link (a link is
    not followed and not chmod'ed — refused), or when a link above it leads where a Codex may write.
    Checked before «the copy is already there»: one written through the link earlier would otherwise
    be handed out."""
    inst = m_boot.paths()
    for level in (path, *path.parents):
        if os.path.islink(level):
            raise LinkedCopies(
                errno.ELOOP,
                f"{m_text.tilde(str(level))} is a link — the copy would not be protected; remove the link and run install again",
            )
        if level == inst.safe_copies:
            break
    # Where it really is must be out of a Codex's reach, link or no link. Both ways: the copies inside
    # a folder a Codex writes, or such a folder inside the copies.
    real = Path(os.path.realpath(inst.safe_copies))

    def overlaps(folder: str) -> bool:
        # By path too, not only by file identity: a root not made yet (…/command/<hash> to come) is
        # reachable all the same. realpath resolves what exists of it.
        # Case-folded on macOS: its own disks ignore case, and realpath keeps the case it was given in
        # for the part not made yet. Not on Linux: /home/a and /home/A are two.
        fold = (lambda x: x.casefold()) if sys.platform == "darwin" else (lambda x: x)
        there = fold(os.path.realpath(os.path.expanduser(folder)))
        here = fold(str(real))
        if there == here or here.startswith(there.rstrip("/") + "/") or there.startswith(here + "/"):
            return True
        return os.path.exists(folder) and (
            (real.exists() and os.path.samefile(real, folder))
            or m_boot._inside(str(real), folder)
            or m_boot._inside(folder, str(real))
        )

    roots = codex_writable_roots()
    # «X is at X» said nothing: the real place only when it is another one.
    place = m_text.tilde(str(inst.safe_copies)) + (
        "" if m_text.tilde(str(real)) == m_text.tilde(str(inst.safe_copies)) else f" (at {m_text.tilde(str(real))})"
    )
    for root in roots or []:
        if overlaps(str(root)):
            raise LinkedCopies(
                errno.EACCES,
                f"{place}: Codex may write {m_text.tilde(str(root))} (writable_roots) — the "
                "copy would not be protected; keep it outside and run install again",
            )
    # A Codex writes its own folder without any writable_roots: an agent added in
    # ~ (/add → Browse → home) could change the copies with no link at all. Named, to know whom.
    for who, folder in codex_workplaces():
        if overlaps(folder):
            raise LinkedCopies(
                errno.EACCES,
                f"{who} works in {m_text.tilde(folder)}, above the copies in "
                f"{m_text.tilde(str(real))} — the copy would not be protected; move that agent or the "
                "copies (COLLAB_SAFE_DIR) and run install again",
            )
    # A link above it (~/.local, ~/.local/share — dotfiles kept elsewhere) is the person's own and
    # allowed, unless it leads into a temporary folder, or Codex's config cannot
    # be read to tell. Only links between ~ and it count: /tmp, /var, ~ itself may be the system's own.
    # A temporary folder is where a Codex writes by default: a copy there protects nothing, however it
    # was put there — COLLAB_SAFE_DIR included, a setting does not weaken a check («а»).
    if m_boot.in_temp_folder(str(real)):
        raise LinkedCopies(
            errno.EACCES,
            f"{place} is a temporary folder, where a Codex may write — the copy "
            "would not be protected; keep it outside (COLLAB_SAFE_DIR) and run install again",
        )
    home = Path.home()
    moved = home in inst.safe_copies.parents and real != Path(os.path.realpath(home)) / inst.safe_copies.relative_to(home)
    if moved and roots is None:
        raise LinkedCopies(
            errno.EACCES,
            f"{m_text.tilde(str(inst.safe_copies))} is at {m_text.tilde(str(real))}, where a Codex "
            "may write — the copy would not be protected; keep it outside and run install again",
        )


def current_name() -> str:
    """What «newer» means for this process. Run from the installed copy (it never changes): what the
    command leads to now. Run from a clone or a working copy (the file itself changes): this file now —
    a developer's chat from a working copy was told «older» for good, beside any install."""
    inst = m_boot.paths()
    return installed_name() if (inst.safe_copies / "command").resolve() in Path(m_boot.SELF).resolve().parents else copy_name()


def codex_workplaces() -> list[tuple[str, str]]:
    """Every folder a room's Codex may write as its own, named: its own folder, and the project of a
    room that has one. Not a Claude: it runs with no sandbox — it could change the copies from
    anywhere, and a Claude in ~ would only have refused install for nothing. An
    agent of unknown kind (added by hand with its own command) counts: it may be a Codex — a refusal
    more rather than a copy left open. The people's own seats do not."""
    out: list[tuple[str, str]] = []
    for who, folder in m_boot.agent_workplaces(
        lambda p: not m_store.is_chat_command(p.get("command")) and p["id"] != "owner" and m_screen.agent_kind(p) != "claude"
    ):
        out.append((m_text.clean_field(who, 40) if who else "a Codex of a room", folder))
    return out


def command_copy() -> Path:
    """The person's `collab` command: this collab, read-only, in a folder named by its
    content where a Codex does not write. Called «collab», not «collab-<hash>» — that name is the
    Codex rule's copy, which only answers in a room."""
    inst = m_boot.paths()
    data = Path(m_boot.SELF).read_bytes()
    folder = inst.safe_copies / "command" / copy_name(data)[len("collab-") :]
    path = folder / "collab"
    refuse_linked_copies(folder)
    if not path.is_file() or path.is_symlink() or path.read_bytes() != data:
        m_store.private_dir(folder, strict=False)  # the person's only; the file in it stays 0555
        tmp = folder / f".collab.{secrets.token_hex(8)}.tmp"
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o555)  # runs as a command; written by no one
        os.replace(tmp, path)
    return path


def safe_copy() -> Path:
    """This collab, read-only, named by its content: an update is a new file, never an edit."""
    inst = m_boot.paths()
    data = Path(m_boot.SELF).read_bytes()
    path = inst.safe_copies / copy_name(data)
    refuse_linked_copies(inst.safe_copies)  # a rule for a copy reached through a link protects nothing
    if not path.is_file() or path.is_symlink() or path.read_bytes() != data:
        m_store.private_dir(inst.safe_copies, strict=False)
        m_store.replace_file(path, data, 0o444)
    return path


def codex_writable_roots() -> list[Path] | None:
    """Every writable_roots of Codex's config, wherever it stands (top level, a profile, a sandbox
    table), read as TOML — a regular expression saw only "double-quoted" paths and missed
    'single-quoted' ones. None when it can't be read for sure (unreadable, or
    invalid TOML): then no rule is given."""
    inst = m_boot.paths()
    try:
        import tomllib  # Python 3.11+ — not yet in install on an older python3, before the hand-over
    except ModuleNotFoundError:
        return None  # not sure, so no rule (and no copy behind a link)
    path = inst.codex_home / "config.toml"
    if not path.exists():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):  # not sure, so no rule
        return None
    roots: list[Path] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "writable_roots" and isinstance(value, list):
                    roots.extend(Path(str(x)).expanduser() for x in value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return roots


def codex_can_touch_python(python: str, folder: str) -> bool:
    """Could Codex change what this interpreter runs: the binary or anything of its install — its
    standard library lies next to it (…/bin/python → …/lib/pythonX.Y). A writable root inside the
    install counts too."""
    prefix = Path(os.path.realpath(python)).parent.parent
    if codex_can_write(prefix, folder):
        return True
    found = codex_writable_roots() or []
    return any(prefix in r.resolve().parents for r in [Path(folder), *found])


def codex_can_write(path: Path, folder: str) -> bool:
    """Could a sandboxed Codex working in `folder` change `path`: its folder, the writable_roots of
    its config, temporary folders. Then the copy is no protection, and no rule is written."""
    roots = [Path(folder)]
    found = codex_writable_roots()
    if found is None:
        return True  # the config could not be read for sure: treat everything as writable
    roots += found
    real = path.resolve()
    # COLLAB_TEMP_PREFIXES only says which rooms the menu lists; this is the fixed list.
    return any(real == r.resolve() or r.resolve() in real.parents for r in roots) or m_boot.in_temp_folder(str(real))


@m_store.shared_locked
def set_agent_rule(room_dir: str, pid: str, cli: str) -> None:
    """Write (cli) or drop (cli="") the rules of one agent of one room; the others stay. A file
    that is not ours, or a link, is never touched."""
    inst = m_boot.paths()
    if inst.agent_rules.is_symlink():
        return
    try:
        text = inst.agent_rules.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if text and not text.startswith(RULES_HEADER):
        return
    tag = f"# room={json.dumps(room_dir, ensure_ascii=False)} id={pid}\n"
    blocks = [b for b in re.split(r"(?m)^(?=# room=)", text[len(RULES_HEADER) :]) if b.strip() and not b.startswith(tag)]
    if cli:
        blocks.append(
            tag
            + "".join(
                "prefix_rule(pattern="
                + json.dumps([*RULE_PYTHON, cli, command, "--room", room_dir, flag, pid], ensure_ascii=False)
                + ', decision="allow")\n'
                for command, flag in (("say", "--from"), ("ack", "--for"), ("inbox", "--for"))
            )
        )
    inst.agent_rules.parent.mkdir(parents=True, exist_ok=True)
    m_store.replace_file(inst.agent_rules, (RULES_HEADER + "".join(blocks)).encode("utf-8"), 0o644)


@m_store.shared_locked
def prune_agent_rules(keep_consent: bool = True) -> None:
    """Drop rules nobody should hold any more (they outlived what they
    were for): of a room whose folder or .collab.json is gone, of an agent no longer in its room
    or no longer a room-started Codex — and all of them when the person has withdrawn consent."""
    inst = m_boot.paths()
    if inst.agent_rules.is_symlink():
        return
    try:
        text = inst.agent_rules.read_text(encoding="utf-8")
    except OSError:
        return
    if not text.startswith(RULES_HEADER):
        return
    kept = []
    for block in re.split(r"(?m)^(?=# room=)", text[len(RULES_HEADER) :]):
        m = re.match(r'# room=(".*") id=(\S+)\n', block)
        if not m or not keep_consent or not inst.codex_consent.exists():
            continue
        try:
            data = json.loads(m_boot.room_config(Path(json.loads(m.group(1)))).read_text(encoding="utf-8"))
            alive = any(
                q.get("id") == m.group(2) and q.get("kind") == "codex" and q.get("started_by_room")
                for q in data.get("participants", [])
            )
        except (OSError, ValueError, AttributeError):
            alive = False
        if alive:
            kept.append(block)
    if "".join(kept) != text[len(RULES_HEADER) :]:
        m_store.replace_file(inst.agent_rules, (RULES_HEADER + "".join(kept)).encode("utf-8"), 0o644)


def room_spellings() -> list[str]:
    """The room's path as hints may spell it: as given and resolved (/var → /private/var on macOS)
    — the rule matches the command's words exactly."""
    rp = m_store.room().paths
    return list(dict.fromkeys([str(rp.root), str(rp.root.resolve())]))


def codex_room_rule(pid: str, folder: str) -> str:
    """Before a Codex the room starts: its rule, if the person agreed at install. Returns the
    collab its hints must name ("" — none: Codex will ask before answering, as before)."""
    inst = m_boot.paths()
    rp = m_store.room().paths
    try:
        prune_agent_rules()
        if not inst.codex_consent.exists():
            return ""
        cli = safe_copy()
        # Nor when Codex could replace the room's folder itself (it lies inside what Codex may
        # write): the rule names the folder by path, and a link put in its place would lead the
        # unsandboxed copy anywhere.
        # Nor the recorded Python: collab started by an older python3 runs whatever it names.
        # Nor when this collab runs on a Python we would not hand over to (in home, in a venv…): the
        # rule names sys.executable, run outside the sandbox.
        if (
            not m_boot.trusted_interpreter(sys.executable)
            or codex_can_write(cli, folder)
            or codex_can_write(rp.root.resolve().parent, folder)
            or codex_can_touch_python(sys.executable, folder)
            or codex_can_write(rp.base, folder)
            or codex_can_write(inst.python_file, folder)
            or (bool(m_boot.recorded_python()) and codex_can_touch_python(m_boot.recorded_python(), folder))
            # The folders of the recorded link: Codex changing a link changes what runs.
            or any(codex_can_write(Path(f), folder) for f in m_boot.link_folders(m_boot.recorded_python(as_written=True)))
        ):
            for spelling in room_spellings():
                set_agent_rule(spelling, pid, "")
            return ""
        for spelling in room_spellings():
            set_agent_rule(spelling, pid, str(cli))
    except OSError:
        # No copy to name (its folder is a link, or unwritable): a rule given earlier must not stay
        # and keep running the old copy outside the sandbox.
        with contextlib.suppress(OSError, SystemExit, m_store.OperationFailed):
            for spelling in room_spellings():
                set_agent_rule(spelling, pid, "")
        return ""
    return str(cli)


# The interpreter a Codex rule allows: this exact one by its absolute path, isolated (-I: no
# PYTHON* variables, no module next to the script) — a bare «python3» matched any file of that name,
# also one a sandboxed Codex had put in its own folder.
# The real file, the one the checks looked at — not a link Codex might replace afterwards; -S: no
# site packages, no .pth to run.
RULE_PYTHON = [os.path.realpath(sys.executable), "-I", "-S"]


def python311_version(path: str) -> str | None:
    """«3.12.4» when this interpreter is Python 3.11 or newer, else None. Asked isolated (-I -S), as the
    hand-over runs it: PYTHONPATH's sitecustomize.py ran here."""
    try:
        r = subprocess.run(
            [path, "-I", "-S", "-c", "import sys; print(sys.version.split()[0]); sys.exit(sys.version_info < (3, 11))"],
            text=True,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def lasting_python_path(real: str) -> str:
    """Homebrew's real path names the exact version — …/Cellar/python@3.14/3.14.7/…/python3.14 — and
    the next `brew upgrade` with its cleanup deletes it: every collab on an older python3 then refused
    until install ran again. Its links in opt/ and bin/ follow
    upgrades: one of them, if it leads to the same file; any other path as it is."""
    m = re.match(r"(.*)/Cellar/python@(\d+\.\d+)/[^/]+/", real)
    if not m:
        return real
    prefix, version = m.groups()
    for link in (f"{prefix}/opt/python@{version}/bin/python{version}", f"{prefix}/bin/python{version}"):
        if os.path.realpath(link) == real:
            return link
    return real


# Folders the system, python.org and Homebrew put Pythons under, and that are nobody's project.
PYTHON_ROOTS = frozenset(
    {"/", "/opt", "/opt/homebrew", "/usr", "/usr/local", "/Library", "/Library/Frameworks", "/Applications", "/System"}
)


def find_python311(skipped: list[str] | None = None) -> tuple[str, str] | None:
    """A Python 3.11+ already on this computer, installed outside home: the one recorded, the one
    running, then python3.14…python3.11 and python3 on PATH, Homebrew's. (path, version) or None.
    `skipped` gets each one passed over for lying where an agent writes — to say so, not silently."""
    names = [f"python3.{minor}" for minor in range(14, 10, -1)] + ["python3"]
    # The one already recorded comes first: a repeated install keeps it while it is still trusted
    # and 3.11+, instead of switching to whichever python3 ran install this time.
    candidates = [m_boot.recorded_python(as_written=True), sys.executable, *(shutil.which(n) or "" for n in names)]
    for folder in ("/opt/homebrew/bin", "/usr/local/bin"):
        candidates += [os.path.join(folder, n) for n in names]
    # Nothing from a folder an agent writes to: the one we are in, every room's project, every agent's
    # folder — that weighs more than the file's permissions. Not the one we are in when
    # it is a root every Python sits under: from / (or /opt/homebrew, /Library) each lay «inside» it, and
    # install offered to install one that was there. Any other folder, home or not, counts
    # (a project on another disk is where an agent writes too).
    here = os.getcwd()
    off = [*([] if os.path.realpath(here) in PYTHON_ROOTS else [here]), *m_boot.agent_folders()]

    seen: set[str] = set()
    for path in candidates:
        real = os.path.realpath(path) if path and os.path.isabs(path) else ""
        # Trusted before it is ever run — running it to ask its version would already run its code.
        # Recorded is a spelling that outlives an upgrade; what runs is checked by its real path, each time.
        keep = lasting_python_path(real) if real else ""
        if real and real not in seen and any(m_boot._inside(w, f) for w in [real, *m_boot.link_folders(keep)] for f in off):
            seen.add(real)
            if skipped is not None and m_boot.trusted_interpreter(real):
                skipped.append(real)
            continue
        if real and real not in seen and m_boot.trusted_interpreter(real):
            seen.add(real)
            version = python311_version(real)
            if version:
                return keep, version
    return None


# python.org's signed installer; installs outside home, in /Library/Frameworks.
PYTHON_ORG_PKG = "https://www.python.org/ftp/python/3.13.9/python-3.13.9-macos11.pkg"
PYTHON_ORG_SHA256 = "dafebf62b8737f341f6b7eca2d6d1153a50a6b1bfbfa4b8aaf0e62adc73e5017"  # that file
# Checked and installed as one root step, on a copy only root can reach: checked in the user's temp
# folder and installed from it, the file could be swapped in between — the temp folder is one Codex's
# sandbox may write. $1 the download, $2 the pinned SHA-256, $3 the check.
ROOT_PKG_INSTALL = (
    "set -e; d=$(mktemp -d /var/root/collab-python.XXXXXX); trap 'rm -rf \"$d\"' EXIT; "
    'cp "$1" "$d/python.pkg"; '
    '[ "$(shasum -a 256 "$d/python.pkg" | cut -d " " -f 1)" = "$2" ] || { echo "the package is not the expected one" >&2; exit 1; }; '
    'sh -c "$3" sh "$d/python.pkg"; '
    'installer -pkg "$d/python.pkg" -target /'
)
# Signed by Apple-issued Developer ID of the Python Software Foundation (team BMM5U3QVKW) and intact:
# pkgutil fails, or says «invalid», on a changed package (checked: one byte flipped — refused).
PKG_SIGNATURE_CHECK = (
    'out=$(pkgutil --check-signature "$1") || exit 1; case "$out" in *invalid*) exit 1;; esac; '
    'printf %s "$out" | grep -q "Status: signed by a developer certificate issued by Apple" && '
    'printf %s "$out" | grep -q "Developer ID Installer: Python Software Foundation (BMM5U3QVKW)"'
)


def python_install_ways() -> list[tuple[str, list[list[str]]]]:
    """Ways to install a Python 3.11+ outside home on this computer: (menu label, commands in order).
    python.org's package is checked to be signed by the Python Software Foundation before it is
    installed; the person types the password («a command — Enter, and it is done»)."""
    ways: list[tuple[str, list[list[str]]]] = []
    if sys.platform == "darwin":
        if shutil.which("brew"):
            ways.append(("Install Python 3.12 with Homebrew", [["brew", "install", "python@3.12"]]))
        pkg = os.path.join(tempfile.gettempdir(), f"collab-python-{secrets.token_hex(4)}.pkg")
        ways.append(
            (
                "Install Python 3.13 from python.org (asks for your password)",
                [
                    ["curl", "-fsSL", "-o", pkg, PYTHON_ORG_PKG],
                    ["sudo", "sh", "-c", ROOT_PKG_INSTALL, "sh", pkg, PYTHON_ORG_SHA256, PKG_SIGNATURE_CHECK],
                ],
            )
        )
    elif shutil.which("apt-get"):
        ways.append(
            ("Install Python 3.12 with apt (asks for your password)", [["sudo", "apt-get", "install", "-y", "python3.12"]])
        )
    elif shutil.which("dnf"):
        ways.append(("Install Python 3.12 with dnf (asks for your password)", [["sudo", "dnf", "install", "-y", "python3.12"]]))
    return ways


def install_python311() -> tuple[str, str] | None:
    """Only with the person's yes (a menu, No first). Then install goes on by itself with the new
    Python — nothing to run again. No terminal to ask in: the exact commands, to paste."""
    ways = python_install_ways()
    if not ways:
        return None
    if not sys.stdin.isatty():
        print("  To install Python 3.11+, run:")
        print("    " + " && ".join(shlex.join(step) for step in ways[-1][1]))
        print("  then: collab install")
        return None
    try:
        choice = m_term.pick(
            [
                "No Python 3.11 or newer was found. collab needs one, so that Codex agents can answer",
                "in the room without asking each time.",
            ],
            [("No", None), *ways],
            ["↑/↓ choose · Enter · Esc — No"],
        )
    except (EOFError, KeyboardInterrupt):
        return None
    if not choice:
        return None
    for step in choice:
        if subprocess.run(step).returncode != 0:
            print(f"  ✗ {shlex.join(step)} did not finish — nothing else was run")
            return None
    return find_python311()


def install_python_step(dry_run: bool) -> bool:
    """install's Python step: a 3.11+ found — or installed with the person's yes — and recorded; on an
    older Python the rest of install is handed over to it. False when there is none to record. Out of
    cmd_install, which is long already."""
    inst = m_boot.paths()
    ok = True
    # Python 3.11+: found, or installed with the person's yes, and recorded — collab
    # started by an older python3 runs itself again with it (PYTHON_FILE, at the top).
    before = m_boot.recorded_python(as_written=True)
    passed_over: list[str] = []
    found = find_python311(passed_over)
    for real in passed_over:  # said, not silent: why the one there is not used
        print(
            f"  · Python {m_text.tilde(real)}: passed over — it lies in the folder you are in or an agent's, where an agent may write"
        )
    found = found or (None if dry_run else install_python311())
    if found and dry_run:
        print(f"  · Python {found[1]}: {found[0]} → {inst.python_file}")
    elif found:
        inst.python_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # A new file of a name nobody can guess, never through a link, then renamed.
        tmp = inst.python_file.with_name(f".python.{secrets.token_hex(8)}.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(found[0] + "\n")
        os.replace(tmp, inst.python_file)
        if m_boot.recorded_python(as_written=True) == found[0]:
            print(f"  ✓ Python {found[1]}: {'kept' if found[0] == before else 'recorded'} ({found[0]})")
        else:
            print(
                f"  ✗ Python {found[1]}: recorded, but {inst.python_file.parent} may be changed by others — fix its permissions (chmod 700)"
            )
            ok = False
    else:
        print(
            "  ✗ Python 3.11 or newer, installed outside your home folder — not found. Install one (macOS: brew install\n"
            "    python@3.12; Linux: your system's python3.11 package), then run collab install again."
        )
        ok = False
    # An older Python has no tomllib — Codex's writable_roots went unread
    # and the copy was placed where a Codex may write, with «✓». The rest runs on the 3.11+ just
    # recorded, as the hand-over at the top of the file does; without one the copy is not placed.
    # By its real path, checked now (recorded_python) — not through the link just recorded.
    handover = (
        (m_boot.recorded_python() if found and m_boot.recorded_python(as_written=True) == found[0] else ""),
        found[1] if found else "",
    )
    if sys.version_info < (3, 11) and not dry_run and handover[0] and os.environ.get("COLLAB_REEXEC") != handover[0]:
        print(f"  → the rest runs on Python {handover[1]}", flush=True)
        os.environ["COLLAB_REEXEC"] = handover[0]
        sys.stderr.flush()
        os.execv(handover[0], [handover[0], "-I", "-S", os.path.realpath(m_boot.SELF), *sys.argv[1:]])
    return ok


def our_command_link(path: Path) -> bool:
    """A link we may replace: it points to this file or to another copy of collab-room."""
    inst = m_boot.paths()
    if not path.is_symlink():
        return False
    target = Path(os.path.realpath(path))
    return (
        target == Path(m_boot.SELF)
        or inst.safe_copies.resolve() in target.parents
        or (target.name == "collab" and (target.parent / ".claude-plugin" / "marketplace.json").is_file())
    )


def install_command_step(args: argparse.Namespace, bin_dir: Path, link: Path, repo: Path) -> bool:
    """install's `collab` command: the read-only copy (or, --dev, the clone's file) linked from bin_dir.
    False when it was not placed. Out of cmd_install, which is long already."""
    inst = m_boot.paths()
    ok = True
    old_python = sys.version_info < (3, 11) and not args.dev and not args.dry_run
    # The command a person runs: by default a read-only copy where a Codex
    # does not write — an agent working in the clone could change the program the person runs next;
    # --dev, for us who work on collab, the clone's own file.
    # The update names the clone: a bare `collab install` is the copy itself, which refuses.
    what = (
        "link to the repository (--dev)"
        if args.dev
        else f"copy (update: git pull, then python3 {shlex.quote(str(Path(m_boot.SELF)))} install)"
    )
    try:
        if args.dry_run:
            print(
                f"  · collab command: {link} → {m_boot.SELF if args.dev else inst.safe_copies / 'command' / copy_name()[7:] / 'collab'} — {what}"
            )
        elif old_python:
            print(
                "  ✗ collab command: needs Python 3.11 or newer to check where Codex may write — install one, "
                "then run collab install again"
            )
            ok = False
        else:
            if args.dev:
                target = Path(m_boot.SELF)
            else:
                target = command_copy()
            if link.exists() or link.is_symlink():
                if not our_command_link(link):
                    raise FileExistsError
                link.unlink()
            else:
                bin_dir.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target)
            m_store.write_private(inst.installed_file, str(link))
            m_store.write_private(inst.source_file, str(repo))
            print(f"  ✓ collab command: {link} — {what}")
    except FileExistsError:
        print(f"  ✗ {link} already exists and is not ours — left untouched (use another --bin-dir)")
        ok = False
    except LinkedCopies as error:
        print(f"  ✗ collab command: {error.strerror}")
        ok = False
        # The refusal kept quiet about the command already there — it still
        # ran the earlier copy, now within a Codex's reach. Ours and leading into the copies: taken away.
        if our_command_link(link) and inst.safe_copies.resolve() in Path(os.path.realpath(link)).parents:
            link.unlink()
            print(
                f"  ! {m_text.tilde(str(link))} removed: it ran a copy a Codex may change — fix the above and run install again"
            )
    except OSError as error:
        # A folder without write access (--bin-dir /usr/local/bin) ended in a traceback.
        print(f"  ✗ collab command: cannot write to {bin_dir} ({error.strerror}) — pass a folder you can write to with --bin-dir")
        ok = False
    return ok


def cmd_install(args: argparse.Namespace) -> None:
    """One step for a new person: the `collab` command, the Claude Code plugin, the Codex skill.

    Preserves conflicting files, hooks and sandbox settings. Communication command rules are
    opt-in; each step says what it did, what it skipped and why.
    """
    inst = m_boot.paths()
    if args.no_codex_room_commands:
        # Withdrawing consent needs nothing else installed (it had no way back).
        if args.dry_run:
            # A dry run reported the withdrawal as done while consent stayed on disk.
            print("· Codex room replies: would withdraw consent and remove the rules (dry run — nothing changed)")
            return
        inst.codex_consent.unlink(missing_ok=True)
        prune_agent_rules(keep_consent=False)
        print(
            "✓ Codex room replies: off — consent withdrawn, rules removed. A Codex already running\n"
            "  keeps its permission until it restarts."
        )
        return
    repo = Path(m_boot.SELF).parent
    if repo.resolve() == inst.safe_copies.resolve() or inst.safe_copies.resolve() in repo.resolve().parents:
        # The installed copy has no scripts or plugin beside it, and an update is the clone's.
        # Named only when it is a clone: a path from a file is shown as a command to run.
        source = m_boot.read_private(inst.source_file)
        is_clone = bool(source) and (Path(source) / "collab").is_file() and (Path(source) / ".git").exists()
        where = shlex.quote(str(Path(source) / "collab")) if is_clone else "<your clone of collab-room>/collab"
        m_store.fail(f"this is the installed copy of collab — update from your clone: git pull, then python3 {where} install")
    ok = True

    def step(name: str, cmd: list[str], show_output: bool = False) -> bool:
        if args.dry_run:
            print(f"  · {name}: {' '.join(shlex.quote(c) for c in cmd)}")
            return True
        r = subprocess.run(cmd, text=True, capture_output=True)
        print(
            f"  {'✓' if r.returncode == 0 else '✗'} {name}"
            + ("" if r.returncode == 0 else f": {(r.stderr or r.stdout).strip()[:200]}")
        )
        if show_output and r.stdout.strip():
            print("\n".join("    " + line for line in r.stdout.strip().splitlines()))
        return r.returncode == 0

    print("collab-room: install")
    # The key the person's chat signs its messages with (made at install).
    if args.dry_run:
        print(f"  · Message signing key: {inst.owner_key}")
    else:
        had = bool(m_log.owner_key())
        m_log.owner_key(create=True)
        print(f"  ✓ Message signing key: {'kept' if had else 'created'} ({inst.owner_key})")
    ok = install_python_step(args.dry_run) and ok
    found = shutil.which("tmux")
    print(f"  {'✓' if found else '✗'} tmux" + ("" if found else f" — {m_tmuxio.tmux_install_hint()}"))
    ok = ok and bool(found)
    on_path = [Path(d) for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    if args.bin_dir:
        bin_dir = Path(args.bin_dir).expanduser()
    else:
        # Always the person's own ~/.local/bin, made if missing — never a system folder that happens to
        # be writable (without ~/.local/bin, install wrote into /opt/homebrew/bin, Homebrew's —
        # brew doctor calls a file there «unbrewed», and brew link or an upgrade may overwrite it).
        # Not on PATH: one line says how to add it. Another folder only when asked with --bin-dir.
        bin_dir = Path("~/.local/bin").expanduser()
    link = bin_dir / "collab"
    if bin_dir.exists() and not bin_dir.is_dir():
        # A file given as --bin-dir ended the install with a traceback.
        m_store.fail(f"{bin_dir} is a file, not a folder — pass a folder with --bin-dir")

    ok = install_command_step(args, bin_dir, link, repo) and ok
    start_cmd = "collab start" if bin_dir in on_path else f"{shlex.quote(str(link))} start"
    # No command there (refused, or taken away): no hint how to reach it.
    if bin_dir not in on_path and os.path.lexists(link):
        # Shown, never written: the person's shell files are theirs. zsh reads ~/.zprofile
        # for every login shell — a new Terminal window — and so does bash ~/.bash_profile.
        shell = os.path.basename(os.environ.get("SHELL", ""))
        profile = {"zsh": "~/.zprofile", "bash": "~/.bash_profile"}.get(shell, "your shell's profile")
        shown = "$HOME/" + str(bin_dir.relative_to(Path.home())) if Path.home() in bin_dir.parents else str(bin_dir)
        print(
            f"  ! {m_text.tilde(str(bin_dir))} is not on PATH: use the full path for now; to type just `collab`, "
            f'add this line to {profile} and open a new terminal: export PATH="{shown}:$PATH"'
        )
    if shutil.which("claude"):
        ok = step("Claude Code plugin source", ["claude", "plugin", "marketplace", "add", str(repo)]) and ok
        ok = step("Claude Code plugin", ["claude", "plugin", "install", "collab-room@collab-room"]) and ok
    else:
        print("  – Claude Code not found — plugin skipped")
    if shutil.which("codex"):
        # The interpreter install runs on, not whichever python3 PATH names.
        ok = step("Codex skill", [sys.executable, str(repo / "scripts" / "install-codex-skill.py")]) and ok
        # Codex room-communication rules are disabled: the rule ran this
        # collab outside Codex's sandbox while a sandboxed Codex could rewrite it. The step only
        # reports (e.g. our Claude plugin enabled in Codex) and never installs rules.
        command = [sys.executable, str(repo / "scripts" / "install-codex-rules.py")] + (["--dry-run"] if args.dry_run else [])
        ok = step("Codex communication permissions", command, show_output=True) and ok
        # The safe way back: asked once here; then each Codex a room starts gets a rule for
        # exactly its room and name, through a read-only copy of collab (codex_room_rule).
        allow = args.codex_room_commands or inst.codex_consent.exists()
        if not allow and not args.dry_run and sys.stdin.isatty():
            print(
                "  Codex agents a room starts can answer in that room without asking each time: only\n"
                "  collab say / ack / inbox, for that room and that agent's own name, through a read-only\n"
                "  copy of collab that Codex cannot change. Your project's permissions stay as they are."
            )
            try:
                allow = m_term.confirm("Let room-started Codex agents answer in their room?")
            except (EOFError, KeyboardInterrupt):
                print()
        if args.dry_run:
            print(f"  · Codex room replies: {'on' if allow else 'asked'} ({inst.codex_consent})")
        elif allow:
            inst.codex_consent.parent.mkdir(parents=True, exist_ok=True)
            m_store.replace_file(inst.codex_consent, (m_store.now() + "\n").encode("utf-8"), 0o600)
            print("  ✓ Codex room replies: on for Codex agents a room starts from now on")
        else:
            print("  – Codex room replies: off — Codex asks before answering (to enable: collab install --codex-room-commands)")
    else:
        print("  – Codex not found — skill skipped")
    print(f"Done. Next, in your project folder: {start_cmd}" if ok else "Some steps did not complete — see ✗ above.")
    if not ok:
        raise SystemExit(1)
