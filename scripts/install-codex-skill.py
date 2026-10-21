#!/usr/bin/env python3
"""Install the bundled Codex skill; update a copy we shipped, never one changed by hand."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

# What we installed, so a later install can tell our untouched copy from the person's edits.
MANIFEST = ".collab-room-install.json"


def tree(directory):
    """{relative path: bytes} of the skill's files, without our manifest."""
    return {
        p.relative_to(directory).as_posix(): p.read_bytes() for p in directory.rglob("*") if p.is_file() and p.name != MANIFEST
    }


def sha256(files):
    return {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}


def write_manifest(directory, files):
    (directory / MANIFEST).write_text(json.dumps({"files": sha256(files)}, indent=1, sort_keys=True) + "\n")


def shipped_by_manifest(target, actual):
    try:
        recorded = json.loads((target / MANIFEST).read_text()).get("files")
    except (OSError, ValueError, AttributeError):
        return False
    return recorded == sha256(actual)


def shipped_in_history(repo, actual):
    """True when the copy equals the skill at some commit of this checkout (installs older than the manifest)."""
    prefix = "integrations/codex/skill/"
    blobs = {name: hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() for name, data in actual.items()}

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True).stdout

    try:
        for commit in git("log", "--format=%H", "--", prefix).split():
            listed = {}
            for line in git("ls-tree", "-r", commit, "--", prefix).splitlines():
                meta, _, path = line.partition("\t")
                listed[path[len(prefix) :]] = meta.split()[2]
            if listed == blobs:
                return True
    except (OSError, IndexError):
        return False
    return False


def replace(files, target):
    """Swap in the new copy; the old one goes to a backup outside the skills folder, so Codex sees one skill."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = target.parent.parent / "collab-room-skill-backups" / f"{target.parent.name}-{stamp}-{os.getpid()}"
    fresh = target.parent / f".collab-room.new-{os.getpid()}"
    shutil.rmtree(fresh, ignore_errors=True)
    for name, data in files.items():
        (fresh / name).parent.mkdir(parents=True, exist_ok=True)
        (fresh / name).write_bytes(data)
    write_manifest(fresh, files)
    backup.parent.mkdir(parents=True, exist_ok=True)
    os.rename(target, backup)
    try:
        os.rename(fresh, target)
    except OSError:
        os.rename(backup, target)
        shutil.rmtree(fresh, ignore_errors=True)
        raise
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skills-dir",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "skills",
        help="Skills directory (default: $CODEX_HOME/skills or ~/.codex/skills)",
    )
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    source = repo / "integrations/codex/skill"
    target = args.skills_dir.expanduser() / "collab-room"
    try:
        if not (source / "SKILL.md").is_file():
            print(f"Installation failed: bundled SKILL.md is missing from {source}. Use a complete checkout.", file=sys.stderr)
            return 1
        expected = tree(source)
        if target.exists() or target.is_symlink():
            actual = tree(target) if target.is_dir() and not target.is_symlink() else None
            if actual == expected:
                if not (target / MANIFEST).is_file():
                    write_manifest(target, expected)
                print(f"Already installed: {target}")
            elif actual is not None and (shipped_by_manifest(target, actual) or shipped_in_history(repo, actual)):
                backup = replace(expected, target)
                print(f"Updated: {target} (previous copy: {backup})")
            else:
                print(
                    f"Refusing to overwrite existing skill: {target}. It is not a copy collab-room shipped "
                    f"(changed by hand?). Back it up and move it before installing.",
                    file=sys.stderr,
                )
                return 1
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target)
            write_manifest(target, expected)
            print(f"Installed: {target}")
    except OSError as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        return 1
    if shutil.which("collab") is None:
        print("Warning: collab is not on PATH. Run collab install from your clone: it puts the command in ~/.local/bin.")
    print("Start a fresh Codex session in your project and invoke $collab-room.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
