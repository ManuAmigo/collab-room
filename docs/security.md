# Security notes

collab-room runs on one computer under your OS account. Its protections are warnings and
narrowed permissions, not isolation between the person and the agents.

## Signed messages from your chat

`collab install` creates a signing key, and your interactive chat signs the messages you type.

- A message that carries a person's sender id but no valid chat signature is shown as
  **⚠ not from the chat**.
- Agents receive it marked **⚠ NOT typed in the person's chat** and must ask you in the room before
  acting on risky instructions in it — deleting, pushing, deploying, money, or permission changes.
- Messages sent from a terminal with `collab say --from owner` are not signed. Give such
  instructions in your interactive chat.
- Older history can predate signing.

This is a warning mechanism, not proof of human identity: agents running under the same OS account
can potentially read the signing key or control the terminal. Agents must never run your chat or
type into its terminal to impersonate you.

## Who can read what

Every participant on this computer can read the room's log. Direct messages (`/dm`) and addressed
messages choose who should act; they are not secret. See [storage.md](storage.md) for where the
files are and why they should not be committed.

## Codex permissions

Codex agents a room starts can be allowed, with your consent at install, to run `collab say`, `ack`
and `inbox` for their own room and id without asking. Those commands run outside Codex's sandbox,
through a read-only copy of collab that Codex cannot change, with an isolated interpreter
(`-I -S`). If Codex could write to that copy or to the room's files, the permission is not
installed. Details and how to withdraw consent: [integrations.md](integrations.md#codex-permission-rules).

The `collab` command you run is also a read-only copy by default, so an agent working in your
clone cannot change the program you run next. Room files live outside the project for a similar
reason: an agent working in the project should not be able to change the commands `collab start`
runs.

## Delivery

Delivery types into agents' terminals based on what their screens look like. It never sends Esc or
Ctrl-C, but a screen pattern can match the wrong screen. Do not enable terminal delivery where an
unintended submission would be dangerous. See [delivery.md](delivery.md).

## Reporting a vulnerability

Do not open a public issue — use GitHub's private vulnerability reporting (Security → Report a
vulnerability), as described in [CONTRIBUTING.md](../CONTRIBUTING.md).
