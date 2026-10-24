from __future__ import annotations
import datetime as dt
import os
import re
import sys
import time
import unicodedata
from pathlib import Path


ROOM = "room"
SPECIAL = {"all"}


def at_time(at: str) -> float:
    """A record's «at» as a timestamp; one that can't be read counts as long ago."""
    try:
        return dt.datetime.fromisoformat(at).timestamp()
    except ValueError:
        return 0.0


def local_hm(stamp: str) -> str:
    """Stored in UTC, shown in this computer's time zone (the room showed UTC)."""
    try:
        return dt.datetime.fromisoformat(stamp).astimezone().strftime("%H:%M")
    except ValueError:
        # Written straight into the log, `at` is anyone's text: five of its characters were enough to
        # hide what follows on the person's screen.
        return visible(str(stamp)[11:16])


def body_text(text: str) -> str:
    """A message's text as it is shown: every «→» in it is drawn «->» — only the room draws a head
    with «→». An agent's say faked a whole message of the person's in inbox and in its window;
    matching only «[a → b]» was passed by a no-break space the window then joined."""
    return visible(text).replace("→", "->")


# The words that address everyone. A name can't be one of them, and they win over any name: an
# agent called «Everyone» got the person's messages to everyone alone.
EVERYONE = {"all": "all", "всем": "all", "все": "all", "everyone": "all"}


def pasted_text(literal: str) -> str:
    """A paste as text: its line breaks one «\\n», a Tab a space, control codes out — they were taken for
    keys, and in the /dm panel went raw into the person's own input line."""
    # Not only what is below a space: DEL and the C1 controls too — «\x9b» is a CSI some terminals act on.
    return "".join(
        c
        for c in literal.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
        if c == "\n" or (c >= " " and not "\x7f" <= c <= "\x9f")
    )


def use_color() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def colours_only(line: str) -> str:
    """A line of another program's screen with its colours (SGR) and nothing else: links, titles,
    cursor moves and other control codes dropped."""
    # OSC (links, titles) whole first — a colour code inside one was taken for the line's own.
    line = re.sub(r"\033\].*?(?:\007|\033\\|$)", "", line.replace("\0", ""), flags=re.S)
    keep = [m.group(0) for m in ANSI.finditer(line)]
    marked = ANSI.sub("\0", line)  # SGR aside for a moment, so the sweep below cannot touch it
    marked = re.sub(r"\033\[[0-9;?<>=!]*[ -/]*[@-~]|\033.?", "", marked)
    marked = "".join(c for c in marked if c == "\0" or unicodedata.category(c) not in ("Cc", "Cf"))
    out = iter(keep)
    return "".join(next(out, "") if c == "\0" else c for c in marked)


def visible(text: str) -> str:
    """Only what is meant to be read: terminal control codes (ESC sequences could repaint the
    feed — erase a ⚠ mark, draw a fake «Anna →» — or, typed into an agent's window, act as
    key presses) and invisible format characters (direction overrides, zero widths) are dropped;
    tabs become spaces, line breaks stay."""
    text = (text or "").replace("\t", " ")
    return "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch) not in ("Cc", "Cf"))


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if use_color() else text


ANSI = re.compile(r"\033\[[0-9;]*m")


def wrap_kept(line: str, width: int) -> list[str]:
    """wrap_visible, the line's indent kept on every piece: a message shown wide and drawn again in a
    narrower window (a /dm panel opened beside the chat) had its body's tails at column 0, where only a
    head begins."""
    plain = ANSI.sub("", line)
    indent = len(plain) - len(plain.lstrip(" "))
    if not indent or indent >= width - 1 or text_cols(plain) <= width:
        return wrap_visible(line, width, words=True)
    cut, codes = 0, ""
    for _ in range(indent):  # the indent's spaces themselves, past any colour code before them
        while m := ANSI.match(line, cut):
            codes, cut = codes + m.group(0), m.end()
        cut += 1
    return [codes + " " * indent + piece for piece in wrap_visible(line[cut:], width - indent, words=True)]


