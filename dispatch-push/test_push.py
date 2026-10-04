"""Run with system Python, never Hermes' venv:
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -q gateway-plugin/dispatch-push/test_push.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

_spec = importlib.util.spec_from_file_location("dispatch_push_core_test", Path(__file__).with_name("push.py"))
push = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = push  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(push)

TOKEN = "ab" * 32
SEAL = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="  # bytes 0..31
ACCOUNT = "c0ffee" * 10 + "c0de"
VECTORS = json.loads(Path(__file__).with_name("seal-vectors.json").read_text())


def needs_crypto(test):
    return unittest.skipUnless(push.SEALS, "sealing needs the cryptography package")(test)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {"DISPATCH_PUSH_DATA_DIR": str(self.root)}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.store = push.Store(self.root / "devices.db")

    def device(self, device_id="device-0001", token=TOKEN, kinds=None, environment="sandbox", seal="", previews=True, account=""):
        return push.Device(device_id, token, environment, push.TOPIC, "*", kinds or {}, previews, seal, account)


SECRET = "sk-proj-4f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c"


class PayloadTests(Base):
    def test_approvals_say_who_and_which_command_masked(self):
        item = push.Push("approval", session="s1", request="r1", session_key="k1", token="t", profile="sam", sender="Sam",
                         text=f"export OPENAI_API_KEY={SECRET}\nnpm run deploy", about="Deploy")
        payload = item.payload()
        self.assertEqual(payload["aps"]["alert"], {"title": "Sam", "body": "Wants to run: export OPENAI_API_KEY=\u2022\u2022\u2022\u2022 \u2026"})
        # More lines than the alert shows: no Approve from the lock screen (it opens the chat instead).
        self.assertNotIn("category", payload["aps"])
        self.assertEqual(payload["aps"]["interruption-level"], "active")  # Time Sensitive needs an entitlement
        self.assertEqual(payload["aps"]["relevance-score"], 1.0)
        self.assertEqual(payload["aps"]["thread-id"], "s1")
        self.assertEqual(payload["dispatch"], {"kind": "approval", "session": "s1", "request": "r1", "sessionKey": "k1", "token": "t", "profile": "sam"})
        self.assertNotIn(SECRET, json.dumps(payload))
        self.assertNotIn("npm run deploy", json.dumps(payload))  # only the first line

    def test_approve_only_when_the_whole_command_shows(self):
        one = push.Push("approval", session="s1", request="r1", session_key="k1", token="t", sender="Sam", text="npm run deploy")
        self.assertEqual(one.payload()["aps"]["category"], push.APPROVAL_CATEGORY)
        for text in ("cd /srv/app\nrm -rf ~/backups", "echo " + "a " * 100):
            item = push.Push("approval", session="s1", request="r1", session_key="k1", token="t", sender="Sam", text=text)
            self.assertNotIn("category", item.payload()["aps"], text[:20])

    def test_replies_preview_markdown_as_plain_text(self):
        payload = push.Push("turnDone", session="s1", profile="harry-revenue-operator",
                            text="# Done\n- Fixed the **flaky** test\n```\ncode\n```").payload()
        self.assertEqual(payload["aps"]["alert"], {"title": "Harry Revenue Operator", "body": "Done: Fixed the flaky test. [code]"})
        self.assertEqual(payload["aps"]["thread-id"], "s1")
        self.assertEqual(push.Push("turnDone", profile="default").payload()["aps"]["alert"], {"title": "Hermes", "body": "New reply"})

    def test_show_previews_off_says_only_who(self):
        reply = push.Push("turnDone", session="s1", sender="Sam", text="secret plans")
        self.assertEqual(reply.payload(previews=False)["aps"]["alert"], {"body": "New reply from Sam"})
        approval = push.Push("approval", request="r1", sender="Sam", text="rm -rf ~")
        payload = approval.payload(previews=False)
        self.assertEqual(payload["aps"]["alert"], {"body": "Sam needs your approval"})
        self.assertNotIn("category", payload["aps"])  # no blind Approve from the lock screen
        self.assertNotIn("rm -rf", json.dumps(payload))

    def test_an_unknown_profile_names_nobody(self):
        """Never "Hermes" for a bot nobody could name: the alert says what happened, not who."""
        self.assertEqual(push.Push("turnDone", text="Done.").payload()["aps"]["alert"], {"title": "New reply", "body": "Done."})
        self.assertEqual(push.Push("approval", text="").payload(previews=False)["aps"]["alert"], {"body": "A request needs your approval"})
        self.assertEqual(push.alert_copy.readable_profile("default"), "Hermes")
        self.assertEqual(push.alert_copy.readable_profile(""), "")

    def test_review_secrets_never_leave_in_a_payload(self):
        secrets = ["Summer2024!", "hunter2", "correcthorse", "Sup3rSecret", "p@ss", "AAHfK3x9mZq_2LpQ8vRtYw4NcBd6EeFgHiJ",
                   "dXNlcjpwYXNzd29yZA==", "S3cretPass", "Hunter22", "npm_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
                   "wXyZ0123456789aBcDeFgHiJkLmNoPqRsTuVwXyZ01234", "4f9a8b7c6d5e4f3a2b1c"]
        lines = ["export DB_PASSWORD=Summer2024!", "PGPASSWORD=hunter2", "POSTGRES_PASSWORD=correcthorse", "redis://:Sup3rSecret@h:6379",
                 "https://u:p@ss@host/x", "7123456789:AAHfK3x9mZq_2LpQ8vRtYw4NcBd6EeFgHiJ", "Authorization: Basic dXNlcjpwYXNzd29yZA==",
                 "curl -u admin:S3cretPass", "--password Hunter22", "npm_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
                 "SG.aBcDeFgHiJkLmNoPqRsTuV.wXyZ0123456789aBcDeFgHiJkLmNoPqRsTuVwXyZ01234", "The API key is 4f9a8b7c6d5e4f3a2b1c."]
        for kind in push.KINDS:
            for text in (" ".join(lines), "\n".join(lines), *lines):
                payload = json.dumps(push.Push(kind, sender="Sam", text=text).payload())
                for secret in secrets:
                    self.assertNotIn(secret, payload, (kind, secret))

    def test_turn_push_carries_the_reply(self):
        ok = {"platform": "desktop", "completed": True, "interrupted": False, "session_id": "s"}
        item = push.turn_push(ok, "sam", "Sam", "x" * 10_000)
        self.assertEqual((item.sender, len(item.text)), ("Sam", 4000))

    def test_shared_preview_cases(self):
        """src/notification-preview-cases.json: the app's renderer and native code agree on these too."""
        cases_path = Path(__file__).parents[2] / "src" / "notification-preview-cases.json"
        if not cases_path.exists():
            self.skipTest("run from the Dispatch repo")
        cases = json.loads(cases_path.read_text())
        for item in cases["redactions"]:
            self.assertEqual(push.alert_copy.redact(item["input"]), item["output"], item["name"])
            self.assertEqual(push.alert_copy.redact(item["output"]), item["output"], item["name"] + " (twice)")
        for item in cases["previews"]:
            self.assertEqual(push.alert_copy.preview(item["input"]), item["output"], item["name"])
        for item in cases["commands"]:
            self.assertEqual(push.alert_copy.command_summary(item["input"]), item["output"], item["name"])
        for item in cases["errors"]:
            self.assertEqual(push.alert_copy.error_sentence(item["input"]), item["output"], item["name"])

    def test_turn_done_thread_and_ids(self):
        payload = push.Push("turnDone", session="s1").payload()
        self.assertEqual(payload["aps"]["thread-id"], "s1")
        self.assertEqual(payload["dispatch"], {"kind": "turnDone", "session": "s1"})
        for kind in push.KINDS:
            self.assertNotIn("thread-id", push.Push(kind).payload()["aps"])

    def test_settled_is_a_silent_background_update(self):
        payload = push.Push("approval", request="r1", settled=True).payload()
        self.assertEqual(payload["aps"], {"content-available": 1})
        self.assertTrue(payload["dispatch"]["settled"])

    def test_only_completed_app_turns_alert(self):
        ok = {"platform": "desktop", "completed": True, "interrupted": False, "session_id": "s"}
        self.assertEqual(push.turn_push(ok, "default").kind, "turnDone")
        for bad in ({**ok, "platform": "telegram"}, {**ok, "platform": "cron"}, {**ok, "completed": False},
                    {**ok, "interrupted": True}, {**ok, "session_id": ""}):
            self.assertIsNone(push.turn_push(bad, "default"))


