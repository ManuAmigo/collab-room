"""The tokens' ledger: usage.jsonl lines, the derived SQLite base, the report to plan by.
No tmux: synthetic files in a room of the test's own."""

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import TEST_ROOTS, isolated  # noqa: E402
from _package import Loader as _Loader  # noqa: E402
from _seam import seam  # noqa: E402

COLLAB = Path(__file__).resolve().parents[1] / "collab"


def entry(model="Opus 5.5", input=0, output=0, cache_read=0, cache_write=0, to="2026-10-19T10:00:00+00:00", **more):
    return {"model": model, "effort": None, "input": input, "output": output, "cache_read": cache_read,
            "cache_write": cache_write, "from_ts": to, "to_ts": to, **more}  # fmt: skip


class LedgerRoom(unittest.TestCase):
    """A room of the test's own, no tests: what both classes below start from."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=TEST_ROOTS))
        self.addCleanup(shutil.rmtree, self.root, True)
        patcher = mock.patch.dict(os.environ, isolated(self.root))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mod = _Loader(f"collab_ledger_{uuid.uuid4().hex[:6]}", str(COLLAB)).load_module()
        self.mod.use_room(str(self.root))
        self.state = self.mod.room().paths.state_dir
        self.mod.private_dir(self.state)

    def use(self, pid, e, task=None, wakeups=0, label=None):
        line = self.mod.usage_line(pid, label or pid, "claude", e, task, wakeups, False)
        self.mod.append_usage([line])
        return line

    def table(self, by, since=""):
        rows = [r.strip("| ").split(" | ") for r in self.mod.report(by, since).splitlines()[2:]]
        return {r[0]: r for r in rows}


class LedgerTest(LedgerRoom):
    def test_spending_is_put_down_to_the_task_it_went_to_and_none_is_a_number_too(self):
        fix1 = {"id": "R1", "kind": "fix", "lane": "B", "by": "claude-5"}
        fix2 = {"id": "R2", "kind": "fix", "lane": "B", "by": "claude-5"}
        review = {"id": "R3", "kind": "review", "lane": "C", "by": "claude-5"}
        self.use("petya", entry(input=100, output=50, cache_read=300, cache_write=100), fix1, wakeups=2)
        self.use("petya", entry(input=200, output=50), fix2, wakeups=1)
        self.use("misha", entry(input=40, output=10), review)
        self.use("misha", entry(input=7, output=3))  # spent outside any task
        t = self.table("kind")
        self.assertEqual(t["fix"][1:9], ["2", "300", "100", "300", "100", "43%", "3", "250"])  # (300+100+100)/2 tasks
        self.assertEqual(t["review"][1], "1")
        self.assertEqual(t["(no task)"][2:4], ["7", "3"], "spending outside any task is counted, not dropped")
        self.assertEqual(list(t)[0], "fix", "the costliest first")

    def test_the_report_groups_by_agent_model_lane_and_task(self):
        task = {"id": "T3", "kind": "fix", "lane": "B", "by": "claude-5"}
        self.use("petya", entry(model="Opus 5.5", input=10), task, label="Петя")
        self.use("petya", entry(model="Sonnet 5.5", input=5), task, label="Петя")
        self.assertEqual(set(self.table("model")), {"Opus 5.5", "Sonnet 5.5"}, "two models, two prices: never summed together")
        self.assertEqual(set(self.table("agent")), {"Петя"})
        self.assertEqual(set(self.table("lane")), {"B"})
        self.assertEqual(self.table("task")["T3"][2], "15")

    def test_the_report_shows_answers_and_the_mean_and_longest_context_of_one(self):
        # A context is input + cache read + cache write of one answer; a line from before the count (no «answers»)
        # is spending, but no answers: it neither lowers the mean nor counts as one.
        self.use("petya", entry(input=10, cache_read=900, cache_write=90, answers=2, ctx_max=700))
        self.use("petya", entry(input=5, cache_read=2900, cache_write=95, answers=1, ctx_max=3000))
        self.use("petya", entry(input=1000, cache_read=10**6))
        self.use("misha", entry(input=1, cache_read=99, answers=1, ctx_max=100))
        t = self.table("agent")
        self.assertEqual(t["petya"][-3:], ["3", "1333", "3000"])  # (1000 + 3000) / 3
        self.assertEqual(t["misha"][-3:], ["1", "100", "100"])

    def test_an_agent_whose_spending_cannot_be_read_is_named_with_why(self):
        # A table without a Claude looked complete: the report says who has no data and why.
        sid = "11111111-2222-3333-4444-555555555555"
        claude_home = self.root / "_claude"
        os.environ["COLLAB_CLAUDE_HOME"] = str(claude_home)
        os.environ["CODEX_HOME"] = str(self.root / "_codex")
        preset = self.mod.PRESETS
        people = [{"id": "owner", "label": "Маша", "command": ["sh"], "delivery": "pull"}]
        agents = [
            {**preset["claude"], "id": "petya", "label": "Петя"},
            {**preset["claude"], "id": "misha", "label": "Миша", "window_session": sid},
            {**preset["codex"], "id": "olya", "label": "Оля"},
        ]
        self.mod.write_config({"version": 2, "session": "s", "participants": people + agents})
        self.use("olya", entry(input=10), label="Оля")
        out = self.mod.report("agent")
        notes = [line for line in out.splitlines() if "no data" in line]
        self.assertEqual(len(notes), 3, out)
        self.assertTrue(notes[0].startswith("Петя (claude): no data — ") and "session" in notes[0], notes)
        self.assertTrue(notes[1].startswith("Миша (claude): no data — ") and sid in notes[1], notes)
        self.assertTrue(notes[2].startswith("Оля (codex): no data — "), notes)
        self.assertNotIn("Маша", out, "a person spends no tokens")
        log = claude_home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(self.root)) / f"{sid}.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text("", encoding="utf-8")
        self.assertFalse(any("Миша" in line for line in self.mod.report("agent").splitlines() if "no data" in line))
        self.assertNotIn("no data", self.mod.report("agent", fmt="csv"), "the csv stays a table")
        self.assertEqual(len(self.mod.unread_notes()), 2, "the csv's reader gets them apart")

    def test_since_leaves_out_earlier_spending(self):
        self.use("petya", entry(input=10, to="2026-10-18T10:00:00+00:00"))
        self.use("petya", entry(input=20, to="2026-10-19T10:00:00+00:00"))
        self.assertEqual(self.table("agent", "2026-10-19")["petya"][2], "20")

    def test_a_broken_line_is_left_out_and_the_rest_counted(self):
        self.use("petya", entry(input=10))
        with open(self.state / "usage.jsonl", "a", encoding="utf-8") as f:
            f.write('{"pid": "petya", "input": 99\n[1, 2]\n')
        self.use("petya", entry(input=5))
        self.assertEqual(self.table("agent")["petya"][2], "15")

    def test_a_usage_line_holds_numbers_and_labels_only(self):
        line = self.use("petya", entry(input=3, output=4), None, wakeups=1)
        self.assertEqual(
            set(line),
            {"at", "pid", "agent", "agent_kind", "model", "effort", "from", "to", "wakeups", "task", "task_kind", "lane",
             "task_by", "split", "input", "output", "cache_read", "cache_write", "answers", "ctx_max"},
        )  # fmt: skip
        self.assertIsNone(line["task"])
        on_disk = [json.loads(x) for x in (self.state / "usage.jsonl").read_text().splitlines()]
        self.assertEqual(on_disk, [line])

    def test_the_base_is_never_opened_through_a_link_put_in_its_place(self):
        # The SQLite base file — no links, 600. A link planted at its name is replaced, not followed.
        precious = self.root / "precious"
        precious.write_text("keep", encoding="utf-8")
        (self.state / "usage.db").symlink_to(precious)
        self.use("petya", entry(input=1))
        self.mod.report("agent")
        self.assertEqual(precious.read_text(encoding="utf-8"), "keep")
        base = self.state / "usage.db"
        self.assertFalse(base.is_symlink())
        self.assertEqual(base.stat().st_mode & 0o777, 0o600)
        self.assertEqual([p.name for p in self.state.glob(".usage-base-*")], [], "no temporary base left")

    def test_a_state_folder_with_uri_marks_in_its_name_is_still_the_base_asked(self):
        # «file:{base}?mode=ro» unescaped — «#» cut it, mode=ro was lost, SQLite made an empty file outside.
        odd = self.root / "st#a?b%c"
        with mock.patch.dict(os.environ, {"COLLAB_STATE_HOME": str(odd)}):
            mod = _Loader(f"collab_ledger_{uuid.uuid4().hex[:6]}", str(COLLAB)).load_module()
            mod.use_room(str(self.root))
            mod.private_dir(mod.room().paths.state_dir)
            mod.append_usage([mod.usage_line("petya", "petya", "claude", entry(input=12), None, 0, False)])
            before = sorted(p.name for p in self.root.iterdir())
            self.assertIn("| petya | 0 | 12 |", mod.report("agent"))
            self.assertEqual(sorted(p.name for p in self.root.iterdir()), before, "nothing made outside the state folder")

    def test_a_line_with_a_field_that_is_not_a_number_is_left_out(self):
        self.use("petya", entry(input=10))
        with open(self.state / "usage.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"pid": "petya", "agent": "petya", "input": "abc", "to": "2026-10-19T10:00:00+00:00"}) + "\n")
        self.assertEqual(self.table("agent")["petya"][2], "10", "the report stands, the bad line is not in it")

    def raw(self, name, record):
        with open(self.state / name, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def test_a_value_sqlite_cannot_take_never_ends_the_report(self):
        # The state files are anybody's — a list, an object, a number past SQLite's integer ended it all.
        self.use("petya", entry(input=10))
        self.raw("usage.jsonl", {"pid": "x", "agent": ["x"], "input": 5, "to": "2026-10-19T10:00:00+00:00"})
        self.raw("usage.jsonl", {"pid": "y", "agent": "y", "input": 10**30, "to": "2026-10-19T10:00:00+00:00"})
        self.raw("tasks.jsonl", {"at": "2026-10-19T10:00:00+00:00", "event": "take", "pid": "petya", "task": {"x": 1}})
        self.raw("merge-log.jsonl", {"at": "2026-10-19T10:00:00+00:00", "by": "petya", "exit": 10**30, "red": "not a list"})
        t = self.table("agent")
        self.assertEqual(t["petya"][2], "10")
        self.assertNotIn("y", t, "a count past the limit leaves its line out")

    def test_nothing_from_the_files_reaches_the_terminal_as_a_control_code_or_breaks_the_table(self):
        # A label with ESC retitled the window and cleared the screen; «|» broke the table.
        self.use("petya", entry(input=10), label="Evil\x1b]0;pwned\x07\x1b[2J|x")
        out = self.mod.report("agent")
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        row = out.splitlines()[2]
        self.assertEqual(row.count("|"), len(self.mod.HEADER) + 1, row)

    def test_a_csv_cell_never_starts_a_formula(self):
        # «=HYPERLINK(...)» opened in a spreadsheet would run.
        for label in ('=HYPERLINK("http://x")', "+1", "-cmd", "@SUM(A1)"):
            self.use(label, entry(input=1), label=label)
        out = self.mod.report("agent", fmt="csv")
        first = [line.split(",")[0].strip('"') for line in out.splitlines()[1:]]
        self.assertTrue(first and all(c.startswith("'") for c in first), first)

    def test_the_tasks_and_queue_journals_go_into_the_base_too(self):
        with open(self.state / "tasks.jsonl", "w", encoding="utf-8") as f:
            f.write(
                json.dumps({"at": "2026-10-19T10:00:00+00:00", "event": "take", "pid": "petya", "task": "T3", "kind": "fix"})
                + "\n"
            )
        with open(self.state / "merge-log.jsonl", "w", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "at": "2026-10-19T10:05:00+00:00",
                        "by": "petya",
                        "what": "train",
                        "who": ["petya"],
                        "exit": 0,
                        "seconds": 290.5,
                        "red": [],
                    }
                )
                + "\n"
            )
        self.mod.report("agent")
        import sqlite3

        with sqlite3.connect(self.state / "usage.db") as db:
            self.assertEqual(db.execute("SELECT task, event FROM task_events").fetchall(), [("T3", "take")])
            self.assertEqual(db.execute("SELECT by, what, seconds FROM runs").fetchall(), [("petya", "train", 290.5)])

    def test_collab_usage_report_prints_the_table(self):
        self.use("petya", entry(input=10, output=2))
        env = {**os.environ, "COLLAB_ROOT": str(self.root)}
        r = subprocess.run([sys.executable, str(COLLAB), "usage", "report", "--by", "agent", "--format", "csv"],
                           cwd=self.root, env=env, capture_output=True, text=True)  # fmt: skip
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            r.stdout.splitlines()[0],
            "group,tasks,input,output,cache read,cache write,cache share,wakeups,per task,answers,mean context,max context",
        )
        self.assertTrue(r.stdout.splitlines()[1].startswith("petya,0,10,2,"), r.stdout)


PAST, FUTURE = "2000-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00"


class LedgerTickTest(LedgerRoom):
    """Part 2: the deliverer's tick writes usage.jsonl from usage.increment, put down to tasks.jsonl."""

    def spend(self, *entries):
        calls = []

        def fake(p):
            calls.append(p["id"])
            return [dict(e) for e in entries]

        self.enterContext(seam(self.mod, "usage.increment", fake))
        return calls

    def lines(self):
        path = self.state / "usage.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def test_the_task_at_a_moment_is_the_last_take_unless_done_or_left_since(self):
        ev = [
            {"at": "2026-10-19T10:00:00+00:00", "event": "take", "pid": "petya", "task": "A", "kind": "fix"},
            {"at": "2026-10-19T10:05:00+00:00", "event": "take", "pid": "misha", "task": "K", "kind": "review"},
            {"at": "2026-10-19T11:00:00+00:00", "event": "left", "pid": "petya", "task": "A"},
            {"at": "2026-10-19T11:00:00+00:00", "event": "take", "pid": "petya", "task": "B", "kind": "docs"},
            {"at": "2026-10-19T12:00:00+00:00", "event": "done", "pid": "petya", "task": "B"},
        ]
        at = lambda ts: (self.mod.task_at(ev, "petya", ts) or {}).get("id")  # noqa: E731
        planted = [{"at": "2999-01-01T00:00:00+00:00", "event": "take", "pid": "petya", "task": "X"}, *ev]
        self.assertEqual(
            (self.mod.task_at(planted, "petya", "2026-10-19T10:30:00+00:00") or {}).get("id"),
            "A",
            "a line with a time to come, put first, does not leave the rest «no task»",
        )
        self.assertEqual(
            [
                at("2026-10-19T09:00:00+00:00"),
                at("2026-10-19T10:30:00+00:00"),
                at("2026-10-19T11:30:00+00:00"),
                at("2026-10-19T12:30:00+00:00"),
            ],
            [None, "A", "B", None],
        )

    def test_each_stretch_goes_to_the_task_at_its_end_and_a_change_inside_is_marked(self):
        self.mod.task_take("petya", "T3", "fix", "B", "claude-5")
        self.spend(
            {"model": "Opus 5.5", "input": 10, "output": 1, "from_ts": PAST, "to_ts": FUTURE},
            {"model": "Sonnet 5.5", "input": 4, "output": 2, "from_ts": FUTURE, "to_ts": FUTURE},
        )
        self.assertEqual(self.mod.record({"id": "petya", "label": "Петя", "idle": "x"}), 2)
        first, second = self.lines()
        self.assertEqual((first["task"], first["task_kind"], first["lane"], first["task_by"]), ("T3", "fix", "B", "claude-5"))
        self.assertTrue(first["split"], "taken inside the stretch")
        self.assertFalse(second["split"])
        self.assertEqual((first["model"], second["model"]), ("Opus 5.5", "Sonnet 5.5"))

    def test_the_context_kept_is_that_of_the_latest_answer(self):
        # What a /compact or /clear is decided by: the context now, not the longest of the stretch.
        self.spend(
            {"model": "Opus 5.5", "input": 1, "ctx_max": 9000, "ctx_last": 9000, "from_ts": PAST, "to_ts": FUTURE},
            {"model": "Sonnet 5.5", "input": 1, "ctx_max": 4000, "ctx_last": 300, "from_ts": PAST, "to_ts": PAST},
        )
        self.mod.record({"id": "petya", "idle": "x"})
        self.assertEqual(self.mod.context_of("petya"), (9000, FUTURE))
        self.assertEqual(self.mod.context_of("misha"), (0, ""), "not counted yet")

    def test_wakes_go_with_the_first_line_and_wait_when_nothing_was_spent(self):
        self.mod.woke("petya")
        self.mod.woke("petya")
        self.spend()
        self.assertEqual(self.mod.record({"id": "petya", "idle": "x"}), 0)
        self.assertEqual(self.lines(), [], "nothing spent — nothing written")
        self.spend(
            {"model": "Opus 5.5", "input": 1, "from_ts": FUTURE, "to_ts": FUTURE},
            {"model": "Sonnet 5.5", "input": 1, "from_ts": FUTURE, "to_ts": FUTURE},
        )
        self.mod.record({"id": "petya", "idle": "x"})
        self.assertEqual([x["wakeups"] for x in self.lines()], [2, 0], "the two wakes wait for the spending they caused")

    def test_the_tick_looks_at_each_agent_once_a_minute_and_never_at_a_person(self):
        calls = self.spend()
        config = {"participants": [{"id": "owner"}, {"id": "petya", "idle": "x"}, {"id": "misha", "busy": "y"}]}
        self.mod.tick(config)
        self.mod.tick(config)
        self.assertEqual(sorted(calls), ["misha", "petya"])

    def test_a_counting_error_never_stops_the_tick(self):
        def broken(p):
            raise OSError("its log is gone\x1b[2J")

        self.enterContext(seam(self.mod, "usage.increment", broken))
        config = {"participants": [{"id": "petya", "idle": "x"}, {"id": "misha", "idle": "x"}]}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.mod.tick(config)  # no raise
            self.enterContext(seam(self.mod, "ledger.EVERY", 0.0))
            self.mod.tick(config)
        self.assertEqual(err.getvalue().count("usage of petya not counted"), 1, "the same error said once, not every minute")
        self.assertNotIn("\x1b", err.getvalue(), "an error's text through the white list")

    def test_one_typing_into_an_agents_window_is_one_wake_whatever_it_carries(self):
        import time
        from types import SimpleNamespace

        mod = self.mod
        os.environ["COLLAB_ENTER_DELAY"] = "0"
        self.addCleanup(os.environ.pop, "COLLAB_ENTER_DELAY", None)
        msgs = [{"id": f"m{i}", "from": "owner", "to": ["kodi"], "at": "2026-10-18T06:00:00", "text": f"hi {i}"} for i in (1, 2)]
        for name, value in {
            "log.read_log_tick": lambda: msgs,
            "log.read_set": lambda *a: set(),
            "log.write_set": lambda *a: None,
            "log.closed_by": lambda *a: set(),
            "store.write_json": lambda *a: None,
            "store.read_json": lambda *a: {},
        }.items():
            self.enterContext(seam(mod, name, value))
        self.enterContext(seam(time, "sleep", lambda s: None))

        def tmux(*a, **k):
            if a[:1] == ("capture-pane",):
                return SimpleNamespace(returncode=0, stdout="› Ask Codex to do anything\n", stderr="")
            if "#{pane_in_mode}" in a:
                return SimpleNamespace(returncode=0, stdout="0\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="111\tcodex\n", stderr="")

        self.enterContext(seam(mod, "tmuxio.tmux", tmux))
        p = {
            "id": "kodi",
            "kind": "codex",
            "delivery": "tmux",
            "pane_pid": "111",
            **{k: mod.PRESETS["codex"][k] for k in ("idle", "draft")},
        }
        self.assertTrue(mod.deliver_to(p, "%1", "idle", {"participants": [{"id": "owner"}, p]}))
        self.assertEqual(mod.WAKEUPS.get("kodi"), 1, "two messages, one typing: one wake")

    def test_the_deliverers_tick_runs_the_ledger(self):
        seen = []
        self.enterContext(seam(self.mod, "ledger.tick", lambda config: seen.append(config)))
        self.mod.deliver_once({"participants": []}, {})
        self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
