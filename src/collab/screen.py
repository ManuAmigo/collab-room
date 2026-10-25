from __future__ import annotations
import calendar
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence, cast
import collab.text as m_text
import collab.i18n as m_i18n
import collab.store as m_store
import collab.tmuxio as m_tmuxio

STATES = {"joining", "working", "idle", "needs_owner", "draft", "unknown", "lost", "limited", "scrolled"}

# How each known agent looks on screen (verified on Codex 0.163 and Claude Code 2.1).
PRESETS: dict[str, dict[str, Any]] = {
    "codex": {
        "label": "Codex",
        "command": ["codex", "--no-alt-screen"],
        "delivery": "tmux",
        # «• Working (12s • esc to interrupt)» begins its line; the same words inside an agent's
        # sentence («I grep'd for "Working ("») are talk. Not only Working: any
        # spinner line that ends «(… • esc to interrupt)» — «• Compacting context (2s • esc to
        # interrupt)» was read as idle and delivery typed into it. A title
        # may hold brackets of its own («Thinking about the fix (approach 2) (5s …)»): in doubt, busy —
        # a wait costs one tick, typing into a turn costs the turn.
        "idle": "^› Ask Codex",
        "busy": "^\\S [A-Z][^\\n]*\\(\\d[^\\n]*• [Ee]sc to interrupt\\)\\s*$",
        # Also Codex's own start-up questions (hooks to trust): a new Codex
        # sat on «t trust all · enter review · esc close» while the room showed Connecting.
        # And the folder to trust: «› 1. Trust and continue» read as a draft.
        # And its very first screen, to sign in («… Press enter to continue»): a new person saw
        # «Connecting» for ever and never learned to open the window (codex 0.163).
        "attention": "^\\s*(Press enter to confirm or esc to cancel|Press enter to continue\\s*$|.*· esc close\\s*$|enter continue · esc back\\s*$)",
        "attention_title": "(Would you like to [^?\\n]+\\?|\\d+ hooks? needs? review[^\\n]*|Trust this folder\\?|Sign in with ChatGPT to use Codex)",
        "draft": "^› (?!Ask Codex)\\S",
        # Typed fast, a long text is taken for a paste and folded: «› [Pasted Content 3859 chars]»,
        # and the Enter that came right after did not send it. Longer than this
        # it goes as a pointer; a fold still on screen after Enter gets one more Enter.
        "paste_limit": 1000,
        "paste_fold": "^› \\[Pasted Content \\d+ chars\\]",
        # Words of a mode may follow the effort: «GPT-6.1-Sol low fast ·» (no model was shown).
        "model": "^\\s+(\\S+) (?:default|minimal|low|medium|high|xhigh)(?: [a-z]+)* ·",
    },
    "claude": {
        "label": "Claude",
        "command": ["claude"],
        "delivery": "tmux",
        # After «❯» Claude Code puts a no-break space (U+00A0), not a space: with a plain
        # space a room-started Claude was never «idle» and hung on Connecting.
        # Busy: the footer under the input («  ⏵⏵ auto mode on · esc to interrupt») or the spinner
        # line («✢ Crystallizing… (9m 59s · …)»); a question: its menu's last line («Esc to cancel ·
        # Tab to amend»). Anywhere in an answer («⏺ Press Esc to cancel the build») they were read
        # as the agent's state — busy forever, or red.
        "idle": '^❯[ \u00a0]*(Try "|$)',
        "busy": "^  (?:[^\\n]*· )?esc to interrupt(?: ·|\\s*$)|^\\S [A-Z][a-z]+… \\(\\d",
        # Its first run too: the theme to choose («Syntax theme: … (ctrl+t to disable)» under the
        # list) and the way to log in (Claude Code 2.1.302).
        "attention": "^\\s*(?:Enter to confirm|Esc to cancel)(?: · [^\\n]*)?\\s*$"
        "|^\\s*Syntax theme: [^\\n]*\\(ctrl\\+t to disable\\)\\s*$|^\\s*Select login method:\\s*$",
        "attention_title": "(Do you want to [^?\\n]+\\?|Is this a project you created[^?\\n]*\\?"
        "|Choose the text style that looks best with your terminal|Select login method)",
        "draft": '^❯[ \u00a0]+(?!Try ")\\S',
        # Typed fast, a long text is taken for a paste and folded — «❯ [Pasted text #1]», sometimes with the rest of it
        # after — and the Enter right after did not send it: four briefs stood as drafts, delivery stopped for all.
        "paste_fold": "^❯[ \u00a0]+\\[Pasted text #\\d+(?: \\+\\d+ lines)?\\]",
        "model_from": "claude-session",
        "dim_is_hint": True,
        # A command it left running in the background: its turn is over, the work is not
        # («  ⏵⏵ auto mode on · 1 shell · ← 1 agent», Claude Code 2.1.302). «· 1 monitor» is not
        # one: a standing watch of the room, there all the time.
        "background": "^  [^\\n]*· (\\d+) shells?\\b",
        # A subagent it left running in the background: a row of its own right under the footer —
        # «  ⏺ main» (the agent itself, not counted) and «  ◯ Explore  Reading …» for each live one
        # (Claude Code 2.1.302). «/tasks to see subagents» is a hint, not this:
        # it went on Ready with one alive and stayed with none; «← 1 agent» is there always.
        "subagent": "^  ◯ \\S",
    },
}


def ping_text(code: str) -> str:
    return f'Connection check: reply in the room with "connected {code}".'


def only_own_seat(config: dict[str, Any], pid: str, verb: str) -> None:
    """A Claude agent acts only on its own seat; the person, outside any Claude, on anyone (a second Claude renamed another's seat; remove and mute did not look)."""
    sid = caller_session_or_refuse()
    if sid:
        mine = [p["id"] for p in config["participants"] if p.get("claude_session") == sid]
        if pid not in mine:
            m_store.fail(
                m_i18n.tr(
                    "an agent {verb} only its own seat ({p1}); {pid} is someone else's — ask the person",
                    verb=verb,
                    p1=", ".join(mine) or "none",
                    pid=repr(pid),
                )
            )


