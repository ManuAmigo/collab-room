"""Outbound failures in a private synthetic room, without HTTP or bot credentials."""

import contextlib
import io
import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ledger import LedgerRoom  # noqa: E402
from _seam import seam  # noqa: E402


class TelegramOutgoingTest(LedgerRoom):
    def setUp(self):
        super().setUp()
        self.mod.write_config(
            {
                "version": 2,
                "session": "synthetic",
                "lead": "lead",
                "participants": [{"id": "owner"}, {"id": "lead", "kind": "claude", "pane": False}],
            }
        )
        self.link = {
            "server": "https://synthetic.invalid/private-route",
            "room_id": "a" * 32,
            "token": "synthetic-secret-token-123456",
            "state": "on",
            "sent": "0" * 12,
        }
        self.mod.save(self.link)
        self.messages = [
            {"id": "0" * 12, "from": "owner", "to": ["lead"], "text": "earlier", "via": "telegram"},
            {"id": "1" * 12, "from": "owner", "to": ["lead"], "text": "x" * 49572},
            {"id": "2" * 12, "from": "lead", "to": ["owner"], "text": "later reply"},
        ]
        self.enterContext(seam(self.mod, "log.read_messages", lambda: self.messages))
        self.enterContext(seam(self.mod, "log.unsigned_person", lambda *a: False))
        self.enterContext(seam(self.mod, "telegram.notices", lambda state, config: state))
        self.calls = []

    def refused(self, server, method, path, body=None, token=""):
        self.calls.append((body["room_msg"], len(body["text"])))
        raise self.mod.Refused(429, "synthetic-secret-body " + self.link["token"])

    def accepted(self, server, method, path, body=None, token=""):
        self.calls.append((body["room_msg"], len(body["text"])))
        return {}

    def one_background_attempt(self):
        captured = []

        class Stop:
            stopped = False

            def is_set(self):
                return self.stopped

            def set(self):
                self.stopped = True

            def wait(self, seconds):
                self.stopped = True

        stop = Stop()

        class Thread:
            def __init__(self, target, **kwargs):
                captured.append(target)

            def start(self):
                pass

        with (
            seam(self.mod, "telegram.threading.Thread", Thread),
            seam(self.mod, "telegram.threading.Event", lambda: stop),
            seam(self.mod, "telegram.load", lambda: {"state": "off"}),
        ):
            self.mod.run()
        stop.stopped = False
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            captured[0]()
        return errors.getvalue()

    def test_large_own_head_is_bounded_and_later_messages_are_delivered(self):
        texts = []

        def limited(server, method, path, body=None, token=""):
            self.calls.append((body["room_msg"], len(body["text"])))
            texts.append(body["text"])
            if len(body["text"].encode("utf-16-le")) // 2 > 4000:
                raise self.mod.Refused(400, "message too long")
            return {}

        with seam(self.mod, "telegram.call", limited):
            self.mod.send_out(self.mod.load())
        self.assertEqual([mid for mid, _ in self.calls], ["1" * 12, "2" * 12])
        self.assertLessEqual(self.calls[0][1], 4000)
        self.assertIn("collab show " + "1" * 12, texts[0])
        self.assertEqual(self.mod.load()["sent"], "2" * 12)
        self.assertEqual(len(self.messages[1]["text"]), 49572)

    def test_permanent_refusal_is_logged_without_secrets_and_later_messages_continue(self):
        def rejected(server, method, path, body=None, token=""):
            if body["room_msg"] == "1" * 12:
                raise self.mod.Refused(400, "synthetic-secret-body " + self.link["token"])
            return self.accepted(server, method, path, body, token)

        with seam(self.mod, "telegram.call", rejected):
            self.mod.send_out(self.mod.load())
        self.assertEqual(self.calls, [("2" * 12, 11)])
        self.assertEqual(self.mod.load()["sent"], "2" * 12)
        records = self.mod.room().paths.messages_path.read_text()
        self.assertIn("not sent to Telegram", records)
        self.assertIn("1" * 12, records)
        self.assertIn("400", records)
        self.assertNotIn(self.link["token"], records)
        self.assertNotIn("synthetic-secret-body", records)

    def test_off_then_on_skips_three_messages_and_only_sends_the_new_one(self):
        self.messages = self.messages[:1]
        self.mod.disable()
        for n in range(1, 4):
            self.messages.append({"id": str(n) * 12, "from": "lead", "to": ["owner"], "text": "while off"})
        with seam(self.mod, "telegram.call", self.accepted):
            self.mod.send_out(self.link)  # A stale on snapshot cannot bypass the menu.
        self.assertEqual(self.calls, [])

        def enabled(server, method, path, body=None, token=""):
            return {"code": "ABCDEFGH", "expires": 1234567890} if path.endswith("/code") else {}

        with seam(self.mod, "telegram.call", enabled):
            self.mod.enable()
        with seam(self.mod, "telegram.call", self.accepted):
            self.mod.send_out(self.mod.load())
        self.assertEqual(self.calls, [])
        self.messages.append({"id": "4" * 12, "from": "lead", "to": ["owner"], "text": "new"})
        with seam(self.mod, "telegram.call", self.accepted):
            self.mod.send_out(self.mod.load())
        self.assertEqual(self.calls, [("4" * 12, 3)])
        records = [json.loads(line) for line in self.mod.room().paths.messages_path.read_text().splitlines()]
        skipped = [r for r in records if "Telegram was off" in r["text"]]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["said"]["p"]["n"], "3")

    def test_preview_budget_handles_unicode_and_keeps_short_messages_whole(self):
        for text in ("x" * 4000, "🙂" * 2000, "🙂" * 2001, "🙂" * 20000, "\ud800" * 5000):
            with self.subTest(length=len(text)):
                self.mod.save(self.link)
                self.messages[1]["text"] = text
                bodies = []
                with seam(self.mod, "telegram.call", lambda *args, bodies=bodies: bodies.append(args[3]) or {}):
                    self.mod.send_out(self.mod.load())
                shown = bodies[0]["text"]
                self.assertLessEqual(len(shown.encode("utf-16-le")) // 2, 4000)
                if len(text.encode("utf-16-le", errors="replace")) // 2 <= 4000:
                    self.assertEqual(shown, text)
                else:
                    self.assertIn("collab show " + "1" * 12, shown)
                self.assertEqual(self.messages[1]["text"], text)

    def test_transient_failures_keep_the_cursor_and_later_messages_wait(self):
        for failure in (self.mod.Refused(429, "retry"), self.mod.Refused(500, "retry"), OSError("offline")):
            with self.subTest(failure=type(failure).__name__):
                self.mod.save(self.link)
                seen = []

                def fail(server, method, path, body=None, token="", seen=seen, failure=failure):
                    seen.append(body["room_msg"])
                    raise failure

                with seam(self.mod, "telegram.call", fail), self.assertRaises(type(failure)):
                    self.mod.send_out(self.mod.load())
                self.assertEqual(seen, ["1" * 12])
                self.assertEqual(self.mod.load()["sent"], "0" * 12)

    def test_a_failed_local_warning_cannot_silently_advance_the_cursor(self):
        def fail(*args):
            raise self.mod.Refused(422, "private response")

        def cannot_record(*args, **kwargs):
            raise OSError("local log unavailable")

        with (
            seam(self.mod, "telegram.call", fail),
            seam(self.mod, "log.room_line", cannot_record),
            self.assertRaises(OSError),
        ):
            self.mod.send_out(self.mod.load())
        self.assertEqual(self.mod.load()["sent"], "0" * 12)

    def test_off_during_upload_prevents_the_message_request(self):
        self.messages[1].update({"from": "lead", "to": ["owner"], "files": [{"name": "example"}]})

        def upload(state, file):
            self.mod.disable()
            return "a" * 16

        with seam(self.mod, "telegram.upload", upload), seam(self.mod, "telegram.call", self.accepted):
            self.mod.send_out(self.mod.load())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.mod.load()["state"], "off")
        self.assertEqual(self.mod.load()["sent"], "0" * 12)

    def test_reenable_during_a_request_cannot_roll_back_the_new_cursor(self):
        seen = []

        def changed(server, method, path, body=None, token=""):
            if path.endswith("/code"):
                return {"code": "ABCDEFGH", "expires": 1234567890}
            if path.endswith("/messages"):
                seen.append(body["room_msg"])
                self.mod.disable()
                self.messages.append({"id": "3" * 12, "from": "lead", "to": ["owner"], "text": "while off"})
                self.mod.enable()
            return {}

        with seam(self.mod, "telegram.call", changed):
            self.mod.send_out(self.mod.load())
        self.assertEqual(seen, ["1" * 12])
        self.assertEqual(self.mod.load()["sent"], "3" * 12)
        with seam(self.mod, "telegram.call", self.accepted):
            self.mod.send_out(self.mod.load())
        self.assertEqual(self.calls, [])

    def test_off_during_poll_does_not_accept_or_acknowledge_its_messages(self):
        requests = []

        def polled(server, method, path, body=None, token=""):
            requests.append((method, path, body))
            if method == "GET":
                self.mod.disable()
                return {"messages": [{"id": "incoming", "to": "lead", "text": "late"}], "after": 1}
            return {}

        with seam(self.mod, "telegram.call", polled):
            self.mod.take_in(self.mod.load(), 0)
        self.assertEqual([method for method, _, _ in requests], ["GET"])
        path = self.mod.room().paths.messages_path
        self.assertFalse(path.exists(), "no signed incoming message while off")
        self.assertEqual(self.mod.load().get("after", 0), 0)

    def test_recipient_removed_during_poll_is_refused_using_fresh_config(self):
        acknowledgements = []

        def polled(server, method, path, body=None, token=""):
            if method == "GET":
                config = self.mod.load_config()
                config["participants"] = [{"id": "owner"}]
                self.mod.write_config(config)
                return {"messages": [{"id": "incoming", "to": "lead", "text": "late"}], "after": 1}
            if path.endswith("/ack"):
                acknowledgements.append(body)
            return {}

        with seam(self.mod, "telegram.call", polled):
            self.mod.take_in(self.mod.load(), 0)
        self.assertEqual(acknowledgements, [{"messages": [], "refused": ["incoming"]}])
        self.assertFalse(self.mod.room().paths.messages_path.exists())

    def test_off_on_during_poll_rejects_the_previous_link_generation(self):
        requests = []

        def polled(server, method, path, body=None, token=""):
            requests.append(path)
            if method == "GET":
                self.mod.disable()
                self.mod.enable()
                return {"messages": [{"id": "incoming", "to": "lead", "text": "late"}], "after": 1}
            return {"code": "ABCDEFGH", "expires": 1234567890} if path.endswith("/code") else {}

        with seam(self.mod, "telegram.call", polled):
            self.mod.take_in(self.mod.load(), 0)
        self.assertFalse(any(path.endswith("/ack") for path in requests))
        self.assertEqual(self.mod.load().get("after", 0), 0)
        records = self.mod.room().paths.messages_path.read_text()
        self.assertNotIn('"tg": "incoming"', records)

    def test_reenable_during_session_lookup_does_not_restart_an_old_action(self):
        config = self.mod.load_config()
        config["participants"][1].update(started_by_room=True, target="%1")
        self.mod.write_config(config)
        restarted = []
        confirmed = []

        def requests(server, method, path, body=None, token=""):
            if method == "GET":
                return {"actions": [{"id": "restart-old", "kind": "restart", "to": "lead"}], "after": 1}
            if path.endswith("/code"):
                return {"code": "ABCDEFGH", "expires": 1234567890}
            if path.endswith("/actions"):
                confirmed.append(body)
            return {}

        def lookup(participant):
            self.mod.disable()
            self.mod.enable()
            return "session-1"

        with (
            seam(self.mod, "telegram.call", requests),
            seam(self.mod, "screen.agent_session", lookup),
            seam(self.mod, "agents.restart_in_window", lambda *args: restarted.append(args)),
        ):
            self.mod.take_in(self.mod.load(), 0)
        self.assertEqual(restarted, [])
        self.assertEqual(confirmed, [])
        self.assertEqual(self.mod.load().get("after", 0), 0)

    def test_locked_actions_reject_old_generation_and_accept_the_current_one(self):
        previous = self.mod.load()
        self.mod.disable()
        with seam(self.mod, "telegram.call", lambda *args: {"code": "ABCDEFGH", "expires": 1234567890}):
            self.mod.enable()
        self.assertEqual(self.mod.act_locked("pause", "", "", link=previous), "off")
        self.assertFalse(self.mod.load_config().get("paused"))
        self.assertEqual(self.mod.answer("refresh", "lead", 0, "", link=previous), ("off", None, None))
        self.assertEqual(self.mod.prepare_video({"prompt": "old"}, link=previous), "off")
        self.assertEqual(self.mod.act_locked("pause", "", "", link=self.mod.load()), "done")
        self.assertTrue(self.mod.load_config().get("paused"))

    def test_suppressed_failure_has_safe_bounded_private_status_and_recovers_once(self):
        with seam(self.mod, "telegram.call", self.refused):
            self.assertEqual(self.one_background_attempt(), "")
        self.assertEqual([mid for mid, _ in self.calls], ["1" * 12])
        self.assertEqual(self.mod.load()["sent"], "0" * 12)
        path = self.state / "telegram-outgoing.json"
        self.assertTrue(path.is_file(), "the background exception must have a visible status")
        record = json.loads(path.read_text())
        self.assertEqual(
            (record["phase"], record["head"], record["error_class"], record["http_status"]), ("send", "1" * 12, "Refused", 429)
        )
        self.assertEqual(record["retry_in"], 2)
        self.assertEqual(record["state"], "blocked")
        self.assertTrue(record["at"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        for _ in range(12):
            with seam(self.mod, "telegram.call", self.refused), self.assertRaises(self.mod.Refused):
                self.mod.send_out(self.mod.load())
        self.assertLess(path.stat().st_size, 1024)
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            self.mod.cmd_telegram(SimpleNamespace(action="status"))
        self.assertIn("Outgoing blocked", shown.getvalue())
        private_text = path.read_text()
        for secret in (self.link["token"], self.link["server"], "synthetic-secret-body", "x" * 100):
            self.assertNotIn(secret, private_text)
            if secret != self.link["server"]:  # the existing status already shows the configured server
                self.assertNotIn(secret, shown.getvalue())
        self.calls.clear()
        with seam(self.mod, "telegram.call", self.accepted):
            self.mod.send_out(self.mod.load())
        recovered = path.read_bytes()
        record = json.loads(recovered)
        self.assertEqual(record["state"], "ready")
        self.assertEqual(record["error_class"], "")
        self.assertTrue(record["last_success"])
        self.assertEqual(self.mod.load()["sent"], "2" * 12)
        with seam(self.mod, "telegram.call", self.accepted):
            self.mod.send_out(self.mod.load())
        self.assertEqual(path.read_bytes(), recovered, "an idle healthy tick does not repeat recovery")
