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
import time
from pathlib import Path
from typing import Any
import collab.boot as m_boot
import collab.text as m_text
import collab.i18n as m_i18n
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


class NotTheProgram(OSError):
    """«This collab» is not the built program (run from the package it is its first module): a copy of it
    would answer nothing. An OSError, as the caller's other «no copy» are: the old rule goes all the same."""


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


# How the one built file ends — and only it: run from the package, SELF is its first module.
ENTRY = b'\nif __name__ == "__main__":\n    main()\n'


def safe_copy() -> Path:
    """This collab, read-only, named by its content: an update is a new file, never an edit. Only the
    built program: run from the package, «this collab» is its first module — a copy of it ran every
    say with exit 0 and did nothing."""
    inst = m_boot.paths()
    data = Path(m_boot.SELF).read_bytes()
    if not data.endswith(ENTRY):
        raise NotTheProgram(
            f"{m_text.tilde(m_boot.SELF)} is not the built collab (it does not end in its entry point): "
            "a copy of it would answer nothing. Run the collab file built from the package (tools/build_collab.py)."
        )
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
                + json.dumps([*rule_python(), cli, command, "--room", room_dir, flag, pid], ensure_ascii=False)
                + ', decision="allow")\n'
                for command, flag in (
                    ("say", "--from"),
                    ("ack", "--for"),
                    ("inbox", "--for"),
                    ("propose", "--from"),
                    ("goal", "--from"),
                )
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


@m_store.room_locked
def codex_rule_notice(pid: str, label: str, reason: str, fix: str) -> None:
    """Explain each refusal once per agent and reason, including after a room restart."""
    rp = m_store.room().paths
    config = m_store.load_config()
    people = m_store.people_ids(config)
    if not people:
        return
    if not label:
        label = next((p.get("label", pid) for p in config.get("participants", []) if p["id"] == pid), pid)
    path = rp.state_dir / "codex-rule-notices.json"
    try:
        seen = json.loads(m_store.read_state_text(path))
    except (OSError, ValueError):
        seen = {}
    if not isinstance(seen, dict):
        seen = {}
    key = json.dumps([pid, reason], ensure_ascii=False)
    if key in seen:
        return
    m_log.append_message(
        m_text.ROOM,
        people,
        f"No room reply rule was issued for {label}: {reason}; Codex will ask permission for every reply; fix: {fix}.",
    )
    seen[key] = True
    m_store.write_json(path, seen)


def codex_rule_refusal(cli: Path, folder: str) -> tuple[str, str]:
    """The first unsafe dependency of a room rule, and how to keep it out of reach."""
    inst = m_boot.paths()
    rp = m_store.room().paths
    reason = ""
    fix = ""
    if codex_writable_roots() is None:
        reason = f"Codex settings in {inst.codex_home / 'config.toml'} could not be read reliably"
        fix = "correct the TOML settings and make the file readable, then restart this agent"
    elif not m_boot.trusted_interpreter(sys.executable):
        reason = f"the Python running collab ({sys.executable}) did not pass the interpreter safety checks"
        fix = "run collab install with a Python installed outside agent projects, then restart this agent"
    else:
        checks = [
            (
                lambda: codex_can_write(cli, folder),
                "Codex can write the collab command copy",
                "keep collab copies outside temporary folders, the agent folder and its writable_roots",
            ),
            (
                lambda: codex_can_write(rp.root.resolve().parent, folder),
                "Codex can replace the room folder through its parent",
                "choose a room whose parent is outside temporary folders, the agent folder and its writable_roots",
            ),
            (
                lambda: codex_can_touch_python(sys.executable, folder),
                "Codex can change the running Python installation or files inside it",
                "keep the Python installation outside temporary folders, the agent folder and its writable_roots",
            ),
            (
                lambda: codex_can_write(rp.base, folder),
                "Codex can write the room storage folder",
                "keep room storage outside temporary folders, the agent folder and its writable_roots",
            ),
            (
                lambda: codex_can_write(inst.python_file, folder),
                "Codex can change the file recording collab's Python",
                "keep the collab installation data outside temporary folders, the agent folder and its writable_roots",
            ),
            (
                lambda: bool(m_boot.recorded_python()) and codex_can_touch_python(m_boot.recorded_python(), folder),
                "Codex can change the recorded Python installation or files inside it",
                "keep the recorded Python installation outside temporary folders, the agent folder and its writable_roots",
            ),
            (
                lambda: any(
                    codex_can_write(Path(f), folder) for f in m_boot.link_folders(m_boot.recorded_python(as_written=True))
                ),
                "Codex can replace a link leading to the recorded Python",
                "keep Python links outside temporary folders, the agent folder and its writable_roots",
            ),
        ]
        for check, why, action in checks:
            if check():
                reason, fix = why, action + ", then restart this agent"
                break
    return reason, fix


