# Delivery and its limits

How a message gets from the room's log to an agent, and what that does and does not prove.

## Delivery modes

Each participant in the room's `.collab.json` has a unique `id`, a `command` array and a
`delivery` mode:

- **`pull`** — the participant reads its own inbox with `collab inbox` or `collab watch`. Your
  chat is a `pull` seat.
- **`tmux`** — `collab deliver` types messages into the participant's tmux pane. Agents added with
  `/add` use this mode.

`collab start` (and `collab` when it opens a room) runs one deliverer in the room's tmux session.
Do not run a second `collab deliver` alongside it; run it by hand only for troubleshooting.
`collab deliver --from-now` skips messages that existed before it started — use it only when you
mean to drop that backlog.

## How tmux delivery decides

1. The deliverer reads the last lines of the agent's screen (10 by default; `COLLAB_IDLE_LINES`
   changes it) and matches them against the participant's `idle` pattern. A `busy` match on the
   same lines prevents delivery even when the idle prompt is still visible.
2. It checks again immediately before typing, then types **one** message and presses Enter. It
   never sends Esc, Ctrl-C or any other key, so it cannot cancel a running turn or answer an
   approval prompt on its own.
3. The next message waits for a new idle cycle.

Before typing, the room also checks the saved process id of the agent's pane, to refuse a window
that now runs another program. For older or external agent windows without that id, it refuses a
bare shell. These checks do not prove that the agent is ready.

**Codex.** One extra Enter is allowed when the input still holds the folded paste
(`[Pasted Content N chars]`), that fold is the last input line, the screen shows no question or
work in progress, and the pane still runs the same process. This retry happens once; input still
left unsent after it is treated as a draft and blocks further typing.

**Long messages** are replaced by an instruction to read the full message with `collab inbox`; the
size threshold depends on the agent.

**Stopping.** On SIGHUP or SIGTERM the deliverer finishes an input attempt already in progress
before exiting.

**Drafts.** Text sitting in an agent's input (its prompt shows something other than the empty
prompt) marks it **Unsent draft** and blocks typing until you send or clear it in its window.

**Usage limits.** When an idle agent shows its usage-limit notice, the room marks it **Out of
limit**, holds its messages until the time in the notice, and posts when it is back. `/unlimit` (or
`collab unlimit ID`) clears the hold by hand if the limit ended early.

## Example configuration

Built-in screen patterns for Claude Code and Codex live in `src/collab/screen.py` (`PRESETS`);
`/add` uses them. A simplified hand-written Codex entry looks like this:

```json
{
  "id": "codex",
  "label": "Codex",
  "command": ["codex"],
  "cwd": ".",
  "delivery": "tmux",
  "idle": "^› Ask Codex",
  "busy": "Working \\("
}
```

**Check these patterns against your CLI version before relying on them.** Delivery is a screen
heuristic: an approval prompt or other input screen could match by accident, quoted prompt or busy
text inside the checked lines can mislead it, and a race remains between checking and typing.
Adjust the patterns for your CLI's language and version, watch the first deliveries, and do not
enable terminal delivery where an unintended submission would be dangerous.

See also [integrations.md](integrations.md#manual-configuration).

## What delivery proves

- A queue write proves local storage.
- A successful tmux call proves an input attempt.
- Neither proves that the agent processed the message; look for its reply or acknowledgement.

Screen checks narrow the race between typing and Enter; they do not remove it.

Never type into another participant's pane by hand with `tmux send-keys`; delivery is the
deliverer's job.

## Who sees what

Your chat displays the whole room, including messages between other participants. Agents'
inboxes are filtered by recipient. Addressing selects who should act on a message; it is not a
privacy boundary inside the room.

## Agent commands and `--room`

Agents run `collab` with the room chosen explicitly: `--room` after the subcommand. The room is
chosen by `--room`, then `COLLAB_ROOT`, then the current folder. Agents should use the full command
prefix and their assigned id exactly as delivery gives them, and handle permission requests
through their normal approval flow — never by disabling their sandbox.
