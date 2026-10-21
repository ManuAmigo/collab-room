# The chat in detail

The [README](../README.md) covers the everyday keys and commands. This page has the rest.

## Opening rooms

Run `collab` in a regular terminal, not in the room's chat. In a folder that has a room, the room
opens. Elsewhere a menu appears:

- **THIS FOLDER** — **New room — …** for the folder you are in (not offered in your home folder
  or `/`);
- **YOUR ROOMS** — **Open room — …** for each existing room;
- **CREATE A ROOM** — **New room — …** for recent projects;
- **Choose another project folder…** — a folder browser.

↑/↓ choose, Enter selects, Esc cancels. A new room asks only for your name; agents are never added
or launched automatically — use `/add`. No separate `init`, `deliver` or `tmux attach` step is
needed. The collab-room clone is the tool's installation, not the place for rooms; a room can be in
any project folder, and a new room needs no reinstallation.

If the `collab` command is not on PATH, run `python3 /absolute/path/to/collab-room/collab`
instead. `collab start` in a project folder opens (or creates) that room without the menu. Rooms
in folders with the same name use separate tmux sessions: the session name includes a hash of the
folder's path, and an older room whose session name collides with another room's gets a new one.
`collab start --session NAME` sets the name yourself. Do not guess a room's tmux session name from
its folder name; use `--room` with the project folder.

An already open chat keeps the code it was started with. To pick up an update (for example a new
chat command), send or save your draft, `/exit`, and reopen the room with `collab`. The chat says
**Your chat runs an older collab** when this applies, and suggests `/restart` for a Codex agent
running an older copy.

## Menus and confirmations

Type `/` to open the command menu. ↑/↓ choose, Enter selects, Esc goes back or closes the menu.
Selecting an action opens its next step or performs it; it does not put a command into your
draft. Choosing **Rename** asks only for the new name.

Confirmations — in the chat and in `collab clear`, `stop`, `install` and when an older room is
moved (see [storage.md](storage.md)) — are **No / Yes** menus: ↑/↓ select, Enter confirms, Esc
means No, and **No** is selected first. You choose; you do not type y/n. Without a terminal,
automation uses explicit flags, and destructive commands require `--yes`.

## Welcome and /help

On your first visit, **Welcome to your room** introduces the basics: Tab chooses a recipient,
Enter sends, `/` opens actions. Choose **Show me around** or **Skip for now**; Esc skips too. The
welcome appears once per user, across all rooms.

`/help` opens a task menu: add an agent, see what an agent is doing, open an agent's window in a
new tab, answer an agent over the chat, pause or resume the room, clear history, stop the room,
and **All keys** (the full reference). Each task shows short instructions and an action to start
it. Opening help alone does nothing; clearing history and stopping still ask for confirmation.

## The input line and history

The line **above** an empty input shows **↑ History · / Commands · Tab Recipient**. While you
type, it shows the character count and **Ctrl+U Clear draft**; while you read history, it shows
navigation and **Esc Latest**. This is help text, not part of your message. Menus and agent
viewing show their own hints there.

Outside a menu, ↑/↓ move to the previous/next message. A message longer than the screen is shown
in parts so its middle is not skipped. Esc returns to the newest messages; sending does too. New
messages arriving while you read history wait at the bottom with an **N new** indicator. Your
draft and recipient stay. PgUp/PgDn move a screenful at a time (optional). In a menu, arrows
select items.

A message addressed to you is shown in full, even when long. Long broadcasts may be shortened: if
you see `… N more lines`, copy the `collab show … --room …` command shown into a regular terminal
to read the rest. Scrolling history does not expand a shortened broadcast.

Piped into `collab chat`, input has no recipient selection: unaddressed lines go to everyone.
Agents' replies reach the room only through `collab`; an agent's terminal output alone is not a
room message.

## The status bar

With one to three agents, each cell shows the name, model, state word and, while working, the
elapsed time. With four or more, the state words are dropped; name, model, time, colour and mark
remain. Cells have equal widths, at most 40 text columns plus padding; the name and mark are on
the left, the model and working time on the right. State changes do not move neighbouring cells.
Agents keep their order and wrap into more rows when needed — none are hidden behind a counter.
Long names and models are shortened with `…`. While an agent is out of its usage limit, its cell
shows when it is back instead of the model. All cells use bold black text; the colours and marks
are listed in the README.

States that need you (a question, an unsent draft, a lost window) are also posted once in the feed.

The quiet bottom strip identifies the room: its name on the left, with **⏸ Paused** before it
while paused; internal tmux windows are hidden. On the right, the date and time
(**DD.MM.YYYY HH:MM**) in windows at least 60 columns wide; 36–59 columns keep only the time;
narrower windows omit it. The name gives way to the pause mark and the clock. The terminal tab
title includes CollabRoom.

## Looking at and answering an agent

**`/view`** — choose an agent with ↑/↓ and Enter. Its live screen appears in the chat; Esc
returns. This is read-only: typing does not reach the agent, so you cannot answer questions or
approve commands here. Your recipient selection is kept.

**`/answer`** — choose an agent; its terminal opens over the chat, where you can answer questions
and approve commands. **Ctrl+]** returns to the chat. This needs tmux 3.2 or newer and a chat
running inside tmux; otherwise use `/open`.