def wrap_visible(line: str, width: int, words: bool = False) -> list[str]:
    """Split a line into pieces of at most `width` visible characters, keeping colour codes
    with the text they colour; nothing is ever dropped. `words`: a piece ends after a space when one is
    there (only a space the break itself falls on is not drawn) — a Russian line in a window 60 wide broke «/от|ветить»."""
    # In columns, not characters: «日本» is four, and a line of them ran past the window and over
    # the status rows.
    if text_cols(ANSI.sub("", line)) <= width:
        return [line]
    pieces: list[str]
    pieces, cur, cur_w, i = [], "", 0, 0
    space, space_w = 0, 0  # where the piece may end at a word: after its last space, and the columns up to there
    while i < len(line):
        m = ANSI.match(line, i)
        if m:
            cur += m.group(0)
            i = m.end()
            continue
        cw = char_cols(line[i], line[i - 1] if i else "")
        if cur_w + cw > width and cur_w:
            if words and line[i] == " ":
                i += 1  # the space the break falls on: past the width, it is not drawn
                pieces.append(cur)
                cur, cur_w, space, space_w = "", 0, 0, 0
                continue
            if words and space:
                pieces.append(cur[:space])
                cur, cur_w = cur[space:], cur_w - space_w
            else:
                pieces.append(cur)
                cur, cur_w = "", 0
            space, space_w = 0, 0
        cur += line[i]
        cur_w += cw
        if words and line[i] == " " and cur.strip(" "):
            space, space_w = len(cur), cur_w
        i += 1
    if cur:
        pieces.append(cur)
    return pieces


# Letters that look alike across Latin, Cyrillic and Greek: «Aнна» with a Latin A must count as
# «Анна» (the name check and the eye read names differently).
_LOOKALIKE = str.maketrans("аеорсухкмнтвіјѕԁԛԝаАЕОРСУХКМНТВІЈЅαοερτυνικμηχβ", "aeopcyxkmhtbijsdqwaaeopcyxkmhtbijsaoeptuvikmnxb")


def name_problem(name: str) -> str:
    """Why a name can't be used, or "": no control or invisible characters, and letters of one
    script — «Aндрей» mixing a Latin A into Cyrillic is how a borrowed name hides."""
    if visible(name) != name or "\n" in name:
        return "a name can't contain control or invisible characters"
    scripts = {unicodedata.name(ch, "?").split()[0] for ch in name if ch.isalpha()}
    if len(scripts) > 1:
        return f"a name uses one alphabet, not several ({', '.join(sorted(scripts)).lower()})"
    if not name.strip():
        return "a name can't be empty"
    if name_skeleton(name) in {name_skeleton(word) for word in EVERYONE}:
        return "that word means everyone in the room"
    return ""


def name_skeleton(name: str | None) -> str:
    """A name as the eye sees it: compatibility forms folded, invisible and joining marks, spaces
    and punctuation dropped, case and look-alike letters merged."""
    text = unicodedata.normalize("NFKD", name or "")
    kept = "".join(ch for ch in text if unicodedata.category(ch)[0] in "LN")
    return kept.casefold().translate(_LOOKALIKE)


def slug(label: str, taken: list[str], fallback: str) -> str:
    """A short latin id from a name: «Бэкенд» → bekend, never a taken one. A letter may become several:
    «Яна» was «ana» and «Юра» «ura», я and ю lost their «y». Ids already given stay."""
    table = str.maketrans(
        {
            "а": "a",
            "б": "b",
            "в": "v",
            "г": "g",
            "д": "d",
            "е": "e",
            "ё": "yo",
            "ж": "zh",
            "з": "z",
            "и": "i",
            "й": "y",
            "к": "k",
            "л": "l",
            "м": "m",
            "н": "n",
            "о": "o",
            "п": "p",
            "р": "r",
            "с": "s",
            "т": "t",
            "у": "u",
            "ф": "f",
            "х": "kh",
            "ц": "ts",
            "ч": "ch",
            "ш": "sh",
            "щ": "shch",
            "ъ": "",
            "ы": "y",
            "ь": "",
            "э": "e",
            "ю": "yu",
            "я": "ya",
            "і": "i",
            "ї": "yi",
            "є": "ye",
            "ґ": "g",
        }
    )
    base = re.sub(r"[^a-z0-9_-]+", "", label.lower().translate(table).replace(" ", "-"))[:16] or fallback
    pid, n = base, 2
    # Not a word that already means someone: «everyone» is how the chat says «all» (aliases), and
    # «owner» is the person's seat in most rooms. Only for new ids — SPECIAL itself is
    # what say accepts as a recipient, and «everyone» there would send to nobody. Nor the room's own
    # windows: an agent's window is named by its id, and two «deliver» windows hide the deliverer's.
    while pid in taken or pid in SPECIAL | {ROOM, "everyone", "owner", "deliver"}:
        pid, n = f"{base}{n}", n + 1
    return pid