def claude_of_window(home: Path, target: str) -> str:
    """The session of the Claude in this window, as Claude itself records it — only when that record
    is of this very process: its pid, this window, and its start time (a pid is used again on macOS,
    and an old record with it would name another conversation)."""
    if not target:
        return ""
    pid = m_tmuxio.tmux("display-message", "-p", "-t", target, "#{pane_pid}", check=False).stdout.strip()
    if not pid.isdigit():
        return ""
    info = m_store.read_json(home / "sessions" / f"{pid}.json")
    started = subprocess.run(["ps", "-o", "lstart=", "-p", pid], capture_output=True, text=True).stdout.strip()
    try:
        ours = time.mktime(time.strptime(started, "%a %b %d %H:%M:%S %Y"))
        theirs = calendar.timegm(time.strptime(str(info.get("procStart", "")), "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return ""
    pane = target if target.startswith("%") else ""
    if str(info.get("pid")) != pid or abs(ours - theirs) > 2 or (pane and not str(info.get("tmux", "")).endswith("." + pane)):
        return ""
    return str(info.get("sessionId", ""))


def brief_owner(config: dict[str, Any], sid: object) -> str:
    """Whose assignment a Claude Code session is given by its SessionStart hook: the participant bound to
    that session, or — after /clear, which starts a new one — the agent the room started whose window's
    Claude now runs it, as Claude itself records it. Never by a name the hook is given: COLLAB_AS is
    anyone's to set. "" — nobody, or two."""
    if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
        return ""
    bound = [p["id"] for p in config["participants"] if p.get("claude_session") == sid]
    if bound:
        return bound[0] if len(bound) == 1 else ""
    home = Path(os.environ.get("COLLAB_CLAUDE_HOME", "~/.claude")).expanduser()
    mine = [
        p["id"]
        for p in config["participants"]
        if p.get("started_by_room")
        and agent_kind(p) == "claude"
        and claude_of_window(home, p.get("target") or "") == sid
        # tmux gives «%N» out again after its server restarts: the window must still be this agent's process
        and not pane_guard(p, p.get("target") or "", idle=False)
    ]
    return mine[0] if len(mine) == 1 else ""


# Between the room's head «[room → codex]» and its text in a Codex log: the time and the line break,
# as JSON writes them («09:17\\n  »), or nothing (older versions). Not a «[»: no other head fits in.
CHECK_GAP = r"(?: \d\d:\d\d)?(?:\\+n)?[ ]*"
SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def agent_session(p: dict[str, Any]) -> str:
    """The conversation of an agent the room started, to continue it: Claude's is told by its hooks;
    Codex's is the session log in its folder where it answered its connection check — the code is
    the room's own, so another Codex in the same folder is not taken for it. "" — not found."""
    rp = m_store.room().paths
    kind = agent_kind(p)
    folder = os.path.realpath(p.get("folder") or str(rp.root))
    if kind == "claude":
        # The Claude in its window tells its own session (~/.claude/sessions/<pid>.json); else the one
        # the room read there itself while it ran (window_session). Never the hooks': they are the
        # agent's own words, and five Claudes share one folder — a borrowed id would open someone
        # else's conversation here.
        home = Path(os.environ.get("COLLAB_CLAUDE_HOME", "~/.claude")).expanduser()
        for sid in [claude_of_window(home, p.get("target") or ""), str(p.get("window_session") or "")]:
            if (
                SESSION_ID.fullmatch(sid)
                and (home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", folder) / f"{sid}.jsonl").is_file()
            ):
                return sid
        return ""
    if kind != "codex" or not p.get("join_code"):
        return ""
    since = m_text.at_time(p.get("joined_at", "")) - 60
    logs = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions"
    try:
        found = sorted(
            (f for f in logs.rglob("rollout-*.jsonl") if f.stat().st_mtime >= since),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return ""
    # What only this agent was given: the room's check addressed to it, «[room → <id>] … connected <code>».
    # Its answer goes to everyone, so another Codex in the same folder has that line too.
    # Only «→»: the room's own header. In a message's text «→» is drawn as «->», so «->» — and a
    # typed «\\u2192» — could be written by another agent into this folder's other Codex.
    # Matched as delivery writes it now — «[room → codex] 09:17», then the text on the next line
    # (glued into one, /restart of a Codex said «not found»): the head, then only its time and line
    # break, then the check with this agent's code; quotes as the log escapes them, once or twice.
    check = re.escape(ping_text(p["join_code"])).replace('"', r'\\*"')
    asked = re.compile(re.escape(f"[room → {p['id']}]") + CHECK_GAP + check)
    for f in found[:50]:
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                meta: dict[str, Any] = json.loads(fh.readline()).get("payload") or {}
                text = fh.read(20_000_000)
                if os.path.realpath(str(meta.get("cwd", ""))) != folder or not asked.search(text):
                    continue
        except (OSError, ValueError, AttributeError):
            continue
        sid = str(meta.get("id", ""))
        if SESSION_ID.fullmatch(sid):
            return sid
    return ""


SCREEN_KEYS = (
    "idle",
    "busy",
    "attention",
    "attention_title",
    "draft",
    "model",
    "dim_is_hint",
    "background",
    "subagent",
    "paste_limit",
    "paste_fold",
)


def screen_rules(p: dict[str, Any]) -> dict[str, Any]:
    """How to read an agent's screen. One the room started reads it by today's rules of its kind:
    /add copies them into its entry, and a fix of a rule never reached the agents already added
    (a new Codex's «Trust this folder?» read as a draft)."""
    kind = PRESETS.get(p.get("kind", "")) if p.get("started_by_room") else None
    return {**p, **{k: v for k, v in kind.items() if k in SCREEN_KEYS}} if kind else p


SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh", "nu", "xonsh", "pwsh", "login"}


# The third field: the room's session a view of /open marks, else the window's group (the
# views before it were grouped with the room's session).
GUARD_FORMAT = f"#{{pane_pid}}\t#{{pane_current_command}}\t#{{?{m_tmuxio.VIEW_OF},#{{{m_tmuxio.VIEW_OF}}},#{{session_group}}}}\t#{{@collab_root}}"
# What capture-pane -e adds to the text: colours (CSI … m) and links (OSC 8 «\033]8;;url\033\\», tmux 3.4+).
SGR = re.compile(r"\033\[[0-9;:]*[A-Za-z]|\033\][^\033\a]*(?:\033\\|\a)")


@dataclass
class Shot:
    """One look at a window in a tick: its screen as capture-pane gives it, and what pane_guard reads."""

    returncode: int
    stdout: str  # the screen (with colour codes when the rules take dim text for hints)
    guard: str  # «pid\tprogram\tgroup\troot» of the same window
    scrolled: bool = False  # in copy mode (the wheel, Ctrl+b [): what is typed goes to its keys, not the agent

    @property
    def plain(self) -> str:
        """The screen as a plain capture-pane gives it: no colour codes, no spaces at line ends."""
        return "\n".join(line.rstrip() for line in SGR.sub("", self.stdout).split("\n"))


def look(p: dict[str, Any], pane: str) -> Shot:
    """The window's screen and its process, session and room in ONE tmux call (a
    tick took three captures and a display-message per agent). Both commands are tmux's own, on the same
    target — resolved exactly as when they went one by one. Between them a mark no window can type: it is
    new for every look. Not found, or no mark: a failed look — the window is lost, as before."""
    mark = secrets.token_hex(8)
    hints = ["-e"] if p.get("dim_is_hint") else []
    shown = m_tmuxio.tmux(
        "capture-pane",
        "-p",
        *hints,
        "-t",
        pane,
        ";",
        "display-message",
        "-p",
        "-t",
        pane,
        f"{mark}\t#{{pane_in_mode}}\t{GUARD_FORMAT}",
        check=False,
    )
    screen, found, guard = (shown.stdout or "").rpartition(mark + "\t")
    if shown.returncode != 0 or not found:
        return Shot(1, "", "")
    in_mode, _, guard = guard.partition("\t")
    return Shot(0, screen, guard, in_mode == "1")


def pane_guard(p: dict[str, Any], pane: str, idle: bool = True, root: Path | None = None, shot: Shot | None = None) -> str:
    """Why this window is not the agent's, or "" when nothing says so. What the screen shows is a
    guess; this is the structure behind it (a shell prompt «❯ » read as an idle
    Claude, the message typed into the shell and run). Two checks:
    — the process the room started in the window is still its process: tmux gives window ids out
      again after its server restarts, and «%19» may then be someone's shell in another session;
    — about to type into a Claude or a Codex whose process the room does not know (a window the
      person named, or one started by an older version), a shell is not it. Checked only when the screen
      says idle: while an agent runs a command, that command may hold the terminal for a moment.
      Not by the program's name: Claude Code names its process by its version («2.1.302»)."""
    rp = m_store.room().paths
    if shot is not None:
        # The same answer tmux gave the tick's one look at this window (look()): the same target, the same moment.
        returncode, answer = shot.returncode, shot.guard
    else:
        shown = m_tmuxio.tmux("display-message", "-p", "-t", pane, GUARD_FORMAT, check=False)
        returncode, answer = shown.returncode, shown.stdout
    if returncode != 0:
        return "Agent window not found."
    pid, program, group, its_root = (answer.rstrip("\n").split("\t", 3) + ["", "", ""])[:4]
    if not its_root and group:
        # The window is in a view /open made (unmarked; it names the room's session, or is grouped with
        # it): tmux may name that one for it — every agent read as another room's.
        its_root = m_tmuxio.session_room(group)
    # Whose session the window is in (room_options marks each room's): after a tmux restart another room
    # took its old «%1»…«%6» — this room's agents' windows — for its own and typed into them.
    # An agent the room started lives only in the room's session; no agent is in another's.
    # The mark guards against a mistake, not an attacker: anyone with the tmux server can set it.
    if (its_root and its_root != os.path.realpath(str(root or rp.root))) or (p.get("started_by_room") and not its_root):
        return "Its window is in another room's session (the window id was reused)."
    if p.get("pane_pid"):
        # Started by the room with `exec`: the window's process IS the agent, and the window
        # closes when it ends — the same process is all there is to check.
        return "" if pid == str(p["pane_pid"]) else "Its window now runs another program (the window was reused)."
    if str(p.get("target", "")).startswith("%"):
        # A bare «%N» with no process to check it by: after a tmux restart it is any window, in a
        # session no room marks too. The room never writes one so; a window the
        # person names goes by its session («codex:0»).
        return "Its window is known only by its number, which tmux gives out again: name it by its session."
    # A seat without a command gets a bare shell from start: there is no agent in it to type into.
    # A .collab.json an agent put in its own folder made one, and delivery typed into the shell, which
    # ran the message. Whatever the screen says, and whatever the kind.
    if "target" not in p and not p.get("command") and program.lstrip("-") in SHELLS:
        return f"A shell ({m_text.clean_field(program, 20)}) is in its window: a seat without a command has no agent."
    # A window the room opens itself from the participant's command is made anew by every start;
    # only a window named by `target` can be someone's shell.
    if idle and "target" in p and agent_kind(p) in ("claude", "codex") and program.lstrip("-") in SHELLS:
        return f"A shell ({m_text.clean_field(program, 20)}), not the agent, is in its window."
    return ""


def screen_state(p: dict[str, Any], pane: str, shot: Shot | None = None) -> tuple[str, str]:
    """What the participant's screen says, from its bottom lines only.

    needs_owner — the screen asks the person for something (approve a command, pick an option);
    working — `busy` matches (an agent's prompt can stay visible while it works: Codex shows `›`
    and `Working (…)` at the same time, and typing then would queue or steer the turn);
    idle — `idle` matches, safe to type; unknown — none matched; lost — the pane is gone.
    Only the bottom: quoted text higher up (a delivered message mentioning "Working (") must not
    change the state — found in a live run.
    """
    hints = bool(p.get("dim_is_hint"))
    shown = shot or m_tmuxio.tmux("capture-pane", "-p", *(["-e"] if hints else []), "-t", pane, check=False)
    if shown.returncode != 0:
        return "lost", "Agent window not found."
    text = shown.stdout
    if hints:
        # Claude Code puts a suggested next prompt into its input in dim text. It is not a
        # draft: the agent is idle. Read as one, it painted an agent red and stopped delivery
        # to it. Drop dim spans, then the remaining colour codes.
        text = re.sub(r"\033\[2m.*?(?:\033\[(?:0|22)m|$)", "", text, flags=re.MULTILINE)
        text = re.sub(r"\033\[[0-9;:]*[A-Za-z]", "", text)
    # Claude Code connected to an IDE shows the file open there as a chip in its input,
    # «[⧉ In AGENTS.md]». It is not typed text: read as a draft, it painted five agents red and
    # stopped delivery to them when the person opened a file in WebStorm.
    text = re.sub(r"\[⧉[^\]\n]*\][ \u00a0]*", "", text)
    lines = [line.rstrip() for line in text.rstrip("\n").splitlines()[-args_tail() :]]
    tail = "\n".join(lines)
    if p.get("attention") and re.search(p["attention"], tail, re.MULTILINE):
        # The question itself may sit above the bottom lines in a long menu: look for its title
        # above the bottom lines (and above the visible screen), only to describe it — the trigger stays anchored at the bottom.
        detail = ""
        if p.get("attention_title"):
            # A short pane (split windows) scrolls the title out of view: read some history too.
            above = m_tmuxio.tmux("capture-pane", "-p", "-S", "-60", "-t", pane, check=False).stdout
            titles = re.findall(p["attention_title"], above, re.MULTILINE)
            detail = titles[-1].strip() if titles else ""
        return "needs_owner", detail[:160]
    if p.get("busy") and re.search(p["busy"], tail, re.MULTILINE):
        return "working", ""
    if p.get("idle") and re.search(p["idle"], tail, re.MULTILINE):
        return "idle", ""
    # Someone typed into the agent's own input and did not send it: delivery waits so as not to
    # mix texts. Say so — "unknown" left the person guessing why the agent fell silent.
    if p.get("draft") and re.search(p["draft"], tail, re.MULTILINE):
        return "draft", ""
    return "unknown", ""


def fold_left(rules: dict[str, Any], pane: str, typed: str = "") -> bool:
    """Our own text folded in the input line and still not sent — and nothing else on screen that an
    Enter would answer (a question, a menu): one more Enter must press no one else's button.

    One look at the screen decides it all: the fold must be the last input line itself (not the
    same words higher up), and that same look shows no question, its title, or work going on.
    """
    if not rules.get("paste_fold"):
        return False
    shown = m_tmuxio.tmux("capture-pane", "-p", "-t", pane, check=False)
    if shown.returncode != 0:
        return False
    tail = "\n".join(line.rstrip() for line in (shown.stdout or "").rstrip("\n").splitlines()[-args_tail() :])
    prompt = rules["paste_fold"].lstrip("^")[:1]  # «›» Codex, «❯» Claude: the input line's first sign
    lines = tail.splitlines()
    at = max((i for i, line in enumerate(lines) if line.startswith(prompt)), default=-1)
    fold = re.match(rules["paste_fold"].lstrip("^").rstrip("$"), lines[at]) if at >= 0 else None
    if fold is None:
        return False
    # After the fold, only what we typed ourselves — the rest of it, and in Claude's input box (between its «─»
    # bars) its wrapped lines: a word of anyone else's there, and an Enter would send theirs. Below an input line
    # with no box (Codex) is its status, not the input.
    box = next((i for i in range(at + 1, len(lines)) if lines[i].lstrip().startswith("─")), None)
    rest = [lines[at][fold.end() :]] + (lines[at + 1 : box] if box is not None else [])
    left = "".join("".join(rest).split())
    if left and left not in "".join(typed.split()):
        return False
    return not any(
        rules.get(key) and re.search(rules[key], tail, re.MULTILINE) for key in ("attention", "attention_title", "busy")
    )


def screen_count_any(pane: str, pattern: str) -> bool:
    """Does a bottom line of the screen match (no group needed)?"""
    shown = m_tmuxio.tmux("capture-pane", "-p", "-t", pane, check=False)
    lines = [line.rstrip() for line in (shown.stdout or "").rstrip("\n").splitlines()[-args_tail() :]]
    return shown.returncode == 0 and bool(re.search(pattern, "\n".join(lines), re.MULTILINE))


def screen_count(pane: str, pattern: str, shot: Shot | None = None) -> int:
    """The number the bottom lines of the screen give in the pattern's group («· 2 shells» → 2); 0 — none."""
    returncode, text = (shot.returncode, shot.plain) if shot else plain_capture(pane)
    lines = [line.rstrip() for line in text.rstrip("\n").splitlines()[-args_tail() :]]
    found = re.search(pattern, "\n".join(lines), re.MULTILINE) if returncode == 0 else None
    return int(found.group(1)) if found and found.group(1).isdigit() else 0


def screen_subagents(pane: str, pattern: str, shot: Shot | None = None) -> int:
    """Live background subagents: the rows matching `pattern` in the block right under the footer —
    below the input box's last rule. Only there: the same words in the agent's own text above
    are not one. The block is the agents' rows («  ⏺ main», «  ◯ …») and ends at the first
    other line."""
    returncode, text = (shot.returncode, shot.plain) if shot else plain_capture(pane)
    if returncode != 0:
        return 0
    lines = [line.rstrip() for line in text.rstrip("\n").splitlines()]
    rules_at = [i for i, line in enumerate(lines) if re.fullmatch(r"─{10,}", line.strip())]
    if not rules_at:
        return 0
    below = lines[rules_at[-1] + 1 :]

    # The footer is skipped whatever its lines (one, two in a narrow window, none): up to the first row.
    def row(line: str) -> bool:
        return bool(re.match(r"^  [⏺◯] \S", line))

    start = next((i for i, line in enumerate(below) if row(line)), len(below))
    count = 0
    for line in below[start:]:
        if not row(line):
            break
        count += bool(re.match(pattern, line))
    return count


def plain_capture(pane: str) -> tuple[int, str]:
    shown = m_tmuxio.tmux("capture-pane", "-p", "-t", pane, check=False)
    return shown.returncode, shown.stdout or ""


def screen_model(p: dict[str, Any], pane: str, shot: Shot | None = None) -> str:
    """The model an agent shows on its own screen (Codex footer: «GPT-6-Luna medium · …»)."""
    if not p.get("model"):
        return ""
    returncode, text = (shot.returncode, shot.plain) if shot else plain_capture(pane)
    # The last one: the footer is at the bottom, and a line of the talk above it may match too
    # («  Risk: low · ok» read as the model).
    hits = list(re.finditer(p["model"], text, re.MULTILINE)) if returncode == 0 else []
    return hits[-1].group(1) if hits else ""


_CLAUDE_MODEL_SEEN: dict[str, tuple[float, str]] = {}


def claude_session_model(pane: str, shot: Shot | None = None) -> str:
    """The model of the Claude Code running in a pane, from its own session record: Claude keeps
    ~/.claude/sessions/<pid>.json (session id, folder), and the transcript names the model of
    every answer. The screen shows the model only in the banner, which scrolls away («why don't I see this agent's model?»). Read at most every 30 s per pane."""
    seen = _CLAUDE_MODEL_SEEN.get(pane)
    if seen and time.time() - seen[0] < 30:
        return seen[1]
    model = ""
    home = Path(os.environ.get("COLLAB_CLAUDE_HOME", "~/.claude")).expanduser()
    pid = (
        shot.guard.split("\t", 1)[0]
        if shot
        else m_tmuxio.tmux("display-message", "-p", "-t", pane, "#{pane_pid}", check=False).stdout
    )
    try:
        info = json.loads((home / "sessions" / f"{pid.strip()}.json").read_text(encoding="utf-8"))
        folder = re.sub(r"[^A-Za-z0-9]", "-", info["cwd"])
        path = home / "projects" / folder / f"{info['sessionId']}.jsonl"
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 400_000))
            found = re.findall(r'"model":"(claude-[^"]+)"', f.read().decode("utf-8", "ignore"))
        model = pretty_model(found[-1]) if found else ""
    except (OSError, ValueError, KeyError, TypeError):
        model = ""
    _CLAUDE_MODEL_SEEN[pane] = (time.time(), model)
    return model


def read_status() -> dict[str, Any]:
    rp = m_store.room().paths
    if not rp.status_path.exists():
        return {}
    try:
        data = json.loads(rp.status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    # Agents' hooks write this file too: an entry that is not a record of text fields ended the
    # chat's feed, and the next chat did not start. Only text fields are kept.
    return (
        {
            pid: {k: v for k, v in cast("dict[object, object]", st).items() if isinstance(k, str) and isinstance(v, str)}
            for pid, st in cast("dict[object, object]", data).items()
            if isinstance(pid, str) and isinstance(st, dict)
        }
        if isinstance(data, dict)
        else {}
    )


def status_lock() -> contextlib.AbstractContextManager[None]:
    """Several hooks of one agent run at the same time (Stop: «idle» and the model): without a
    lock one read-modify-write undid the other and left «working» after the turn."""
    import contextlib
    import fcntl

    @contextlib.contextmanager
    def held():
        # Opened without following a link and without truncating: «w» emptied whatever file a
        # link at this name pointed to.
        rp = m_store.room().paths
        dir_fd, name = m_store.state_dir(rp.state_dir / "status.lock")
        try:
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
        except OSError as exc:
            m_store.fail(m_i18n.tr("refusing the status lock: {strerror} (a link in the room's state?)", strerror=exc.strerror))
        finally:
            os.close(dir_fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    return held()


def set_model(pid: str, model: str) -> None:
    """Only the model; the state is someone else's business."""
    rp = m_store.room().paths
    with status_lock():
        data = read_status()
        entry = data.get(pid) or {"state": "unknown", "detail": "", "since": m_store.now(), "at": m_store.now(), "source": "self"}
        entry["model"] = m_text.clean_field(model, 40)
        data[pid] = entry
        m_store.replace_state(rp.status_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def write_status(
    pid: str, state: str, detail: str, source: str, model: str = "", background: int | None = None, screen: str | None = None
) -> None:
    with status_lock():
        _write_status(pid, state, detail, source, model, background, screen)


def screen_lines(p: dict[str, Any], plain: str) -> list[str]:
    """The screen without the lines its busy state is read by — a spinner's verb and clock, «esc to interrupt»:
    they move while a hung tool waits for ever."""
    busy = re.compile(p["busy"]) if p.get("busy") else None
    return [line for line in plain.split("\n") if not (busy and busy.search(line))]


def screen_print(p: dict[str, Any], plain: str) -> str:
    """What says the screen moved: its lines but the busy ones, digits as «#» — clocks and token counts tick on a
    stuck screen too."""
    return hashlib.sha256(re.sub(r"\d+", "#", "\n".join(screen_lines(p, plain))).encode("utf-8")).hexdigest()[:16]


# A run of letters and digits this long with both in it reads as a key or a token: not copied into the lead's line.
SECRET_LIKE = re.compile(r"(?=[\w\-]*\d)(?=[\w\-]*[A-Za-z])[\w\-]{24,}")


def screen_tail(p: dict[str, Any], plain: str, n: int = 6) -> list[str]:
    """Its last lines of work for the lead: above its input line (the person's draft is not the agent's), no busy
    lines nor box rules, no control codes, each cut; what looks like a key or a token is left out — the line stays in
    the room's log."""
    lines = screen_lines(p, plain)
    prompt = re.compile("|".join(f"(?:{p[k]})" for k in ("idle", "draft") if p.get(k)) or "(?!)")
    at = max((i for i, line in enumerate(lines) if prompt.search(line)), default=len(lines))
    shown = [SECRET_LIKE.sub("[…]", m_text.clean_field(line, 160)) for line in lines[:at]]
    return [line for line in shown if line.strip("─━-—═ ")][-n:]


BACKGROUND_DETAIL = re.compile(r"background \d+")


def _write_status(
    pid: str, state: str, detail: str, source: str, model: str = "", background: int | None = None, screen: str | None = None
) -> None:
    rp = m_store.room().paths
    m_store.private_dir(rp.state_dir)
    detail, model = m_text.clean_field(detail, 300), m_text.clean_field(model, 40)
    data = read_status()
    old: dict[str, Any] = data.get(pid) or {}
    # The same state goes on: only how many background commands run changed — Ready for 20 min
    # with «↻1», then «↻2», was shown Ready for a second.
    same = old.get("detail") == detail or all(BACKGROUND_DETAIL.fullmatch(d or "") for d in (old.get("detail"), detail))
    since = old.get("since") if old.get("state") == state and same else m_store.now()
    data[pid] = {
        "state": state,
        "detail": detail,
        "since": since,
        "at": m_store.now(),
        "source": source,
        "model": model or old.get("model", ""),
    }
    # Since when its background commands run: from the first look that saw one, Working or Ready,
    # until a look sees none — the chat's clock goes on over «Ready» from there. Which
    # one ended first the screen does not say: the earliest seen stands while any still runs.
    # background: the count a look of the screen gave; None — not counted (the agent's own hook at
    # every turn, «limited», «joining»): what was known stays — those reset the clock.
    bg_since = old.get("bg_since") if background is None or background > 0 else None
    if background:
        bg_since = bg_since or m_store.now()
    if bg_since:
        data[pid]["bg_since"] = bg_since
    # Since when its screen stands still (screen_print): a look that saw another one starts the clock anew; a write
    # with no look (its own hooks) keeps what was seen.
    if screen is not None:
        data[pid]["screen"] = screen
        data[pid]["screen_since"] = (
            old.get("screen_since") if old.get("screen") == screen and old.get("screen_since") else m_store.now()
        )
    elif old.get("screen") and old.get("screen_since"):
        data[pid]["screen"], data[pid]["screen_since"] = old["screen"], old["screen_since"]
    m_store.replace_state(rp.status_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


BAR_WORDS = {
    "joining": "Connecting",
    "working": "Working",
    "idle": "Ready",
    "needs_owner": "Needs your input",
    "draft": "Unsent draft",
    "unknown": "Unknown",
    "lost": "Disconnected",
    "limited": "Out of limit",
    "scrolled": "Scrolled — q to exit",
}
STATE_WORDS = {
    "joining": "is connecting",
    "working": "is working",
    "idle": "is ready",
    "needs_owner": "needs your input",
    "draft": "has an unsent draft — send or clear it in its window to resume message delivery",
    "unknown": "status unknown",
    "lost": "disconnected",
    "limited": "is out of its usage limit",
    "scrolled": "is scrolled back — press q in its window to resume message delivery",
}


SCREEN_STALE_MIN = 2.0


def describe(pid: str, st: dict[str, Any], stale_min: float, lang: str = "en") -> str:
    # Cleaned on show too: status.json may be written by others, and `collab status` printed its
    # escape codes straight to the terminal.
    state, detail = st.get("state", "unknown"), m_text.clean_field(str(st.get("detail", "")), 300)
    state = state if isinstance(state, str) and state in STATES else "unknown"  # a list is unhashable
    # What the deliverer read from the screen is renewed every second while it runs: two minutes old,
    # it has stopped, and the last states must not pass for live ones.
    limit = stale_min if st.get("source") == "self" else SCREEN_STALE_MIN if st.get("source") == "screen" else None
    if limit is not None:
        try:
            age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(st["at"])).total_seconds() / 60
        except (KeyError, ValueError):
            age = 0
        if age > limit:
            last = m_i18n.status_word(BAR_WORDS.get(state, state), lang)
            if st.get("source") == "self":
                return m_i18n.tr("· {pid}: No update for {n} min (last seen: {last})", lang, pid=pid, n=int(age), last=last)
            return m_i18n.tr(
                "· {pid}: No update for {n} min, delivery is not running (last seen: {last})",
                lang,
                pid=pid,
                n=int(age),
                last=last,
            )
    mark = "‼" if state in ("needs_owner", "lost", "draft") else "·"
    bg = re.fullmatch(r"background (\d+)", detail) if state == "idle" else None
    if bg:
        n = int(bg.group(1))
        if n == 1:
            return m_i18n.tr("· {pid} is ready · 1 background task", lang, pid=pid)
        return m_i18n.tr("· {pid} is ready · {n} background tasks", lang, pid=pid, n=n)
    if state == "needs_owner" and not detail:
        return m_i18n.tr("{mark} {pid} needs your input. Open the agent's window to continue.", lang, mark=mark, pid=pid)
    words = m_i18n.state_words(state, STATE_WORDS.get(state, state), lang)
    if detail:
        return m_i18n.tr("{mark} {pid} {words}: {detail}", lang, mark=mark, pid=pid, words=words, detail=detail)
    return m_i18n.tr("{mark} {pid} {words}", lang, mark=mark, pid=pid, words=words)


def args_tail() -> int:
    return int(os.environ.get("COLLAB_IDLE_LINES", "10"))


def limit_of(limits: dict[str, Any], pid: str) -> dict[str, Any]:
    """The agent's limit record, its fields of the right kind only («until» a word, a record a list —
    a TypeError on every tick); anything else reads as no record."""
    rec = limits.get(pid)
    if not isinstance(rec, dict):
        return {}
    number: Callable[[object], bool] = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    return {
        k: v
        for k, v in cast("dict[str, Any]", rec).items()
        if (k in ("until", "over", "at") and number(v)) or (k == "line" and isinstance(v, str))
    }


# An agent out of its usage limit answers every message with the same notice and falls idle — the
# room took that for silence and warned the person once per message («Codex ran out
# of limits, three warnings at once»). Now: one notice, messages wait, and go out again after.
# The agent's own notice starts its line («■ You've hit your usage limit…», «5-hour limit reached ∙
# resets 3pm»); the same words inside an agent's sentence are a quote (an agent quoted Codex's
# notice in its answer, and the room put it «out of limit»).
# Before it, only the marks the agents put there themselves — Codex «■», Claude Code «⎿», «⚠»: «- »,
# «• », «* » start an item of an agent's list quoting a notice.
LIMIT_RE = re.compile(
    r"(?i)^[\s■⎿⚠]{0,6}(?:you[’']?ve |you have |claude(?: ai)? )?"
    # Claude's other limits too: «Weekly limit reached ∙ resets …», «Session limit reached», «You've
    # reached your usage limit» were missed and messages went into a stopped agent.
    r"(hit your usage limit|reached your usage limit|usage limit reached"
    r"|(?:\d+-hour|weekly|daily|session|monthly|opus|sonnet) limit reached|limit reached\b.*\bresets?\b).*"
)


# Whose notice is whose: Codex says «hit your usage limit», Claude Code «… limit reached». A Claude
# showing Codex's screen (a grep in its tool output: «⎿  ■ You’ve hit…») starts its line the same
# way, and only the agent's kind tells them apart.
LIMIT_OF_KIND = {
    "codex": re.compile(r"(?i)hit your usage limit"),
    "claude": re.compile(r"(?i)limit reached|reached your usage limit"),
}


def agent_kind(p: dict[str, Any]) -> str:
    """claude, codex — or "" for an agent of another kind, or added by hand with its own rules."""
    return p.get("kind") or ("claude" if "❯" in p.get("idle", "") else "codex" if "Ask Codex" in p.get("idle", "") else "")


def usage_limit(pane: str, kind: str = "", shot: Shot | None = None) -> str:
    """The agent's own «out of limit» line, if it is among the bottom lines of its screen; with its
    kind known, only that kind's notice counts."""
    # Every notice LIMIT_RE takes says «limit»; on the tick's look, a wrapped one is cut by a line end.
    if shot and shot.returncode == 0 and "limit" not in shot.plain.replace("\n", "").lower():
        return ""
    # -J joins wrapped lines: a narrow window cut «… try again at 6:26 PM» off its time.
    shown = m_tmuxio.tmux("capture-pane", "-p", "-J", "-t", pane, check=False).stdout or ""
    lines = [x.strip() for x in shown.splitlines() if x.strip()][-12:]
    hits = [
        m.group(0)
        for x in lines
        for m in [LIMIT_RE.search(x)]
        if m and (kind not in LIMIT_OF_KIND or LIMIT_OF_KIND[kind].search(m.group(1)))
    ]
    return hits[-1] if hits else ""


def limit_is_last(pane: str, line: str, idle: str) -> bool:
    """Is the notice the last thing the agent said — nothing but rules between it and its input? An
    old one stays on screen above what came after it: the next message and the answer to it."""
    shown = m_tmuxio.tmux("capture-pane", "-p", "-J", "-t", pane, check=False).stdout or ""
    lines = [x.strip() for x in shown.splitlines() if x.strip()]
    at = max((i for i, x in enumerate(lines) if line in x), default=-1)
    prompt = next((i for i in range(len(lines) - 1, at, -1) if idle and re.search(idle, lines[i])), len(lines))
    return at >= 0 and all(set(x) <= set("─━-") for x in lines[at + 1 : prompt])


# Codex names the day when the limit ends after midnight: «try again at Oct 2nd, 2026 12:42 AM» —
# read as «no time» it gave 15 minutes, and the room said 23:05 for 00:42.
LIMIT_DAY = r"(?:([A-Za-z]{3})[a-z]*\.? (\d{1,2})(?:st|nd|rd|th)?,? (?:(\d{4}),? )?(?:at )?)?"
# «resets in 2h 14m»: a time from now (it was read as «no time»).
LIMIT_IN = re.compile(r"(?i)(?:try again|resets?) in\s+(?:(\d+)\s*h(?:ours?|rs?)?)?\s*(?:(\d+)\s*m(?:in(?:utes?|s)?)?)?")
MONTHS: Sequence[str] = "jan feb mar apr may jun jul aug sep oct nov dec".split()


def limit_until(line: str, now_: float) -> float | None:
    """When the limit ends, from «try again at 1:26 PM» / «resets 3pm»; else in 15 minutes. None for
    a time that has just passed: an old notice still on screen — a resumed Codex shows its history,
    and «6:26 PM» seen at 19:43 was moved to tomorrow."""
    after = LIMIT_IN.search(line)
    if after and (after.group(1) or after.group(2)):
        return now_ + int(after.group(1) or 0) * 3600 + int(after.group(2) or 0) * 60 + 30
    m = re.search(r"(?i)(?:try again at|resets?(?: at)?)\s+" + LIMIT_DAY + r"(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?", line)
    if not m:
        return now_ + 900
    month, day, year = m.group(1), m.group(2), m.group(3)
    hour, minute = int(m.group(4)), int(m.group(5) or 0)
    if m.group(6):
        hour = hour % 12 + (12 if m.group(6).lower().startswith("p") else 0)
    if hour > 23 or minute > 59 or (month and month.lower() not in MONTHS):
        return now_ + 900
    at = dt.datetime.fromtimestamp(now_).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if month:
        try:
            at = at.replace(year=int(year or at.year), month=MONTHS.index(month.lower()) + 1, day=int(day))
            if not year and at.timestamp() < now_ - 86400:
                at = at.replace(year=at.year + 1)  # «Jan 1st» seen on Dec 31 is next year
        except ValueError:
            return now_ + 900
        if at.timestamp() - now_ > 7 * 86400:
            # Real limits end within a week; a far date is a quoted line, and it held the agent
            # for years.
            return now_ + 900
        return at.timestamp() + 30 if at.timestamp() > now_ else None
    # A time without a date: the last time it was that o'clock, or the next. Passed within six hours,
    # it is an old notice; «11:10 PM» seen at 00:10 passed an hour ago — it was read as tonight,
    # 23 hours ahead. Limits without a date end within hours; «9am» seen at 20:00
    # is tomorrow morning.
    past = at if at.timestamp() <= now_ else at - dt.timedelta(days=1)
    if now_ - past.timestamp() < 6 * 3600:
        return None
    return (past + dt.timedelta(days=1)).timestamp() + 30


def caller_claude_session() -> str:
    """The Claude Code session this command runs under, if any: walk up the parent processes
    and take the first one that Claude registered in ~/.claude/sessions/<pid>.json."""
    sessions = Path(os.environ.get("COLLAB_CLAUDE_HOME", "~/.claude")).expanduser() / "sessions"
    pid = os.getppid()
    for _ in range(20):
        if pid <= 1:
            break
        try:
            return json.loads((sessions / f"{pid}.json").read_text(encoding="utf-8"))["sessionId"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            r = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], text=True, capture_output=True, timeout=2)
            pid = int(r.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            # A sandbox may forbid `ps` (Codex's did, and `say` itself failed):
            # binding is a convenience and must never stop a message.
            break
    return ""


def caller_session_or_refuse() -> str:
    """For the seat rules (one name, one Claude; an agent renames only itself). A sandbox without
    ps is handled inside and means «not a Claude»; anything else is a fault, and a broad
    `except` turned it into «allowed» — the rules silently off. Now it refuses."""
    try:
        return caller_claude_session()
    except Exception as exc:  # noqa: BLE001 — whatever it is, the answer is «not sure», so no
        m_store.fail(m_i18n.tr("could not tell which Claude session runs this command ({exc}); nothing was changed", exc=exc))
        return ""


def claude_session_alive(session_id: str) -> bool:
    """Is a Claude Code session with this id still running (its process alive)?"""
    sessions = Path(os.environ.get("COLLAB_CLAUDE_HOME", "~/.claude")).expanduser() / "sessions"
    for f in sessions.glob("*.json"):
        try:
            info = json.loads(f.read_text(encoding="utf-8"))
            if info.get("sessionId") == session_id:
                os.kill(int(info.get("pid") or f.stem), 0)
                return True
        except (OSError, ValueError, TypeError):
            continue
    return False


def check_name_is_mine(pid: str, config: dict[str, Any]) -> None:
    """One name, one Claude. A second Claude, started in the same folder, wrote under the first one's name, and the
    room took it for the first one («you started duplicating each other»). A name
    held by another running session is refused, with how to join under one's own."""
    bound = next((p.get("claude_session") for p in config["participants"] if p["id"] == pid), None)
    if not bound:
        return
    sid = caller_session_or_refuse()
    if sid and sid != bound and claude_session_alive(bound):
        m_store.fail(
            m_i18n.tr(
                "«{p0}» ({pid}) is another running Claude session in this room. Speak under your own ID; a new agent is added by the person with /add.",
                p0=m_store.label_of(config, pid),
                pid=pid,
            )
        )


@m_store.room_locked
def bind_claude_session(pid: str) -> None:
    """The session number is automatic: when a Claude acts as a participant
    itself (joins or speaks), its session becomes the participant's — so only that session's
    hooks report the participant's state. A restarted Claude rebinds the same way."""
    sid = caller_claude_session()
    if not sid:
        return
    data = m_store.read_config_raw()
    entry = next((p for p in data["participants"] if p["id"] == pid), None)
    # A Claude speaking under a Codex's id is not that Codex: bound, it would pass for the Codex lead's own session.
    if entry is None or entry.get("claude_session") == sid or agent_kind(entry) == "codex":
        return
    entry["claude_session"] = sid
    m_store.write_config(data)


def pretty_model(raw: str) -> str:
    """«claude-opus-5-5» → «Opus 5.5»; anything unfamiliar is shown as it came."""
    m = re.fullmatch(r"claude-([a-z]+)-(\d+)(?:-(\d+))?(?:-\d{8})?(?:\[.*\])?", raw.strip())
    if not m:
        return raw.strip()
    return f"{m.group(1).capitalize()} {m.group(2)}" + (f".{m.group(3)}" if m.group(3) else "")
