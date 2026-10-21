# collab-room

A local chat room for one person and several AI coding agents — Claude Code, Codex, and other
terminal agents — working on the same project. You talk to all of them from one chat instead of
copy-pasting between terminals, and the agents claim files before editing them, so they do not
step on each other's work.

If collab-room is useful to you, please star the repository ⭐ — it helps others find it.

<!-- screenshot: docs/img/room.png — a room with a person, Claude Code and Codex -->

## How it works

```text
you type in the chat
   → message log (local files on this computer)
   → the deliverer types the message into the agent's tmux pane when the agent looks idle
   → the agent works, then answers with `collab say …`
   → the answer appears in your chat
```

Everything runs on one computer: the room is a set of local files plus a tmux session that holds
the agents' terminals, your chat and a delivery process. There is no server and no account.

## Requirements

- **macOS** is the main platform. **Linux** is supported: the full test suite runs on Linux and
  macOS in CI, and the installer knows `apt` and `dnf`. Windows is not supported.
- **Python 3.11 or later**, standard library only. You can start `collab install` with an older
  `python3`: it finds a 3.11+ installation, or offers to install one (asking first; this may ask for
  your system password), and records it. After that, `collab` started by an older `python3` hands
  itself over to the recorded 3.11+.
- **tmux** for the agents' panes and delivery; `/answer` needs tmux 3.2 or newer.
  macOS: install [Homebrew](https://brew.sh/) if needed, then `brew install tmux`.
  Debian/Ubuntu: `sudo apt install tmux`.
- **Git** to clone collab-room. On macOS, install it with `brew install git` or
  `xcode-select --install`; see the [Git installation guide](https://git-scm.com/install/mac).
- **The agents you want to use.** Install and sign in before adding them with `/add`; a room
  opens without any agent installed.
  - **Claude Code:** install with the recommended native installer:
    `curl -fsSL https://claude.ai/install.sh | bash`, then run `claude` and complete
    the sign-in flow in the browser. npm is an alternative and requires Node.js 22+:
    `npm install -g @anthropic-ai/claude-code`. See the [official Claude Code setup guide](https://code.claude.com/docs/en/setup).
  - **Codex CLI:** on macOS or Linux, install with
    `curl -fsSL https://chatgpt.com/codex/install.sh | sh`, then run `codex login`
    and complete the browser sign-in. See [Codex CLI setup](https://developers.openai.com/codex/cli/)
    and [Codex authentication](https://learn.chatgpt.com/docs/auth).

`/add` starts Claude Code and Codex. Other CLI agents need a hand-written configuration; see
[docs/integrations.md](docs/integrations.md#manual-configuration).

## Install

```sh
git clone https://github.com/ManuAmigo/collab-room.git
cd collab-room
python3 /absolute/path/to/collab-room/collab install
```

Use the absolute path of your clone. Install checks Python and tmux and preserves files it did not
create. It also:

- puts a read-only copy of `collab` under `~/.local/share/collab-room/` and links the `collab`
  command to it from `~/.local/bin` (`--bin-dir` chooses another folder, `--dry-run` only shows
  the steps);
- creates the key your chat signs your messages with;
- installs the Claude Code plugin (status hooks and the `/collab` skill) if `claude` is found, and
  the Codex skill if `codex` is found;
- asks once whether Codex agents started by a room may answer in that room without asking you each
  time. The permission covers only `collab say`, `ack` and `inbox`, for that room and that agent's
  own id; your project's permissions do not change. You can change your answer later with
  `collab install --codex-room-commands` or `--no-codex-room-commands`.

If `~/.local/bin` is not on your PATH, install prints the line to add to your shell profile; then
open a new terminal. Until then, use the full command printed on the last line
(`Done. Next, in your project folder: …`).

To update: `git pull` in your clone, then run the same install command again — the `collab` command
points to a copy, not to your clone. Details, manual setup and the Codex permission rules:
[docs/integrations.md](docs/integrations.md).

## Your first room

1. Open a terminal in your project folder and run `collab`. A menu appears with **New room — <this
   folder>** under the cursor. Press **Enter**. (↑/↓ move, Esc cancels. In a folder that already has
   a room, `collab` opens it directly.)
2. The room asks for your name and opens your chat. In the room your seat's id is `owner`; agents
   and commands refer to you by that id.
3. A one-time welcome explains the basics; choose **Show me around** or **Skip for now**.
4. Type `/add` and press Enter. Choose **Claude**, then its project folder (current and recent
   folders are suggested; **Browse folders…** and **Enter path manually…** are there too), then its
   name (Enter accepts the suggested one). The room starts it in its own tmux window and shows it as
   **Connecting** until it answers a connection check.
5. Repeat `/add` for **Codex**.
6. The first run of an agent often stops at a sign-in, theme or "trust this folder" question. Its
   status turns red: **Needs your input**. Type `/answer`, choose the agent and answer in its
   terminal; **Ctrl+]** returns to the chat. If `/answer` is unavailable, `/open` copies a command
   that opens the agent's window in a new terminal tab.
7. Claude Code may ask you to approve the `collab say` command it runs to answer you; collab-room
   does not pre-approve it. Answer through `/answer` as above. If Claude Code offers to remember the
   approval for this project, accepting avoids the same question next time.
8. Press **Tab** until the input shows the agent's name, type `hi`, press **Enter**. The reply
   appears in your chat once the agent answers with `collab say`.

## Talking in the room

- **Recipient:** **Tab / Shift+Tab** chooses who your message is for; the input shows the name.
  It starts at everyone and stays selected after sending. `@id text` sends one message to
  someone else without changing the selection; `@claude @codex Review this plan` addresses several.
- **Send:** **Enter**. For a new line inside a message: **Shift+Enter** (if your terminal
  distinguishes it), **Option+Enter**, or end the line with `\`. Pasted multi-line text stays one
  message. **Ctrl+U** clears the draft.
- **History:** **↑/↓** step through earlier messages, **PgUp/PgDn** by a page, **Esc** returns to
  the newest.
- **What needs you:** the line above the input shows **● N for you · /mine**; `/mine` lists agents
  waiting for you, then unread messages to you and replies to yours.
- **Direct conversation:** `/dm NAME` opens a panel beside the chat for one agent. **Ctrl+O**
  switches between chat and panel, **Ctrl+]** or `/dm close` closes it.
- **Not secret:** addressing chooses who acts on a message. Every message — direct ones too — is in
  the room's shared log, which every participant on this computer can read.

More keys and details: [docs/chat.md](docs/chat.md).

## Chat commands

Type `/` to open the command menu (↑/↓, Enter), or type a command directly.

| Command | What it does |
| --- | --- |
| `/add` | Start a new agent: choose its kind (Claude or Codex), its folder, its name |
| `/remove` | Disconnect a participant; the agent keeps running in its window |
| `/mute` · `/unmute` | Stop / resume room-wide messages to an agent; messages to it by name still arrive |
| `/unlimit` | An agent is back from its usage limit: the room delivers to it again |
| `/name` | Rename yourself or an agent; ids do not change |
| `/view` | See an agent's screen, read-only; Esc returns |
| `/answer` | Respond in an agent's terminal over the chat; Ctrl+] returns |
| `/open` | Copy a command that opens an agent's window in a new terminal tab |
| `/restart` | Restart an agent the room started; its conversation continues |
| `/mine` | What is for you: agents waiting for you, unread messages, replies to yours |
| `/dm` · `/dm NAME` · `/dm close` | Talk with one agent in a panel beside the chat |
| `/pause` · `/resume` | Hold delivery to agents and their outgoing messages / release it |
| `/clear` | Clear the history for everyone; messages move to a local archive |
| `/help` | Task menu and the full key guide |
| `/stop` | Stop the room: close its windows and the agents it started; history stays |
| `/exit` (or `/quit`) | Close your chat; agents keep running |

## Agent statuses

The bar above the input shows one cell per agent: its name, model, state, and how long it has been
working.

| Look | State | What to do |
| --- | --- | --- |
| Green | Ready | Nothing; it takes messages |
| Green, spinner and a number | Ready, with background tasks still running | Nothing |
| Yellow, spinner | Working | Wait |
| Red `!` | Needs your input | Answer its question: `/answer` |
| Red `!` | Unsent draft | Text in its input blocks delivery: `/answer`, then send or clear it |
| Red `!` | Disconnected | Its window is gone or runs another program; the room does not type into it |
| Orange `×` | Muted | `/unmute` when room-wide messages should reach it again |
| Orange `◷` | Out of limit | The cell shows when it is back; `/unlimit` if the limit was lifted earlier |
| Grey `…` | Connecting | Wait; if it lasts, look with `/view` |
| Grey `?` | Unknown / no recent update | Delivery may not be running: reopen the room with `collab` |

`collab status` in a terminal prints the same states in words — for example `is working`,
`is ready`, `needs your input`. With `--as`, a participant sets its own state to `working`, `idle`
(Ready) or `needs_owner` (Needs your input). The spinner shows the reported state, not measured
progress; stale data is shown as no update.

## Working together

The agents follow a short protocol; you can hold them to it.

- **Claim before editing.** Before changing shared files, a participant posts
  `Taking: <task/files>` — as a quiet note, `collab say --quiet`, which wakes nobody — and checks
  that nobody else has claimed them.
- **Address the responsible participant**, not everyone, when a question is for one of them.
- **Report after a commit:** the hash, the changed files, the checks actually run and their output,
  what the checks do not prove, and whether it is pushed. `collab report` prints the commit part
  (hash, files, push state); it does not run tests.
- **Close the loop.** An agent answers with `collab say --re MESSAGE_ID`, which closes that
  message; one that needs no answer is closed with `collab ack --no-reply`. `ack` means "read and
  accounted for", not "done".

The full protocol: [docs/protocol.md](docs/protocol.md).

## When something goes wrong

- **A red `!` cell:** the agent waits for you or has a draft in its input. `/answer` opens its
  terminal over the chat; `/open` gives a command for a new tab instead.
- **Messages are not delivered:** delivery types only into an agent that looks idle on screen. Text
  left in the agent's input (**Unsent draft**) blocks typing until you send or clear it there.
- **Usage limit:** the room recognises the agent's limit notice, holds messages until the time
  shown, and posts when the agent is back. If the limit was lifted early, `/unlimit`.
- **An agent is stuck or confused:** `/restart` restarts its program in the same window and
  continues its saved conversation (if none is found, it asks before starting a new one).
- **Grey `?` for everyone:** delivery is not running. Run `collab` and choose the room — delivery
  restarts.

How delivery decides and what it can get wrong: [docs/delivery.md](docs/delivery.md).

## Leave, stop, reopen

- **Leave the chat:** `/exit` or **Ctrl+C** asks **Close chat? Agents keep running** (No is
  selected first). Agents and delivery keep running.
- **Stop the room:** `/stop` shows which windows will close, then asks. It closes the room's
  windows, including delivery and the agents it started. Configuration and history stay. From a
  terminal: `collab stop --room /absolute/path/to/your-project`.
- **Reopen:** run `collab` in the project folder, or anywhere and choose the room. A running room:
  delivery restarts on the current code and your chat comes back; agents are not restarted. A
  stopped room: its agents start as new sessions in their saved folders; their earlier conversation
  is not resumed (use `/restart` instead of `/stop` when you want to keep it).

## CLI reference

| Command | Purpose |
| --- | --- |
| `collab` | Open the room in this folder, or choose / create one |
| `collab install` | One-time setup: the command, Claude Code plugin, Codex skill |
| `collab start` · `init` | Create the room here if needed and open it · only write its configuration |
| `collab chat` | A person's seat: read the room and write |
| `collab say` (`send`) | Post a message to the room |
| `collab inbox` · `ack` | A participant's unacknowledged messages · mark them read and handled |
| `collab watch` · `show` | Print new messages (for one participant or `all`) · print one message in full |
| `collab status` | Everyone's state, or set your own with `--as` |
| `collab report` | Commit summary: hash, files, push state |
| `collab dm` · `open` | The direct-conversation panel · an agent's window in this terminal tab |
| `collab name` · `remove` | Name a participant · disconnect it |
| `collab mute` · `unmute` · `unlimit` | Room-wide messages off / on · clear a usage limit by hand |
| `collab pause` · `resume` · `clear` · `stop` | Hold / release delivery · archive the history · stop the room |
| `collab deliver` | The delivery loop (`start` runs it for you) |
| `collab task` · `usage` | The task a participant is on · tokens spent by task |
| `collab hook-model` | Used by the Claude Code hooks |

Run `collab COMMAND --help` for the arguments. Commands that act on a room take `--room
<project-folder>`; without it they use `COLLAB_ROOT`, then the current folder.

## Limits and privacy

- **Local only.** One computer. Room files live under `~/.collab-room/`, outside your project; do
  not commit or sync them — they hold working context, local paths and possibly secrets. See
  [docs/storage.md](docs/storage.md).
- **The log is shared.** Your chat shows every message, including those between agents; agents'
  inboxes are filtered by recipient, but addressing is not a privacy boundary.
- **Delivery is a screen heuristic.** It reads the bottom of the agent's screen, and a race remains
  between checking and typing. A queued message proves local storage; a successful tmux call proves
  an input attempt. Neither proves the agent processed the message — look for its reply.
- **Unsigned messages.** Your chat signs what you type. A message under your id that is not signed
  (for example `collab say --from owner` from a terminal) shows **⚠ not from the chat**, and agents
  are told not to act on risky instructions in it without asking you. This is a warning, not proof
  of identity. See [docs/security.md](docs/security.md).
- **Prototype.** A live exchange between a person, Claude Code and Codex works: direct messages,
  several recipients, broadcasts, replies in your chat. Tests cover the CLI and delivery to a
  simulated participant in real tmux. Not yet verified: delivery during long Codex tasks without
  duplicates, recovery after an agent restarts, interactive Codex setup, installing the plugin from a
  published marketplace, and Codex idle detection across CLI versions. There is no hosted service
  and no installation package.

## Contributing

```sh
python3 -m unittest discover -s tests
```

Some core tests require tmux. Passing tests with simulated participants do not establish reliable operation with actual AI sessions.

Improvements are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

collab-room is free software under the [GNU General Public License v3.0 or later](LICENSE). Use it and change it
for yourself as you like. If you distribute a modified version — publish a fork, ship it in a product, hand it to others —
you must make its source available under the same license. Better still, send the change back as a pull request.
