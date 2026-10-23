"""The chat's input row and the menu above it: drawn from the chat's own objects."""

from __future__ import annotations
import shutil
import sys
from dataclasses import dataclass
from typing import Any
import collab.text as m_text
import collab.i18n as m_i18n
import collab.chat_base as m_chat_base


@dataclass(frozen=True)
class ChatMenuContext:
    """The chat's own objects and callbacks the input row and its menu draw with — the same
    objects, never copies; all of them made before the context."""

    args: Any
    buf: Any
    config: Any
    dm_focus: Any
    draw_bottom: Any
    draw_reading: Any
    feed_log: Any
    history_label: Any
    menu_items: Any
    repaint: Any
    rows: Any
    ui: Any
    wizard: Any


def chat_feed_rows(ctx: ChatMenuContext, n: int, w: int) -> list[str]:
    """The feed's last n screen rows, as feed() and repaint() put them: wrapped to the width,
    a command to copy left whole; blank rows on top when the feed is shorter."""
    out: list[str] = []
    for line in ctx.feed_log[-n * 2 :]:
        out.extend([line[1:]] if line.startswith(m_chat_base.WHOLE) else m_text.wrap_visible(line, w - 1))
    return ([""] * n + out)[-n:]


def chat_draw_menu(ctx: ChatMenuContext, items: list[tuple[str, str, str]], h: int, w: int) -> None:
    """A column above the rule, over the bottom of the feed; the feed is repainted when it closes."""
    if not items:
        return
    # A long list (40 folders) scrolls with the choice; the old cut showed only its end, and
    # the highlight fell off the screen.
    cap = max(1, h - 6)
    sel = ctx.ui.menu_index % len(items)
    first = max(0, min(sel - cap // 2, len(items) - cap))
    rows_ = items[first : first + cap]
    sel -= first
    # Only a menu that got shorter leaves its old rows over the feed — then the screen is
    # rebuilt. The check compared the count of items with rows + header, so a menu that fits
    # was rebuilt on every key: the whole screen cleared and redrawn — the flicker and the
    # «shifted» screen that was seen.
    out = "\0337"
    top = h - 3 - len(rows_)
    if ctx.ui.popup and len(rows_) + 1 < ctx.ui.popup:
        if items[0][1].startswith("pick:"):
            # A question's next step is shorter: what it said was printed under the old menu,
            # so the screen is rebuilt — once per step, not per key.
            ctx.ui.popup = 0
            ctx.repaint()
            return
        # Typing filters the command menu, key by key: the rows it no longer covers get the
        # feed back, written over in place — no screen cleared (it flickered), and none left
        # blank (a step of /add came next and the feed above stayed empty).
        lines = chat_feed_rows(ctx, h - 3, w)
        for row in range(max(1, h - 3 - (ctx.ui.popup - 1)), top):
            out += f"\033[{row};1H" + lines[row - 1] + "\033[K"
    if items[0][1].startswith("pick:"):
        # The menu covers the bottom of the feed, where the question was said — so the
        # question heads the menu itself, or the choices come without what they answer.
        more = f"{ctx.ui.menu_index % len(items) + 1}/{len(items)}  " if len(items) > cap else ""
        hint = m_i18n.tr("  ↑↓ choose · Enter select · Esc back")
        # One row, never wrapped: a long folder path gives way in its middle, the end is its name.
        prompt = ctx.wizard.get("prompt", "Choose")
        if len(more) + m_text.text_cols(prompt) + len(hint) + 3 > w:
            hint = ""  # a narrow window: the question first, the keys are known by now
        space = max(10, w - 3 - len(more) - len(hint))
        if m_text.text_cols(prompt) > space:
            prompt = m_text.cut_cols(prompt, space // 2 - 1) + "…" + m_text.cut_cols(prompt, space - space // 2, from_end=True)
        header = m_text.paint(f"  {more}", "2") + m_text.paint(prompt, "1") + m_text.paint(hint, "2")
    else:
        header = m_text.paint(m_text.cut_cols(m_i18n.tr("  ↑↓ choose · Enter select · keep typing to filter"), w - 1), "2")
    # Each row written over the old one, then the rest of the line erased: erasing first left
    # it blank for a moment on every key.
    out += f"\033[{top};1H" + header + "\033[K"
    # A label longer than the window gives way in its middle: a path's end is the
    # folder's name, the part that matters.
    fit = lambda t, n: (
        t if m_text.text_cols(t) <= n else m_text.cut_cols(t, n // 2) + "…" + m_text.cut_cols(t, n - n // 2 - 1, from_end=True)
    )
    label_w = min(max(m_text.text_cols(label) for label, _, _ in rows_), max(8, w - 4))
    for i, (label, _, hint) in enumerate(rows_):
        mark = "›" if i == sel else " "
        cell = fit(label, label_w)
        text = m_i18n.tr(" {mark} {cell}{p2}", mark=mark, cell=cell, p2=" " * (label_w - m_text.text_cols(cell))) + (
            f"   {hint}" if hint else ""
        )
        text = m_text.cut_cols(text, w - 1)  # columns: CJK paths cut at half the width they took
        out += f"\033[{top + 1 + i};1H" + (m_text.paint(text, "1;38;5;75") if i == sel else text) + "\033[K"
    sys.stdout.write(out + "\0338")
    ctx.ui.popup = len(rows_) + 1


def chat_render(ctx: ChatMenuContext) -> None:
    """Draw the input row: «Ann → Bob › text», cursor where the person is typing."""
    h, w = ctx.rows(), shutil.get_terminal_size((100, 24)).columns
    head = m_chat_base.input_head(
        ctx.config, ctx.args.as_, ctx.ui.selected, max(10, w - 1 - 12)
    )  # twelve columns at least for the text
    sys.stdout.write(m_chat_base.input_row(head, ctx.buf, ctx.ui.pos, h, w))
    items = ctx.menu_items()
    if items:
        ctx.ui.menu_index %= len(items)
    dm_open, here = ctx.dm_focus
    ctx.ui.menu_line = m_chat_base.input_hint(
        len(ctx.buf),
        ctx.ui.mine_count,
        dm_open,
        here,
        ctx.history_label() if ctx.ui.top is not None else None,
        bool(items),
        bool(ctx.wizard),
        w,
    )
    chat_draw_menu(ctx, items, h, w)
    if ctx.ui.popup and not items:
        ctx.ui.popup = 0
        ctx.repaint()
    if ctx.ui.reading:
        ctx.draw_reading()  # a repaint drew the feed over it: the message of /mine stays on show
    ctx.draw_bottom()
    sys.stdout.write(m_chat_base.SYNC_OFF)
    sys.stdout.flush()
