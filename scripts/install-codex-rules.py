#!/usr/bin/env python3
"""Install opt-in Codex permissions for collab communication, preserving user rules."""

import argparse
import json
import os
from pathlib import Path
import re
import sys


def rule_text(cli):
    return (
        "# Managed by collab-room. Allows communication commands outside the sandbox.\n"
        "# Does not change the sandbox mode or approval policy.\n"
        + "".join(
            "prefix_rule(pattern=" + json.dumps(["python3", str(cli), action]) + ', decision="allow")\n'
            for action in ("say", "ack", "inbox")
        )
    )


def claude_plugin_enabled(home):
    config = home / "config.toml"
    if not config.is_file():
        return False
    # Recognise the standard plugin table written by Codex; never rewrite TOML here.
    section = re.search(
        r"""(?m)^\s*\[\s*plugins\s*\.\s*["']collab-room@collab-room["']\s*\]\s*(?:#.*)?$([\s\S]*?)(?=^\s*\[|\Z)""",
        config.read_text(encoding="utf-8"),
    )
    return bool(section and re.search(r"(?m)^\s*enabled\s*=\s*true\s*(?:#.*)?$", section[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow", action="store_true", help="explicitly permit say/ack/inbox outside the sandbox")
    parser.add_argument("--dry-run", action="store_true", help="show the destination without changing files")
    parser.add_argument("--check", action="store_true", help="exit successfully only if these exact rules are installed")
    args = parser.parse_args()
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    cli = Path(__file__).resolve().parents[1] / "collab"
    target = home / "rules/collab-room.rules"
    try:
        expected = rule_text(cli)
        if args.check:
            return 0 if (not target.is_symlink() and target.is_file() and target.read_text(encoding="utf-8") == expected) else 1
        # Revoke what an earlier version installed: only our own file —
        # a regular file that starts with our header; a foreign file or a link is left alone.
        if (
            not args.dry_run
            and not target.is_symlink()
            and target.is_file()
            and target.read_text(encoding="utf-8").startswith("# Managed by collab-room.")
        ):
            target.unlink()
            print(
                f"Removed earlier collab-room Codex rules (unsafe): {target}\n"
                "Restart open Codex sessions so they stop using them."
            )
        if claude_plugin_enabled(home):
            print(
                "Warning: the Claude Code plugin collab-room@collab-room is enabled in Codex.\n"
                "Its Claude hooks can block startup or report for the wrong participant.\n"
                "Disable that plugin in Codex; keep the Codex skill. No plugin settings were changed."
            )
        if args.allow:
            # Disabled: an allow rule runs this collab outside Codex's
            # sandbox, but the checkout is writable by a sandboxed Codex (writable_roots), and any
            # --room is accepted — a sandboxed agent could rewrite collab, or write outside its
            # folders, and run it unsandboxed. Comes back only with an immutable installed copy.
            print(
                "Codex communication rules are disabled: they would let a sandboxed Codex run code outside "
                "its sandbox. No rules were changed.",
                file=sys.stderr,
            )
            return 2
        print("No global Codex rules for collab: a room adds a narrow rule for each Codex it starts.")
        return 0
    except (OSError, ValueError) as error:
        print(f"Codex rules installation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