def tilde(path: str) -> str:
    """A path to show: a folder's name is anybody's text — ESC in it repainted the person's screen
    from the /add menu."""
    home = str(Path.home())
    path = "~" + path[len(home) :] if path == home or path.startswith(home + "/") else path
    return visible(path).replace("\n", " ")


def clean_field(text: str, limit: int) -> str:
    """A model name or a state detail comes from the agent itself (or its transcript): no control
    codes into the person's terminal through the status line."""
    return " ".join(visible(text or "").split())[:limit]


def age(since: str) -> str:
    """«12s», «3m», «1h5m» since an ISO time; "" when unknown."""
    try:
        sec = max(0, int(time.time() - dt.datetime.fromisoformat(since).timestamp()))
    except (TypeError, ValueError):
        return ""
    return f"{sec}s" if sec < 60 else f"{sec // 60}m" if sec < 3600 else f"{sec // 3600}h{sec % 3600 // 60}m"


def char_cols(c: str, prev: str = "") -> int:
    """Terminal columns of one character: wide (CJK, most emoji) two, combining marks none. The emoji
    selector after a narrow sign makes it an emoji, two columns as tmux draws it («⚠️»): one more —
    counted as one, the line ran a column past the /dm panel and the terminal moved its last letter
    down alone."""
    if c == "\ufe0f":
        return 1 if prev and char_cols(prev) == 1 else 0
    if unicodedata.combining(c) or unicodedata.category(c) in ("Mn", "Me"):
        return 0
    if "\u1160" <= c <= "\u11ff" or "\ud7b0" <= c <= "\ud7ff":
        return 0  # a Hangul vowel or final of a jamo syllable joins the initial: «한» of three is two
    return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1


def text_cols(text: str) -> int:
    """Terminal columns of plain text: wide characters count two."""
    return sum(char_cols(c, prev) for prev, c in zip(" " + text, text))


def wrap_cols(text: str, width: int) -> list[str]:
    """Words into lines of at most `width` columns, as textwrap would but in columns: a line of
    «日本» went past the window. A word wider than a line — CJK has no spaces — is cut."""
    # The spaces as written: the line's indent and those between words — code, a table, a command to copy
    # lost them (a regression of c7d4258; textwrap kept them). Only at a break they go.
    lines: list[str] = []
    cur, gap = "", ""
    for token in re.findall(r" +|[^ ]+", text):
        if token.startswith(" "):
            if cur or lines:
                gap = token
            elif text_cols(token) < width:
                cur = token  # the indent of the line; wider than the line, it would leave it empty
            continue
        word = token
        while text_cols(word) > width:
            if cur.strip():
                lines.append(cur.rstrip())
            cur, gap = "", ""
            piece = cut_cols(word, width) or word[0]
            lines.append(piece)
            word = word[len(piece) :]
        joined = cur + gap + word
        if text_cols(joined) <= width:
            cur = joined
        else:
            lines.append(cur.rstrip())
            cur = word
        gap = ""
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def cut_cols(text: str, n: int, from_end: bool = False) -> str:
    """The longest start (or end) of plain text that fits `n` columns — never half a wide character."""
    out: list[str]
    out, used = [], 0
    for c in reversed(text) if from_end else text:
        cw = char_cols(c, "" if from_end or not out else out[-1])
        if used + cw > n:
            break
        out.append(c)
        used += cw
    return "".join(reversed(out) if from_end else out)