class ApprovalTests(Base):
    request = {"surface": "gateway", "session_key": "k1", "session_id": "s1", "command": "rm -rf build", "pattern_keys": ["rm"]}

    def test_finds_the_queued_request_and_signs_an_action_token(self):
        pending = [{"command": "ls", "pattern_keys": ["ls"], "request_id": "other"},
                   {"command": "rm -rf build", "pattern_keys": ["rm"], "request_id": "r-9"}]
        item = push.approval_push(self.request, pending, self.root, "default")
        self.assertEqual((item.request, item.session, item.session_key), ("r-9", "s1", "k1"))
        self.assertTrue(push.action_token_valid(self.root, "r-9", "k1", item.token))
        self.assertFalse(push.action_token_valid(self.root, "other", "k1", item.token))
        self.assertFalse(push.action_token_valid(self.root, "r-9", "k2", item.token))
        self.assertFalse(push.action_token_valid(self.root, "r-9", "k1", ""))

    def test_skips_smart_coalesced_and_unqueued_requests(self):
        pending = [{"command": "rm -rf build", "pattern_keys": ["rm"], "request_id": "r-9"}]
        self.assertIsNone(push.approval_push({**self.request, "surface": "smart"}, pending, self.root, "d"))
        self.assertIsNone(push.approval_push({**self.request, "coalesced": True}, pending, self.root, "d"))
        self.assertIsNone(push.approval_push(self.request, [], self.root, "d"))

    def test_withdraws_the_alert_it_sent_once(self):
        sent = {push.approval_signature(self.request): push.Push("approval", session="s1", request="r-9", session_key="k1", profile="work")}
        settled = push.settled_push({**self.request, "choice": "once"}, sent)
        self.assertTrue(settled.settled)
        self.assertEqual(settled.profile, "work")
        self.assertEqual(settled.request, "r-9")
        self.assertIsNone(push.settled_push({**self.request, "choice": "once"}, sent))


