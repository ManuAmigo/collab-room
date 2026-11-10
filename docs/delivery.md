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

**After Enter.** A message is marked delivered only once the agent's input is seen empty after
an Enter, or the message shows above the input. The room presses Enter again, on a later check,
if the input still holds exactly the typed text:

- the whole of it;
- Codex's fold `[Pasted Content N chars]`, with N equal to the text's length in characters;
- Claude's fold `[Pasted text #k]` — the very fold seen there when the room typed the text (a
  later paste has another k), with nothing after it but the text's end;
- a Claude input box grown to its full height (in a narrow or low window) that shows the text's end —
  only once the room has given that text an Enter, its start seen before it.

It presses only when no question is on the screen and the pane still runs the same process, both
judged on the same look as the input; up to three Enters in all. Anything else in the input, or
the three Enters spent: nothing more is pressed or cleared, even if the input later reads as ours
again, and the people are told after two minutes. If someone clears the input, the message is not marked and is typed again later. A
different text of exactly the same length in a Codex fold reads as ours, and in a full-height
Claude box a change to the hidden part above its first row is not seen; both risks are accepted.
Each attempt and its result are written to `.collab/deliver-log.jsonl`. Records carry a schema version,
run and attempt IDs, participant, message IDs, message/service kind, stage and reason, wall time with
fractions, process-monotonic elapsed time, Enter number and acceptance method (`empty`, `token`,
`queue` or `unverified`). Paste and Enter results include their return code. An unchanged wait reason
is written once, with a separate interval end; a batch keeps all its message IDs in one attempt.
The journal never records message text, commands, tokens, folds, screen contents or stderr, and adds
no tmux reads. It stays bounded by discarding the older half; rotation or a write failure marks later
records `complete: false`, so an observation window is not mistaken for a complete sample. Logging
failures never block delivery.
A text left in an input is kept in the room's state folder (`left.json`), so a restarted
deliverer (every `collab update` restarts it) goes on with it — presses it or sees it taken —
instead of taking it for a draft and typing the message again. Its attempt ID survives the restart;
monotonic elapsed values from different runs are never compared. A damaged record there is skipped.

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