def codex_room_rule(pid: str, folder: str, label: str = "") -> str:
    """Issue an opted-in rule, or explain the failed check to the person."""
    inst = m_boot.paths()
    reason = ""
    fix = ""
    if not inst.codex_consent.exists():
        prune_agent_rules()
        return ""
    stage = "remove obsolete Codex room reply rules"
    try:
        prune_agent_rules()
        stage = "prepare the read-only collab copy"
        try:
            cli = safe_copy()
        except (SystemExit, m_store.OperationFailed) as exc:
            # safe_copy can refuse via fail(), in both CLI and chat mode. Its earlier
            # rule must be removed just as on an IO error.
            raise OSError(f"could not prepare the read-only collab copy: {exc}") from exc
        reason, fix = codex_rule_refusal(cli, folder)
        stage = "write the Codex room reply rules"
        if not reason:
            if inst.agent_rules.is_symlink():
                reason = f"the rules file {inst.agent_rules} is a symbolic link"
                fix = "replace the link with a regular rules file, then restart this agent"
            elif (
                inst.agent_rules.exists()
                and (text := inst.agent_rules.read_text(encoding="utf-8"))
                and not text.startswith(RULES_HEADER)
            ):
                reason = f"the rules file {inst.agent_rules} is not managed by collab"
                fix = "move that file aside after reviewing it, then restart this agent"
        for spelling in room_spellings():
            set_agent_rule(spelling, pid, "" if reason else str(cli))
    except (OSError, UnicodeError) as exc:
        reason = f"could not {stage}: {exc}"
        fix = "check the named path, its permissions and free disk space, run collab install, then restart this agent"
        with contextlib.suppress(OSError, SystemExit, m_store.OperationFailed):
            for spelling in room_spellings():
                set_agent_rule(spelling, pid, "")
    if reason:
        codex_rule_notice(pid, label, reason, fix)
        return ""
    return str(cli)


def rule_python() -> list[str]:
    """The same installed interpreter for rules and hints, even in different Python processes."""
    recorded = m_boot.recorded_python()
    python = recorded if recorded and m_boot.trusted_interpreter(recorded) else sys.executable
    # Use the real binary, isolated from environment variables and site packages.
    return [os.path.realpath(python), "-I", "-S"]


def check_codex_rule_python(pid: str, python: list[str]) -> None:
    """Drop an obsolete rule when a delivery process would name a different interpreter."""
    inst = m_boot.paths()
    if not inst.codex_consent.exists() or inst.agent_rules.is_symlink():
        return
    try:
        text = inst.agent_rules.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return
    if not text.startswith(RULES_HEADER):
        return
    tags = {f"# room={json.dumps(spelling, ensure_ascii=False)} id={pid}" for spelling in room_spellings()}
    active = False
    for line in text.splitlines():
        if line.startswith("# room="):
            active = line in tags
        elif active and line.startswith("prefix_rule(pattern="):
            pattern = line.removeprefix("prefix_rule(pattern=").removesuffix(', decision="allow")')
            try:
                argv = json.loads(pattern)
            except ValueError:
                continue
            if isinstance(argv, list) and argv[:3] != python:
                for spelling in room_spellings():
                    set_agent_rule(spelling, pid, "")
                codex_rule_notice(
                    pid,
                    "",
                    "the reply command uses a different Python from this agent's saved permission rule",
                    "restart this agent so collab can issue a matching rule",
                )
                return


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


# python.org's installer puts each version here, and links on PATH only if its command-line tools are chosen.
PYTHON_ORG = Path("/Library/Frameworks/Python.framework/Versions")