class StoreTests(Base):
    def test_seal_and_account_round_trip_and_old_stores_migrate(self):
        import sqlite3
        from contextlib import closing
        old = self.root / "old.db"
        with closing(sqlite3.connect(old)) as db, db:
            db.execute("""create table devices (device_id text primary key, token text not null, environment text not null,
                topic text not null, profile text not null, kinds text not null, updated_at real not null,
                previews integer not null default 1)""")
            db.execute("insert into devices values ('device-0009', ?, 'sandbox', ?, '*', '{}', 0, 1)", (TOKEN, push.TOPIC))
        store = push.Store(old)
        self.assertEqual((store.devices()[0].seal, store.devices()[0].account), ("", ""))
        store.upsert(self.device("device-0009", seal=SEAL, account=ACCOUNT))
        self.assertEqual((store.get("device-0009").seal, store.get("device-0009").account), (SEAL, ACCOUNT))

    def test_one_row_per_token_and_removal(self):
        self.store.upsert(self.device("device-0001"))
        self.store.upsert(self.device("device-0002"))  # reinstall: same token, new id
        self.assertEqual([d.device_id for d in self.store.devices()], ["device-0002"])
        self.store.remove_token(TOKEN)
        self.assertEqual(self.store.devices(), [])

    def test_show_previews_is_kept_and_old_stores_migrate(self):
        self.store.upsert(push.Device("device-0003", TOKEN, "sandbox", "com.charlesmcdowell.dispatch", "default", {}, False))
        self.assertFalse(self.store.devices()[0].previews)
        import sqlite3
        legacy = self.root / "legacy.db"
        with sqlite3.connect(legacy) as db:
            db.execute("""create table devices (device_id text primary key, token text not null, environment text not null,
                topic text not null, profile text not null, kinds text not null, updated_at real not null)""")
            db.execute("insert into devices values ('device-0004', ?, 'sandbox', 'com.charlesmcdowell.dispatch', 'default', '{}', 0)", (TOKEN,))
        self.assertTrue(push.Store(legacy).devices()[0].previews)

    def test_kind_preferences(self):
        device = self.device(kinds={"turnDone": False})
        self.assertFalse(device.wants("turnDone"))
        self.assertTrue(device.wants("approval"))


class FakeRun:
    def __init__(self, status="200", body=""):
        self.calls, self.status, self.body = [], status, body

    def __call__(self, args, **kwargs):
        header_file = next(a[1:] for a in args if a.startswith("@/"))
        self.calls.append((args, kwargs, Path(header_file).read_text()))
        return subprocess.CompletedProcess(args, 0, stdout=f"{self.body}\n{self.status}", stderr="")


