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
import collab.vpn_macos_process as m_vpn_macos_process

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
        # a wait costs one tick, typing into a turn costs the turn. After the bracket Codex may add its own
        # «· 1 background terminal running · /ps to view · /stop to close» (codex 0.164): still busy.
        "idle": "^› Ask Codex",
        "busy": "^\\S [A-Z][^\\n]*\\(\\d[^\\n]*• [Ee]sc to interrupt\\)(?: · [^\\n]*)?\\s*$",
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
        # Words of a mode may follow the effort: «GPT-6.1-Sol low fast ·», «GPT-6.1-Sol default Fast ·».
        "model": "^\\s+(\\S+) (?:default|minimal|low|medium|high|xhigh)(?: [a-z]+)*(?: Fast)? ·",
        # While it works, a line typed with Enter goes into the running turn after its next tool call: shown
        # under «• Messages to be submitted after next tool call», and its input is empty again (codex 0.164).
        "input": "^›",
        "input_empty": "^› Ask Codex",
        "queued": "^• Messages to be submitted after next tool call",
        # Still starting (its servers): an Enter given then shows «Waiting for startup  · esc cancel» under the input,
        # the fold stays, and it took one more Enter after it (codex 0.164) — that Enter is no proof it was taken.
        "starting": "^\\s*Waiting for startup\\b",
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
        # While it works, a line typed with Enter waits in its queue and goes in after its next tool call: the
        # input shows a dim «Press up to edit queued messages» meanwhile (Claude Code 2.1.310).
        "input": "^❯",
        "input_empty": "^❯[ \u00a0]*$",
        "queued": "Press up to edit queued messages",
        # Typed as keys, a text past about a kilobyte lost its first ~1022 bytes on the way into the input, before
        # the Enter — 6 or 7 times in 10 (Claude Code 2.1.310). As one bracketed paste none of 20 did.
        "bracketed_paste": True,
    },
    "kimi": {
        "label": "Kimi",
        "command": ["kimi"],
        "delivery": "tmux",
        # Kimi Code's editor is boxed; normalize only its frame before reading its input.
        # A running turn keeps an empty editor visible. Never steer it through that editor.
        "kind": "kimi",
        "boxed_input": True,
        "idle_only": True,
        "idle": r"^>\s*$",
        "input": r"^>",
        "input_empty": r"^>\s*$",
        "draft": r"^>\s+\S",
        "busy": r"^\s{1,3}[🌑🌒🌓🌔🌕🌖🌗🌘⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏](?:\s|$)",
        # The live footer: «Ask When Needed  probe  [2 tasks running]». A narrow pane shortens it to
        # «[2 tasks runn…». Only this area is read, never a matching line quoted in the transcript.
        "model": r"^\s*(?:Always Ask|Ask When Needed|Never Ask) +(\S+)(?: +|$)",
        "background": r"^\s*(?:Always Ask|Ask When Needed|Never Ask) +\S+ +\[(\d+) tasks? (?:running\b|runn…)",
        "attention": r"^\s*(?:↑↓ navigate · Enter select · Esc (?:exit|cancel)|↑/↓ select · .+ choose · ↵ confirm.*|esc back to approval|Type feedback · ↵ submit\.)\s*$",
        "attention_title": r"(?:Trust this folder\?|Run this command\?|Sign in to Kimi Code|Cache expired)",
        # No queued-turn delivery is enabled. This impossible pattern still lets idle delivery
        # check that its Enter emptied the editor instead of trusting a successful keypress.
        "queued": r"(?!)",
        "bracketed_paste": True,
        "paste_limit": 1000,
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
    if config.get("macos_vpn") is True and (owner := m_vpn_macos_process.mac_vpn_bind_hook(sid)):
        return owner
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
    deadline = time.monotonic() + 2
    try:
        if p.get("target") and p.get("pane_pid"):
            # The current process's open rollout is stronger than hints in another log.
            current = m_tmuxio.tmux("display-message", "-p", "-t", p["target"], "#{pane_pid}", check=False)
            if current.returncode or current.stdout.strip() != str(p["pane_pid"]) or not str(p["pane_pid"]).isdigit():
                return ""
            opened = subprocess.run(["lsof", "-a", "-p", str(p["pane_pid"]), "-Fn"], capture_output=True, text=True, timeout=1)
            if opened.returncode or len(opened.stdout) > 1_000_000:
                return ""
            candidates: list[Path] = []
            for row in opened.stdout.splitlines():
                if row.startswith("n"):
                    path = Path(row[1:])
                    if path.name.startswith("rollout-") and path.suffix == ".jsonl" and logs.resolve() in path.resolve().parents:
                        candidates.append(path)
        else:
            # Legacy entries have no process identity. Never guess among two checks.
            candidates = []
            for path in logs.rglob("rollout-*.jsonl"):
                if len(candidates) >= 5000 or time.monotonic() > deadline:
                    return ""
                candidates.append(path)
        dated: list[tuple[float, Path]] = []
        for path in set(candidates):
            if time.monotonic() > deadline:
                return ""
            modified = path.stat().st_mtime
            if modified >= since:
                dated.append((modified, path))
        found = [path for _, path in sorted(dated, reverse=True)]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ""
    check = re.escape(ping_text(p["join_code"]))
    asked = re.compile(
        r"(?m)^"
        + re.escape(f"[room → {p['id']}]")
        + r"(?:[ \t]*\d\d:\d\d)?[ \t]*(?:\n[ \t]*)?(?:⟦"
        + check
        + "⟧|"
        + check
        + r")(?=\n|$)"
    )
    matches: set[str] = set()
    budget = 20_000_000
    for f in found[:50]:
        if time.monotonic() > deadline or budget <= 0:
            return ""
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                header = fh.readline(65_537)
                if len(header) > 65_536:
                    continue
                first = json.loads(header)
                meta: dict[str, Any] = first.get("payload") or {}
                if first.get("type") != "session_meta" or os.path.realpath(str(meta.get("cwd", ""))) != folder:
                    continue
                sid = str(meta.get("id", ""))
                if not SESSION_ID.fullmatch(sid):
                    continue
                text = fh.read(budget)
                budget -= len(header) + len(text)
                for line in text.splitlines():
                    if time.monotonic() > deadline:
                        return ""
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    for content, tool in codex_check_texts(record):
                        for match in asked.finditer(content):
                            prefix = content[: match.start()]
                            if not tool and prefix:
                                continue
                            if tool and prefix and prefix.splitlines() and prefix.splitlines()[-1].strip():
                                continue
                            if sum(row.lstrip().startswith("```") for row in prefix.splitlines()) % 2:
                                continue
                            matches.add(sid)
                    if sid in matches:
                        break
        except (OSError, ValueError, AttributeError):
            continue
    return next(iter(matches)) if len(matches) == 1 else ""


def codex_check_texts(record: Any) -> list[tuple[str, bool]]:
    """Only user input and actual tool output can carry a room connection check."""
    if not isinstance(record, dict):
        return []
    record = cast(dict[str, Any], record)
    raw_payload: Any = record.get("payload")
    payload = cast(dict[str, Any], raw_payload) if isinstance(raw_payload, dict) else {}
    tool = False
    if record.get("type") == "response_item":
        if payload.get("type") in ("function_call_output", "custom_tool_call_output"):
            value = payload.get("output")
            tool = True
        elif payload.get("type") == "message" and payload.get("role") == "user":
            value = payload.get("content")
        else:
            return []
    elif record.get("type") == "event_msg" and payload.get("type") == "user_message":
        value = payload.get("message")
    elif record.get("type") == "event_msg" and payload.get("type") == "item_completed":
        item = cast(dict[str, Any], payload.get("item")) if isinstance(payload.get("item"), dict) else {}
        if (
            item.get("type") != "CommandExecution"
            or item.get("source") != "unified_exec_startup"
            or item.get("status") != "completed"
            or item.get("exit_code") != 0
        ):
            return []
        value = item.get("aggregated_output")
        tool = True
    elif set(record) == {"text"}:  # legacy delivery record
        value = record["text"]
    else:
        return []
    texts: list[tuple[str, bool]] = []

    def chunks(text: str) -> list[dict[str, Any]] | None:
        # The tool transport can concatenate complete exec results under its truncation header.
        # Decode only that framing, never search prose or quoted examples for embedded JSON.
        header = re.match(r"Warning: truncated output \(original token count: \d+\)\nTotal output lines: \d+\n\n", text)
        rest = text[header.end() :] if header else text
        decoder = json.JSONDecoder()
        out: list[dict[str, Any]] = []
        while rest.strip() and len(out) < 64:
            rest = rest.lstrip()
            try:
                raw, end = decoder.raw_decode(rest)
            except ValueError:
                return None
            if not isinstance(raw, dict):
                return None
            chunk = cast(dict[str, Any], raw)
            if (
                not {"chunk_id", "wall_time_seconds", "output"} <= chunk.keys()
                or not chunk.keys()
                <= {"chunk_id", "wall_time_seconds", "output", "exit_code", "original_token_count", "session_id"}
                or not isinstance(chunk["chunk_id"], str)
                or not isinstance(chunk["output"], str)
                or type(chunk["wall_time_seconds"]) not in (int, float)
            ):
                return None
            out.append(chunk)
            rest = rest[end:]
        return out if out and not rest.strip() else None

    def walk(item: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(item, str):
            try:
                decoded = json.loads(item)
            except ValueError:
                framed = chunks(item) if tool else None
                if framed is None:
                    texts.append((item, tool))
                else:
                    for chunk in framed:
                        walk(chunk["output"], depth + 1)
            else:
                if decoded != item:
                    walk(decoded, depth + 1)
        elif isinstance(item, list):
            for child in cast(list[Any], item)[:64]:
                walk(child, depth + 1)
        elif isinstance(item, dict):
            for key in ("text", "output", "content"):
                if key in item:
                    walk(item[key], depth + 1)

    walk(value)
    return texts


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
    "input",
    "input_empty",
    "queued",
    "bracketed_paste",
    "starting",
    "boxed_input",
    "idle_only",
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


def look(p: dict[str, Any], pane: str, deadline: float | None = None) -> Shot:
    """The window's screen and its process, session and room in ONE tmux call (a
    tick took three captures and a display-message per agent). Both commands are tmux's own, on the same
    target — resolved exactly as when they went one by one. Between them a mark no window can type: it is
    new for every look. Not found, or no mark: a failed look — the window is lost, as before."""
    if deadline is not None and time.monotonic() >= deadline:
        return Shot(1, "", "")
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
        timeout=None if deadline is None else deadline - time.monotonic(),
    )
    screen, found, guard = (shown.stdout or "").rpartition(mark + "\t")
    if shown.returncode != 0 or not found:
        return Shot(1, "", "")
    in_mode, _, guard = guard.partition("\t")
    return Shot(0, screen, guard, in_mode == "1")


def pane_guard(
    p: dict[str, Any],
    pane: str,
    idle: bool = True,
    root: Path | None = None,
    shot: Shot | None = None,
    deadline: float | None = None,
) -> str:
    """Why this window is not the agent's, or "" when nothing says so. What the screen shows is a
    guess; this is the structure behind it (a shell prompt «❯ » read as an idle
    Claude, the message typed into the shell and run). Two checks:
    — the process the room started in the window is still its process: tmux gives window ids out
      again after its server restarts, and «%19» may then be someone's shell in another session;
    — about to type into a Claude or a Codex whose process the room does not know (a window the
      person named, or one started by an older version), a shell is not it. Checked only when the screen
      says idle: while an agent runs a command, that command may hold the terminal for a moment.
      Not by the program's name: Claude Code names its process by its version («2.1.302»)."""
    if "container" in p:
        return "Container VPN is frozen; launch and delivery refused."
    if deadline is not None and time.monotonic() >= deadline:
        return "Screen check timed out."
    rp = m_store.room().paths
    if shot is not None:
        # The same answer tmux gave the tick's one look at this window (look()): the same target, the same moment.
        returncode, answer = shot.returncode, shot.guard
    else:
        shown = m_tmuxio.tmux(
            "display-message",
            "-p",
            "-t",
            pane,
            GUARD_FORMAT,
            check=False,
            timeout=None if deadline is None else deadline - time.monotonic(),
        )
        returncode, answer = shown.returncode, shown.stdout
    if returncode != 0:
        return "Agent window not found."
    pid, program, group, its_root = (answer.rstrip("\n").split("\t", 3) + ["", "", ""])[:4]
    if not its_root and group:
        # The window is in a view /open made (unmarked; it names the room's session, or is grouped with
        # it): tmux may name that one for it — every agent read as another room's.
        its_root = m_tmuxio.session_room(group, deadline)
        if deadline is not None and time.monotonic() >= deadline:
            return "Screen check timed out."
    # Whose session the window is in (room_options marks each room's): after a tmux restart another room
    # took its old «%1»…«%6» — this room's agents' windows — for its own and typed into them.
    # An agent the room started lives only in the room's session; no agent is in another's.
    # The mark guards against a mistake, not an attacker: anyone with the tmux server can set it.
    if (its_root and its_root != os.path.realpath(str(root or rp.root))) or (p.get("started_by_room") and not its_root):
        return "Its window is in another room's session (the window id was reused)."
    if p.get("pane_pid"):
        # Started by the room with `exec`: the window's process IS the agent, and the window
        # closes when it ends — the same process is all there is to check.
        if pid != str(p["pane_pid"]):
            return "Its window now runs another program (the window was reused)."
        return ""
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
    if idle and "target" in p and agent_kind(p) in PRESETS and program.lstrip("-") in SHELLS:
        return f"A shell ({m_text.clean_field(program, 20)}), not the agent, is in its window."
    return ""


def boxed_editor(text: str) -> str:
    """Read only a complete Kimi editor frame. A bare quoted `>` or half a redraw is no input.
    Other screen text stays visible for busy/attention detection, but cannot become an input line."""
    lines = SGR.sub("", text).splitlines()
    shown = [" " + line if line.startswith(">") else line for line in lines]
    at = max((i for i, line in enumerate(lines) if re.match(r" ?│ > ", line)), default=-1)
    top = at - 1
    bottom = next((i for i in range(at + 1, len(lines)) if re.fullmatch(r" ?╰─+╯ *", lines[i])), -1)
    if at > 0 and bottom > at and re.fullmatch(r" ?[╭├]─+[╮┤] *", lines[top]):
        body = lines[at:bottom]
        if len(lines[top].rstrip()) == len(lines[bottom].rstrip()) and all(re.fullmatch(r" ?│ .*│ *", line) for line in body):
            shown[top] = shown[bottom] = "──────────"
            content = [re.sub(r"^ ?│ (.*?) ?│ *$", r"\1", line).rstrip() for line in body]
            # One logical input row: an empty first row followed by a draft is still a draft,
            # and a quoted `>` on a continuation must never become another input marker.
            shown[at:bottom] = ["> " + "".join([content[0][2:], *content[1:]])]
    return "\n".join(shown)


def screen_tail_text(p: dict[str, Any], text: str, whole: bool = False) -> str:
    """The bottom lines of a screen (whole: all of it) as its state is read from them: no hints, no chips, no colour
    codes."""
    if p.get("boxed_input"):
        text = boxed_editor(text)
    if p.get("dim_is_hint"):
        # Claude Code puts a suggested next prompt into its input in dim text. It is not a
        # draft: the agent is idle. Read as one, it painted an agent red and stopped delivery
        # to it. Drop dim spans, then the remaining colour codes.
        text = re.sub(r"\033\[2m.*?(?:\033\[(?:0|22)m|$)", "", text, flags=re.MULTILINE)
        text = re.sub(r"\033\[[0-9;:]*[A-Za-z]", "", text)
    # Claude Code connected to an IDE shows the file open there as a chip in its input,
    # «[⧉ In AGENTS.md]». It is not typed text: read as a draft, it painted five agents red and
    # stopped delivery to them when the person opened a file in WebStorm.
    text = re.sub(r"\[⧉[^\]\n]*\][ \u00a0]*", "", text)
    lines = text.rstrip("\n").splitlines()
    return "\n".join(line.rstrip() for line in (lines if whole else lines[-args_tail() :]))


KIMI_CACHE_RULE = re.compile(r"^ ─{20,}$")
KIMI_CACHE_FOOTER = "↑↓ navigate · Enter select · Esc cancel"
KIMI_CACHE_OPTION = re.compile(r"^  (?: (❯) |   )(\S.*?)\s*$")
KIMI_CACHE_OPTIONS = (
    ("Compact and continue", "one-time compact cost · cheapest way to keep this topic"),
    ("Start a new session", "zero context cost · best for a new task"),
    ("Continue as-is", "full history kept · highest cost per turn"),
    ("Don't ask me again", ""),
)
KIMI_CACHE_TITLE = re.compile(r"This session has been idle for .+? and is ~(?:\d+|\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?[kM]) tokens\.")
KIMI_CACHE_BODY = "Cache expired — the next message re-sends the entire history at full price."
KIMI_CACHE_STATUS_FOOTER = re.compile(
    r"^ Ask When Needed  K3\.1 Pro Highspeed 2x Usage Cost thinking: high  [~/].+? {2,}ctrl\+o expand$"
)
KIMI_CACHE_CONTEXT_FOOTER = re.compile(r"^ +context: (?:100|\d{1,2})% \(\d+(?:\.\d+)?[kM]?/\d+(?:\.\d+)?[kM]?\)$")


def kimi_cache_dialog(screen: str, width: int = 0) -> dict[str, Any] | None:
    """The current Cache expired frame; complete says its text is not truncated."""
    lines = screen.rstrip("\n").splitlines()
    last = max((i for i, line in enumerate(lines) if line.strip()), default=-1)
    rules = [i for i, line in enumerate(lines) if KIMI_CACHE_RULE.fullmatch(line)]
    if len(rules) < 2:
        return None
    top, bottom = rules[-2], rules[-1]
    if bottom != last:
        trailing = [line for line in lines[bottom + 1 : last + 1] if line.strip()]
        if (
            len(trailing) != 2
            or not KIMI_CACHE_STATUS_FOOTER.fullmatch(trailing[0])
            or not KIMI_CACHE_CONTEXT_FOOTER.fullmatch(trailing[1])
        ):
            return None
    if len(lines[top]) != len(lines[bottom]) or (width and len(lines[top]) != width - 1):
        return None
    title, body = "", ""
    options: list[dict[str, Any]] = []
    selected = 0
    footer = False
    complete = True
    for line in lines[top + 1 : bottom]:
        text = line.strip()
        if not text:
            continue
        if not title:
            title = text
            continue
        if not footer:
            if text != KIMI_CACHE_FOOTER:
                return None
            footer = True
            continue
        if not body:
            body = text
            continue
        match = KIMI_CACHE_OPTION.fullmatch(line)
        if not match or len(options) >= len(KIMI_CACHE_OPTIONS):
            return None
        label, description = KIMI_CACHE_OPTIONS[len(options)]
        content = match.group(2)
        if not content.startswith(label):
            return None
        detail = content[len(label) :].strip()
        prefix = detail[:-3] if detail.endswith("...") else detail
        if not description.startswith(prefix):
            return None
        complete = complete and detail == description
        options.append({"n": len(options) + 1, "lines": [label, *([detail] if detail else [])]})
        if match.group(1):
            if selected:
                return None
            selected = len(options)
    if not title.startswith("This session has been idle for ") or " and is ~" not in title:
        return None
    prefix = body[:-3] if body.endswith("...") else body
    if not body.startswith("Cache expired —") or not KIMI_CACHE_BODY.startswith(prefix):
        return None
    complete = complete and bool(KIMI_CACHE_TITLE.fullmatch(title)) and body == KIMI_CACHE_BODY
    if len(options) != len(KIMI_CACHE_OPTIONS) or not selected:
        return None
    return {"lines": [title, body], "options": options, "selected": selected, "complete": complete}


def screen_state(p: dict[str, Any], pane: str, shot: Shot | None = None) -> tuple[str, str]:
    """What the participant's screen says, from its bottom lines or a complete current Kimi Cache modal.

    needs_owner — the screen asks the person for something (approve a command, pick an option);
    working — `busy` matches (an agent's prompt can stay visible while it works: Codex shows `›`
    and `Working (…)` at the same time, and typing then would queue or steer the turn);
    idle — `idle` matches, safe to type; unknown — none matched; lost — the pane is gone.
    Ordinary states use only the bottom: quoted text higher up must not change the state. A Kimi Cache
    modal is read whole only as the last visible frame, never when a current editor follows it.
    """
    hints = bool(p.get("dim_is_hint"))
    shown = shot or m_tmuxio.tmux("capture-pane", "-p", *(["-e"] if hints else []), "-t", pane, check=False)
    if shown.returncode != 0:
        return "lost", "Agent window not found."
    if agent_kind(p) == "kimi" and not getattr(shown, "scrolled", False) and kimi_cache_dialog(shown.stdout):
        return "needs_owner", "Cache expired"
    tail = screen_tail_text(p, shown.stdout)
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
    if p.get("starting") and re.search(p["starting"], tail, re.MULTILINE):
        return "joining", ""  # not ready yet: nothing is typed into it
    if p.get("busy") and re.search(p["busy"], tail, re.MULTILINE):
        return "working", ""
    if p.get("idle") and re.search(p["idle"], tail, re.MULTILINE):
        return "idle", ""
    # Someone typed into the agent's own input and did not send it: delivery waits so as not to
    # mix texts. Say so — "unknown" left the person guessing why the agent fell silent.
    if p.get("draft") and re.search(p["draft"], tail, re.MULTILINE):
        return "draft", ""
    # Or its input box grew taller than the bottom lines (a long text in a narrow window): its first row is higher.
    whole = screen_tail_text(p, shown.stdout, whole=True).split("\n")
    at = input_line(p, whole) if p.get("draft") and p.get("input") else -1
    if at >= 0 and re.match(p["draft"], whole[at]):
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
    lines = [line.rstrip() for line in (shown.stdout or "").rstrip("\n").splitlines()]
    tail = "\n".join(lines[-args_tail() :])
    prompt = rules["paste_fold"].lstrip("^")[:1]  # «›» Codex, «❯» Claude: the input line's first sign
    at = input_line({**rules, "input": re.escape(prompt)}, lines)
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


def type_text(rules: dict[str, Any], pane: str, text: str) -> subprocess.CompletedProcess[str]:
    """The text into the agent's input, no Enter: as keys, or for an agent that loses the start of a long typed
    text, as one bracketed paste from a buffer of its own (removed after it)."""
    if not rules.get("bracketed_paste"):
        return m_tmuxio.tmux("send-keys", "-t", pane, "-l", text, check=False)
    buffer = f"collab-{secrets.token_hex(8)}"
    loaded = m_tmuxio.tmux("set-buffer", "-b", buffer, "--", text, check=False)
    if loaded.returncode != 0:
        # Not answered in time, the server may still have set it: the message's text is not left there either.
        m_tmuxio.tmux("delete-buffer", "-b", buffer, check=False)
        return loaded
    pasted = m_tmuxio.tmux("paste-buffer", "-p", "-d", "-b", buffer, "-t", pane, check=False)
    if pasted.returncode != 0:
        # -d removes it only with a paste: the message's text is not left on the server.
        m_tmuxio.tmux("delete-buffer", "-b", buffer, check=False)
    return pasted


def still_starting(rules: dict[str, Any], pane: str) -> bool:
    """Its screen says it is starting — one look, no wait: the deliverer looks again next tick, not holding the
    room's lock meanwhile (say and ack waited for it up to 20 s)."""
    if not rules.get("starting"):
        return False
    shown = m_tmuxio.tmux("capture-pane", "-p", "-t", pane, check=False)
    return shown.returncode == 0 and bool(re.search(rules["starting"], screen_tail_text(rules, shown.stdout), re.MULTILINE))


def is_rule(line: str) -> bool:
    """A line of «─», as Claude draws above and below its input."""
    return line.lstrip().startswith("─")


def input_line(rules: dict[str, Any], lines: list[str]) -> int:
    """The last input line of a screen's lines, or -1: in the bottom lines, or the first row of an input box between
    its rules that ends there — a long text in a narrow window wraps to more rows than the bottom lines hold."""
    at = max((i for i, line in enumerate(lines) if re.match(rules["input"], line)), default=-1)
    bottom = len(lines) - args_tail()
    if at < 0 or at >= bottom:
        return at
    below = next((i for i in range(at + 1, len(lines)) if is_rule(lines[i])), -1)
    return at if at > 0 and is_rule(lines[at - 1]) and below >= bottom else -1


def input_starts(rules: dict[str, Any], shot: Shot, text: str) -> bool:
    """The last input line is the text's start — the whole of what it shows, not only its first signs (the
    room's head, the same in every message) — or is its fold and nothing else: not its middle (what came in lost
    its start, and an Enter would send the rest as the whole message), and not a screen with no input line to
    judge by. Or the box is as tall as it grows and shows the text's end (box_shows_end): a text that lost its
    start came in short, a box with its tail only."""
    lines = (screen_tail_text(rules, shot.stdout, whole=True) if rules.get("boxed_input") else shot.plain.rstrip("\n")).split(
        "\n"
    )
    at = input_line(rules, lines)
    if at < 0:
        return False
    fold = re.match(rules["paste_fold"], lines[at]) if rules.get("paste_fold") else None
    if fold:
        return not lines[at][fold.end() :].strip()
    whole = "".join(text.split())
    shown = "".join(re.sub(rules["input"], "", lines[at], count=1).split()).rstrip("…")
    return bool(shown and whole.startswith(shown)) or box_shows_end(rules, shot, lines, at, text)


def box_shows_end(rules: dict[str, Any], shot: Shot, lines: list[str], at: int, text: str) -> bool:
    """The input box at line `at` is as tall as it grows and shows the text's end, not all of it: scrolled to it,
    its first row marked as the input line — Claude Code in its full screen keeps its input to max(3, rows // 2 - 5)
    rows (2.1.311). What is hidden above its first row is not seen."""
    whole = "".join(text.split())
    below = next((i for i in range(at + 1, len(lines)) if is_rule(lines[i])), -1)
    if below < 0 or not is_rule(lines[at - 1] if at else "") or below - at < max(3, shot.stdout.count("\n") // 2 - 5):
        return False
    box = "".join("".join([re.sub(rules["input"], "", lines[at], count=1), *lines[at + 1 : below]]).split())
    return bool(box) and len(box) < len(whole) and whole.endswith(box)


def footer_lines(p: dict[str, Any], text: str) -> list[str]:
    """The footer after a complete current editor and before its required context row."""
    lines = SGR.sub("", text).splitlines()
    at = max((i for i, line in enumerate(lines) if re.match(r" ?│ > ", line)), default=-1)
    top = at - 1
    bottom = next((i for i in range(at + 1, len(lines)) if re.fullmatch(r" ?╰─+╯ *", lines[i])), -1)
    body = lines[at:bottom] if bottom > at else []
    if (
        at > 0
        and bottom > at
        and re.fullmatch(r" ?[╭├]─+[╮┤] *", lines[top])
        and len(lines[top].rstrip()) == len(lines[bottom].rstrip())
        and all(re.fullmatch(r" ?│ .*│ *", line) for line in body)
    ):
        below = lines[bottom + 1 :]
        end = next((i for i, line in enumerate(below) if re.match(r"^\s*context:", line)), -1)
        return below[:end] if end >= 0 else []
    return []


def screen_count(pane: str, pattern: str, shot: Shot | None = None, rules: dict[str, Any] | None = None) -> int:
    """The number the current footer gives in the pattern's group («· 2 shells» → 2); 0 — none."""
    returncode, text = (shot.returncode, shot.plain) if shot else plain_capture(pane)
    lines = (
        footer_lines(rules, text)
        if rules and rules.get("boxed_input")
        else [line.rstrip() for line in text.rstrip("\n").splitlines()[-args_tail() :]]
    )
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
    area = "\n".join(footer_lines(p, text)) if p.get("boxed_input") else text
    hits = list(re.finditer(p["model"], area, re.MULTILINE)) if returncode == 0 else []
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
        if (k in ("until", "over", "at") and number(v))
        or (k in ("line", "quota_code", "quota_turn", "pane_pid") and isinstance(v, str))
        or (k == "recovery" and isinstance(v, dict))
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


KIMI_QUOTA_CODE = "kimi-5h-quota"
KIMI_QUOTA_LINE = "Kimi 5-hour usage limit exhausted; reset time unknown"
KIMI_QUOTA_ERROR = re.compile(
    r"^   Error: \[provider\.api_error\] 429 You[’']ve reached your 5-hour usage limit\."
    r" Your quota will reset when the current 5-hour window ends\."
    r"(?: To continue now, purchase extra usage or upgrade your plan:)?$"
)


def kimi_quota_screen(shot: Shot | None) -> tuple[str, str, set[str]]:
    """Current visible native turn only. No history read; uncertain frames/quotes give no evidence.

    The hashes identify visible user-turn boundaries without persisting their text. A reply is
    recovery evidence only when the saved error's turn is visible before this later completed turn.
    """
    if not shot or shot.returncode or shot.scrolled or not footer_lines(PRESETS["kimi"], shot.stdout):
        return "", "", set()
    lines = shot.plain.splitlines()
    editor = max((i for i, line in enumerate(lines) if re.match(r" ?│ > ", line)), default=-1) - 1
    turns = [i for i, line in enumerate(lines[:editor]) if line.startswith(" ❯ ")]
    if not turns:
        return "", "", set()
    errors: set[str] = set()
    latest, latest_key = "", ""
    for n, start in enumerate(turns):
        end = turns[n + 1] if n + 1 < len(turns) else editor
        body = [line.rstrip() for line in lines[start + 1 : end] if line.strip()]
        key = hashlib.sha256(lines[start].encode("utf-8")).hexdigest()
        # A completed response has a separate blank-delimited native marker. The failure path
        # instead follows wrapped user-input rows directly (there need not be a blank separator).
        separated = start + 1 < end and not lines[start + 1].strip()
        # User prompts wrap onto indented rows before the native error. Any native response,
        # tool event or quotation/code marker makes attribution ambiguous, so do not scan past it.
        quota = bool(any(KIMI_QUOTA_ERROR.fullmatch(line) for line in body))
        quota = quota and not any(re.match(r"^ ?[●✗]|^\s*[>│]|^\s*```", line) for line in body)
        if not quota:
            # A provider failure can terminate a turn after completed tool events. Require
            # the native terminal help block, not an error quoted in an assistant reply.
            markers = [i for i, line in enumerate(body) if re.match(r"^ [●✗] ", line)]
            failures = [i for i, line in enumerate(body) if KIMI_QUOTA_ERROR.fullmatch(line)]
            if markers and failures and markers[-1] < failures[-1]:
                # Completed generic headers carry the tool's name, not a fixed list of tools.
                # Todo-list reads and shell commands have their own completed wording.
                tool = bool(
                    re.match(
                        r"^ [●✗] (?:(?:Used [A-Za-z][A-Za-z0-9_]*|Read TodoList)(?:$| \(| ·)|Ran a command(?:$| ·))",
                        body[markers[-1]],
                    )
                )
                tail = body[failures[-1] + 1 :]
                unquoted = not any(re.match(r"^\s*(?:>|│|```)", line) for line in body[markers[-1] + 1 : failures[-1]])
                quota = (
                    tool
                    and unquoted
                    and tail
                    == [
                        " https://www.kimi.com/settings/subscription?tab=quota",
                        "   If this persists, run `/export-debug-zip` and share the file with us for diagnosis. Please don't share it publicly.",
                    ]
                )
        if quota:
            errors.add(key)
        if n + 1 == len(turns):
            latest_key = key
            if quota:
                latest = "quota"
            elif (
                separated
                and body
                and re.match(r"^ ● (?!Ran a command|Bash\b|bash task\b|Task\b)\S", body[0])
                and not any("Error: [provider.api_error]" in line or "```" in line for line in body)
            ):
                latest = "reply"
    return latest, latest_key, errors


def usage_limit(pane: str, kind: str = "", shot: Shot | None = None) -> str:
    """The agent's own «out of limit» line, if it is among the bottom lines of its screen; with its
    kind known, only that kind's notice counts."""
    if kind == "kimi":
        return KIMI_QUOTA_LINE if kimi_quota_screen(shot)[0] == "quota" else ""
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
    if entry is None or entry.get("claude_session") == sid or agent_kind(entry) in ("codex", "kimi"):
        return
    entry["claude_session"] = sid
    m_store.write_config(data)


def pretty_model(raw: str) -> str:
    """«claude-opus-5-5» → «Opus 5.5»; anything unfamiliar is shown as it came."""
    m = re.fullmatch(r"claude-([a-z]+)-(\d+)(?:-(\d+))?(?:-\d{8})?(?:\[.*\])?", raw.strip())
    if not m:
        return raw.strip()
    return f"{m.group(1).capitalize()} {m.group(2)}" + (f".{m.group(3)}" if m.group(3) else "")
