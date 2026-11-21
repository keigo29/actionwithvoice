import json
import time
import unittest
import urllib.error
import urllib.request

from ray import app as app_mod
from ray.app import Assistant
from ray.ui import StateHub, check_notify, serve
from tests.test_ray import FakeBrain, FakeSpeaker, cfg

TOKEN = "t" * 24
JSON = {"Content-Type": "application/json"}


class CheckTest(unittest.TestCase):
    def body(self, d):
        return json.dumps(d).encode()

    def test_ok_with_either_header(self):
        for h in ({"X-Ray-Token": TOKEN}, {"Authorization": f"Bearer {TOKEN}"}):
            code, d = check_notify({**JSON, **h}, self.body({"text": " 雨です ", "title": "天気"}), TOKEN)
            self.assertEqual((code, d), (202, {"text": "雨です", "title": "天気"}))

    def test_refused(self):
        good = {**JSON, "X-Ray-Token": TOKEN}
        self.assertEqual(check_notify(good, self.body({"text": "a"}), "")[0], 403)  # 合言葉を決めていない
        self.assertEqual(check_notify({**JSON, "X-Ray-Token": "x"}, self.body({"text": "a"}), TOKEN)[0], 401)
        self.assertEqual(check_notify({"X-Ray-Token": TOKEN, "Content-Type": "text/plain"},
                                      self.body({"text": "a"}), TOKEN)[0], 415)  # ブラウザのフォームから
        self.assertEqual(check_notify(good, b"{", TOKEN)[0], 400)
        self.assertEqual(check_notify(good, self.body({"text": "  "}), TOKEN)[0], 400)
        self.assertEqual(check_notify(good, self.body({"text": "a" * 100}), TOKEN, max_bytes=50)[0], 413)


class ServerTest(unittest.TestCase):
    def test_post_reaches_callback(self):
        got = []
        server = serve(StateHub(), 0, on_notify=lambda t, ti: got.append((t, ti)) or len(got),
                       notify_cfg={"token": TOKEN})
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/notify"

        def post(headers):
            req = urllib.request.Request(url, data=json.dumps({"text": "届きました"}).encode(),
                                         headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        self.assertEqual(post({**JSON, "X-Ray-Token": TOKEN}), (202, {"ok": True, "queued": 1}))
        self.assertEqual(post({**JSON, "X-Ray-Token": "wrong"})[0], 401)
        self.assertEqual(got, [("届きました", "")])


class AnnounceTest(unittest.TestCase):
    def make(self):
        self.logged = []
        s, hub = FakeSpeaker(), StateHub()
        a = Assistant(cfg(), FakeBrain([]), s, hub, log=lambda c, q, ans, **kw: self.logged.append((q, ans, kw)))
        return a, s, hub

    def test_plain_text(self):
        a, s, hub = self.make()
        a.announce("明日は雨です。傘を持ってください。", "天気")
        self.assertEqual(s.said, ["明日は雨です。傘を持ってください。"])
        self.assertEqual(hub.state["state"], "idle")
        self.assertEqual(hub.state["question"], "n8n：天気")
        self.assertEqual(self.logged[0][0], "n8n：天気")
        self.assertEqual(self.logged[0][2]["extra"]["brain"], "n8n")

    def test_with_screen(self):
        a, s, hub = self.make()
        a.announce('@画面 steps {"title": "今日", "steps": ["会議", "ジム"]}\n今日の予定は2つです。')
        self.assertEqual(len(a.deck), 1)
        self.assertEqual(s.said, ["今日の予定は2つです。"])
        self.assertIsNotNone(hub.state["scene"])

    def test_keeps_waiting_for_confirm(self):
        a, s, hub = self.make()
        a.awaiting_confirm = True
        hub.set(state="confirm", answer="確認です。消してよいですか？")
        a.announce("メールが1件届きました。")
        self.assertTrue(a.awaiting_confirm)
        self.assertEqual(hub.state["state"], "confirm")
        self.assertEqual(hub.state["scene"]["kind"], "confirm")

    def test_waits_until_conversation_ends(self):
        a, s, hub = self.make()
        old = app_mod.NOTICE_POLL
        app_mod.NOTICE_POLL = 0.01
        self.addCleanup(setattr, app_mod, "NOTICE_POLL", old)
        hub.set(state="speaking")
        a.notify("お知らせです。")
        time.sleep(0.1)
        self.assertEqual(s.said, [])  # 話している途中は割り込まない
        hub.set(state="idle")
        for _ in range(100):
            if s.said:
                break
            time.sleep(0.01)
        self.assertEqual(s.said, ["お知らせです。"])


if __name__ == "__main__":
    unittest.main()
