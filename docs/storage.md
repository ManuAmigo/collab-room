# Where a room keeps its files

A project folder identifies a room. The room's configuration and history live outside that
project, under:

```text
~/.collab-room/rooms/<folder>-<12-character-path-hash>/
├── .collab.json   participants and launch settings
└── .collab/       messages, receipts, archives, delivery state
```

The hash is taken from the project's resolved absolute path. Always pass the project folder to
`--room`, never this storage directory. Moving or renaming a project changes its room identity; the
history is not carried over automatically.

`COLLAB_STATE_HOME` overrides the base directory for rooms (useful for isolated tests).

`/clear` and `collab clear` move the history into `.collab/archive/` in this directory.

One file is the person's, not a room's: `~/.collab-room/command-counts.json` counts the chat
commands you run, for the menu's order in all your rooms. It stays on this computer.

## Keep it local

Do not commit, publish or sync room files without the team's agreement. Messages and delivery
state can contain working context; `.collab.json` holds commands and local paths, and may hold
secrets. This is local storage, not remote delivery.

## Upgrading from an older version

Older versions kept `.collab.json` and `.collab/` inside the project. For such a room, other
commands keep using the old location until you run `collab start` (or open the room with
`collab`), which moves the files out:

- The move stops the old chat and delivery, then start brings them back. Save any chat draft first.
- Existing agents are not restarted by the move. A Codex already running may need a restart to load
  refreshed command rules.
- Launch commands other than the chat are shown for confirmation; without a terminal, the move
  refuses when such confirmation is needed.
- A conflicting destination, or a symbolic link at the old config or state path, is refused. Do not
  delete old files to get past an error.
- Moving the files does not make an old configuration safe; review it.
