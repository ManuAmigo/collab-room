"""The chat's /help: the task menu and the guide."""

from __future__ import annotations
import shutil
import collab.text as m_text
import collab.i18n as m_i18n


# /help as a task menu («the user must learn all this easily»). (key, title, steps, action label, command that starts it)
HELP_TASKS = [
    (
        "add",
        "Add an agent",
        [
            "Choose an agent type, project folder, and name.",
            "The room checks the connection before showing the agent as connected.",
        ],
        "Add an agent",
        "/add",
    ),
    (
        "view",
        "See what an agent is doing",
        ["Choose an agent to see its live screen.", "This is read-only. Press Esc to return to the chat."],
        "View an agent",
        "/view",
    ),
    # «I don't see the agent's window that opens in another tab» — the name says so.
    # Before the agent is chosen nothing is copied yet: «already in the clipboard» comes after.
    (
        "open",
        "Open an agent's window in a new tab",
        [
            "Choose an agent: its command goes into your clipboard.",
            "Open a new terminal tab (+), paste with Cmd+V and press Enter.",
        ],
        "Open an agent terminal",
        "/open",
    ),
    (
        "answer",
        "Answer an agent over the chat",
        ["Choose an agent to open its terminal over this chat.", "Respond in that window. Press Ctrl+] to return to the chat."],
        "Answer an agent",
        "/answer",
    ),
    (
        "pause",
        "Pause or resume the room",
        [
            "Pause holds delivery to agents and their outgoing messages.",
            "A turn already running may finish. Resume releases waiting messages.",
        ],
        "",
        "",
    ),
    (
        "clear",
        "Clear conversation history",
        [
            "Messages move to the room's local archive. Confirm before clearing.",
            "This does not erase an agent's own session history.",
        ],
        "Clear history",
        "/clear",
    ),
    ("stop", "Stop the room", ["Stop asks which room windows will close and preserves history."], "Stop room", "/stop"),
    (
        "keys",
        "All keys",
        ["The full guide: recipients, multiline input, clearing a draft, history, status indicators,", "and all commands."],
        "",
        "",
    ),
]


def HELP(me: str, full: bool = False, ids: list[str] | None = None, lang: str = "en") -> str:
    """Shown when the seat opens and on /help: a framed card, keys in one column (the room must explain itself on start — and look tidy doing it)."""
    # On start — only what is needed right away; /help shows everything.
    rows_ = [
        ("Tab / Shift+Tab", "Choose a recipient"),
        ("/", "Open the command menu; ↑/↓ to choose, Enter to select"),
        ("Esc", "Go back or close the menu"),
        ("Enter", "Send a message; multiline pastes stay in one message"),
        ("↑ / ↓", "Earlier messages, one at a time; Esc — back to the newest"),
        # Kept short: the start card must not grow a line (a taller card moved PgUp's page).
        ("Red status", "An agent needs you — /answer opens its terminal"),
        ("/name", "Rename yourself or an agent"),
        ("/add · /remove", "Add an agent · disconnect a participant"),
        ("/view · /open", "See an agent's screen · open its window in a new tab"),
        ("/help", "Show this guide"),
    ]
    if full:
        # /help: every command on its own line, with what it actually does.
        rows_ = [
            ("/", "Open the command menu; ↑/↓ to choose, Enter to select"),
            ("Esc", "Go back or close the menu"),
            ("Tab / Shift+Tab", "Choose a recipient; shown by the input"),
            ("Enter", "Send a message; multiline pastes stay in one message"),
            ("Shift/Option+Enter", "New line inside a message (or end the line with \\)"),
            ("Ctrl+U", "Clear the whole draft, even a long pasted one"),
            ("↑ / ↓ · PgUp / PgDn", "Earlier messages: one at a time, or a page; Esc — back to the newest"),
            ("@name text", "Send to someone without changing the chosen recipient"),
            ("Red status", "An agent needs your input — use /answer, then Ctrl+] to return"),
            ("Status colours", "Green: ready · Yellow: working · Orange: muted or limit · Red !: act in its window"),
            ("Status marks", "× muted · ◷ limit reached · ? no recent data · … connecting"),
            ("", "Green with a spinner and a number: ready for messages, background tasks still run"),
            ("Status layout", "More agents wrap to another row; none are hidden"),
            ("/add", "Start a new agent: choose its kind, its folder, its name"),
            ("/team", "See the team and where each agent is with its assignment; in a new room, start a ready team"),
            ("/remove", "Disconnect a participant; the agent keeps running in its window"),
            # A muted agent still gets what is addressed to it by name — the text said all.
            ("/mute · /unmute", "Stop / resume room-wide messages to an agent; messages to it by name still arrive"),
            ("/unlimit", "An agent is back from its usage limit: the room delivers to it again"),
            ("/name", "Rename yourself or an agent: choose whom, type the new name"),
            ("/help", "Show this guide"),
            ("/view", "See an agent's screen, read-only; Esc — back to chat"),
            ("/answer", "Respond in an agent terminal over the chat; Ctrl+] returns"),
            ("/open", "Open an agent's window in a new terminal tab, to answer it"),
            ("/restart", "Restart an agent the room started; its conversation continues (new rules too)"),
            ("/mine", "What is for you: agents waiting for you, then unread messages to you and answers to yours"),
            (
                "/dm name · /dm close",
                "Talk with one agent in a panel beside the chat: Ctrl+O there and back, Tab tabs in it, Ctrl+] close",
            ),
            ("/pause · /resume", "Pause the room: agents get no messages until /resume"),
            ("/clear", "Clear the history for everyone; old messages move to an archive"),
            ("/stop", "Stop the room: its windows and the agents it started close; history stays"),
            ("Ctrl+C · /exit", "Close this chat panel (asks first); agents keep running"),
            ("/language", "The room's language: Russian or English"),
        ]
    # In the room's language: each command by the name it shows, each line from the catalog; keys stay as they are.
    labels = ("Red status", "Status colours", "Status marks", "Status layout", "@name text", "/dm name · /dm close")
    rows_ = [
        (
            m_i18n.help_words(k, lang) if k in labels else m_i18n.shown_commands(k, lang),
            m_i18n.help_words(v, lang),
        )
        for k, v in rows_
    ]
    key_w = max(m_text.text_cols(k) for k, _ in rows_)
    body = [f"{k.ljust(key_w)}   {v}" for k, v in rows_]
    title = " " + m_i18n.help_words("Room guide · {me}", lang, me=me) + " "
    inner = max(max(m_text.text_cols(b) for b in body) + 4, m_text.text_cols(title) + 4)  # columns: a CJK name
    if inner + 2 > shutil.get_terminal_size((100, 24)).columns:
        return "\n".join(
            [m_text.paint(title.strip(), "1;37")]
            + ["  " + m_text.paint(k.ljust(key_w), "1;38;5;75") + "   " + v for k, v in rows_]
        )
    frame = lambda t: m_text.paint(t, "38;5;240")
    out = [frame("╭─") + m_text.paint(title, "1;37") + frame("─" * (inner - m_text.text_cols(title) - 1) + "╮")]
    for k, v in rows_:
        text = "  " + m_text.paint(k.ljust(key_w), "1;38;5;75") + "   " + m_text.paint(v, "38;5;252")
        pad = inner - (2 + key_w + 3 + m_text.text_cols(v))
        out.append(frame("│") + text + " " * pad + frame("│"))
    out.append(frame("╰" + "─" * inner + "╯"))
    return "\n".join(out)