**`/open`** — choose an agent. The command that opens its window is copied to your clipboard (on
macOS with `pbcopy`; on Linux with `wl-copy` or `xclip` if installed). Open a new terminal tab,
paste (Cmd+V on macOS, your terminal's Paste elsewhere) and press Enter. The command is also shown
on its own line in the chat; a narrow terminal may wrap it visually, but the chat inserts no
newlines. If the chat says **Could not copy automatically**, copy the shown command yourself.
Copying replaces your clipboard; it does not run anything. The room does not open the tab for you.
In the new tab you have the agent's full terminal; close the tab when finished — the agent keeps
running, and the room stays in its own tab. `/help` → **Open an agent's window in a new tab** leads
to the same choice.

The copied command is normally `collab open ID`. If that id exists in several running rooms, it
includes `--room`; keep it when pasting. An ambiguous `collab open ID` typed by hand fails with the
room choices instead of opening an arbitrary agent.

Agents outside tmux cannot be viewed, answered or opened.

In a Codex terminal opened with `/open` or `/answer`, the mouse wheel scrolls tmux history instead
of recalling old messages into Codex's input. Press **q** to leave scrollback before typing or
before Ctrl+] to return. Mouse selection there is tmux's; copying to the system clipboard depends
on your terminal. Text selection in the room chat is unchanged. A Codex started or restarted by the
room keeps its full output; an existing fullscreen Codex has only the history its pane saved.

## /mine

The **● N for you · /mine** hint counts what needs you. `/mine` lists agents waiting for your input
first (**Open agent terminal** opens `/answer`), then unread messages addressed to you and agents'
replies to your messages (**Open conversation** opens that agent's `/dm` tab; other messages open
for reading, Enter or Esc returns). Broadcasts are counted separately. **Earlier for you** offers up
to 20 already read messages, newest first. Only messages you open become read; **Mark all as
read** marks the rest.

## /dm

`/dm` asks for an agent; `/dm NAME` opens its conversation in a panel beside the chat. **Ctrl+O**
switches between chat and panel; **Tab / Shift+Tab** change tabs in the panel; ↑/↓ browse its
history and Esc returns to the latest. **Ctrl+]** or `/dm close` closes the panel without stopping
the agent. The active tab names the recipient. Replies to your room messages can appear in the tab
too. While a tab is open, messages only between you and that agent fold into one summary line in
the main feed. Direct messages are **not secret**: the room's shared log contains them.

## Adding an agent

`/add` → **Claude** or **Codex** (only those installed are offered). For the project folder, pick a
current or recent folder, **Browse folders…** or **Enter path manually…**. In the browser, open
subfolders, choose **Parent folder** to go up and **Use this folder** to confirm; Esc goes back;
long lists scroll. The same browser is behind **Choose another project folder…** at startup.
Folders marked **no access** cannot be entered or chosen. If the folder cannot be opened when the
agent is about to start, choose another; the error names it. Long paths are shortened in the middle
so the final folder stays visible.

A name is suggested: Enter accepts it, typing replaces it. Names must be non-empty and unique,
without control or invisible characters, with letters from one alphabet; visually similar names
may count as duplicates. The room opens a dedicated tmux window and sends a connection check. Until
the agent answers it, the agent is **Connecting**, unless its screen needs you (a sign-in or trust
question shows **Needs your input**). Starting the process alone does not prove a connection. Once
added, the name is available with Tab.

Agents join only through `/add`. Connecting an already running session, invitations and
`collab join` are no longer available; participants connected that way earlier keep working.

## Renaming, removing, muting

**`/name`** — choose a participant, type only the new name. Ids stay the same.

**`/remove`** — choose an agent, then **No / Yes, disconnect**. The room stops sending to it and
drops it from Tab and the status bar; it keeps running in its window and the conversation history
stays. The owner's seat cannot be removed. If your draft was addressed to it, the draft stays and
you must choose a new recipient explicitly — it is never redirected to everyone. A participant
added again starts with a fresh inbox: old queued messages and broadcasts sent meanwhile are not
replayed. Removing does not give a way to reconnect an external session. From a terminal:
`collab remove ID` (disconnects; does not stop the agent or close its window).

**`/mute` / `/mute NAME`** — the agent stays in the room, shown in orange. Messages addressed to it
by name still arrive, even when they name others too; broadcasts are withheld. `/unmute` resumes
broadcasts without replaying those sent meanwhile. Mute does not stop the agent's current work or
its outgoing messages.

## Pause, clear, restart

**`/pause`** holds delivery to agents and their outgoing messages; a turn already running may
finish. **`/resume`** releases the queue.

**`/clear`** (after confirmation) moves the conversation to a local archive and clears arrow-key
history. It keeps the agents' own session context.

**`/restart`** — choose an agent the room started and confirm (**No** first). **Yes, restart**
stops its current work and restarts its program in the same window, continuing the saved
conversation and loading the current room command rules. If no saved conversation is found, a
separate **No / Yes, a new conversation** question asks before starting fresh. The agent repeats
the connection check. This differs from `/stop` and reopening the room, which starts the room's
agents with new conversations.

## Leaving

`/exit` (or `/quit`) or **Ctrl+C** opens **Close chat? Agents keep running**. **Yes** closes the
chat; **No** or Esc returns to your draft. Pressing Ctrl+C again while the question is open closes
the chat. **Ctrl+D** on an empty input opens the same question. To detach from tmux without
closing anything: **Ctrl-b, then d**. Ctrl-C in the delivery window stops the deliverer.

After `/stop`, agents the room started come back as new sessions in their saved project folders
when you reopen the room, and repeat the connection check; their earlier in-memory conversation is
not resumed. Agents connected from their own sessions keep running through `/stop`. An older agent
entry without a saved project folder starts in the room's folder.