def find_python311(skipped: list[str] | None = None, refused: list[tuple[str, str]] | None = None) -> tuple[str, str] | None:
    """A Python 3.11+ already on this computer, installed outside home: the one recorded, the one
    running, then python3.14…python3.11 and python3 on PATH, Homebrew's, python.org's. (path, version) or None.
    `skipped` gets each one passed over for lying where an agent writes, `refused` each one there that
    may not be run, with why — to say so, not «not found» of three Pythons in place."""
    names = [f"python3.{minor}" for minor in range(14, 10, -1)] + ["python3"]
    # The one already recorded comes first: a repeated install keeps it while it is still trusted
    # and 3.11+, instead of switching to whichever python3 ran install this time.
    candidates = [m_boot.recorded_python(as_written=True), sys.executable, *(shutil.which(n) or "" for n in names)]
    for folder in ("/opt/homebrew/bin", "/usr/local/bin"):
        candidates += [os.path.join(folder, n) for n in names]
    # Not on PATH, it was never looked at: three of them stood there and install said «not found».
    candidates += [str(PYTHON_ORG / f"3.{minor}" / "bin" / f"python3.{minor}") for minor in range(14, 10, -1)]
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
        elif real and real not in seen and os.path.isfile(real):
            seen.add(real)
            if refused is not None:
                refused.append((real, m_boot.interpreter_refused(real)))
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
        # Only a Homebrew that runs here: an Apple Silicon one carried over to an Intel Mac fails.
        if brew := m_tmuxio.working_brew():
            ways.append(("Install Python 3.12 with Homebrew", [[brew, "install", "python@3.12"]]))
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
        print(m_i18n.tr("  To install Python 3.11+, run:"))
        print("    " + " && ".join(shlex.join(step) for step in ways[-1][1]))
        print(m_i18n.tr("  then: collab install"))
        return None
    try:
        choice = m_term.pick(
            [
                m_i18n.tr(
                    "No Python 3.11 or newer was found. collab needs one, so that Codex agents can answer in the room without asking each time."
                )
            ],
            [(m_i18n.tr("No"), None), *ways],
            [m_i18n.tr("↑/↓ choose · Enter · Esc — No")],
        )
    except (EOFError, KeyboardInterrupt):
        return None
    if not choice:
        return None
    for step in choice:
        if subprocess.run(step).returncode != 0:
            print(m_i18n.tr("  ✗ {p0} did not finish — nothing else was run", p0=shlex.join(step)))
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
    refused: list[tuple[str, str]] = []
    found = find_python311(passed_over, refused)
    for real in passed_over:  # said, not silent: why the one there is not used
        print(
            m_i18n.tr(
                "  · Python {p0}: passed over — it lies in the folder you are in or an agent's, where an agent may write",
                p0=m_text.tilde(real),
            )
        )
    for real, why in [] if found else refused:
        print(m_i18n.tr("  · Python {p0}: found, refused — {why}", p0=m_text.tilde(real), why=why))
    found = found or (None if dry_run else install_python311())
    if found and dry_run:
        print(m_i18n.tr("  · Python {p0}: {p1} → {python_file}", p0=found[1], p1=found[0], python_file=inst.python_file))
    elif found:
        inst.python_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # A new file of a name nobody can guess, never through a link, then renamed.
        tmp = inst.python_file.with_name(f".python.{secrets.token_hex(8)}.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(found[0] + "\n")
        os.replace(tmp, inst.python_file)
        if m_boot.recorded_python(as_written=True) == found[0]:
            print(
                m_i18n.tr(
                    "  ✓ Python {p0}: {p1} ({p2})", p0=found[1], p1="kept" if found[0] == before else "recorded", p2=found[0]
                )
            )
        else:
            print(
                m_i18n.tr(
                    "  ✗ Python {p0}: recorded, but {parent} may be changed by others — fix its permissions (chmod 700)",
                    p0=found[1],
                    parent=inst.python_file.parent,
                )
            )
            ok = False
    else:
        brew = m_tmuxio.working_brew()
        mac = f"{brew} install python@3.12" if brew else "the installer from https://www.python.org/downloads/macos/"
        print(
            m_i18n.tr(
                "  ✗ Python 3.11 or newer, installed outside your home folder — not found. Install one (macOS: {mac};\n    Linux: your system's python3.11 package), then run collab install again.",
                mac=mac,
            )
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
        print(m_i18n.tr("  → the rest runs on Python {p0}", p0=handover[1]), flush=True)
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
                m_i18n.tr(
                    "  · collab command: {link} → {p1} — {what}",
                    link=link,
                    p1=m_boot.SELF if args.dev else inst.safe_copies / "command" / copy_name()[7:] / "collab",
                    what=what,
                )
            )
        elif old_python:
            print(
                m_i18n.tr(
                    "  ✗ collab command: needs Python 3.11 or newer to check where Codex may write — install one, then run collab install again"
                )
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
            else:
                bin_dir.mkdir(parents=True, exist_ok=True)
            # In one step: never a moment without the command — an update stopped here leaves the old one.
            fresh = link.with_name(f".{link.name}.{os.getpid()}.new")
            fresh.unlink(missing_ok=True)
            fresh.symlink_to(target)
            os.replace(fresh, link)
            m_store.write_private(inst.installed_file, str(link))
            m_store.write_private(inst.source_file, str(repo))
            print(m_i18n.tr("  ✓ collab command: {link} — {what}", link=link, what=what))
    except FileExistsError:
        print(m_i18n.tr("  ✗ {link} already exists and is not ours — left untouched (use another --bin-dir)", link=link))
        ok = False
    except LinkedCopies as error:
        print(m_i18n.tr("  ✗ collab command: {strerror}", strerror=error.strerror))
        ok = False
        # The refusal kept quiet about the command already there — it still
        # ran the earlier copy, now within a Codex's reach. Ours and leading into the copies: taken away.
        if our_command_link(link) and inst.safe_copies.resolve() in Path(os.path.realpath(link)).parents:
            link.unlink()
            print(
                m_i18n.tr(
                    "  ! {p0} removed: it ran a copy a Codex may change — fix the above and run install again",
                    p0=m_text.tilde(str(link)),
                )
            )
    except OSError as error:
        # A folder without write access (--bin-dir /usr/local/bin) ended in a traceback.
        print(
            m_i18n.tr(
                "  ✗ collab command: cannot write to {bin_dir} ({strerror}) — pass a folder you can write to with --bin-dir",
                bin_dir=bin_dir,
                strerror=error.strerror,
            )
        )
        ok = False
    return ok


# The update: the clone install was made from is moved to the newest release (a tag v<VERSION> of its
# origin) and installed again — what a person did by hand with git pull and install. Only the installed
# copy updates itself: a --dev install runs the clone's own file, and its clone is the developer's.
UPDATE_FETCH_WAIT = 20  # seconds; `collab start` never waits for it — the check runs beside the room
UPDATE_ROOM_WAIT = 60  # seconds for the room this start opens to be up before its deliverer is restarted
# What an update beside a start leaves to the person: only the deliverer moves to the new copy.
UPDATED_IN_ROOM = " The room's delivery moves to it; your chat and running agents keep the old code until you reopen the chat (/exit, then collab) and /restart each agent."


def release_of(tag: str) -> tuple[int, ...] | None:
    found = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", tag)
    return tuple(int(n) for n in found.groups()) if found else None


def release_text(release: tuple[int, ...]) -> str:
    return ".".join(map(str, release))


def update_source() -> tuple[Path | None, str]:
    """The clone to update from, or None and why there is none."""
    inst = m_boot.paths()
    if inst.safe_copies.resolve() not in Path(m_boot.SELF).resolve().parents:
        return None, "this collab is not the installed copy — a --dev install is updated with git pull in its clone"
    source = m_boot.read_private(inst.source_file)
    if not source or not (Path(source) / "collab").is_file() or not (Path(source) / ".git").exists():
        return None, "the git clone collab was installed from is not found — install again from a clone"
    return Path(source), ""


# Fetched releases are kept under a name of their own: a tag made in the clone itself is not a release.
UPDATE_REFS = "refs/collab-update/"
# Releases come only from collab-room's own repository. COLLAB_UPDATE_ORIGIN names one more (a mirror
# of the person's own, the tests' local one): set in the person's environment, it is their choice.
OUR_ORIGIN = re.compile(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)ManuAmigo/collab-room(?:\.git)?/?")


def our_origin(origin: str) -> bool:
    return bool(OUR_ORIGIN.fullmatch(origin)) or (bool(origin) and origin == os.environ.get("COLLAB_UPDATE_ORIGIN"))


def codex_reaches(source: Path) -> str:
    """Who could change the clone the update installs from — the update runs what is there outside any
    sandbox, so a clone a Codex may write would be the very way out the read-only copy closes. "" if no one."""
    real = os.path.realpath(source)
    if m_boot.in_temp_folder(real):
        return "it is in a temporary folder, where a Codex may write"
    roots = codex_writable_roots()
    if roots is None:
        return "Codex's config cannot be read to tell where a Codex may write"
    places = [(f"Codex may write {m_text.tilde(str(r))} (writable_roots)", str(r)) for r in roots]
    places += [(f"{who} works in {m_text.tilde(folder)}", folder) for who, folder in codex_workplaces()]
    # By spelling, case-folded on macOS (its disks ignore case), and by file identity — as for the copies.
    fold = (lambda x: x.casefold()) if sys.platform == "darwin" else (lambda x: x)
    for why, folder in places:
        there = os.path.realpath(os.path.expanduser(folder))
        a, b = fold(real), fold(there)
        if a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a + "/"):
            return why
        if os.path.exists(there) and (
            os.path.samefile(real, there) or m_boot._inside(real, there) or m_boot._inside(there, real)
        ):
            return why
    return ""


def git_in(source: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    """git in the person's clone, never asking anything: a password or host-key question in a check
    that runs beside the room would wait for no one. None of the clone's own hooks or helpers run."""
    env = os.environ | {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""}
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    safe = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "protocol.ext.allow=never"]
    return subprocess.run(
        ["git", *safe, "-C", str(source), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        stdin=subprocess.DEVNULL,
    )


def run_update(source: Path) -> tuple[str, str]:
    """Fetch the clone's origin; a newer release: move the clone to it and install it. (outcome, line):
    outcome is «current», «offline» (no answer — said to no one), «blocked» or «updated»."""
    reach = codex_reaches(source)
    git_dir = source / ".git"
    if not reach and (git_dir.is_symlink() or not git_dir.is_dir()):
        # A .git file or link leads to a git folder elsewhere — whose hooks and refs the check above never saw.
        reach = "its .git is not a folder inside it"
    # The URL git fetches from (insteadOf applied), and then that very URL is what is fetched.
    origin = git_in(source, "remote", "get-url", "origin").stdout.strip()
    if not reach and not our_origin(origin):
        reach = f"its origin {origin or '(none)'} is not collab-room's repository"
    if not reach and origin and (origin.startswith("file://") or not re.match(r"[a-z][a-z0-9+.-]*://|[^/:]+:", origin)):
        # A path for origin: the releases come from that folder — the same question for it.
        reach = codex_reaches(source / os.path.expanduser(origin.removeprefix("file://")))
    if reach:
        return "blocked", (
            f"collab is not updated by itself from {m_text.tilde(str(source))}: {reach} — update by hand when you "
            f"have looked at it: git pull there, then python3 {shlex.quote(str(source / 'collab'))} install"
        )
    try:
        fetched = git_in(
            source,
            "fetch",
            "--quiet",
            "--prune",
            "--no-tags",
            origin,
            f"+refs/tags/v*:{UPDATE_REFS}v*",
            timeout=UPDATE_FETCH_WAIT,
        )
    except subprocess.TimeoutExpired:
        return "offline", f"no answer from the clone's origin in {UPDATE_FETCH_WAIT} s"
    if fetched.returncode:
        return "offline", (fetched.stderr.strip().splitlines() or ["git fetch failed"])[-1]
    fetched_tags = git_in(source, "for-each-ref", "--format=%(refname:strip=2)", UPDATE_REFS).stdout.split()
    releases = [r for r in map(release_of, fetched_tags) if r]
    mine = release_of("v" + m_boot.VERSION) or (0, 0, 0)
    if not releases or max(releases) <= mine:
        return "current", f"collab {m_boot.VERSION} is the newest"
    newest = release_text(max(releases))
    by_hand = (
        f"update it by hand: git pull in {m_text.tilde(str(source))}, then python3 {shlex.quote(str(source / 'collab'))} install"
    )
    if git_in(source, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return "blocked", f"collab {newest} is out, not installed: your clone has changes of its own — {by_hand}"
    release = git_in(source, "rev-parse", "--verify", "-q", f"{UPDATE_REFS}v{newest}^{{commit}}").stdout.strip()
    before = git_in(source, "rev-parse", "HEAD").stdout.strip()
    if git_in(source, "merge", "--ff-only", "--quiet", release).returncode:
        return "blocked", f"collab {newest} is out, not installed: your clone has commits of its own — {by_hand}"

    def back(why: str) -> tuple[str, str]:
        # The clone goes back to where it was: what runs and what the clone holds stay one version.
        git_in(source, "reset", "-q", "--keep", before)
        return "blocked", f"collab {newest} is out, not installed: {why} — {by_hand}"

    # Whole before it replaces anything: the clone holds exactly the release's commit (git checks every
    # object by its hash), and its collab runs and says it is that release.
    if (
        git_in(source, "rev-parse", "HEAD").stdout.strip() != release
        or git_in(source, "diff", "--quiet", release, "--").returncode
    ):
        return back("the clone does not hold the release as it was published")
    said = subprocess.run([sys.executable, str(source / "collab"), "--version"], capture_output=True, text=True, timeout=60)
    if said.stdout.strip() != f"collab {newest}":
        return back(f"its collab says {said.stdout.strip()[:40] or 'nothing'!r}, not «collab {newest}»")
    link = m_boot.read_private(m_boot.paths().installed_file)
    command = [sys.executable, str(source / "collab"), "install"] + (["--bin-dir", str(Path(link).parent)] if link else [])
    was = installed_collab()
    # No terminal: install asks nothing (the Codex consent stays as the person answered it).
    done = subprocess.run(command, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=600)
    failed = [x.strip() for x in done.stdout.splitlines() if x.strip().startswith("✗")] or [done.stderr.strip()[-200:]]
    if installed_collab() == was:
        # The command was not replaced (install swaps it in one step, or not at all): the old one runs on.
        return back(f"its install did not complete ({failed[0]})")
    if done.returncode:
        return "updated", f"collab updated from {m_boot.VERSION} to {newest}; install also said: {failed[0]}"
    return "updated", f"collab updated from {m_boot.VERSION} to {newest}."


PLUGIN = "collab-room@collab-room"
PLUGIN_WAIT = 120  # seconds for each `claude plugin` call; beside the room, so the start never waits for it


def claude_plugin_version(claude: str) -> str | None:
    """The version of collab-room's plugin Claude Code has installed; None when it has none or does not say."""
    listed = subprocess.run(
        [claude, "plugin", "list", "--json"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=PLUGIN_WAIT
    )
    try:
        plugins = json.loads(listed.stdout)
    except ValueError:
        return None
    mine = [x for x in plugins if isinstance(x, dict) and x.get("id") == PLUGIN] if isinstance(plugins, list) else []
    return (str(mine[0].get("version") or "") or None) if mine else None


def update_claude_plugin() -> tuple[str, str]:
    """After the update: Claude Code reads collab-room's plugin source again and moves the plugin to its version.
    (outcome, line): «updated», «current», «skipped» (no Claude Code, no plugin) or «failed» — one line each,
    and nothing it meets stops the update. A plugin Claude Code does not have is not installed here: that is
    `collab install`'s, which says it."""
    claude = shutil.which("claude")
    if not claude:
        return "skipped", m_i18n.tr("Claude Code not found — its plugin is not updated.")
    try:
        was = claude_plugin_version(claude)
        if was is None:
            return "skipped", m_i18n.tr("Claude Code has no collab-room plugin — `collab install` adds it.")
        for command in (["marketplace", "update", "collab-room"], ["update", PLUGIN]):
            done = subprocess.run(
                [claude, "plugin", *command],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=PLUGIN_WAIT,
            )
            if done.returncode:
                said = (done.stderr.strip() or done.stdout.strip()).splitlines() or ["exit " + str(done.returncode)]
                return "failed", m_i18n.tr(
                    "The Claude plugin is not updated: claude plugin {p0} said: {p1}", p0=command[0], p1=said[-1][:200]
                )
        moved = claude_plugin_version(claude)
    except (OSError, subprocess.TimeoutExpired) as e:
        return "failed", m_i18n.tr("The Claude plugin is not updated: {p0}", p0=str(e)[:200])
    if moved == was:
        return "current", m_i18n.tr("The Claude plugin {p0} is the newest.", p0=was)
    return "updated", m_i18n.tr(
        "The Claude plugin updated from {p0} to {p1}: Claude agents take its new hooks after /restart.", p0=was, p1=moved or "?"
    )


def update_in_background(root: str) -> None:
    """`collab start`: look for a newer collab beside the room — the start never waits for the network.
    Not from a --dev install, not when switched off."""
    inst = m_boot.paths()
    if os.environ.get("COLLAB_NO_UPDATE") or inst.update_off.exists() or update_source()[0] is None:
        return
    m_store.private_dir(inst.update_state.parent, strict=False)
    with open(inst.update_state.with_suffix(".log"), "ab") as log:
        subprocess.Popen(
            [sys.executable, m_boot.SELF, "update", "--auto", "--after", str(os.getpid()), "--room", root],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,  # not the start's: its Ctrl+C or its terminal closing do not end an install midway
        )


def wait_for_room(session: str, starter: int) -> bool:
    """The room this start opens is up: its deliverer's window is there (made last, before attach).
    False when it is not coming: no session, and the start that ran this check has ended (or there was none)."""
    deadline = time.monotonic() + UPDATE_ROOM_WAIT
    while time.monotonic() < deadline:
        if m_tmuxio.tmux("list-panes", "-t", f"={session}:=deliver", check=False).returncode == 0:
            return True
        if m_tmuxio.tmux("has-session", "-t", f"={session}", check=False).returncode and not process_alive(starter):
            return False
        time.sleep(0.5)
    return False


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def auto_update(starter: int) -> None:
    """The check `collab start` runs beside the room. One at a time; what it found goes to the room's
    people once — an update every start, a refusal once per new version; no network is said to no one.
    After an update the room's deliverer and the Codex rules move to the new copy: `collab start` of the
    new copy, in the running room (the chat and the agents stay; each says when it runs older code)."""
    inst = m_boot.paths()
    source, _ = update_source()
    if source is None:
        return
    import fcntl

    with open(inst.update_state.with_suffix(".lock"), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return  # another room's start is updating already
        outcome, line = run_update(source)
        # The plugin follows the clone it is read from: after an update, and at a start with nothing newer
        # (a plugin left behind catches up). Said with the update; alone only when the plugin moved.
        plugin, plugin_line = update_claude_plugin() if outcome in ("updated", "current") else ("", "")
        print(line)
        if plugin_line:
            print(plugin_line)
        try:
            told = json.loads(inst.update_state.read_text(encoding="utf-8")).get("told", "")
        except (OSError, ValueError, AttributeError):
            told = ""
        tell = outcome == "updated" or (outcome == "blocked" and line != told)
        state = {"at": m_store.now(), "outcome": outcome, "line": line, "told": line if tell else told, "plugin": plugin}
        m_store.write_private(inst.update_state, json.dumps(state, ensure_ascii=False))
    if not tell and plugin != "updated":
        return
    config = m_store.config_now()
    if outcome == "updated":
        line += UPDATED_IN_ROOM
    said = " ".join(x for x in (line if tell else "", plugin_line if outcome == "updated" or plugin == "updated" else "") if x)
    m_log.append_message(m_text.ROOM, m_store.people_ids(config), said)
    link = m_boot.read_private(inst.installed_file)
    session = config.get("session", "collab-room")
    if outcome == "updated" and link and wait_for_room(session, starter):
        env = os.environ | {"COLLAB_NO_ATTACH": "1", "COLLAB_NO_UPDATE": "1"}
        rp = m_store.room().paths
        subprocess.run(
            [sys.executable, link, "start", "--room", str(rp.root)], env=env, capture_output=True, stdin=subprocess.DEVNULL
        )


def cmd_update(args: argparse.Namespace) -> None:
    """`collab update`: now, in this terminal; --off / --on: the update at `collab start`."""
    inst = m_boot.paths()
    if args.off:
        m_store.write_private(inst.update_off, m_store.now())
        print(
            m_i18n.tr("The update at collab start is off. `collab update` still updates when you run it; --on turns it back on.")
        )
        return
    if args.on:
        inst.update_off.unlink(missing_ok=True)
        print(m_i18n.tr("The update at collab start is on: each start looks for a newer collab and installs it."))
        return
    if args.auto:
        auto_update(args.after)
        return
    source, why = update_source()
    if source is None:
        m_store.fail(why)
    outcome, line = run_update(source)
    print(line)
    if outcome in ("updated", "current"):
        print(update_claude_plugin()[1])
    if outcome == "updated":
        print(
            m_i18n.tr(
                "Running rooms keep the old code: `collab start` in a room moves its delivery; /restart an agent, reopen a chat."
            )
        )
    elif outcome in ("blocked", "offline"):
        raise SystemExit(1)


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
            print(m_i18n.tr("· Codex room replies: would withdraw consent and remove the rules (dry run — nothing changed)"))
            return
        inst.codex_consent.unlink(missing_ok=True)
        prune_agent_rules(keep_consent=False)
        print(
            m_i18n.tr(
                "✓ Codex room replies: off — consent withdrawn, rules removed. A Codex already running\n  keeps its permission until it restarts."
            )
        )
        return
    repo = Path(m_boot.SELF).parent
    if repo.resolve() == inst.safe_copies.resolve() or inst.safe_copies.resolve() in repo.resolve().parents:
        # The installed copy has no scripts or plugin beside it, and an update is the clone's.
        # Named only when it is a clone: a path from a file is shown as a command to run.
        source = m_boot.read_private(inst.source_file)
        is_clone = bool(source) and (Path(source) / "collab").is_file() and (Path(source) / ".git").exists()
        where = shlex.quote(str(Path(source) / "collab")) if is_clone else "<your clone of collab-room>/collab"
        m_store.fail(
            m_i18n.tr(
                "this is the installed copy of collab — update from your clone: git pull, then python3 {where} install",
                where=where,
            )
        )
    ok = True

    def step(name: str, cmd: list[str], show_output: bool = False) -> bool:
        if args.dry_run:
            print(m_i18n.tr("  · {name}: {p1}", name=name, p1=" ".join(shlex.quote(c) for c in cmd)))
            return True
        r = subprocess.run(cmd, text=True, capture_output=True)
        print(
            f"  {'✓' if r.returncode == 0 else '✗'} {name}"
            + ("" if r.returncode == 0 else f": {(r.stderr or r.stdout).strip()[:200]}")
        )
        if show_output and r.stdout.strip():
            print("\n".join("    " + line for line in r.stdout.strip().splitlines()))
        return r.returncode == 0

    print("collab-room: install")  # the product's name: a heading in any language
    # The key the person's chat signs its messages with (made at install).
    if args.dry_run:
        print(m_i18n.tr("  · Message signing key: {owner_key}", owner_key=inst.owner_key))
    else:
        had = bool(m_log.owner_key())
        m_log.owner_key(create=True)
        print(m_i18n.tr("  ✓ Message signing key: {p0} ({owner_key})", p0="kept" if had else "created", owner_key=inst.owner_key))
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
        m_store.fail(m_i18n.tr("{bin_dir} is a file, not a folder — pass a folder with --bin-dir", bin_dir=bin_dir))

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
            m_i18n.tr(
                '  ! {p0} is not on PATH: use the full path for now; to type just `collab`, add this line to {profile} and open a new terminal: export PATH="{shown}:$PATH"',
                p0=m_text.tilde(str(bin_dir)),
                profile=profile,
                shown=shown,
            )
        )
    if shutil.which("claude"):
        ok = step("Claude Code plugin source", ["claude", "plugin", "marketplace", "add", str(repo)]) and ok
        ok = step("Claude Code plugin", ["claude", "plugin", "install", "collab-room@collab-room"]) and ok
    else:
        print(m_i18n.tr("  – Claude Code not found — plugin skipped"))
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
                m_i18n.tr(
                    "  Codex agents a room starts can answer in that room without asking each time: only\n  collab say / ack / inbox, for that room and that agent's own name, through a read-only\n  copy of collab that Codex cannot change. Your project's permissions stay as they are."
                )
            )
            try:
                allow = m_term.confirm(m_i18n.tr("Let room-started Codex agents answer in their room?"))
            except (EOFError, KeyboardInterrupt):
                print()
        if args.dry_run:
            print(
                m_i18n.tr(
                    "  · Codex room replies: {p0} ({codex_consent})",
                    p0="on" if allow else "asked",
                    codex_consent=inst.codex_consent,
                )
            )
        elif allow:
            inst.codex_consent.parent.mkdir(parents=True, exist_ok=True)
            m_store.replace_file(inst.codex_consent, (m_store.now() + "\n").encode("utf-8"), 0o600)
            print(m_i18n.tr("  ✓ Codex room replies: on for Codex agents a room starts from now on"))
        else:
            print(
                m_i18n.tr(
                    "  – Codex room replies: off — Codex asks before answering (to enable: collab install --codex-room-commands)"
                )
            )
    else:
        print(m_i18n.tr("  – Codex not found — skill skipped"))
    print(f"Done. Next, in your project folder: {start_cmd}" if ok else "Some steps did not complete — see ✗ above.")
    if not ok:
        raise SystemExit(1)
