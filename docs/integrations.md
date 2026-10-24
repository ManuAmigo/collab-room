# Installation and integrations

The [README](../README.md#install) covers the standard install. This page has the details, the
manual setups, and troubleshooting. The installer handles the standard integrations for you.

## What `collab install` does

```sh
python3 /absolute/path/to/collab-room/collab install
```

Each step prints what it did, skipped or refused (`✓`, `–`, `✗`). Conflicting files are preserved.

- **Message signing key** — created once (or kept) for your chat's signatures; see
  [security.md](security.md).
- **Python 3.11+** — found, or installed with your consent from a menu (No is selected first):
  on macOS with Homebrew (`python@3.12`) or the python.org package (checked to be signed by the
  Python Software Foundation; asks for your password); on Linux with `apt` or `dnf`. Without a
  terminal, it prints the commands to run instead. The chosen interpreter is recorded in
  `~/.collab-room/python`; `collab` started by an older `python3` re-runs itself with it, and the
  rest of an install started on an older Python continues on it. A Python inside your current
  folder or an agent's folder is passed over, because an agent may write there.
- **tmux** — checked; if missing, install prints how to get it.
- **The `collab` command** — by default a read-only copy of `collab` under
  `~/.local/share/collab-room/`, linked from `~/.local/bin/collab`. `--bin-dir /your/bin` puts the
  link elsewhere; an existing file there that is not collab-room's is left untouched. If the folder
  is not on PATH, install prints the line to add to your shell profile (`~/.zprofile` for zsh,
  `~/.bash_profile` for bash) — it never edits shell files — and the last line gives the full
  command to use until you open a new terminal.
- **Claude Code plugin** (if `claude` is found) — registers this clone as a plugin source and
  installs `collab-room@collab-room`: status hooks and the `/collab` skill.
- **Codex skill** (if `codex` is found) — see [Codex](#codex) below.
- **Codex room replies** — see [Codex permission rules](#codex-permission-rules).

`--dry-run` shows the steps without changing anything.

**Updating.** `collab start` updates by itself: it fetches your clone's `origin`, moves the clone
to the newest release tag and runs install from it, beside the room (see [Updates](../README.md#updates);
`collab update --off` turns it off). It updates the Claude Code plugin too; running Claude agents take its new hooks after `/restart`. By hand: the `collab` command runs the copy, not your clone. After
`git pull`, run `python3 /absolute/path/to/collab-room/collab install` again to refresh the copy. Running
`collab install` through the installed copy refuses and names the clone to use.

**Developers.** `collab install --dev` links the command directly to the clone's file; edits then
affect later launches. Processes already running keep the code they loaded.

## Codex permission rules

Install asks once whether Codex agents started by a room may answer in that room without asking
you for each message. With your consent, each new room-started Codex gets rules that allow only
`collab say`, `ack` and `inbox`, restricted to its own room and participant id.

- The rules run a separate copy of collab under `~/.local/share/collab-room/`, named by its
  content, not your editable clone. The command uses an absolute interpreter path with `-I -S`;
  agents must keep that whole prefix and the argument order.
- These commands run outside Codex's sandbox. The rules grant no general project write access.
- If Codex could write to that copy or to the room's location (for example through its
  `writable_roots`), the automatic permission is not installed and Codex asks instead.
- Your existing rules are preserved. Managed restrictions can still require approval.
- Rules are prepared before a room launches its Codex; a running session does not reload them —
  restart it (`/restart`).

Decline at install and enable later with `collab install --codex-room-commands`. Without a
terminal, consent is not asked; it is given only by that flag. To withdraw it,
`collab install --no-codex-room-commands` forgets the consent and removes the managed rules;
Codex sessions already running keep the loaded permission until they restart. `/remove` removes
that participant's rules, and stale entries are cleaned up when another room-started Codex starts.

The installer warns if the Claude-only collab-room plugin is enabled in Codex; it never disables
plugins. Codex uses the skill; Claude Code uses the plugin.

## Claude Code

The plugin is in `integrations/claude-code/`; its skill is `/collab`, and its hooks report the
agent's state (working, idle, waiting on a permission prompt) and model to the room. Validate its
structure with:

```sh
claude plugin validate /absolute/path/to/collab-room/integrations/claude-code
```

Validation does not install or activate the plugin. To load it by hand, use your Claude Code
version's local plugin workflow, then invoke `/collab` in the project. The skill tells a
room-started Claude to answer its connection check and use the room's delivery; no periodic
monitor restart is needed. Installing from a published marketplace has not been verified yet.

collab-room adds no Claude Code permission rule for `collab` commands; Claude Code may ask you to
approve them.

## Codex

`collab install` installs the bundled skill. To install it by hand:

```sh
python3 /absolute/path/to/collab-room/scripts/install-codex-skill.py
```

It copies the skill to `$CODEX_HOME/skills/collab-room`, or `~/.codex/skills/collab-room` when
`CODEX_HOME` is unset. Running `collab install` or the script again updates an unchanged copy that
collab-room shipped, keeping the previous version under `<parent of skills>/collab-room-skill-backups/`;
it prints `Updated: … (previous copy: …)`, or `Already installed` when nothing changed.

The installed `.collab-room-install.json` records file fingerprints. For older installations without
that file, an exact match to a skill version in the clone's local Git history is also recognised.
If ownership cannot be established, the files were changed by hand, or the destination is a
symlink, the script refuses and keeps the existing copy — do not delete your edits to silence it.
It warns if `collab` is not on PATH, and it does not change project instructions or Codex
settings. Start a fresh Codex session after updating; an open session does not reload the skill.

For a project-local installation, run from your project's root:

```sh
python3 /absolute/path/to/collab-room/scripts/install-codex-skill.py --skills-dir .agents/skills
```

Then start a fresh Codex session and invoke `$collab-room`. A fresh non-interactive `codex exec`
session with a project-local installation has been checked: it read the skill, received a message,
replied in the room and acknowledged it. That does not cover the interactive TUI setup or a
separate `CODEX_HOME`. Alternatively, merge [integrations/codex/AGENTS.md](../integrations/codex/AGENTS.md)
into your project's instructions, and check that the participant id matches the room's
`.collab.json`.

**Writable directories.** If you change Codex's `writable_roots`, start a fresh session, preferably
with explicit directory access:

```sh
codex --add-dir /absolute/path/to/collab-room
```

The flag adds a writable directory next to the main workspace; see the
[CLI reference](https://learn.chatgpt.com/docs/developer-commands?surface=cli). `codex resume --last`
may keep the old sandbox, leaving a newly added directory unwritable — start a fresh session
instead, and check access with a temporary file after restarting.

**Launching by hand inside tmux.** Open a shell first and run `codex` from it. If Codex is the
pane's only process, `/quit` ends the pane, and ending the last window ends the session; a shell
leaves a terminal for restarting.

## Manual configuration

Agents normally join with `/add`, which uses the built-in settings for Claude Code and Codex.
For a custom setup — another CLI agent, different screen patterns — edit the room's `.collab.json`
(where it lives: [storage.md](storage.md)) and review it before `collab start`; existing settings
are preserved.

- `collab init` writes a `.collab.json` for the current folder without opening the room: your
  `owner` seat plus an entry for each installed Claude Code and Codex, asking for names in a
  terminal (`--no-ask` skips that, `--force` replaces an existing file).
- `collab start` opens the chat in the room's window and each agent in a window of its own;
  you watch and answer them from the chat (`/view`, `/answer`). `"layout": "tiled"` at the top level of
  `.collab.json` puts every seat a room starts into the chat's window as panes instead.
- [examples/team.json](../examples/team.json) is a hand-written four-participant example (a person,
  Claude Code, Codex and a reviewer); replace the placeholder reviewer command before using it.
  collab-room does not read it by itself — it is for manual setups.

Each participant needs a unique `id`, a `command` array and a `delivery` mode; tmux agents also
need `idle` (and usually `busy`) screen patterns. See [delivery.md](delivery.md) for what those mean
and their limits.