def status_cell(mark: str, name: str, state: str, model: str, since: str, width: int) -> str:
    """One agent's cell, exactly `width` columns: mark and name (and the state word) on the left, the
    model and the working time on the right. Short of room, given up in this order:
    the space in «↻ 2»; the name, down to four letters and «…»; the state word — the name gives way
    before it; the time; the model last, with «…». The mark and the count never go."""

    def fits(left: str, right: str) -> bool:
        return text_cols(left) + (1 + text_cols(right) if right else 0) <= width

    def named(mark_: str, tail: str, right: str) -> str:
        """The name as long as it fits beside the rest; "" when not even four letters and «…» do."""
        space = width - text_cols(mark_) - 1 - text_cols(tail) - (text_cols(right) + 1 if right else 0)
        if text_cols(name) <= space:
            return f"{mark_} {name}{tail}"
        return f"{mark_} {fit_cols(name, space).rstrip()}{tail}" if space >= 5 else ""

    full = " ".join(x for x in (model, since) if x)
    tail = f": {state}" if state else ""
    for mark_, tail_, right in ((mark, tail, full), (mark.replace(" ", ""), tail, full)):
        if fits(f"{mark_} {name}{tail_}", right):
            return spread(f"{mark_} {name}{tail_}", right, width)
    mark_ = mark.replace(" ", "")
    for tail_, right in ((tail, full), ("", full), ("", model)):
        left = named(mark_, tail_, right)
        if left:
            return spread(left, right, width)
    left = f"{mark_} {fit_cols(name, 5).rstrip()}"
    space = width - text_cols(left) - 1
    return spread(left, fit_cols(model, space).rstrip() if space >= 2 else "", width)


def spread(left: str, right: str, width: int) -> str:
    """`left` at the left edge, `right` at the right, exactly `width` columns. Short of room the
    right side (model, time) is cut first down to a few letters, then the left (name)."""
    if text_cols(left) + 1 + text_cols(right) > width:
        right = fit_cols(right, max(0, min(text_cols(right), width - min(text_cols(left), 10) - 1))).rstrip()
        left = fit_cols(left, width - (text_cols(right) + 1 if right else 0)).rstrip()
    return left + " " * max(1 if right else 0, width - text_cols(left) - text_cols(right)) + right


def fit_cols(text: str, width: int) -> str:
    """Exactly `width` terminal columns: cut with «…» or padded. Wide characters count two."""
    cols = text_cols
    out, used = "", 0
    for i, c in enumerate(text):
        rest = sum(cols(x) for x in text[i:])
        if used + rest <= width:
            return out + text[i:] + " " * (width - used - rest)
        if used + cols(c) + 1 > width:
            return out + "…" + " " * (width - used - 1)
        out, used = out + c, used + cols(c)
    return out + " " * (width - used)


def shorten_cols(text: str, width: int) -> str:
    """Text cut to at most `width` columns with «…». A path gives way in its middle — its end is the folder's
    name, the part that tells two apart; words keep their start (a scenario's line lost its start's meaning
    when cut in its middle: «Разделить работ…ить изменения»)."""
    if text_cols(text) <= width:
        return text
    if width < 2:
        return cut_cols(text, width)
    if re.search(r"[~\w.-]*/[^ /]+/", text):
        return cut_cols(text, width // 2) + "…" + cut_cols(text, width - width // 2 - 1, from_end=True)
    return cut_cols(text, width - 1).rstrip() + "…"


def until_hm(until: float) -> str:
    """When a limit ends: «00:42» today, «23.11 00:42» another day — «until 00:42» read late in the
    evening looked like a time already past."""
    at = dt.datetime.fromtimestamp(until)
    return at.strftime("%H:%M") if at.date() == dt.date.today() else at.strftime("%d.%m %H:%M")
