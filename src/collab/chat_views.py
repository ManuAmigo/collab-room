from __future__ import annotations
import shlex
from pathlib import Path
from typing import Any, Callable
import collab.text as m_text
import collab.i18n as m_i18n
import collab.store as m_store
import collab.log as m_log

# The chat's views: an agent's live screen (/view), the feed's history (↑/↓) and one message on
# its own (/mine). Frames and steps from explicit inputs; cmd_chat writes them and keeps its state.


def view_frame(label: str, returncode: int, shot: str, h: int, w: int) -> tuple[str, str]:
    """/view («see what the agent is doing»): the agent's own screen where the
    feed is, read-only, refreshed with the chat; Esc goes back. The frame and the line
    over the input."""
    # Colours only: tmux hands over hyperlinks (OSC 8) and other codes too, and a link to anywhere,
    # left open by the cut, made the rest of the chat a link.
    body = (
        [m_text.colours_only(x) for x in shot.rstrip("\n").split("\n")]
        if returncode == 0
        else ["", m_text.paint(m_i18n.tr("  Live view unavailable: the agent's window is gone"), "1;38;5;196")]
    )
    while body and not m_text.ANSI.sub("", body[-1]).strip():
        body.pop()  # the capture is the window's full height: its empty bottom is not the screen
    area = max(1, h - 4)
    lines = [m_text.paint(m_i18n.tr(" Live view · {label} · Read-only ", label=label), "1;7")] + body[-(area - 1) :]
    out = "\0337"
    for r in range(area):
        text = lines[r] if r < len(lines) else ""
        out += f"\033[{r + 1};1H\033[2K" + (m_text.wrap_visible(text, w - 1)[0] if text else "") + "\033[0m"
    return out + "\0338", m_text.paint(m_i18n.tr("── Esc — Back to chat ") + "─" * max(0, min(w, 120) - 22), "1;38;5;214")


def history_frame(lines: list[str], top: int, area: int) -> str:
    """The feed's lines from `top` over the `area` rows above the fixed part, the rest blank."""
    window = lines[top : top + area]
    window = window + [""] * (area - len(window))
    out = "\0337"
    for r, text in enumerate(window):
        out += f"\033[{r + 1};1H\033[2K" + text + "\033[0m"
    return out + "\0338"


def history_hint(older: Any, top: Any, unseen: int, w: int) -> str:
    """The line over the input while the person reads history."""
    first = m_i18n.tr("Beginning of the room · ") if not older and top == 0 else ""
    new = m_i18n.tr(" · {unseen} new", unseen=unseen) if unseen else ""
    label = m_i18n.tr("── {first}↑/↓ Earlier/later messages · Esc Latest{new} ", first=first, new=new)
    return m_text.paint(label + "─" * max(0, min(w, 120) - len(label)), "1;38;5;214")


def history_step(
    top: int | None,
    area: int,
    direction: int,
    page: bool,
    wrapped: Callable[[], tuple[list[str], list[int]]],
    load_older: Callable[[], int],
    older: Callable[[], Any],
) -> int | None:
    """One message back (−1) or forward (+1); with PgUp/PgDn whole messages until a window's
    worth has passed. A message taller than the window is read in parts, not jumped over.
    The new first line on screen, or None — past the newest: back to the feed."""
    lines, starts = wrapped()
    if top is None:
        top = max(0, len(lines) - area)
    origin = top
    while True:
        if direction < 0:
            if top == 0:
                added = load_older()
                if not added:
                    break
                top += added
                origin += added
                lines, starts = wrapped()
            prev = [x for x in starts if x < top]
            target = prev[-1] if prev else 0
            if top - target > area:
                target = top - (area - 1)
            top = max(0, target)
        else:
            nxt = [x for x in starts if x > top]
            target = nxt[0] if nxt else len(lines)
            if target - top > area:
                target = top + area - 1
            if target + area >= len(lines):
                return None
            top = target
        if not page or abs(top - origin) >= area - 1 or (direction < 0 and top == 0 and not older()):
            break
    return top


def reading_frame(
    config: dict[str, Any],
    me: str,
    root: Path,
    reading: dict[str, Any],
    everything: list[dict[str, Any]],
    h: int,
    w: int,
    pretty: Callable[[dict[str, Any], dict[str, Any], int, int], str],
) -> tuple[str, str, dict[str, Any] | None]:
    """/mine shows one message on its own, where the feed is (printed into the
    feed it stood right under the same message there — «not clear the first time»). The frame, the line
    over the input, and the message shown — shown is read, the caller marks it."""
    m = next((x for x in everything if x["id"] == reading["ids"][reading["at"]]), None)
    body: list[str] = []
    if m is not None:
        if m.get("re"):
            orig = next((x for x in everything if x["id"] == m["re"]), None)
            if orig:
                # The quote is text on the person's screen too: cleaned, and a line «from the person»
                # the chat did not sign is marked, as in the feed.
                first = m_text.clean_field(m_text.visible(orig["text"]).replace("→", "->"), 80)
                forged = m_i18n.tr(" ⚠ not from the chat") if m_log.unsigned_person(orig, config) else ""
                # «your message» only when it is: an agent's answer to its own words read as the
                # person's, with no ⚠.
                whose = (
                    m_i18n.tr("your message")
                    if orig.get("from") == me
                    else m_i18n.tr("{p0}'s message", p0=m_store.label_of(config, orig.get("from", "")))
                )
                body.append(
                    m_text.paint(
                        m_i18n.tr(
                            "  ↳ to {whose} of {p1}: «{first}»{forged}",
                            whose=whose,
                            p1=m_text.local_hm(orig["at"]),
                            first=first,
                            forged=forged,
                        ),
                        "2",
                    )
                )
        body += [piece for line in pretty(config, m, w - 1, 0).split("\n") for piece in m_text.wrap_visible(line, w - 1)]
    area = max(1, h - 4)
    title = f" /mine · {reading['at'] + 1} of {len(reading['ids'])} "
    if len(body) > area - 1 and m is not None:
        # Not cut in silence: read means read whole, so the rest is named, as in the feed.
        rest = len(body) - (area - 2)
        body = body[: area - 2] + [
            m_text.paint(
                m_i18n.tr(
                    "… {rest} more lines — collab show {p1} --room {p2}",
                    rest=rest,
                    p1=shlex.quote(m_text.visible(str(m["id"]))),
                    p2=shlex.quote(m_text.tilde(str(root))),
                ),
                "2",
            )
        ]
    lines = [m_text.paint(title, "1;7")] + body[: area - 1]
    out = "\0337"
    for row in range(area):
        text = lines[row] if row < len(lines) else ""
        out += f"\033[{row + 1};1H\033[2K" + (m_text.wrap_visible(text, w - 1)[0] if text else "") + "\033[0m"
    keys_ = m_i18n.tr("── ↑/↓ Previous / next · Enter or Esc — back to the list ")
    return out + "\0338", m_text.paint(keys_ + "─" * max(0, min(w, 120) - len(keys_)), "1;38;5;214"), m