class DeliveryTests(Base):
    def sender(self, run):
        sender = push.DirectAPNs(str(self.root / "unused.p8"), "KEYID12345", "TEAM123456", runner=run)
        sender._jwt = (time.time(), "test-jwt")
        return sender

    def test_real_jwt_signing(self):
        try:
            import jwt
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec
        except ImportError:
            self.skipTest("real JWT signing requires cryptography and PyJWT")
        key = ec.generate_private_key(ec.SECP256R1())
        path = self.root / "AuthKey_TEST.p8"
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
        sender = push.DirectAPNs(str(path), "KEYID12345", "TEAM123456", runner=FakeRun())
        token = sender._bearer()
        claims = jwt.decode(token, key.public_key(), algorithms=["ES256"])
        self.assertEqual(claims["iss"], "TEAM123456")
        self.assertEqual(jwt.get_unverified_header(token)["kid"], "KEYID12345")
        self.assertEqual(sender._bearer(), token)

    def test_direct_apns_request(self):
        run = FakeRun()
        self.sender(run).send(self.device(environment="production"), push.Push("turnDone", session="s1"))
        args, kwargs, headers = run.calls[0]
        self.assertEqual(args[-1], f"https://api.push.apple.com/3/device/{TOKEN}")
        self.assertIn("--http2", args)
        self.assertIn("apns-topic: com.charlesmcdowell.dispatch", headers)
        self.assertIn("apns-push-type: alert", headers)
        self.assertTrue(headers.startswith("authorization: bearer test-jwt\n"))
        self.assertNotIn("bearer", " ".join(args))  # never on the command line
        self.assertEqual(json.loads(kwargs["input"])["dispatch"]["kind"], "turnDone")

    def test_sandbox_host_and_dead_tokens(self):
        run = FakeRun("410", json.dumps({"reason": "Unregistered"}))
        with self.assertRaises(push.Gone):
            self.sender(run).send(self.device(), push.Push("turnDone", session="s1"))
        self.assertIn("api.sandbox.push.apple.com", run.calls[0][0][-1])

    def test_dispatcher_prunes_dead_devices_and_debounces_turn_bursts(self):
        self.store.upsert(self.device())
        class Dead:
            def send(self, device, item):
                raise push.Gone("Unregistered")
        dispatcher = push.Dispatcher(self.store, sender_factory=lambda: Dead())
        self.assertEqual(dispatcher.deliver(push.Push("turnDone", session="s1")), 0)
        self.assertEqual(self.store.devices(), [])
        dispatcher.thread = type("Alive", (), {"is_alive": lambda self: True})()
        dispatcher.submit(push.Push("turnDone", session="s1"))
        dispatcher.submit(push.Push("turnDone", session="s1"))
        dispatcher.submit(push.Push("turnDone", session="s2"))
        self.assertEqual(dispatcher.queue.qsize(), 2)


class CoreRouteTests(Base):
    body = {"token": TOKEN, "environment": "sandbox", "topic": push.TOPIC,
            "kinds": {"turnDone": False, "jobDone": True, "bogus": True}}

    def test_registration_status_and_delete(self):
        device = push.registration("device-0001", **self.body)
        self.assertEqual(device.profile, "*")
        self.assertEqual(device.kinds, {"turnDone": False})
        self.assertTrue(device.previews)
        self.assertFalse(push.registration("device-0001", **self.body, previews=False).previews)
        self.store.upsert(device)
        self.assertEqual(push.status_body(self.root, None), {
            "ok": True, "version": 2, "delivery": None, "kinds": ["turnDone", "approval"], "seal": push.SEALS, "devices": 1})
        self.assertEqual(push.status_body(self.root, push.Relay("https://relay.example", "fake"))["delivery"], "Relay")
        self.assertEqual(push.remove_device(self.root, device.device_id), {"ok": True, "removed": True})
        self.assertEqual(push.remove_device(self.root, device.device_id), {"ok": True, "removed": False})

    def test_registration_validation(self):
        for values in ({"topic": "com.other.app"}, {"token": "not-hex"}, {"token": TOKEN + "\n"},
                       {"environment": "prod"}, {"profile": ""}, {"profile": "a/b"},
                       {"profile": "a" * 65}, {"profile": "work\n"}):
            with self.subTest(values=values), self.assertRaises(push.RouteError) as raised:
                push.registration("device-0001", **{**self.body, **values})
            self.assertEqual((raised.exception.status_code, str(raised.exception)), (400, "Invalid device registration."))
        for device_id in ("short", "device_0001", "device-0001\n"):
            with self.subTest(device_id=device_id), self.assertRaises(push.RouteError) as raised:
                push.registration(device_id, **self.body)
            self.assertEqual(raised.exception.status_code, 400)
            with self.assertRaises(push.RouteError) as raised:
                push.remove_device(self.root, device_id)
            self.assertEqual((raised.exception.status_code, str(raised.exception)), (400, "Invalid device id."))
        for profile in ("*", "default", "work_1.2-3"):
            self.assertEqual(push.registration("device-0001", **self.body, profile=profile).profile, profile)
        self.assertEqual(push.registration("device-0001", **{**self.body, "token": TOKEN.upper()}).token, TOKEN)
        # A seal key is exactly 32 bytes of standard base64; the account tag is kept only when it is the app's hash.
        for seal in ("not base64!", "AAEC", SEAL[:-2], SEAL + "\n", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gIQ=="):
            with self.subTest(seal=seal), self.assertRaises(push.RouteError) as raised:
                push.registration("device-0001", **self.body, seal=seal)
            self.assertEqual(raised.exception.status_code, 400)
        device = push.registration("device-0001", **self.body, seal=SEAL, account=ACCOUNT.upper())
        self.assertEqual((device.seal, device.account), (SEAL, ACCOUNT))
        for account in ("", None, "not-a-hash", ACCOUNT + "0", "z" * 64):
            self.assertEqual(push.registration("device-0001", **self.body, account=account).account, "")

    def test_relay_mode_takes_only_devices_that_can_open_sealed_alerts(self):
        with self.assertRaises(push.RouteError) as raised:
            push.registration("device-0001", **self.body, sealed_only=True)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(push.registration("device-0001", **self.body, seal=SEAL, sealed_only=True).seal, SEAL)

    def test_approval_validation_and_resolution(self):
        token = push.action_token(self.root, "r-9", "k1")
        resolve = Mock(return_value=1)
        for request_id, key, choice, supplied, code, detail in (
                ("bad!", "k1", "once", token, 400, "Invalid request id."),
                ("r-9", "k1", "once", "wrong", 403, "This notification can't answer that request."),
                ("r-9", "k1", "once", "", 403, "This notification can't answer that request."),
                ("r-9", "k2", "once", token, 403, "This notification can't answer that request."),
                ("r-9", "k1", "always", token, 422, "Invalid approval choice.")):
            with self.subTest(code=code, supplied=supplied), self.assertRaises(push.RouteError) as raised:
                push.answer_approval(self.root, request_id, key, choice, supplied, resolve)
            self.assertEqual((raised.exception.status_code, str(raised.exception)), (code, detail))
            resolve.assert_not_called()
        self.assertEqual(push.answer_approval(self.root, "r-9", "k1", "deny", token, resolve), {"ok": True, "resolved": 1})
        resolve.assert_called_once_with("k1", "deny", request_id="r-9")


class ScopeTests(Base):
    def test_delivery_and_withdrawal_scope(self):
        for index, (topic, profile, kinds) in enumerate(((push.TOPIC, "*", {}), (push.TOPIC, "work", {}),
                (push.TOPIC, "default", {}), ("com.other.app", "*", {}), (push.TOPIC, "*", {"approval": False}))):
            self.store.upsert(push.Device(f"device-{index:04}", f"{index:064x}", "sandbox", topic, profile, kinds))
        sender = Mock()
        dispatcher = push.Dispatcher(self.store, sender_factory=lambda: sender)
        item = push.Push("approval", session="s1", request="r1", profile="work")
        request = {"session_key": "k1"}
        settled = push.settled_push(request, {push.approval_signature(request): item})
        for event in (item, settled):
            sender.reset_mock()
            self.assertEqual(dispatcher.deliver(event), 2)
            self.assertEqual([call.args[0].device_id for call in sender.send.call_args_list], ["device-0000", "device-0001"])


class RelayTests(Base):
    def test_url_rules(self):
        self.assertEqual(push.Relay("https://relay.example/", "fake").url, "https://relay.example")
        for url in ("http://relay.example", "https://user:pw@relay.example", "https://@relay.example",
                    "https:///path", "https://", "ftp://relay.example", "//relay.example", "", "https://relay.example\n"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                push.Relay(url, "fake")

    @needs_crypto
    def test_relayed_alerts_are_sealed_with_the_devices_own_previews(self):
        """The relay sees "Dispatch: New notification" and a box; the phone opens the real alert."""
        item = push.Push("turnDone", session="s1", sender="Sam", text="The launch plan is ready.")
        relay = push.Relay("https://relay.example")
        key = push.seal_key(SEAL)
        for previews, body in ((True, "The launch plan is ready."), (False, "New reply from Sam")):
            sent = relay.body(self.device(seal=SEAL, previews=previews, account=ACCOUNT), item)
            self.assertEqual(sent["payload"]["aps"]["alert"], push.SEALED_ALERT)
            self.assertNotIn("Sam", json.dumps({**sent, "payload": {**sent["payload"], "seal": {}}}))
            self.assertNotIn("s1", sent["collapseId"])
            inner = push.unseal(sent["payload"], key)
            self.assertEqual(inner["aps"]["alert"]["body"], body)
            self.assertEqual(inner["dispatch"]["account"], ACCOUNT)

    @needs_crypto
    def test_relay_skips_devices_without_a_seal_key(self):
        self.store.upsert(self.device("device-0001"))
        self.store.upsert(self.device("device-0002", token="cd" * 32, seal=SEAL))
        relay = push.Relay("https://relay.example")
        with patch.object(push.Relay, "send") as send:
            self.assertEqual(push.Dispatcher(self.store, sender_factory=lambda: relay).deliver(push.Push("turnDone", session="s1")), 1)
        self.assertEqual(send.call_args.args[0].device_id, "device-0002")

    def test_relay_key_is_optional(self):
        sent = []
        fake = type("Httpx", (), {"post": staticmethod(lambda url, **kw: sent.append((url, kw)) or Mock(status_code=200))})
        with patch.dict(sys.modules, {"httpx": fake}), patch.object(push.Push, "for_device", return_value=({"aps": {}}, "c")):
            push.Relay("https://relay.example/").send(self.device(seal=SEAL), push.Push("turnDone", session="s1"))
            push.Relay("https://relay.example", "private").send(self.device(seal=SEAL), push.Push("turnDone", session="s1"))
        self.assertEqual(sent[0][0], "https://relay.example/v1/push")
        self.assertNotIn("authorization", sent[0][1]["headers"])
        self.assertEqual(sent[1][1]["headers"]["authorization"], "Bearer private")

    def test_senders(self):
        """The developer's key goes straight to Apple; everyone else gets the public relay; `off` sends nothing."""
        direct = {"DISPATCH_APNS_KEY_PATH": "k.p8", "DISPATCH_APNS_KEY_ID": "KEYID12345", "DISPATCH_APNS_TEAM_ID": "TEAM123456"}
        with patch.object(push, "SEALS", True):
            for env, kind, url in (({}, push.Relay, push.DEFAULT_RELAY_URL), (direct, push.DirectAPNs, None),
                                   ({**direct, "DISPATCH_PUSH_RELAY_URL": "https://mine.example"}, push.Relay, "https://mine.example")):
                with self.subTest(env=env), patch.dict(os.environ, env):
                    sender = push.sender_from_env()
                    self.assertIsInstance(sender, kind)
                    if url:
                        self.assertEqual(sender.url, url)
            for value in ("off", "OFF", "none", "0"):
                with patch.dict(os.environ, {**direct, "DISPATCH_PUSH_RELAY_URL": value}):
                    self.assertIsNone(push.sender_from_env())
        with patch.object(push, "SEALS", False), self.assertLogs(push.log, level="WARNING"):
            self.assertIsNone(push.sender_from_env())

    def test_invalid_relay_disables_delivery_without_fallback(self):
        for url in ("http://relay.example", "", "https:///path", "https://user:pw@relay.example", "ftp://relay.example"):
            with patch.dict(os.environ, {"DISPATCH_PUSH_RELAY_URL": url, "DISPATCH_PUSH_RELAY_KEY": "fake",
                    "DISPATCH_APNS_KEY_PATH": "unused", "DISPATCH_APNS_KEY_ID": "unused", "DISPATCH_APNS_TEAM_ID": "unused"}), \
                    patch.object(push, "SEALS", True), patch.object(push, "DirectAPNs") as direct, patch.object(push.Relay, "send") as send, \
                    patch.object(push.subprocess, "run") as run, self.assertLogs(push.log, level="WARNING"):
                sender = push.sender_from_env()
                self.assertIsNone(sender)
                self.assertIsNone(push.status_body(self.root, sender)["delivery"])
                direct.assert_not_called()
                send.assert_not_called()
                run.assert_not_called()


@needs_crypto
class SealTests(Base):
    def test_shared_vectors(self):
        """Byte for byte what the app's DispatchSeal opens (scripts/native-platform/main.swift reads the same file)."""
        key = push.seal_key(VECTORS["key"])
        self.assertEqual(push.seal_kid(key), VECTORS["kid"])
        self.assertEqual(VECTORS["aad"].encode(), push.SEAL_AAD)
        for case in VECTORS["cases"]:
            with self.subTest(case["name"]):
                self.assertEqual(push.sealed(case["inner"], key, bytes(range(100, 112))), case["outer"])
                self.assertEqual(push.unseal(case["outer"], key), case["inner"])

    def test_sealed_alerts_hide_everything_but_that_one_arrived(self):
        approval = push.Push("approval", session="s1", request="r1", session_key="k1", token="t" * 64, profile="sam",
                             sender="Sam", text="npm run deploy")
        payload, collapse = approval.for_device(self.device(seal=SEAL, account=ACCOUNT))
        self.assertEqual(set(payload), {"aps", "seal"})
        self.assertEqual(payload["aps"], {"alert": push.SEALED_ALERT, "mutable-content": 1, "sound": "default",
                                          "interruption-level": "active", "relevance-score": 1.0, "category": push.APPROVAL_CATEGORY})
        # Everything around the box (the box itself is ciphertext, where any short string can turn up by chance).
        visible = json.dumps({**payload, "seal": {**payload["seal"], "box": ""}}) + collapse
        for secret in ("Sam", "npm", "s1", "r1", "k1", "t" * 64, ACCOUNT):
            self.assertNotIn(secret, visible)
        self.assertRegex(collapse, "^[0-9a-f]{32}$")
        self.assertEqual(collapse, approval.for_device(self.device(seal=SEAL))[1])  # updates of one alert still collapse
        self.assertNotEqual(collapse, push.Push("approval", request="r2").for_device(self.device(seal=SEAL))[1])
        # Each push gets its own nonce.
        self.assertNotEqual(payload["seal"]["box"], approval.for_device(self.device(seal=SEAL))[0]["seal"]["box"])
        inner = push.unseal(payload, push.seal_key(SEAL))
        self.assertEqual(inner, approval.payload(True, ACCOUNT))
        # A settle stays a silent background push.
        settled = push.Push("approval", session="s1", request="r1", settled=True).for_device(self.device(seal=SEAL))[0]
        self.assertEqual(settled["aps"], {"content-available": 1})
        self.assertTrue(push.unseal(settled, push.seal_key(SEAL))["dispatch"]["settled"])

    def test_a_tampered_box_does_not_open(self):
        from cryptography.exceptions import InvalidTag
        payload, _ = push.Push("turnDone", session="s1", sender="Sam", text="hi").for_device(self.device(seal=SEAL))
        box = bytearray(push.base64.b64decode(payload["seal"]["box"]))
        box[-1] ^= 1
        payload["seal"]["box"] = push.base64.b64encode(bytes(box)).decode()
        with self.assertRaises(InvalidTag):
            push.unseal(payload, push.seal_key(SEAL))

    def test_direct_mode_seals_too(self):
        run = FakeRun()
        sender = push.DirectAPNs("unused.p8", "KEYID12345", "TEAM123456", runner=run)
        sender._jwt = (time.time(), "test-jwt")
        sender.send(self.device(seal=SEAL), push.Push("turnDone", session="s1", sender="Sam", text="hi"))
        args, kwargs, headers = run.calls[0]
        self.assertEqual(json.loads(kwargs["input"])["aps"]["alert"], push.SEALED_ALERT)
        self.assertNotIn("apns-collapse-id: turnDone:s1", headers)


class RouteTests(Base):
    """The dashboard routes, in-process, with the approval queue faked."""

    def setUp(self):
        super().setUp()
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi not installed")
        import types
        self.resolved = []
        fake = types.ModuleType("tools.approval")
        fake.resolve_gateway_approval = lambda key, choice, request_id=None: self.resolved.append((key, choice, request_id)) or 1
        modules = patch.dict(sys.modules, {"dispatch_push_core": push, "tools": types.ModuleType("tools"), "tools.approval": fake})
        modules.start()
        self.addCleanup(modules.stop)
        spec = importlib.util.spec_from_file_location("dispatch_push_api_test", Path(__file__).parent / "dashboard" / "plugin_api.py")
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        app = FastAPI()
        app.include_router(api.router, prefix="/api/plugins/dispatch-push")
        # The developer's direct mode, which takes devices without a seal key (relay mode is tested on its own).
        direct = patch.dict(os.environ, {"DISPATCH_APNS_KEY_PATH": "k.p8", "DISPATCH_APNS_KEY_ID": "KEYID12345",
                                         "DISPATCH_APNS_TEAM_ID": "TEAM123456"})
        direct.start()
        self.addCleanup(direct.stop)
        self.client = TestClient(app)

    def test_device_registration_and_status(self):
        url = "/api/plugins/dispatch-push/devices/0A1B2C3D-0000-4000-8000-000000000000"
        body = {"token": TOKEN, "environment": "sandbox", "topic": "com.charlesmcdowell.dispatch", "kinds": {"turnDone": False, "bogus": True, "jobDone": True}}
        self.assertEqual(self.client.put(url, json=body).status_code, 200)
        status = self.client.get("/api/plugins/dispatch-push/status").json()
        self.assertEqual(status["devices"], 1)
        self.assertEqual(status["kinds"], ["turnDone", "approval"])
        self.assertEqual(self.store.devices()[0].profile, "*")
        for values in ({"topic": "com.other.app"}, {"profile": "a/b"}):
            self.assertEqual(self.client.put(url, json={**body, **values}).status_code, 400)
        self.assertEqual(self.client.delete("/api/plugins/dispatch-push/devices/bad!").status_code, 400)
        self.assertEqual(push.Store(self.root / "devices.db").devices()[0].kinds, {"turnDone": False})
        self.assertTrue(push.Store(self.root / "devices.db").devices()[0].previews)
        self.assertEqual(self.client.put(url, json={**body, "previews": False}).status_code, 200)
        self.assertFalse(push.Store(self.root / "devices.db").devices()[0].previews)
        self.assertEqual(self.client.put(url, json={**body, "token": "not-hex"}).status_code, 400)
        self.assertEqual(self.client.put(url, json={**body, "environment": "prod"}).status_code, 422)
        self.assertEqual(self.client.put(url, json={**body, "seal": SEAL, "account": ACCOUNT}).status_code, 200)
        self.assertEqual((self.store.devices()[0].seal, self.store.devices()[0].account), (SEAL, ACCOUNT))
        self.assertEqual(self.client.put(url, json={**body, "seal": "AAEC"}).status_code, 400)
        self.assertTrue(self.client.delete(url).json()["removed"])

    def test_relay_mode_refuses_devices_without_a_seal_key(self):
        url = "/api/plugins/dispatch-push/devices/0A1B2C3D-0000-4000-8000-000000000000"
        body = {"token": TOKEN, "environment": "sandbox", "topic": "com.charlesmcdowell.dispatch"}
        with patch.object(push, "SEALS", True), patch.dict(os.environ, {"DISPATCH_PUSH_RELAY_URL": "https://relay.example"}):
            self.assertEqual(self.client.get("/api/plugins/dispatch-push/status").json()["delivery"], "Relay")
            self.assertEqual(self.client.put(url, json=body).status_code, 409)
            self.assertEqual(self.client.put(url, json={**body, "seal": SEAL}).status_code, 200)

    def test_approval_needs_the_notifications_token(self):
        token = push.action_token(self.root, "r-9", "k1")
        url = "/api/plugins/dispatch-push/approvals/r-9"
        self.assertEqual(self.client.post(url, json={"session_key": "k1", "choice": "once", "token": "x" * 64}).status_code, 403)
        self.assertEqual(self.client.post(url, json={"session_key": "k2", "choice": "once", "token": token}).status_code, 403)
        self.assertEqual(self.client.post(url, json={"session_key": "k1", "choice": "always", "token": token}).status_code, 422)
        self.assertEqual(self.client.post(url, json={"session_key": "k1", "choice": "once"}).status_code, 403)
        self.assertEqual(self.client.post("/api/plugins/dispatch-push/approvals/bad!",
                         json={"session_key": "k1", "choice": "once", "token": token}).status_code, 400)
        self.assertEqual(self.resolved, [])
        self.assertEqual(self.client.post(url, json={"session_key": "k1", "choice": "deny", "token": token}).json()["resolved"], 1)
        self.assertEqual(self.resolved, [("k1", "deny", "r-9")])


class RegisterTests(unittest.TestCase):
    def test_a_hermes_without_the_reply_hook_still_loads_the_plugin(self):
        """Loads the plugin's __init__ from its file (standard library only; it imports Hermes lazily, never here)."""
        spec = importlib.util.spec_from_file_location("dispatch_push_plugin_test", Path(__file__).with_name("__init__.py"))
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        hooks = []

        def register_hook(name, fn):
            if name == "post_llm_call":
                raise ValueError("unknown hook")
            hooks.append(name)
        with self.assertLogs("dispatch-push", level="WARNING"):
            plugin.register(Mock(register_hook=register_hook))
        self.assertEqual(hooks, ["on_session_end", "pre_approval_request", "post_approval_response"])


if __name__ == "__main__":
    unittest.main()

